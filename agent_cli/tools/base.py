"""Tool abstraction surface.

Each tool is a :class:`Tool` subclass that owns its schema, its dispatch,
and its wire-key namespace in one place:

- **schema** (``name`` / ``description`` / ``parameters``) — what used to
  live in the central ``registry.TOOL_SCHEMAS`` dict,
- **dispatch** (``_run``) — what used to be a free ``tool_*`` function
  referenced from the central ``__init__.TOOLS`` dict,
- **prefix** — the wire surface namespaces ``action_input`` keys as
  ``{name}_{param}`` (e.g. ``read_file_path``). Everything prefix-related
  is derived from ``name`` on this base class: :meth:`strip_prefix`
  (wire → standard keys, applied in :meth:`run`) and :meth:`claims`
  (does this payload's key shape belong to me, for recovering a dropped
  action name). Subclasses never override them — they just set ``name``.

``Tool`` instances are the values of ``registry.TOOL_SCHEMAS`` (and
``TOOLS``): they expose the same ``.name`` / ``.description`` /
``.parameters`` attributes the old ``ToolSchema`` dataclass did, so every
schema consumer (system prompt, input validation, MCP adapter) keeps
working unchanged.

Virtual tools (complete/ask/...) keep standard keys, so for them
:meth:`strip_prefix` is a no-op and :meth:`claims` is always False — they
fall through to the normal NO_ACTION recovery rather than being inferred.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from agent_cli.tools.effect import EffectIntent, EffectKind
from agent_cli.tools.result import ToolResult


@dataclass(frozen=True)
class RunContext:
    """Per-invocation LOOP context threaded to a tool's two surfaces.

    The loop knows a handful of values *per tool call* — the session
    directory, the oversized-observation cap, and which tools are callable
    in the current loop. Rather than growing the keyword list on
    :meth:`Tool.run` / :meth:`Tool._run` and :meth:`Tool.render_oversized`
    every time a new such value appears, they take ONE frozen object; a new
    per-call value becomes a field here, not another parameter on 13+7
    signatures. Frozen + shared-safe (the same instance can be handed to
    concurrent parallel-delegate loops), which is why this is per-CALL data,
    never stored on the shared ``TOOLS`` singletons.

    SCOPE DISCIPLINE — keep this from becoming a god-object: only per-call
    LOOP context belongs here. Per-RESULT data (a specific result's body /
    token count) stays an explicit argument on ``render_oversized``;
    unrelated machinery (parser, provider, history) does NOT go here.

    Fields:
        session_dir: current session dir, or None in headless / no-ctx runs.
        oversized_cap: the over-cap threshold (context_window/10), 0 = off.
        tools_available: tool names callable in THIS loop (``delegate`` is
            absent inside a depth-limited subagent), so guidance never names
            a tool the model cannot invoke.
    """

    session_dir: Path | None = None
    oversized_cap: int = 0
    tools_available: frozenset[str] = frozenset()
    turn_isolation: object = None


def default_oversized_nudge(tool_name: str, tokens: int, cap: int) -> str:
    """The generic over-cap observation: the whole tool output is dropped and
    the model is steered to re-request a narrower slice. Shared default behind
    :meth:`Tool.render_oversized` (and the loop's no-tool fallback), so tools
    that don't override the seam keep the historical behaviour byte-for-byte."""
    return (
        f"[{tool_name or 'tool'}: output too large — ~{tokens:,} tokens "
        f"> cap {cap:,} (context_window/10). NOT added to context; the call "
        f"itself succeeded. Large outputs crowd out reasoning and lower quality. "
        f"Re-request a narrower slice: read a specific line range or symbols, add "
        f"a LIMIT / tighter filter, or pipe through `head`/`grep`. To keep a full "
        f"large result, write it to a file (e.g. `… | tee /tmp/out.txt`) then read "
        f"specific parts with read_file.]"
    )


def on_disk_oversized_nudge(
    tool_name: str,
    subject: str,
    location: str,
    read_path: str,
    tokens: int,
    cap: int,
    tools_available: frozenset[str],
    *,
    nlines: int = 0,
    part_extra: str = "",
    tail_bullets: tuple[str, ...] = (),
) -> str:
    """Shared over-cap nudge for tools whose large output is ON DISK.

    The invariant behind read_file / shell / delegate / fetch: once the bulky
    content is a file at ``read_path``, recovery is the same shape — (a) read a
    SPECIFIC part (range / search), or (b) fan out over SECTIONS of that file
    with delegate. The fan-out bullet steers to **N-way PARALLEL** — several
    agent run ops in ONE turn (which agent-cli runs concurrently), each reading a
    distinct line range and returning only a short summary, so no single
    subagent (nor the parent) ever holds the raw bulk and the sections finish in
    parallel. ``nlines`` (the file's line count, from the caller's ``body``)
    lets the nudge propose concrete line-range sections; without it the bullet
    falls back to a generic parallel-split wording. Emitted only when
    ``delegate`` is callable (``tools_available``), so it never points at a tool
    the model cannot invoke. ``subject`` names the thing; ``location`` is the
    on-disk clause; ``part_extra`` appends a tool-specific narrowing to bullet
    (a); ``tail_bullets`` adds extra options (e.g. re-delegate-narrower)."""
    lines = [
        (
            f"[{tool_name}: {subject} is ~{tokens:,} tokens — too large for one "
            f"context (cap {cap:,}). NOT added to context; {location}."
        ),
        f"· Need a SPECIFIC part? read_file '{read_path}' with a line range"
        + (f", or {part_extra}" if part_extra else "")
        + ".",
    ]
    if "agent" in tools_available:
        if nlines and nlines > 1:
            # Split so each section stays well under cap; boundaries by line.
            k = max(2, min(8, (tokens + cap - 1) // cap + 1))
            step = max(1, (nlines + k - 1) // k)
            lines.append(
                "· Need the WHOLE thing analysed/searched? Fan out IN PARALLEL: "
                f"'{read_path}' is {nlines:,} lines — split into ~{k} sections "
                f"and emit {k} agent run ops in ONE turn (same-turn run ops "
                "run concurrently), each like:\n"
                f'    agent(mode="run", task="read_file \'{read_path}\' lines 1-{step} and '
                'return a 3-line summary + anything about <your question>")\n'
                f'    agent(mode="run", task="read_file \'{read_path}\' lines {step + 1}-'
                f'{2 * step} …")   … (and so on, through line {nlines:,})\n'
                f"  Merge the {k} summaries. One subagent doing it all is slower "
                "and larger; parallel sections stay small and fast."
            )
        else:
            lines.append(
                "· Need the WHOLE thing analysed/searched? Fan out IN PARALLEL: "
                f"split '{read_path}' into several contiguous line-range sections "
                "and emit one delegate op per section in the SAME turn (they run "
                "concurrently), each returning a short summary; then merge them."
            )
    lines.extend(f"· {b}" for b in tail_bullets)
    return "\n".join(lines) + "]"


def narrow_oversized_nudge(
    tool_name: str,
    subject: str,
    tokens: int,
    cap: int,
    *,
    bullets: tuple[str, ...],
) -> str:
    """Over-cap nudge for tools whose output is NOT a file — the model re-issues
    a NARROWER call in place (SQL ``LIMIT`` / ``substr``, a single symbol fetch)
    rather than reading a saved slice. Sibling of :func:`on_disk_oversized_nudge`
    (which points at a persisted file); ``bullets`` are the tool-specific
    narrowing options, so the advice fits the tool instead of the generic
    line-range / head / grep default."""
    lines = [
        (
            f"[{tool_name}: {subject} is ~{tokens:,} tokens — too large for one "
            f"context (cap {cap:,}). NOT added to context; the call succeeded."
        ),
    ]
    lines.extend(f"· {b}" for b in bullets)
    return "\n".join(lines) + "]"


class Tool(ABC):
    """Base class for every dispatchable tool.

    Subclasses set ``name`` / ``description`` / ``parameters`` as class
    attributes and implement :meth:`_run`. ``parameters`` is a JSON Schema
    object identical in shape to what the old ``ToolSchema.parameters``
    held.
    """

    name: str
    description: str
    parameters: dict

    #: Whether a turn's consecutive ops of THIS tool may run concurrently.
    #: Default False — ops dispatch sequentially, which is the correctness
    #: guarantee for side-effecting / order-dependent tools (write_file,
    #: edit_file, shell: e.g. write-then-edit the same file, or mkdir-then-
    #: touch, must run in order). Only side-effect-free / independent tools
    #: set this True. The loop reads it to batch a run of same-tool ops into
    #: one concurrent dispatch (see ``AgentLoop._dispatch_parallel_batch``).
    #: Today only ``delegate`` opts in (independent subagents = the case where
    #: concurrency is both safe and worth the wall-clock win).
    parallel_safe: bool = False

    #: Explicit opt-out for tools with no leaf workspace effect, or composites
    #: whose children acquire their own gates.  False by default so a new
    #: plugin/tool that omits classification fails closed at the workspace
    #: gate instead of silently running unlocked.
    non_workspace_or_composite: bool = False

    def parallel_batchable(self, action_input: dict) -> bool:
        """이 op 이 병렬 배치에 합류 가능한가 (5.0.0 mode-aware 배칭).

        기본 = ``parallel_safe`` 그대로. AgentTool 이 override —
        ``mode:"run"`` op 만 배치 가능(상주 모드는 즉시-반환이라 배칭
        불필요·혼합 턴은 순차가 정확성 보장)."""
        return self.parallel_safe

    #: Whether the oversized-observation cap applies to THIS tool's
    #: observation. Default True → the cap (context_window/10) is enforced
    #: consistently for every tool: an observation over the cap is replaced
    #: with a narrow-it nudge instead of crowding out the context. A tool
    #: whose large output is genuinely essential can opt out by setting
    #: this False. The loop reads it at the result→observation seam.
    apply_oversized_cap: bool = True

    @property
    def key_prefix(self) -> str:
        """Wire-key namespace for this tool: ``{name}_``."""
        return self.name + "_"

    def strip_prefix(self, args: dict) -> dict:
        """Strip ``key_prefix`` from top-level ``args`` keys (wire →
        standard). Keys without the prefix pass through unchanged — a
        model that emits a bare standard key still works — and nested
        keys inside arrays/objects are never touched.
        """
        if not isinstance(args, dict):
            return args
        p = self.key_prefix
        return {(k.removeprefix(p)): v for k, v in args.items()}

    def add_prefix(self, args: dict) -> dict:
        """Inverse of :meth:`strip_prefix`: namespace top-level ``args``
        keys with ``key_prefix``. Idempotent — keys already carrying the
        prefix are left as-is, and nested keys are untouched. Used to
        render inline-guide examples that are authored in standard keys.
        """
        if not isinstance(args, dict):
            return args
        p = self.key_prefix
        return {(k if k.startswith(p) else p + k): v for k, v in args.items()}

    def claims(self, action_input: dict) -> bool:
        """Whether *action_input* belongs to this tool by key shape — the
        per-tool vote behind ``registry.infer_action`` (dropped-action recovery
        seam, parse_stage 3). True iff any top-level key carries this tool's
        prefix; ``infer_action`` selects a tool only when exactly one claims, so
        the prefix namespace keeps claims mutually exclusive by construction.

        As of consolidation Step 3 every builtin tool is flat-native (no
        prefix), so this is False for all builtin payloads — the seam is latent,
        kept live for a FUTURE wire-key-prefixed tool/format (see
        ``infer_action``). MCP tools are prefix-less by design and never claim.
        """
        if not isinstance(action_input, dict):
            return False
        return any(k.startswith(self.key_prefix) for k in action_input)

    def render_action_input_for_context(self, action_input: dict) -> dict:
        """This tool's ``action_input`` as it should appear when the assistant
        turn is RE-FED to the LLM each subsequent turn (the symmetric
        counterpart to :meth:`render_observation` — action side vs result side).

        Default: identity (the action_input unchanged — same object). A tool
        whose action carries a bulky body (write_file's ``content``,
        edit_file's ``lines``) overrides to replace that value with a short
        marker while KEEPING the op shape (so format self-reinforcement
        survives) — the file is on disk, so re-feeding the body verbatim every
        turn only crowds out context. Must NOT mutate ``action_input`` (the
        caller passes the stored record; history.jsonl stays faithful) — return
        a copy when changing anything.
        """
        return action_input

    def render_observation(self, result: ToolResult, args: dict) -> str:
        """Render this tool's result into the observation body that enters
        the context + the LLM (the text after ``Observation: ``).

        Default reproduces the historical behaviour: the output on success,
        the error on failure. This is the single per-tool seam for "what this
        tool contributes to context" — override to customise (e.g. a tool that
        echoes a large artifact can trim it here, keeping its confirmation but
        pointing back to the file/refs instead of dumping the whole thing).
        ``args`` are the standard (prefix-stripped) action_input keys.
        """
        return result.output if result.success else result.error

    def render_oversized(
        self,
        result: ToolResult,
        args: dict,
        *,
        body: str,
        tokens: int,
        ctx: RunContext,
    ) -> str:
        """Observation substituted when THIS tool's output exceeds the oversized
        cap (``context_window / 10``). The tool OWNS the full over-cap policy.

        Default: :func:`default_oversized_nudge` — the whole body is dropped and
        the model is steered to re-request a narrower slice. An override may
        return tool-specific recovery guidance, or a BOUNDED slice of ``body``
        plus a pointer (``body`` and ``tokens`` are the per-RESULT payload, so a
        tool can show a head without re-running). Per-CALL loop context arrives
        in ``ctx`` (:class:`RunContext`): ``ctx.oversized_cap`` is the threshold,
        ``ctx.tools_available`` names the tools callable in the CURRENT loop
        (e.g. ``delegate`` is absent inside a depth-limited subagent) so guidance
        never points at an uncallable tool, and ``ctx.session_dir`` lets a tool
        whose output is not already on disk (shell/fetch) persist ``body`` there
        and point at it — uniform with tools whose output already is a file
        (read_file, delegate's ``result.md``). Fires only when
        :attr:`apply_oversized_cap` is True AND the body is over cap — so an
        override never needs to re-check either condition.
        """
        return default_oversized_nudge(self.name, tokens, ctx.oversized_cap)

    def validate(self, args: dict) -> str | None:
        """의미론 검증 훅 (C7, v4.49.0) — ``None``=통과, ``str``=오류 문구.

        shape(존재/required/타입/coercion)는 중앙 ``validate_tool_input``
        1~5단계 소유; 여기는 **실행 없이 판정 가능한 의미론**만 — mode별
        조건부 필수, enum, 필드 형식. 파일 내용이 필요한 검사(hashline
        ref 대조 등)는 실행 소관이라 넣지 않는다.

        호출 지점 2곳, 로직은 여기 1곳: ① 중앙 검증 6단계(A5 경로 —
        실패가 ``SCHEMA_MISMATCH`` 로 기록되고 format-error 렌더를 탐;
        관찰 문구는 이 훅이 돌려준 짧은 오류 그대로 — 전체 스키마 전문은
        shape 실패에만 동봉[정밀화 결정]) ② :meth:`run` 초입(직접
        호출자 방어 — loop 경로에선 ①이 먼저라 사실상 no-op 재검사).
        ``args`` 는 표준(strip 후) 키."""
        return None

    def run(self, args: dict, *, ctx: RunContext | None = None) -> ToolResult:
        """Public dispatch: strip the tool-name prefix from ``action_input``
        keys, validate semantics(:meth:`validate` — 직접 호출자 방어), then
        hand standard keys to :meth:`_run`. ``ctx`` carries the per-call loop
        context (:class:`RunContext`); tools that do not need it ignore it."""
        std = self.strip_prefix(args)
        err = self.validate(std) if isinstance(std, dict) else None
        if err:
            return ToolResult(False, error=err)
        return self._run(std, ctx=ctx)

    def wrap_single_op(self, flat: dict) -> dict:
        """Convert a multi-op format's flat single-target op into this tool's
        canonical (wire-key-prefixed) input.

        Multi-op formats emit ONE target per op with plain standard keys
        (``{"path": "x"}``) — the turn's op array is the batch mechanism, so
        their ops never carry the per-tool batch wrapper. Batch-shaped tools
        override this to re-wrap (``{"read_file_reads": [{"path": "x"}]}``)
        so the existing validate → strip → run pipeline applies unchanged.

        Default: prefix the keys (no structural change) — right for tools
        whose canonical input is already flat (shell, write_file, ask, ...).
        Overrides must be tolerant of an already-canonical input (idempotent)
        so a model that emits the batch shape anyway still works. Only called
        on the multi-op dispatch path; single-action formats bypass it.
        """
        if not isinstance(flat, dict):
            return flat
        return self.add_prefix(flat)

    def touched_paths(self, action_input: dict) -> list[str]:
        """File-list entries this action contributes during compaction.

        Default: none. Path-handling tools override to pull paths out of
        their OWN action_input shape (prefixed keys, arrays) — keeping that
        schema knowledge in the tool itself, not duplicated in the
        compaction extractor (:func:`context._file_extract`). Overrides
        should use :meth:`strip_prefix` so they read standard keys.
        """
        return []

    def effect_intent(self, action_input: dict) -> EffectIntent:
        """이 호출이 일으키는 부수효과의 선언 — A3 계층 락의 전제.

        Default: :attr:`EffectKind.UNKNOWN_WORKSPACE_EFFECT` (=배타). 자기 부수효과를
        **증명할 수 있는** 도구만 override 해 좁힌다. 기본값이 배타인 것은
        안전측 설계다 — 미분류 도구가 실수로 병렬 진입해 파일을 동시에
        만지는 것보다, 줄을 서서 느린 편이 항상 낫다.

        :meth:`touched_paths` 와 같은 소유권 규율(도구가 자기 ``action_input``
        shape 를 안다)을 따르며, override 는 :meth:`strip_prefix` 로 표준 키를
        읽는다. 분류 어휘와 호환성 행렬은 :mod:`agent_cli.tools.effect` 참조.

        ``code_index`` 는 공유 인덱스 DB를 갱신할 수 있고 경로 없는 모드도 있어
        명시적인 workspace-exclusive intent를 낸다. 반면 ``memory`` 는 세션
        memory.jsonl, ``read_context`` 는 세션 history, ``fetch`` 는 네트워크를
        대상으로 하므로 NON_WORKSPACE_OR_COMPOSITE를 명시한다. ``agent`` 와
        ``run_skill`` 은 중첩 도구이고 가상 도구(complete/ask/message)는 사용자
        작업공간 파일을 만지지 않는다.

        워크스페이스 밖 상태만 다루거나 자식 잎 도구가 스스로 잠그는 복합 도구는
        반드시 ``NON_WORKSPACE_OR_COMPOSITE`` 를 명시한다. 이 구분 덕분에 새
        plugin/tool 이 선언을 빠뜨리면 조용히 무잠금으로 실행되지 않고 배타로
        떨어지며, 명시된 복합 도구만 부모 락을 건너뛰어 중첩 교착을 피한다.
        """
        kind = (
            EffectKind.NON_WORKSPACE_OR_COMPOSITE
            if self.non_workspace_or_composite
            else EffectKind.UNKNOWN_WORKSPACE_EFFECT
        )
        return EffectIntent(kind)

    def summary_arg(self, action_input: dict) -> str:
        """Short label for this action in the compaction transcript /
        observation header (e.g. ``write_file(src/x.c)``).

        Default: the first non-empty string value (after ``strip_prefix``),
        capped at 60 chars. Tools with a salient field (path / command /
        agent) override to pick it deterministically. Sibling of
        :meth:`touched_paths` — both read the tool's OWN action_input shape.
        """
        for v in self.strip_prefix(action_input).values():
            if isinstance(v, str) and v:
                return v[:60]
        return ""

    @abstractmethod
    def _run(self, args: dict, *, ctx: RunContext | None = None) -> ToolResult:
        """Execute the tool with standard (un-prefixed) keys. ``ctx`` is the
        per-call loop context (:class:`RunContext`), or ``None`` for a
        direct/test caller — consumers that read a field guard for ``None``."""
