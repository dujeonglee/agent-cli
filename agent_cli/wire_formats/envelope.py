"""Envelope wire format — XML envelope wrapping a JSON action_input.

Wire shape::

    <tool_use id="r1" action="<tool_name>">
    <free reasoning text — purpose and why this action>

    {"<arg>": "<value>", ...}
    </tool_use>

The envelope tag carries the action name as an XML attribute so the
action survives even if the inner JSON is malformed. The reasoning is
free natural language, freed from JSON schema constraints (which
small models tend to truncate or omit). The JSON dict carries only
``action_input`` — schema validation, nested types, list-of-dicts all
work as in ReAct because the dict shape is unchanged.

Compared with the ReAct shape ``{"thought":...,"action":...,"action_input":...}``:
  - More tokens than ReAct (the envelope wrapper).
  - Reasoning is unconstrained — model can write multi-paragraph
    rationale without breaking JSON.
  - Action name is robust to inner-JSON corruption (XML attribute).
  - Multi-action future-friendly: the ``id`` attribute is already in
    place for distinguishing parallel envelopes.

Self-contained boundary: this module owns the format rules text, the
parser, the recovery wording, and the history-pipeline policy. No
external file references EnvelopeFormat by name. Removing this file
removes envelope support entirely.
"""

from __future__ import annotations

import json
import re

from agent_cli.recovery.intervention import Intervention
from agent_cli.recovery.primitives import echo_prior_output
from agent_cli.tools.action_summary import summarize_action_args
from agent_cli.wire_formats.base import ParsedAction


# ── Prompt section ───────────────────────────────────────────
# Format Rules section is composed by the shared builder
# ``_format_rules_builder.build_format_rules``: it carries the
# completion intro and rules 3-6 verbatim and calls the three
# render hooks below for the wire-shape parts. Two plugins rendering
# the same logical content therefore share byte-equivalent text in
# the common parts, which is what makes side-by-side measurement of
# model behaviour fair.

_FORMAT_RULES_ANCHOR = (
    "Output your response inside a single <tool_use> envelope. The envelope\n"
    "contains free-text reasoning followed by a JSON dict for the action_input:"
)

_FORMAT_RULES_FIELD_SPECIFIC = (
    "1. The reasoning text MUST state purpose (what you want to achieve)\n"
    "   and reason (why this specific action). Do not leave it empty.\n"
    "2. The JSON dict MUST match the tool's input schema."
)


# ── Recovery fragments ────────────────────────────────────────
# Mirror the structure ReActFormat uses: framings + reminders +
# static fallbacks. The strings reflect the envelope format so the
# model sees concrete what-to-fix guidance.
_CONSTRAINT_REMINDER_CALL = (
    'Output a single <tool_use id="r1" action="<tool_name>">…</tool_use> '
    "envelope: reasoning text on its own line(s), then a JSON action_input "
    "dict, then </tool_use>."
)

_CONSTRAINT_REMINDER_ACTION_REQUIRED = (
    "The <tool_use> tag MUST carry an action attribute. Either invoke a tool: "
    '<tool_use id="r1" action="<tool_name>">…{"…":"…"}…</tool_use> '
    "or finish: "
    '<tool_use id="r1" action="complete">…{"result":"…"}…</tool_use>'
)

_FAILURE_FRAMING_PARSE_FAIL = (
    "Your response did not match the <tool_use> envelope format."
)
_FAILURE_FRAMING_NO_ACTION = (
    "Your <tool_use> envelope was parsed but the action attribute is missing."
)

_STATIC_RETRY_HINT_NO_JSON = (
    f"{_FAILURE_FRAMING_PARSE_FAIL} {_CONSTRAINT_REMINDER_CALL}"
)

_STATIC_RETRY_HINT_NO_ACTION = (
    f"{_FAILURE_FRAMING_NO_ACTION} {_CONSTRAINT_REMINDER_ACTION_REQUIRED}"
)

