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

import hashlib
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

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


# ── Oversized-output policy (ONE policy, every tool) ─────────────
#
# A tool observation over the cap (``context_window / 10``) is never fed to the
# model. Historically each tool answered "what now?" its own way — three nudge
# builders and six ``render_oversized`` overrides — which meant a new tool got
# the *generic* advice ("pipe through head/grep", "tee it to a file") even when
# that advice was nonsense for it (MCP), and two tools (code_index,
# read_context) dropped their body without saving it anywhere, so a large
# result could never be looked at again.
#
# Now there is exactly one policy, applied by :meth:`Tool.render_oversized`
# for every tool including MCP:
#
#   1. the full body ALWAYS ends up in a file — either one the tool already
#      wrote (``oversized_source_path``: read_file's own path, agent's
#      result.md) or one persisted here,
#   2. the model gets a bounded head+tail EXCERPT so the common cases (a build
#      log whose answer is the last 30 lines) need no extra round trip,
#   3. the model gets the same three recovery routes every time — regex/string
#      match, line range, or a divide-and-conquer agent fan-out,
#   4. plus one tool-specific line on how to avoid the bulk at the source.
#
# A new tool inherits all of it and only sets ``oversized_retry_hint``.

OVERSIZED_DIRNAME = "oversized"

# Excerpt shape. Tail is weighted heavier than head: when a big output has an
# answer in it (build failure, test summary, exit code, stack trace) it is far
# more often at the end than the beginning.
_EXCERPT_HEAD_LINES = 20
_EXCERPT_TAIL_LINES = 30
_EXCERPT_CAP_RATIO = 0.15  # excerpt never exceeds 15% of the cap
_EXCERPT_TAIL_SHARE = 0.6  # of that budget, 60% goes to the tail
_EXCERPT_MIN_CHARS = 400  # floor, so a tiny cap still shows something

# Fan-out sizing. Each section targets ``_FANOUT_FILL`` of the cap so a
# sub-agent reading one section is comfortably under it — the old sizing
# (clamped at 8 sections) handed every sub-agent a still-over-cap slice
# whenever the body was more than ~8 caps, so the fan-out just re-hit the
# same wall one level down.
_FANOUT_FILL = 0.6
_FANOUT_MAX_SECTIONS = 16

#: Fallback retry hint — true for any tool, including MCP servers, whose
#: schema we know nothing about. Tools override with something concrete.
GENERIC_RETRY_HINT = "re-issue the call with a narrower scope so the result is smaller."


def oversized_dir(session_dir: Path | None) -> Path:
    """Directory that holds persisted over-cap bodies. The session dir when
    there is one (the output belongs to that session and is browsable next to
    its history); otherwise a stable temp dir, so "the full output is always in
    a file" holds in headless runs too rather than silently degrading."""
    if session_dir:
        return Path(session_dir) / OVERSIZED_DIRNAME
    return Path(tempfile.gettempdir()) / "agent-cli-oversized"


def persist_oversized(
    tool_name: str, body: str, key: str, session_dir: Path | None
) -> str:
    """Write *body* to ``<oversized_dir>/<tool>-<digest>.txt`` and return the
    path (``""`` if even the temp-dir write failed — the nudge then degrades to
    excerpt + narrowing advice).

    The digest covers ``key`` (the call's identity — command / url / query) and
    the body, so re-running the same call overwrites the same file instead of
    littering the session dir with copies. Called LAZILY, only when a result is
    actually over cap, so ordinary calls never touch disk."""
    digest = hashlib.sha1((key + "\x00" + body).encode("utf-8", "replace")).hexdigest()[
        :8
    ]
    base = oversized_dir(session_dir)
    try:
        base.mkdir(parents=True, exist_ok=True)
        out = base / f"{tool_name}-{digest}.txt"
        out.write_text(body, encoding="utf-8")
    except OSError:
        return ""
    return str(out)


def _take_lines(lines: list[str], budget: int, *, from_end: bool) -> list[str]:
    """As many whole lines as fit in *budget* chars, taken from the head (or
    the tail when ``from_end``). A single line longer than the whole budget is
    hard-sliced so the excerpt is never empty."""
    seq = list(reversed(lines)) if from_end else lines
    out: list[str] = []
    spent = 0
    for line in seq:
        cost = len(line) + 1
        if spent + cost > budget:
            if not out:  # one pathological long line — slice it
                out.append(line[-budget:] if from_end else line[:budget])
            break
        out.append(line)
        spent += cost
    return list(reversed(out)) if from_end else out


