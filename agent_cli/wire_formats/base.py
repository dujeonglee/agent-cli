"""Wire format plugin base class and shared types.

A "wire format" is the on-the-wire shape of a single LLM response — what
the model is asked to emit, what the parser reads, and what the recovery
layer shows the model when something goes wrong. The bundle is hot-
swappable so new format experiments live in their own module and can be
added or removed without touching the loop, prompts, or recovery
primitives.

Lifecycle per assistant turn — all priors are rebuilt from one stored record::

    (A) Emit        consumer: model (produces)
       │            shape:    plugin wire shape, raw string
       │
       └── serialize_assistant_for_history(raw)   ← save-time sanitize
                                  ▼
                            (B) Store
                            consumer: history.jsonl reader / analysis
                            shape:    structured dict {thought, action, action_input}
                                  │
                                  └── render_assistant_from_history(record)
                                                              ▼
                                                        (C) Feed
                                                        consumer: LLM — live next-turn
                                                          prior AND overflow/resume restore
                                                        shape:    plugin wire shape (≈ A)

The live prior and the resume prior are the SAME transition (B → render → C):
the next-turn prior is always rebuilt from the stored record, never the raw
emission. A wire sentinel the model leaked mid-turn is sanitized once at save
time (B), so it can't ride back into the prior and re-teach a runaway shape.

Each transition is owned by the plugin via a method on this base class.
Default implementations are provided for the common cases:

  - ``serialize_assistant_for_history`` — parse + structured-field extraction;
    sanitizes at save time (``sanitize_thought`` on thought + bare content).
  - ``render_assistant_from_history`` — re-emit via ``self.render_full_example``;
    builds the next-turn prior (live AND resume).
  - ``render_action_input`` — dict → JSON via ``json.dumps``.
  - ``provider_call_kwargs`` — empty dict.
  - ``prefill`` — empty string.

So a typical plugin only implements the wire-shape-specific abstract
methods: ``parse_turn``(루프의 1차 파서 — v8.41.0 추상 승격),
``render_full_example``, ``format_rules``, and the recovery wording
strings. The serialize / render defaults compose those into the lifecycle
automatically — ``serialize`` calls ``self.parse()``(기본 = parse_turn 의
첫-op 투영) and extracts structured fields; ``render`` calls
``self.render_full_example()`` to re-emit the wire shape from the stored
record.

See ``agent_cli/wire_formats/json_fc.py`` for the reference implementation.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ParsedAction:
    """Format-agnostic parse result.

    Carries everything the loop needs to dispatch one action, plus a small
    set of generally-useful metadata fields. Format-specific debug info
    belongs inside the plugin — this dataclass is the *boundary* between
    plugin and loop.

    Field semantics:
      - ``thought / action / action_input``: the action to execute. ``None``
        when parse failed (``parse_stage == 0``).
      - ``raw``: the model's emitted text after any leading-thinking strip.
        Recovery primitives echo this back verbatim, so any normalization
        upstream loses fidelity.
      - ``parse_stage``: 0 means "parse failed, no action available."
        Values ≥ 1 are plugin-defined success paths (e.g. ReAct uses
        1=json.loads, 2=json_repair, 3=regex). The loop only checks
        ``parse_stage > 0``; the exact value is for observability.
      - ``thinking``: contents of any leading ``<think>...</think>`` block
        the parser stripped. Used by the renderer in verbose mode.
      - ``truncated``: the parser had to repair the JSON (e.g. closed an
        unterminated string). The loop uses this as a "result is suspect"
        signal — currently gates ``edit_file`` truncation handling.
    """

    thought: str | None = None
    action: str | None = None
    action_input: dict | str | None = None
    raw: str = ""
    parse_stage: int = 0
    thinking: str | None = None
    truncated: bool = False


@dataclass
class Op:
    """One tool invocation within a turn — the per-op unit of a ``ParsedTurn``.

    Mirrors the action-carrying fields of :class:`ParsedAction`. A
    single-action wire format (하나도 내장돼 있지 않음 — 미래 플러그인용 기본) yields a turn with exactly
    one ``Op``; a multi-op format yields several.
    """

    action: str | None = None
    action_input: dict | str | None = None
    truncated: bool = False


@dataclass
class ParsedTurn:
    """Turn-level parse result — the loop boundary that supersedes the
    singular :class:`ParsedAction`.

    Carries the turn's reasoning plus an ORDERED list of ops to dispatch.
    A single-action format produces ``ops`` of length 0 (parse failure) or 1;
    a multi-op format produces several. ``terminal`` marks a completion turn
    that carries no ops (e.g. a thought-only "done" emission) — single-action
    formats never set it (they complete via a ``complete`` op), so it is
    ``False`` for them and the loop's behaviour is unchanged.

    :meth:`WireFormat.parse_turn` (abstract, v8.41.0 — 루프의 1차 경계) 가
    이 shape 을 반환한다; a plugin opts into multi-op
    only by overriding ``parse_turn``; ``parse`` (and the history round-trip
    built on it) is untouched.

    Field semantics mirror :class:`ParsedAction`: ``thought`` / ``raw`` /
    ``parse_stage`` (0 = parse failed) / ``thinking`` carry the same meaning.
    """

    thought: str | None = None
    ops: list[Op] = field(default_factory=list)
    terminal: bool = False
    raw: str = ""
    parse_stage: int = 0
    thinking: str | None = None


class WireFormat(ABC):
    """Plugin base class for one wire format.

    Plugins inherit from this class and override the abstract methods
    that define their wire shape. Concrete defaults handle the common
    cases (history pipeline round-trip, identity hooks, shared builder)
    so a typical plugin only specifies what makes its wire shape unique:
    the parser, the rendering of one example, the rules section bits,
    and the recovery wording.

    See the module docstring for the assistant-turn lifecycle that
    these methods orchestrate.

    Method groups:
      - **Prompt**: what the model is told to emit.
      - **Parsing**: how the emitted text becomes a ``ParsedAction``.
      - **Recovery**: what the model is told when parsing failed.
      - **Provider / lifecycle**: prefill, provider kwargs, the (A)→(C)
        normalization, and the (A)↔(B)↔(D) history round-trip.
    """

    name: str
    """Short identifier used by the CLI ``--response-format`` option and
    the registry. Convention: lowercase, ``[a-z0-9_-]``."""

    thought_required: bool = True
    """Whether a missing ``thought`` triggers recovery vs. is tolerated.

    True: the recovery layer fires NO_THOUGHT when an action is emitted
    without a thought — the loop asks the model to re-emit with reasoning.
    False: the thought slot is optional and its absence is valid, not a
    drift signal (e.g. wire formats where the thought is preceding free
    text outside a structured field). Mirror of :attr:`action_required`."""

    action_required: bool = True
    """Whether a missing ``action`` triggers recovery vs. inference.

    True (default, conservative): an emission whose ``action`` slot is
    empty/invalid goes straight to NO_ACTION recovery — the loop asks the
    model to re-emit with an action. False: the loop first tries
    ``infer_action`` on the preserved ``action_input`` (wire-key prefix →
    tool) and only falls back to NO_ACTION recovery when inference is
    ambiguous/empty. Plugins whose ``action_input`` keys are namespaced —
    so a dropped action name is unambiguously recoverable — set False.
    Mirror of :attr:`thought_required`. Either flag's recovery path
    depends on the parser preserving ``action_input`` (see :meth:`parse`)."""

    multi_op: bool = False
    """Whether the format expresses several tool ops in one turn.

    False (default — 단일-action 포맷용; 내장 둘은 True): one action per turn; per-tool batch
    fields (``read_file_reads`` etc.) let one turn touch several targets. The
    prompt shows wire-key-prefixed params and the tools' batch prose.

    True (multi-op formats): the turn carries an array of ops, so per-tool
    batch is redundant. The prompt layer renders tool params with the prefix
    stripped (the format's flat ``{action, params}`` convention) and drops the
    batch-specific guide prose; the "one op per target" instruction lives once
    in :meth:`format_rules`. See docs/inputs-array-schema/DESIGN.md §5."""

    exposes_complete: bool = True
    """Whether ``complete`` is offered to the model as a tool.

    True (default): ``complete`` appears in the Available Tools listing — the
    model finishes by calling it. False: ``complete`` is withheld (the format
    signals completion another way, e.g. a thought-only terminal turn), so the
    prompt layer omits it from the always-included tools."""

    # ─── Prompt (abstract) ──────────────────────────────────────

    @abstractmethod
    def render_full_example(self, *, thought, action: str, action_input: str) -> str:
        """Render one full example of the wire shape.

        The Format Rules builder calls this with shared logical inputs —
        a schema example and a ``complete`` example — so the *content* is
        identical across
        plugins and only the on-the-wire form differs. Measurement of
        model compliance can therefore compare two plugins fairly.

        Also used by ``render_assistant_from_history`` (default) to
        round-trip a stored record back into the wire shape on overflow
        recovery / session resume.

        Args:
            thought: Reasoning text. ``None`` means "invocation only";
                each plugin chooses how to handle the absent slot —
                typically substituting a short placeholder so the slot
                stays visible.
            action: Action name (e.g. ``"read_file"``, ``"complete"``).
            action_input: action_input as a JSON string. Plugins
                splice it into their wire shape verbatim — receiving
                a string rather than a dict avoids each plugin having
                to make formatting decisions about whitespace / key
                order.

        Returns:
            The rendered example, no surrounding whitespace, no
            trailing newline.
        """

    # (v8.41.0) ``format_rules_anchor`` / ``format_rules_field_specific``
    # 추상 훅 제거 — 유일 소비자였던 ``_format_rules_builder`` 와 함께 소멸
    # (리뷰 §4.2: 호출 0 + 공유 문구가 multi-op 규칙과 모순). 섹션 조립은
    # 각 포맷의 ``format_rules`` 가 통째로 소유한다.

    # ─── Parsing ────────────────────────────────────────────────
    # v8.41.0 (리뷰 §4.2 — parse/parse_turn 역전 수리): 루프의 실제 디스패치
    # 경계는 ``parse_turn`` 인데 종전엔 ``parse`` 가 추상이고 parse_turn 은
    # parse 를 감싸는 기본 구현이었다 — 등록된 두 포맷(json_fc/xml_fc) 모두
    # multi-op 라 그 기본 구현은 사문이었고, 새 포맷 작성자는 루프가 부르지
    # 않는 메서드를 구현하라고 안내받았다. 이제 **parse_turn 이 추상(1차)**,
    # parse 는 첫-op 투영 기본 구현(레거시 단수 소비자용 — history 직렬화
    # 기본·테스트)이다. 기존 플러그인의 자체 parse 오버라이드는 그대로.

    @abstractmethod
    def parse_turn(self, llm_text: str) -> ParsedTurn:
        """Parse one emission into a turn-level :class:`ParsedTurn` — the
        loop's dispatch boundary (구현 필수 — 루프가 부르는 1차 파서).

        Must not raise on malformed input — return a ``ParsedTurn`` with
        ``parse_stage = 0`` (ops 비움) instead. The loop's recovery path
        expects every emission to round-trip through this method, including
        garbage that needs an intervention.

        Preservation invariant (recovery paths depend on it): when an op's
        ``action`` slot is empty or invalid but an ``action_input`` was
        still identified, the parser MUST keep the op with its
        ``action_input`` rather than dropping it. ``infer_action`` (for
        ``action_required=False``) and the NO_ACTION recovery echo (for
        ``action_required=True``) both read it. ``parse_stage`` should be
        > 0 whenever an ``action_input`` was recovered this way (the exact
        value stays observability-only).
        """

    def parse(self, llm_text: str) -> ParsedAction:
        """Parse one emission into the singular legacy :class:`ParsedAction`.

        Default: first-op projection of :meth:`parse_turn` — for consumers
        of the pre-multi-op surface (the base history-serialization default,
        direct callers, tests). Plugins may override when their singular
        projection differs from "first op" (both built-ins do, keeping
        their historical projections byte-identical).
        """
        turn = self.parse_turn(llm_text)
        op = turn.ops[0] if turn.ops else Op()
        return ParsedAction(
            thought=turn.thought,
            action=op.action,
            action_input=op.action_input,
            raw=turn.raw,
            parse_stage=turn.parse_stage,
            thinking=turn.thinking,
            truncated=op.truncated,
        )

    # 미닫힘 thinking opener 의 truncation 정지점 — 포맷의 첫-구조 마커.
    # None 이면 EOF 까지 (종전 동작). 각 플러그인이 자기 마커로 지정.
    thinking_stop: re.Pattern | None = None

    @classmethod
    def strip_thinking(cls, text: str) -> tuple[str, str | None]:
        """파서 stage 0 — content 에 새어든 thinking 블록(완전 블록·미닫힘
        opener) 격리. ``(cleaned, thinking|None)`` 반환; 격리분은
        ``ParsedTurn.thinking``(verbose 전용, **비재공급**)으로 실린다.

        openai provider 는 같은 함수를 이미 호출하므로(5.10.0) 그 경로에선
        no-op 이고, provider 를 안 거치는 경로(anthropic/http 의 content-태그
        leak, bench 의 provider 우회, 직접 파서 호출)의 유일한 방어가 이
        stage 0 다. 구현은 ``agent_cli.thinking_tags`` 단일 소스 — 포맷 간
        behavior 공유가 아니라 ABC 기계(serialize/render 기본 구현과 동일
        관례)이므로 self-contained 불변식과 충돌하지 않는다. 고아/트레일링
        태그(③)는 앵커 위치가 포맷 소유라 여기 없다 — 각 플러그인이
        ``thinking_tags`` 의 정규식으로 자기 수리 파이프라인에서 처리.
        """
        from agent_cli.thinking_tags import strip_think_blocks

        cleaned, thinking = strip_think_blocks(text, stop=cls.thinking_stop)
        return cleaned, (thinking or None)

    # P0-4: 스트림 조기종료 게이트 문자 — 이 문자가 청크에 나타날 때만
    # ``is_degenerate`` 를 재실행한다(비용 게이트, O(트리거 발생 수)). 러너웨이
    # 시그니처가 shape 마다 다르므로 **플러그인이 소유**한다: json_fc 는 마크다운
    # 헤더 반복이라 "#", xml_fc 는 ``<tool_call>`` 반복이라 "<". 종전엔 http 골격이
    # "#" 을 하드코딩해 xml_fc 러너웨이에서 조기종료가 구조적으로 무발화했다.
    degeneration_trigger: str = "#"

    def is_degenerate(self, text: str) -> bool:
        """Whether *text* is a format runaway: the model repeated the wire
        shape instead of emitting one turn (e.g. several empty ``## Thought``
        / ``## Action`` blocks in a single json_fc response). Two uses: the
        loop passes it to ``provider.call(degeneration_check=...)`` to break
        the stream early, and labels the final emission ``FAILURE_DEGENERATE``.

        Default False — a wire shape with no observed runaway pattern (e.g.
        구 react 가 그랬듯 runaway-불가 shape) opts out. Shapes that can run away
        override with a cheap structural check (header count, etc.)."""
        return False

    # NOTE (v8.4.0): ``prose_completion`` (v7.14.0 — treat an action-less
    # prose turn as an implicit ``complete``) was removed. A production run
    # completed a skill with the transitional narration "Now let me write the
    # plan document:" as its result — the counterexample the 2026-07-23
    # bakeoff measured as zero. Completion intent is tool-input SEMANTICS and
    # semantics stay strict (v8.0.0 line; only wire SYNTAX is lenient):
    # action-less prose always takes the NO_ACTION nudge, whose per-format
    # wording (``constraint_reminder_action_required``) tells the model to
    # re-emit a prose answer through an explicit ``complete`` op.

    def sanitize_thought(self, thought: str | None) -> str | None:
        """Strip wire-shape sentinels the model leaked into its thought text,
        so they are not re-injected into the next-turn prior. A thought ending
        in a stray ``## Thought`` would render back as ``## Thought … ##
        Thought`` in the prior, teaching the model (self-reinforcement) that
        repeating the shape is fine — the root cause of format runaway. Applied
        at save time in two spots: ``parse`` cleans ``ParsedAction.thought``
        (structured turns), and ``serialize_assistant_for_history`` cleans the
        bare-content fallback (fully-degenerate turns with no valid action).
        Both feed history → prior (render) → on-screen, so cleaning once at
        save covers every consumer.

        Default identity: a wire whose thought cannot carry its own sentinels
        (예: thought 가 JSON-이스케이프 문자열인 포맷) opts out. json_fc
        overrides to drop stray ``##`` header lines. (``action`` / ``action_
        input`` need no cleaning — an invalid action token is already rejected,
        and action_input is JSON-escaped so its content can't form a line-start
        sentinel.)"""
        return thought

    # ─── Recovery wording (abstract) ────────────────────────────

    @abstractmethod
    def constraint_reminder_call(self) -> str:
        """One-sentence reminder of the required tool call shape.

        Embedded by ``recovery.wf_recovery.format_no_json_retry`` as
        the "Honor that. <reminder>." tail of the intervention message.
        Should describe the envelope and the inner JSON fields the
        parser expects.
        """

    @abstractmethod
    def constraint_reminder_action_required(self) -> str:
        """Reminder used when parsing succeeded but ``action`` was missing.

        Should present BOTH paths the model can take:
        invoke a tool *or* call ``complete``. Embedded by
        ``recovery.wf_recovery.format_no_action_retry``.
        """

    @abstractmethod
    def failure_framing_parse_fail(self) -> str:
        """Opening line of the intervention when parsing failed entirely.

        e.g. ``"Your response was not valid JSON."`` for ReAct. Embedded
        as the first line of ``format_no_json_retry``'s message.
        """

    @abstractmethod
    def failure_framing_no_action(self) -> str:
        """Opening line of the intervention when parsing succeeded but
        ``action`` was missing.

        e.g. ``"Your JSON was parsed but has no action."`` for ReAct.
        """

    @abstractmethod
    def static_retry_hint_no_json(self) -> str:
        """Fallback message when the prior emission was empty / whitespace.

        Used by ``format_no_json_retry`` when there's nothing meaningful
        to echo back. Should be self-contained — framing + reminder
        rolled into one short paragraph.
        """

    @abstractmethod
    def static_retry_hint_no_action(self) -> str:
        """Fallback message when the prior emission was empty / whitespace
        and parsing produced no action."""

    def diagnose_syntax_error(self, prior_content: str) -> str | None:
        """Pinpoint *where* the prior emission's JSON broke (message +
        line/column + caret), or ``None`` when there's nothing to diagnose.

        Opt-in seam: the base returns ``None`` so a format that carries no
        JSON (or chooses not to diagnose) keeps the generic NO_JSON hint
        unchanged. JSON-bearing formats override to extract their JSON
        candidate (format-specific) and hand it to
        ``wire_formats._json_diag.describe_json_error`` (the shared pure
        formatter). Consumed by ``recovery.wf_recovery.format_no_json_retry``
        via the loop's parse-fail recovery.
        """
        return None

    @abstractmethod
    def system_user_prefixes(self) -> tuple[str, ...]:
        """Return the list of recovery framing prefixes this plugin emits.

        Used by ``recent_exchanges`` (context/session.py) to skip
        system-injected user messages when surfacing the resume preview
        — without this list the user would see "Your response was not
        valid JSON." style hints as if they were real conversation.

        Each entry is the *opening prefix* of a message produced by this
        plugin's recovery (``failure_framing_*``, ``static_retry_hint_*``).
        Format-agnostic prefixes (``"You have called"``, etc. for B1
        action-loop interventions) live in
        ``wire_formats._FORMAT_AGNOSTIC_USER_PREFIXES`` and are unioned
        with this list at consume time.
        """

    # ─── Prompt (default) ───────────────────────────────────────

    @staticmethod
    def _gated_rule(required: bool, strong: str, soft: str | None = None) -> str:
        """Pick a Format-Rules clause by a required-flag — the hook that lets
        ``thought_required`` / ``action_required`` weaken (or drop) a field's
        rule once an optional phrasing is validated.

        When ``required`` is True, or no ``soft`` variant is supplied, the
        strong obligation is used. Today every caller omits ``soft``, so the
        prompt is byte-for-byte unchanged whatever the flags say; supplying a
        ``soft`` string (or ``""`` to drop the line) is the single edit needed
        to soften a field's rule later, with no parser/loop change. Symmetric
        with how the flags already gate the *recovery* side in the loop."""
        return soft if (not required and soft is not None) else strong

    @abstractmethod
    def format_rules(self) -> str:
        """Compose the ``## Response Format`` section for the system prompt.

        v8.41.0 (리뷰 §4.2): 종전 기본 구현은 ``_format_rules_builder`` 에
        위임했는데, 등록된 두 포맷 모두 자체 구현이라 **호출 0** 인 데다
        빌더의 공유 문구("Exactly ONE action per turn")가 현행 multi-op
        규칙과 정면 모순이었다 — 새 포맷 작성자가 사문·모순 텍스트를
        상속받는 함정. 빌더는 제거하고 추상으로 승격: 각 포맷이 자기
        wire shape 에 맞는 섹션을 직접 소유한다.
        """

    def render_action_input(self, action_input: dict) -> str:
        """Render an action_input dict in this format's inner shape.

        The wire format owns serialization. ReAct and tag-wrapped
        formats all nest action_input as a JSON object, so
        the default serializes with ``json.dumps``. A plugin whose inner
        shape is not JSON (e.g. XML attribute encoding, key:value lines)
        overrides this hook. Callers (system-prompt inline guides,
        history rendering) pass a dict and never assume JSON — the JSON
        assumption is captured here, in one wire-owned place.
        """
        return json.dumps(action_input, ensure_ascii=False)

    # ─── Provider / lifecycle (default) ─────────────────────────

    def provider_call_kwargs(self, capabilities) -> dict:
        """Extra kwargs for ``provider.call()`` — wire-shape ⨯ capability 를
        조합하는 단일 지점 (provider 는 capabilities 를 직접 안 본다).

        기본 = 빈 dict. (json_mode 기계는 v7.0.0 에서 유일 소비자 react 와
        함께 제거 — JSON-object 모드는 선두 ``{`` 를 강제해 산문-선행/태그
        envelope 과 양립 불가했고, 실측 이점도 없었다. 미래 플러그인이
        provider 힌트가 필요하면 이 훅을 override.)
        """
        return {}

    def prefill(self) -> str:
        """Return assistant-turn prefill string, or empty for no prefill.

        Default no prefill — the model's prior produces the wire shape
        on its own. Non-canonical formats override to force the wire
        shape from the first generated token.

        When non-empty, the loop appends
        ``{"role":"assistant","content":<prefill>}`` as the last message
        before the LLM call. The provider treats this as "continue from
        here," forcing the wire format from the first generated token.
        The loop prepends the prefill to the response so downstream
        parsers see a complete emission.
        """
        return ""

    # ─── History / context-window (default) ─────────────────────
    # Default implementations of the (A → B) and (B → D) transitions
    # compose ``self.parse()`` and ``self.render_full_example()``. They
    # form the round-trip: ``serialize`` and ``render`` are inverses up
    # to JSON normalization (key order = thought→action→action_input,
    # default ``json.dumps`` spacing). Plugins override only when their
    # wire shape needs non-round-trip behavior.

    def serialize_assistant_for_history(self, raw_text: str) -> dict:
        """Convert a raw emission into the dict stored in history.jsonl.

        Default: ``self.parse(raw_text)`` + structured-field extraction.
        Returned dict carries ``role="assistant"`` plus
        ``thought / action / action_input`` as top-level fields when
        parse succeeded with an action, falling back to bare ``content``
        when parse produced no action so corrupt emissions still survive
        in the log for postmortem.

        Both branches are sanitized at this single save-time point (the ABI
        contract): the structured ``thought`` is cleaned inside ``parse``,
        and the bare ``content`` fallback is passed through
        :meth:`sanitize_thought` here. This is what keeps a wire sentinel the
        model leaked from riding back into the next-turn prior (which is built
        by ``render`` from this record) and re-teaching the runaway shape.

        Routing parse through this default also means the live-dispatch
        parser and the history-write parser share the same 3-stage
        fallback — including JSON repair — so a recoverable emission
        produces the same structured record either way.
        """
        parsed = self.parse(raw_text)
        if parsed.action:
            return {
                "role": "assistant",
                "thought": parsed.thought or "",
                "action": parsed.action,
                "action_input": (
                    parsed.action_input if parsed.action_input is not None else {}
                ),
            }
        # ``or ""`` (NOT ``or raw_text``): if sanitize empties the content
        # (a fully-degenerate emission that was nothing but sentinel lines),
        # the prior must be blank — falling back to raw would re-inject the
        # exact sentinels we are trying to strip. Bare content that is real
        # prose (e.g. broken-JSON NO_JSON turns with no ## headers) is left
        # intact because sanitize returns it unchanged.
        return {
            "role": "assistant",
            "content": self.sanitize_thought(raw_text) or "",
        }

    def serialize_terminal_for_history(self, thought: str, result: str) -> dict:
        """History record for a terminal ``complete`` turn.

        The loop's complete handler holds the (possibly nested-envelope-
        unwrapped) result rather than the raw emission, so it cannot route
        through :meth:`serialize_assistant_for_history`. This is the parallel
        entry point that stores the terminal turn in the SAME shape this
        format uses for every other op, keeping history homogeneous (and
        resume / shape-reading tooling consistent — a hand-built record here
        once stored ``complete`` in a different shape than the rest).

        Default is the singular ``{action, action_input}`` shape; multi-op
        formats override to their ``ops`` shape.
        """
        return {
            "role": "assistant",
            "thought": thought or "",
            "action": "complete",
            "action_input": {"result": result},
        }

    def render_assistant_from_history(self, record: dict) -> dict:
        """Convert a history.jsonl assistant record into a message dict.

        Default: round-trip the structured fields back to the wire shape
        via ``self.render_full_example`` so the model on overflow
        recovery / session resume sees the same shape it originally
        emitted (self-reinforcement preserved across the recovery
        boundary).

        ``action_input`` is serialized via ``render_action_input`` (the
        wire's own hook) before passing to ``render_full_example`` (which
        accepts the already-serialized string). Records that lack
        structured fields — typically those that
        ``serialize_assistant_for_history`` stored as bare ``content``
        because parse produced no action — are returned as-is.

        Differences from the original emission are limited to JSON
        normalization (key order, default ``json.dumps`` spacing).
        Semantic content is preserved verbatim.
        """
        if "thought" not in record and "action" not in record:
            return {"role": "assistant", "content": record.get("content", "")}

        # Serialize through the wire's own ``render_action_input`` hook so
        # the JSON assumption lives in one place, not duplicated here. The
        # default hook is ``json.dumps`` — which handles every valid JSON
        # value (dict, list, string, number, bool, null) with correct
        # quoting (``str()`` would emit bare strings that re-render as
        # malformed JSON). Real driver: complete action with raw-string
        # ``action_input`` (legacy / drift).
        action_input = record.get("action_input", {})
        action_input_str = self.render_action_input(action_input)

        return {
            "role": "assistant",
            "content": self.render_full_example(
                thought=record.get("thought") or "",
                action=record.get("action") or "",
                action_input=action_input_str,
            ),
        }