# Recent-exchanges filter: skip system-injected user messages that
# start with one of these prefixes. Same role as ReActFormat's list.
_SYSTEM_USER_PREFIXES = (
    _FAILURE_FRAMING_PARSE_FAIL,
    _FAILURE_FRAMING_NO_ACTION,
    "Your <tool_use> envelope was missing the reasoning text.",
)


# NO_THOUGHT recovery (envelope-specific framing).
_NO_THOUGHT_FRAMING = "Your <tool_use> envelope was missing the reasoning text."

_NO_THOUGHT_CONSTRAINT = (
    "The free text inside the envelope (before the JSON dict) MUST state "
    "purpose (what you want to achieve) and reason (why this specific action). "
    "Do not leave it empty."
)


# ── Parser ───────────────────────────────────────────────────
# Plugin-internal helpers. Surrogate sanitisation and thinking-block
# stripping are duplicated from react.py rather than shared via a
# common module — keeping each plugin folder-deletable trumps DRY for
# this short helper. If a third plugin appears with the same policy
# we can lift these into a wire_formats common module.

_THINKING_TAGS = ["think", "thinking", "reasoning", "reflection"]
_THINKING_PATTERN = re.compile(
    r"<(" + "|".join(_THINKING_TAGS) + r")>(.*?)</\1>",
    re.S | re.I,
)

# Envelope tag — non-greedy inner so the first closing tag wins.
_ENVELOPE_PATTERN = re.compile(
    r"<tool_use\b([^>]*)>(.*?)</tool_use>",
    re.DOTALL,
)
_ACTION_ATTR_PATTERN = re.compile(r'\baction\s*=\s*"([^"]*)"')


def _sanitize_surrogates(text: str) -> str:
    """Remove unpaired Unicode surrogates that break JSON parsing."""
    return re.sub(r"[\ud800-\udfff]", "", text)


def _strip_thinking_blocks(text: str) -> tuple[str, str | None]:
    """Strip ``<think>`` / ``<reasoning>`` style blocks, returning the
    cleaned text plus the joined thinking content (or ``None``)."""
    parts: list[str] = []

    def _collect(match: re.Match) -> str:
        content = match.group(2).strip()
        if content:
            parts.append(content)
        return ""

    cleaned = _THINKING_PATTERN.sub(_collect, text).strip()
    if parts:
        return cleaned, "\n\n".join(parts)
    return text, None


def _find_last_json_block(text: str) -> tuple[int, int] | None:
    """Find the last balanced top-level ``{...}`` block.

    Returns ``(start, end_exclusive)`` or ``None``. Uses brace counting
    that respects string literals (so ``{"a":"}"}`` doesn't trip on
    the inner brace), and tracks the *last* fully balanced block — the
    JSON action_input is by convention the trailing dict in the
    envelope, after any reasoning text that itself might mention a
    dict-like fragment.
    """
    last: tuple[int, int] | None = None
    depth = 0
    start = -1
    in_str = False
    escape = False
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    last = (start, i + 1)
    return last