def oversized_excerpt(body: str, cap: int) -> str:
    """Bounded head+tail excerpt of an over-cap body, or ``""`` when there is
    no budget for one. Sized in CHARS against a fraction of the cap so the
    excerpt itself can never be what blows the context — a 5-line body of
    enormous lines is clamped the same as a 100k-line log."""
    budget = max(_EXCERPT_MIN_CHARS, int(cap * 4 * _EXCERPT_CAP_RATIO))
    lines = body.split("\n")
    total = len(lines)
    if total <= _EXCERPT_HEAD_LINES + _EXCERPT_TAIL_LINES:
        # Too few lines to take a head and a tail without overlapping — split
        # down the middle instead so nothing is shown twice.
        mid = max(1, total // 2)
        head_src, tail_src = lines[:mid], lines[mid:]
    else:
        head_src = lines[:_EXCERPT_HEAD_LINES]
        tail_src = lines[-_EXCERPT_TAIL_LINES:]

    tail_budget = int(budget * _EXCERPT_TAIL_SHARE)
    tail = _take_lines(tail_src, tail_budget, from_end=True)
    # The tail rarely spends its whole allowance; hand the remainder to the head.
    head_budget = budget - sum(len(x) + 1 for x in tail)
    head = _take_lines(head_src, head_budget, from_end=False)
    if not head and not tail:
        return ""

    omitted = total - len(head) - len(tail)
    parts = []
    if head:
        parts.append(f"--- first {len(head)} line(s) ---\n" + "\n".join(head))
    if omitted > 0:
        parts.append(
            f"… {omitted:,} line(s) omitted — full text is in the file above …"
        )
    if tail:
        parts.append(f"--- last {len(tail)} line(s) ---\n" + "\n".join(tail))
    return "\n".join(parts)


def _fanout_route(path: str, nlines: int, tokens: int, cap: int) -> str:
    """The divide-and-conquer route: split the file into sections sized to fit
    a sub-agent's context and emit one ``agent(mode="run")`` op per section in
    ONE turn (agent-cli runs same-turn run ops concurrently), each returning a
    short summary. Neither the sub-agents nor the parent ever hold the bulk."""
    per_section = max(1, int(cap * _FANOUT_FILL))
    k = max(2, -(-tokens // per_section))  # ceil
    capped = k > _FANOUT_MAX_SECTIONS
    k = min(k, _FANOUT_MAX_SECTIONS)
    if nlines <= 1:
        return (
            f"Analyse the WHOLE thing? Divide and conquer: split '{path}' into "
            f"~{k} contiguous line-range sections and emit one "
            'agent(mode="run") op per section in the SAME turn (they run '
            "concurrently), each returning a short summary; then merge them."
        )
    step = max(1, -(-nlines // k))
    tail = (
        "\n  Each section is still large — tell each sub-agent to narrow "
        "further (read_file search=) rather than read its whole range."
        if capped
        else ""
    )
    return (
        f"Analyse the WHOLE thing? Divide and conquer — '{path}' is "
        f"{nlines:,} lines; split it into {k} sections and emit {k} "
        "agent run ops in ONE turn (same-turn run ops execute concurrently):\n"
        f'    agent(mode="run", task="read_file \'{path}\' lines 1-{min(step, nlines)} '
        'and report a 3-line summary + anything about <your question>")\n'
        f'    agent(mode="run", task="… lines {min(step, nlines) + 1}-'
        f'{min(2 * step, nlines)} …")   … (through line {nlines:,})\n'
        f"  Merge the {k} summaries — no context ever holds the whole thing."
        f"{tail}"
    )


def oversized_nudge(
    *,
    tool_name: str,
    path: str,
    body: str,
    tokens: int,
    cap: int,
    retry_hint: str,
    tools_available: frozenset[str],
    reason: str = "",
) -> str:
    """The single over-cap observation, identical in shape for every tool.

    ``path`` is where the full body lives (``""`` only if persisting failed).
    ``reason`` overrides the "too large for one context" opening — the turn
    budget guard passes its own so the model is told the truth about WHY its
    output was replaced (the turn's accumulated total, not this one result)."""
    nlines = body.count("\n") + 1
    why = reason or (
        f"output is ~{tokens:,} tokens — too large for one context (cap {cap:,})"
    )
    head = [f"[{tool_name}: {why}. NOT added to context; the call itself succeeded."]
    if path:
        head.append(
            f"Full output saved to '{path}' ({nlines:,} lines, {len(body):,} chars)."
        )
    else:
        head.append(
            "The full output could NOT be saved to a file — it is gone; "
            "re-run with a narrower scope to get it back."
        )

    excerpt = oversized_excerpt(body, cap)
    if excerpt:
        head.append("")
        head.append(excerpt)

    routes: list[str] = []
    if path:
        routes.append(
            "Match by string/regex (cheapest — start here):\n"
            f"    read_file(path='{path}', search='<regex>', context=5)"
        )
        routes.append(
            "Know roughly where it is? Read that range:\n"
            f"    read_file(path='{path}', line_start=<N>, line_end=<M>)"
        )
        if "agent" in tools_available:
            routes.append(_fanout_route(path, nlines, tokens, cap))
    routes.append(f"Root cause — {retry_hint or GENERIC_RETRY_HINT}")

    body_lines = ["", "Get what you need from it:"] if path else ["", "What to do:"]
    for i, route in enumerate(routes, 1):
        body_lines.append(f"{i}. {route}")
    return "\n".join(head + body_lines) + "]"


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

    # ── 루프 정책 선언 (T3 선언화 — 리뷰 §4.1) ──────────────────────
    # 종전엔 아래 정책들이 루프 코드의 도구명 문자열 목록(core.py 의
    # tools_list 구성, dispatch.py 의 턴-종결 분기)에 산재했다 — 새 도구가
    # 루프 여러 파일을 동기 수정해야 했고, 누락은 조용한 오동작. 이제 도구
    # 클래스가 자기 정책을 선언하고 루프는 속성만 읽는다 (단일 소스).
    # 엔진 바인딩(edit_file 같은-path 배치, agent 병렬 엔진)은 정책이 아니라
    # 루프 쪽 엔진 코드와 함께 산다 — 속성만 세우고 배선이 없으면 v8.37.0
    # 이전 parallel_safe 크래시 트랩의 재판이 되기 때문에 여기 두지 않는다.

    #: 이 도구의 op 은 턴을 종결한다 (complete/run_skill) — 멀티-op 턴에서
    #: 누적 결과를 먼저 flush 한 뒤 이 op 을 디스패치하고 턴을 끝낸다.
    terminal: bool = False

    #: 결합 호출 깊이 상한(depth >= max_depth)에서 tools_list 에서 제거
    #: (run_skill/agent) — LLM 이 거부될 도구를 광고받지 않게. 디스패치-시점
    #: 깊이 가드는 belt-and-suspenders 로 남는다.
    depth_gated: bool = False

    #: 이 루프 자원이 없으면 tools_list 에서 제거. "ctx"(ask — 비대화형
    #: 루프는 질문 불가) / "message_handler"(message — 상주 서브에이전트
    #: 전용). None = 무조건 사용 가능.
    requires_handler: str | None = None

    #: requires_handler 자원이 **있으면** 프로파일 allowed-tools 와 무관하게
    #: 커널이 강제 탑재 (message — 상주 루프의 기본 능력, v5.11 의미).
    #: requires_handler 와 함께일 때만 의미가 있다.
    force_mount: bool = False

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

    #: One line telling the model how to avoid producing the bulk in the FIRST
    #: place, appended as the last route of the over-cap nudge. The only
    #: per-tool variation left in the oversized policy — everything else
    #: (persist, excerpt, regex/range/fan-out routes) is identical for every
    #: tool, so a new tool sets this string and inherits correct behaviour.
    #: Phrased to complete "Root cause — …".
    oversized_retry_hint: str = GENERIC_RETRY_HINT

    def oversized_source_path(
        self, result: ToolResult, args: dict, ctx: RunContext
    ) -> str:
        """Path to an EXISTING file already holding this result's full body, or
        ``""`` to have the seam persist it.

        Two tools already wrote the bulk to disk before we got here — read_file
        (the file it read; its hashline numbers are 1:1 with the real lines, so
        ranges quoted in the nudge stay valid) and agent (the run's
        ``result.md``). They return that path so the nudge points at the
        original instead of littering the session with a byte-identical copy.
        Everything else — shell, fetch, code_index, read_context, MCP — has
        nothing on disk and returns ``""``.

        A returned path that does not exist is ignored (the seam persists
        instead), so a since-deleted file can never make the nudge point at
        nothing."""
        return ""

    def oversized_key(self, args: dict) -> str:
        """Identity of THIS call, mixed into the persisted filename's digest so
        re-running the same call reuses one file instead of accumulating
        copies. Defaults to :meth:`summary_arg` (command / url / path / query),
        which is exactly the "which call was this" label every tool already
        defines."""
        return self.summary_arg(args)

    def render_oversized(
        self,
        result: ToolResult,
        args: dict,
        *,
        body: str,
        tokens: int,
        ctx: RunContext,
        reason: str = "",
    ) -> str:
        """Observation substituted when THIS tool's output exceeds the oversized
        cap (``context_window / 10``). Fires only when :attr:`apply_oversized_cap`
        is True AND the body is over cap, so it never needs to re-check either.

        This is deliberately NOT a per-tool decision any more: the body is put
        in a file, a bounded head+tail excerpt is shown, and the same recovery
        routes (regex match / line range / agent fan-out) are offered, for every
        tool — see the module-level "Oversized-output policy" note. Tools
        customise through :attr:`oversized_retry_hint` and
        :meth:`oversized_source_path`, not by overriding this.

        ``reason`` lets a caller state a different cause for the replacement;
        the loop's per-TURN budget guard passes its own so the model is told
        the truth (the turn's accumulated total) rather than being told this
        one result was too big when it was not."""
        path = self.oversized_source_path(result, args, ctx)
        if path:
            try:
                if not Path(path).exists():
                    path = ""
            except OSError:
                path = ""
        if not path:
            path = persist_oversized(
                self.name, body, self.oversized_key(args), ctx.session_dir
            )
        return oversized_nudge(
            tool_name=self.name,
            path=path,
            body=body,
            tokens=tokens,
            cap=ctx.oversized_cap,
            retry_hint=self.oversized_retry_hint,
            tools_available=ctx.tools_available,
            reason=reason,
        )

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
