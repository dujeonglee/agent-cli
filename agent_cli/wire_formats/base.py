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
  - ``format_rules`` — delegate to the shared builder.
  - ``render_action_input`` — dict → JSON via ``json.dumps``.
  - ``provider_call_kwargs`` — empty dict.
  - ``prefill`` — empty string.

So a typical plugin only implements the wire-shape-specific abstract
methods: ``parse``, ``render_full_example``, ``format_rules_anchor``,
``format_rules_field_specific``, and the recovery wording strings. The
serialize / render defaults compose those into the lifecycle automatically
— ``serialize`` calls ``self.parse()`` and extracts structured fields;
``render`` calls ``self.render_full_example()`` to re-emit the wire shape
from the stored record.

See ``agent_cli/wire_formats/react.py`` for the reference implementation.
"""

from __future__ import annotations

import json
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
    single-action wire format (react) yields a turn with exactly
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

    The default :meth:`WireFormat.parse_turn` wraps a plugin's existing
    :meth:`WireFormat.parse` into this shape, so a plugin opts into multi-op
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

    False (default — react): one action per turn; per-tool batch
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

        The Format Rules builder calls this three times with shared
        logical inputs — schema example, ``ready_for_review`` example,
        ``complete`` example — so the *content* is identical across
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
            action: Action name (e.g. ``"read_file"``,
                ``"ready_for_review"``).
            action_input: action_input as a JSON string. Plugins
                splice it into their wire shape verbatim — receiving
                a string rather than a dict avoids each plugin having
                to make formatting decisions about whitespace / key
                order.

        Returns:
            The rendered example, no surrounding whitespace, no
            trailing newline.
        """

    @abstractmethod
    def format_rules_anchor(self) -> str:
        """One-sentence anchor that opens the section after the heading.

        Tells the model what wire shape it must emit. ReAct says
        "You MUST output a single JSON object only — …". Newlines are
        allowed for multi-line anchors.
        """

    @abstractmethod
    def format_rules_field_specific(self) -> str:
        """Lines for Rules 1 and 2 of the section.

        Rules 1 and 2 obligate the model to populate the reasoning /
        thought slot and the action input slot, but the field names
        differ by wire shape. Rules 3-6 are shared text and live in the
        builder.

        The returned string contains both rules joined by newlines and
        starts with ``"1. …\\n2. …"``; it is spliced between
        ``"Rules:"`` and the shared tail.
        """

    # ─── Parsing (abstract) ─────────────────────────────────────

    @abstractmethod
    def parse(self, llm_text: str) -> ParsedAction:
        """Parse one model emission into a ``ParsedAction``.

        Must not raise on malformed input — return a ``ParsedAction`` with
        ``parse_stage = 0`` instead. The loop's recovery path expects
        every emission to round-trip through this method, including
        garbage that needs an intervention.

        Preservation invariant (both flags' recovery paths depend on it):
        when the ``action`` slot is empty or invalid but an
        ``action_input`` was still identified, the parser MUST keep it in
        ``action_input`` rather than dropping it. ``infer_action`` (for
        ``action_required=False``) and the NO_ACTION recovery echo (for
        ``action_required=True``) both read it — dropping it here is what
        regressed prefix_md's dropped-action recovery. ``parse_stage``
        should be > 0 whenever an ``action_input`` was recovered this way,
        so the loop treats the emission as parsed (the exact value stays
        observability-only — see :class:`ParsedAction`).
        """

    def parse_turn(self, llm_text: str) -> ParsedTurn:
        """Parse one emission into a turn-level :class:`ParsedTurn` — the
        loop's dispatch boundary.

        Default: wrap :meth:`parse`. A single-action format yields a turn with
        one ``Op`` (or zero ops on parse failure / no action); ``terminal`` is
        always ``False`` (it completes via a ``complete`` op, not a thought-only
        turn). So single-action plugins need not override this, and the loop's
        one-op iteration reproduces today's behaviour exactly.

        A multi-op format overrides this to return several ops, or
        ``terminal=True`` for a no-op completion turn. ``parse`` (and the
        history round-trip built on it) is left untouched either way.
        """
        pa = self.parse(llm_text)
        # Preserve the parse invariant: keep a single Op whenever there is an
        # action OR a recovered action_input (the dropped-action /
        # parse_stage-3 case), so the loop's per-op infer_action / NO_ACTION
        # echo can still recover it. Only a total parse failure (no action,
        # no input) yields zero ops.
        ops = (
            [Op(pa.action, pa.action_input, pa.truncated)]
            if (pa.action is not None or pa.action_input is not None)
            else []
        )
        return ParsedTurn(
            thought=pa.thought,
            ops=ops,
            terminal=False,
            raw=pa.raw,
            parse_stage=pa.parse_stage,
            thinking=pa.thinking,
        )

    def is_degenerate(self, text: str) -> bool:
        """Whether *text* is a format runaway: the model repeated the wire
        shape instead of emitting one turn (e.g. several empty ``## Thought``
        / ``## Action`` blocks in a single md_array response). Two uses: the
        loop passes it to ``provider.call(degeneration_check=...)`` to break
        the stream early, and labels the final emission ``FAILURE_DEGENERATE``.

        Default False — a wire shape with no observed runaway pattern (e.g.
        react under json_object mode) opts out. Shapes that can run away
        override with a cheap structural check (header count, etc.)."""
        return False

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
        (react: thought is a JSON string, escaped) opts out. md_array
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

    def format_rules(self) -> str:
        """Compose the ``## Response Format`` section via shared builder.

        Default delegates to
        ``_format_rules_builder.build_format_rules(self)`` which sources
        the shared text (completion intro, rules 3-6) and calls
        ``format_rules_anchor`` / ``render_full_example`` /
        ``format_rules_field_specific`` for the wire-shape-dependent
        parts. Plugins whose section diverges so much that templating
        would obscure rather than clarify may override.
        """
        from agent_cli.wire_formats._format_rules_builder import build_format_rules

        return build_format_rules(self)

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
        """Extra kwargs for ``provider.call()``, decided from model
        ``capabilities`` — the single place where wire-shape ⨯ capability
        is combined (so the provider layer never has to).

        Default — JSON-shaped formats (ReAct, envelope) request the
        provider's JSON-object mode iff the model supports structured
        output: ``{"json_mode": capabilities.supports_structured_output}``.
        md_array's markdown overrides to ``{"json_mode": False}``
        regardless of capability — forcing JSON mode on a markdown-shaped
        prompt makes the model degenerate (the ``[2025]`` / ``[1000,1000]``
        bug).

        Providers treat ``json_mode`` opaquely (openai → response_format;
        anthropic ignores it) and never inspect ``capabilities`` for this
        decision themselves.
        """
        return {"json_mode": capabilities.supports_structured_output}

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
        The unsanitized raw is still kept in ``raw_failures.jsonl`` for failed
        turns, so no postmortem fidelity is lost.

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