def parse_envelope(text: str) -> ParsedAction:
    """Parse an envelope-shape LLM emission into a :class:`ParsedAction`.

    Stages:
      0. Sanitize surrogates, strip thinking blocks.
      1. Match ``<tool_use ...>...</tool_use>`` (DOTALL, non-greedy).
      2. Extract ``action`` attribute from the opening tag.
      3. Find the last balanced ``{...}`` block in the inner content;
         everything before it is the reasoning text (``thought``).
      4. ``json.loads`` the block to get ``action_input``.

    parse_stage policy:
      0 — no envelope match in the text (full failure).
      1 — envelope + action + valid JSON (success).
      2 — envelope + action attribute, but the JSON block did not parse.
          The loop sees ``action`` present and ``action_input=None``;
          downstream schema-mismatch / no-input recovery handles it.
          We do not invoke ReAct's ``repair_json`` because envelope JSON
          observed in our probes has been clean — we fail fast and
          revisit if measurement shows otherwise.
      3 — envelope present, no action attribute. Loop sees
          ``action=None`` → NO_ACTION intervention.

    Never raises on malformed input — always returns a ``ParsedAction``.
    """
    text = _sanitize_surrogates(text)
    text, thinking = _strip_thinking_blocks(text)
    result = ParsedAction(raw=text, thinking=thinking)

    m = _ENVELOPE_PATTERN.search(text)
    if not m:
        return result  # parse_stage stays 0

    attrs = m.group(1)
    inner = m.group(2)

    action_match = _ACTION_ATTR_PATTERN.search(attrs)
    action = action_match.group(1) if action_match else None

    block = _find_last_json_block(inner)
    action_input: dict | str | None = None
    thought_text: str
    if block is not None:
        json_start, json_end = block
        json_text = inner[json_start:json_end]
        thought_text = inner[:json_start].strip()
        try:
            parsed = json.loads(json_text)
            if isinstance(parsed, dict):
                action_input = parsed
        except (json.JSONDecodeError, ValueError):
            # Fail fast (see docstring) — leave action_input None.
            pass
    else:
        thought_text = inner.strip()

    result.thought = thought_text or None
    result.action = action
    result.action_input = action_input  # may be None if JSON missing/broken

    if action is None:
        # Envelope present, action attribute missing — NO_ACTION path.
        # action_input may still be populated (if a JSON dict was inside);
        # surfacing it doesn't trigger dispatch because action is None,
        # but echoing it back can help the recovery framing.
        result.parse_stage = 3
    elif action_input is not None:
        # Full happy path: envelope + action + parseable JSON.
        result.parse_stage = 1
    else:
        # Action attribute present, JSON missing or unparseable.
        result.parse_stage = 2

    return result


# ── Plugin class ─────────────────────────────────────────────


