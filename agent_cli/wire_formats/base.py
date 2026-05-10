"""Wire format plugin protocol and shared types.

A "wire format" is the on-the-wire shape of a single LLM response — what the
model is asked to emit, what the parser reads, and what the recovery layer
shows the model when something goes wrong. The bundle is hot-swappable so
new format experiments live in their own module and can be added or removed
without touching the loop, prompts, or recovery primitives.

See ``agent_cli/wire_formats/react.py`` for the reference implementation
that mirrors the pre-plugin behavior bit-for-bit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class ParsedAction:
    """Format-agnostic parse result.

    Carries everything the loop needs to dispatch one action, plus a small
    set of generally-useful metadata fields. Format-specific debug info
    (envelope reject reasons, extra-field leakage, etc.) belongs inside
    the plugin — this dataclass is the *boundary* between plugin and loop.

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


@runtime_checkable
class WireFormat(Protocol):
    """Plugin contract for one wire format.

    Plugins live in ``agent_cli/wire_formats/<name>.py`` and register
    themselves via :func:`agent_cli.wire_formats.register` at import time.
    The loop / prompt / recovery layers only see this Protocol — they
    never branch on the plugin's name.

    Method groups:
      - **Prompt**: what the model is told to emit.
      - **Parsing**: how the emitted text becomes a ``ParsedAction``.
      - **Recovery**: what the model is told when parsing failed.

    Each method returns a string fragment or a ``ParsedAction``. Composing
    those fragments into the actual prompt section / intervention message
    is the caller's job — keeping plugins thin avoids re-implementing the
    surrounding recovery logic per plugin.
    """

    name: str
    """Short identifier used by the CLI ``--response-format`` option and
    the registry. Convention: lowercase, ``[a-z0-9_-]``."""

    thought_required: bool
    """Whether the wire format treats ``thought`` as a mandatory schema
    field. ReAct sets True (the recovery layer fires NO_THOUGHT when an
    action is emitted without a thought field). Envelope-style formats
    where the thought is preceding free text set False — its absence is
    valid, not a drift signal."""

    # ─── Prompt ────────────────────────────────────────────────
    # The ``## Response Format`` section is composed by
    # ``wire_formats._format_rules_builder.build_format_rules``: it
    # carries the shared text (completion intro, rules 3-6) and calls
    # the three plugin methods below for the wire-shape-dependent parts.
    # Plugins themselves implement ``format_rules()`` simply by
    # returning ``build_format_rules(self)``.

    def format_rules(self) -> str:
        """Return the ``## Response Format`` section body for this plugin.

        Plugins typically delegate to
        ``_format_rules_builder.build_format_rules(self)``; the
        builder sources the shared text and calls the rendering hooks
        below. Returning a hand-written string instead is permitted
        for plugins whose section diverges so much that templating
        would obscure rather than clarify.
        """
        ...

    def format_rules_anchor(self) -> str:
        """One-sentence anchor that opens the section after the heading.

        Tells the model what wire shape it must emit. ReAct says
        "You MUST output a single JSON object only — …"; envelope
        formats say "Output your response inside a single <tool_use>
        envelope. …". Newlines are allowed for multi-line anchors.
        """
        ...

    def render_full_example(self, *, thought, action: str, action_input: str) -> str:
        """Render one full example of the wire shape.

        The builder calls this three times with shared logical inputs
        — schema example, ``ready_for_review`` example, ``complete``
        example — so the *content* is identical across plugins and
        only the on-the-wire form differs. Measurement of model
        compliance can therefore compare two plugins fairly.

        Args:
            thought: Reasoning text. ``None`` means "invocation only";
                each plugin chooses how to handle the absent slot —
                ReAct simply omits the field, envelope formats may
                substitute a short placeholder so the slot is visible.
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
        ...

    def format_rules_field_specific(self) -> str:
        """Lines for Rules 1 and 2 of the section.

        Rules 1 and 2 obligate the model to populate the reasoning /
        thought slot and the action input slot, but the field names
        differ by wire shape (``thought`` / ``reasoning text``,
        ``action_input`` / ``JSON dict``). Rules 3-6 are shared text
        and live in the builder.

        The returned string contains both rules joined by newlines
        and starts with ``"1. …\\n2. …"``; it is spliced between
        ``"Rules:"`` and the shared tail.
        """
        ...

    # ─── Parsing ───────────────────────────────────────────────

    def parse(self, llm_text: str) -> ParsedAction:
        """Parse one model emission into a ``ParsedAction``.

        Must not raise on malformed input — return a ``ParsedAction`` with
        ``parse_stage = 0`` instead. The loop's recovery path expects
        every emission to round-trip through this method, including
        garbage that needs an intervention.
        """
        ...

    # ─── Recovery (string fragments) ───────────────────────────

    def constraint_reminder_call(self) -> str:
        """One-sentence reminder of the required tool call shape.

        Embedded by ``recovery.builders.format_no_json_retry`` as the
        "Honor that. <reminder>." tail of the intervention message.
        Should describe the envelope and the inner JSON fields the
        parser expects.
        """
        ...

    def constraint_reminder_action_required(self) -> str:
        """Reminder used when parsing succeeded but ``action`` was missing.

        Should present BOTH paths the model can take:
        invoke a tool *or* call ``complete``. Embedded by
        ``recovery.builders.format_no_action_retry``.
        """
        ...

    def failure_framing_parse_fail(self) -> str:
        """Opening line of the intervention when parsing failed entirely.

        e.g. ``"Your response was not valid JSON."`` for ReAct. Embedded
        as the first line of ``format_no_json_retry``'s message.
        """
        ...

    def failure_framing_no_action(self) -> str:
        """Opening line of the intervention when parsing succeeded but
        ``action`` was missing.

        e.g. ``"Your JSON was parsed but has no action."`` for ReAct.
        """
        ...

    def static_retry_hint_no_json(self) -> str:
        """Fallback message when the prior emission was empty / whitespace.

        Used by ``format_no_json_retry`` when there's nothing meaningful
        to echo back. Should be self-contained — framing + reminder
        rolled into one short paragraph.
        """
        ...

    def static_retry_hint_no_action(self) -> str:
        """Fallback message when the prior emission was empty / whitespace
        and parsing produced no action."""
        ...

    def system_user_prefixes(self) -> tuple[str, ...]:
        """Return the list of recovery framing prefixes this plugin emits.

        Used by ``recent_exchanges`` (context/session.py) to skip
        system-injected user messages when surfacing the resume preview
        — without this list the user would see "Your response was not
        valid JSON." style hints as if they were real conversation.

        Each entry is the *opening prefix* of a message produced by this
        plugin's recovery (``failure_framing_*``, ``static_retry_hint_*``).
        Format-agnostic prefixes (``"You have called"``, etc. for B1
        action-loop interventions) are kept in
        ``constants.SYSTEM_USER_PREFIXES`` and unioned with this list at
        consume time.
        """
        ...

    # ─── Provider / lifecycle ──────────────────────────────────

    def prefill(self) -> str:
        """Return assistant-turn prefill string, or empty for no prefill.

        When non-empty, the loop appends ``{"role":"assistant","content":<prefill>}``
        as the last message before the LLM call. The provider treats this
        as "continue from here," forcing the model into the wire format
        from the first generated token. The loop prepends the prefill to
        the response so downstream parsers see a complete emission.

        ReAct returns ``""`` (no prefill needed — the model's prior
        already produces ReAct shape). Envelope formats return e.g.
        ``'<tool_use id="r1">{'`` so the model emits ``"action": ...``
        next.
        """
        ...

    # ─── History / context-window policy ───────────────────────
    # Three knobs control how an assistant turn is shaped while it
    # travels through the conversation pipeline. Different wire
    # formats benefit from different policies — see
    # ``docs/ARCHITECTURE.md`` §5 for the trade-offs (raw self-
    # reinforcement vs. natural-language compactness vs. drift
    # filtering).
    #
    # Pipeline call order:
    #   1. Model emits raw_text.
    #   2. ``normalize_assistant_for_messages(raw_text)`` runs first —
    #      result is appended to the in-memory ``messages`` buffer the
    #      LLM sees as next-turn prior.
    #   3. ``serialize_assistant_for_history(raw_text)`` runs in
    #      parallel — result is persisted to history.jsonl.
    #   4. On overflow recovery / session resume, history records
    #      pass through ``render_assistant_from_history(record)`` to
    #      become messages again.

    def normalize_assistant_for_messages(self, raw: str) -> str:
        """Rewrite a model emission for the in-memory ``messages`` buffer.

        The buffer is fed back to the LLM as next-turn prior. When the
        model drifts to a different shape mid-conversation (e.g. emits
        ReAct in tool_use mode), leaving the raw text in the buffer
        teaches the model "I emitted that last time" and reinforces the
        very prior we wanted to override.

        ReAct returns ``raw`` unchanged (raw IS the ReAct shape).
        Envelope formats parse ``raw`` and re-render in their envelope
        shape so the model's own prior teaches the envelope.

        Pure function — does not touch ``history.jsonl``. The lossless
        principle is preserved by recording raw text on disk; this
        method affects only the in-memory next-turn prior.
        """
        ...

    def serialize_assistant_for_history(self, raw_text: str) -> dict:
        """Convert a raw model emission into the dict stored in
        ``history.jsonl``.

        The returned dict carries ``role="assistant"`` plus whatever
        plugin-specific fields the plugin will later expect to read in
        :meth:`render_assistant_from_history`. ReAct splits into
        ``thought``, ``action``, ``action_input``. Envelope plugins may
        keep an envelope summary or store the raw text as ``content``.

        Must not raise on malformed input — return a fallback shape
        like ``{"role": "assistant", "content": raw_text}`` so corrupt
        emissions still survive in the log for postmortem.
        """
        ...

    def render_assistant_from_history(self, record: dict) -> dict:
        """Convert one history.jsonl assistant record into a message
        dict for the LLM.

        Used when restoring messages from disk — overflow recovery,
        session resume, fork. The returned dict has the
        ``{"role": "assistant", "content": …}`` shape consumed by
        chat completion APIs.

        Each plugin chooses how to render: ReAct produces a natural-
        language summary (``"thought: …\\naction: read_file({path})"``)
        which is compact and easy for the model to reason about, but
        loses self-reinforcement on the wire format. Envelope plugins
        may keep the original envelope text so self-reinforcement
        survives an overflow boundary.
        """
        ...

    def provider_call_kwargs(self) -> dict:
        """Extra kwargs to pass to ``provider.call()`` for this plugin.

        Currently used to disable Ollama's ``format=json`` mode for
        envelope formats (the envelope tag is a non-JSON prefix that
        ``format=json`` rejects). ReAct returns ``{}`` to keep
        capability-driven JSON mode active.

        Keeping these as opaque dict kwargs lets plugins hook into
        provider features without the provider layer learning per-plugin
        details — providers ignore unknown kwargs by contract.
        """
        ...