class EnvelopeFormat:
    """XML-envelope wire format plugin.

    See module docstring for the wire shape and design rationale. The
    class has no constructor parameters; registration is a single
    ``register(EnvelopeFormat())`` call wherever the registry init
    decides to enable it.
    """

    name = "envelope"
    thought_required = True

    # ─── Prompt ────────────────────────────────────────────────

    def format_rules(self) -> str:
        from agent_cli.wire_formats._format_rules_builder import build_format_rules

        return build_format_rules(self)

    def format_rules_anchor(self) -> str:
        return _FORMAT_RULES_ANCHOR

    def format_rules_field_specific(self) -> str:
        return _FORMAT_RULES_FIELD_SPECIFIC

    def render_action_input(self, action_input: str) -> str:
        # Envelope nests action_input as a JSON dict verbatim — identity,
        # same as ReAct. The wire shape (envelope wrap) is taught in the
        # Format Rules section, not by repeating it at every inline
        # example.
        return action_input

    def render_full_example(self, *, thought, action: str, action_input: str) -> str:
        # Envelope shape: <tool_use id="r1" action="...">reasoning\n\nJSON</tool_use>.
        # When ``thought`` is None (skill / agent invocation example)
        # we still emit a visible reasoning slot — the envelope's
        # whole point is that the slot is always there — but use a
        # short hint string rather than letting the slot collapse.
        # The id is fixed at "r1" since the example is one envelope;
        # multi-action ids are a future concern.
        reasoning = thought if thought is not None else "reasoning here"
        return (
            f'<tool_use id="r1" action="{action}">\n'
            f"{reasoning}\n"
            "\n"
            f"{action_input}\n"
            "</tool_use>"
        )

    # ─── Parsing ───────────────────────────────────────────────

    def parse(self, llm_text: str) -> ParsedAction:
        return parse_envelope(llm_text)

    # ─── Recovery ──────────────────────────────────────────────

    def constraint_reminder_call(self) -> str:
        return _CONSTRAINT_REMINDER_CALL

    def constraint_reminder_action_required(self) -> str:
        return _CONSTRAINT_REMINDER_ACTION_REQUIRED

    def failure_framing_parse_fail(self) -> str:
        return _FAILURE_FRAMING_PARSE_FAIL

    def failure_framing_no_action(self) -> str:
        return _FAILURE_FRAMING_NO_ACTION

    def static_retry_hint_no_json(self) -> str:
        return _STATIC_RETRY_HINT_NO_JSON

    def static_retry_hint_no_action(self) -> str:
        return _STATIC_RETRY_HINT_NO_ACTION

    def system_user_prefixes(self) -> tuple[str, ...]:
        return _SYSTEM_USER_PREFIXES

    # ─── Envelope-specific recovery (NO_THOUGHT) ──────────────
    # Same shape as ReActFormat.format_no_thought_retry — duck typed
    # because it is not in the WireFormat Protocol (only plugins with
    # ``thought_required=True`` emit it; the loop gates the call on
    # that flag).

    def format_no_thought_retry(self, *, prior_content: str = "") -> Intervention:
        echo = echo_prior_output(prior_content)
        if not echo:
            return Intervention(
                message=f"{_NO_THOUGHT_FRAMING} {_NO_THOUGHT_CONSTRAINT}",
                primitives=[],
            )

        msg = "\n".join(
            [
                _NO_THOUGHT_FRAMING,
                "",
                echo,
                "Honor that. " + _NO_THOUGHT_CONSTRAINT,
            ]
        )
        return Intervention(
            message=msg,
            primitives=["echo_prior_output"],
        )

    # ─── Provider / lifecycle ──────────────────────────────────

    def prefill(self) -> str:
        # Force the model to start emitting the envelope from the very
        # first token, with the action attribute opening already in
        # place. The model then chooses the tool name and continues.
        # Stronger enforcement than ReAct's empty prefill — envelope is
        # less canonical so the prior alone won't reliably wrap.
        return '<tool_use id="r1" action="'

    def provider_call_kwargs(self) -> dict:
        # Disable Ollama's ``format=json`` mode: it forces the first
        # token to be ``{`` which conflicts with the ``<tool_use``
        # envelope opening. Other providers ignore this kwarg. See
        # ``providers/ollama.py:68``.
        return {"skip_json_format": True}

    # ─── History / context-window policy ──────────────────────

    def normalize_assistant_for_messages(self, raw: str) -> str:
        # Identity. The envelope IS the wire shape this plugin asks for,
        # so leaving the buffer raw reinforces the prior on every turn.
        # If the model drifts into ReAct shape mid-conversation, a
        # future variant could re-render here to repair the prior; v1
        # keeps the lossless identity.
        return raw

    def serialize_assistant_for_history(self, raw_text: str) -> dict:
        # Round-trip through the parser and store ``thought / action /
        # action_input`` as top-level fields. Mirrors ReAct's on-disk
        # shape so ``manager._convert_observation`` and friends don't
        # care which plugin produced the record.
        parsed = parse_envelope(raw_text)
        if parsed.action:
            return {
                "role": "assistant",
                "thought": parsed.thought or "",
                "action": parsed.action,
                "action_input": parsed.action_input
                if parsed.action_input is not None
                else {},
            }
        return {"role": "assistant", "content": raw_text}

    def render_assistant_from_history(self, record: dict) -> dict:
        # Natural-language summary identical in form to ReAct's render
        # so resumed sessions read the same regardless of the format
        # they were recorded under. The underlying structured fields in
        # history.jsonl already capture the envelope-specific shape;
        # the rendered message is for the LLM's next-turn prior, where
        # natural language is the most universally understood form.
        thought = record.get("thought", "")
        action = record.get("action", "")
        action_input = record.get("action_input", {})

        if action == "complete":
            result = ""
            if isinstance(action_input, dict):
                result = action_input.get("result", "")
            elif isinstance(action_input, str):
                result = action_input
            if thought:
                content = f"thought: {thought}\n\n{result}"
            else:
                content = result
            return {"role": "assistant", "content": content.strip()}

        if action:
            args_summary = summarize_action_args(action, action_input)
            parts = []
            if thought:
                parts.append(f"thought: {thought}")
            parts.append(f"action: {action}({args_summary})")
            return {"role": "assistant", "content": "\n".join(parts)}

        content = record.get("content", thought)
        return {"role": "assistant", "content": content}
