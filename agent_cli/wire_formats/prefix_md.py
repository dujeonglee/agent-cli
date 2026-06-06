"""PREFIX-MD wire format — markdown section headings.

Wire shape::

    ## Thought
    <free reasoning, multi-line OK>

    ## Action
    <tool_name on its own line>

    ## Input
    {"<arg>": "<value>", ...}

Three markdown ATX H2 headings delimit three sections — reasoning,
action name, and JSON action_input. No quotes, no brackets, no closing
tags, no attribute syntax. Each header is its own line and matched
strictly (``^## Thought$`` / ``^## Action$`` / ``^## Input$``).

Motivation: small models often struggle to emit XML envelopes (extra
brackets, attribute quoting, matching close tags) and to populate
``thought`` slots embedded inside JSON (a tagged-string field is easy
to leave empty). PREFIX-MD removes both burdens — reasoning is plain
text inside a markdown section, action is a bare tool name on its
own line, and the only JSON island is the input dict.

Parser policy (parse_prefix_md):
  - LAST ``## Action`` wins. Earlier ``## Action`` occurrences inside
    reasoning sub-headings are absorbed into the preceding body.
  - Thought is OPTIONAL (``thought_required = False``): with a
    ``## Thought`` heading, FIRST ``## Thought`` to LAST ``## Action`` is
    the thought; WITHOUT the heading, the free-text prose before
    ``## Action`` is taken as the thought (the model often reasons in
    plain prose, e.g. "I see the problem... Let me fix it."). A missing
    thought is allowed — no NO_THOUGHT retry.
  - LAST ``## Input`` after the last ``## Action`` provides the JSON.
  - Action body must match ``^[\\w.-]+$`` (single token, first non-empty
    line). Drift to natural-language text in the Action body produces
    parse_stage=3 so the recovery layer can re-prompt.

Inherits the WireFormat ABC's lifecycle defaults (serialize / render
round-trip, identity hooks). Overrides only ``provider_call_kwargs``
because an OpenAI-compatible JSON mode (``response_format`` json_object)
forces the first token to ``{`` which conflicts with the ``## `` markdown
opening.

Self-contained boundary: this module owns the format-rules text, the
parser, the recovery wording, and the provider hint. No external file
references PrefixMdFormat by name. Removing this file removes PREFIX-MD
support entirely.
"""

from __future__ import annotations

import json
import re

from agent_cli.recovery.intervention import Intervention
from agent_cli.recovery.primitives import echo_prior_output
from agent_cli.wire_formats.base import ParsedAction, WireFormat


# ── Prompt section ───────────────────────────────────────────
# Format Rules section composes through the shared builder
# ``_format_rules_builder.build_format_rules``. The three render hooks
# below + the anchor + field-specific rules supply the wire-shape-
# dependent parts; the surrounding completion intro and rules 3-6 are
# the byte-equivalent shared text every plugin sees.

_FORMAT_RULES_ANCHOR = (
    "Output your response as three markdown sections — `## Thought`, `## Action`,\n"
    "`## Input` — each header on its own line, exact spelling:"
)

# Split into per-field clauses so the strength can be gated on
# ``thought_required`` / ``action_required`` via ``WireFormat._gated_rule``
# (see format_rules_field_specific). The composed string is byte-identical
# to the previous single constant; the gating hook is wired but inert until
# a ``soft`` variant is supplied.
_THOUGHT_RULE = (
    "The body of `## Thought` MUST state purpose (what you want to achieve)\n"
    "   and reason (why this specific action). Do not leave it empty."
)
_ACTION_RULE = (
    "The body of `## Action` MUST be a single tool name on its own line;\n"
    "   `## Input` MUST contain a JSON dict matching the tool's input schema."
)


# ── Recovery fragments ────────────────────────────────────────
# Match the structure of ReActFormat's recovery wording so the loop's
# composed Intervention messages keep their shape (framing + echo +
# constraint reminder). The strings reflect the PREFIX-MD format so
# the model sees concrete what-to-fix guidance.

_CONSTRAINT_REMINDER_CALL = (
    "Output three markdown sections: `## Thought` (reasoning text), "
    "`## Action` (single tool name on one line), `## Input` (JSON dict). "
    "Each header on its own line, exact spelling."
)

_CONSTRAINT_REMINDER_ACTION_REQUIRED = (
    "The `## Action` section MUST contain a single tool name on its "
    "own line. Either invoke a tool, or finish with `## Action\\ncomplete"
    '\\n\\n## Input\\n{"result": "..."}`'
)

_FAILURE_FRAMING_PARSE_FAIL = (
    "Your response did not match the `## Thought` / `## Action` / `## Input` "
    "section format."
)

_FAILURE_FRAMING_NO_ACTION = (
    "Your response had a `## Action` header but its body was not a "
    "valid tool name on its own line."
)

_STATIC_RETRY_HINT_NO_JSON = (
    f"{_FAILURE_FRAMING_PARSE_FAIL} {_CONSTRAINT_REMINDER_CALL}"
)

_STATIC_RETRY_HINT_NO_ACTION = (
    f"{_FAILURE_FRAMING_NO_ACTION} {_CONSTRAINT_REMINDER_ACTION_REQUIRED}"
)

_SYSTEM_USER_PREFIXES = (
    _FAILURE_FRAMING_PARSE_FAIL,
    _FAILURE_FRAMING_NO_ACTION,
    "Your `## Thought` section was empty.",
)


# NO_THOUGHT recovery (PREFIX-MD-specific framing).
_NO_THOUGHT_FRAMING = "Your `## Thought` section was empty."

_NO_THOUGHT_CONSTRAINT = (
    "The text under `## Thought` MUST state purpose (what you want to "
    "achieve) and reason (why this specific action). Do not leave it "
    "empty."
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

# Strict ATX-H2 sentinel — exact spelling, line-anchored.
# Multi-line mode + ``^…$`` ensures the sentinel doesn't match inside
# a sentence (e.g. "the ## Action of saving" mid-line is ignored).
_THOUGHT_HEADER = re.compile(r"^## Thought$", re.MULTILINE)
_ACTION_HEADER = re.compile(r"^## Action$", re.MULTILINE)
_INPUT_HEADER = re.compile(r"^## Input$", re.MULTILINE)

# Valid action body: a single token of word chars, dots, hyphens.
# Mirrors tool-name conventions (read_file, run_skill, etc.). Lines
# that contain prose ("read the file") will not match, so the parser
# can distinguish a real action sentinel from a sub-header drift.
_ACTION_NAME = re.compile(r"^[\w.-]+$")


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
    that respects string literals so ``{"a":"}"}`` doesn't trip on
    the inner brace, and tracks the *last* fully balanced block — the
    JSON action_input is by convention the trailing dict in the Input
    section, after any reasoning text that itself might mention a
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


# Strip section sentinels so leftover text can serve as a thought when an
# action header is absent (the recovered-input path below).
_ANY_HEADER = re.compile(r"^## (?:Thought|Action|Input)$", re.MULTILINE)


def _extract_json_dict(text: str) -> dict | None:
    """Last balanced ``{...}`` in ``text`` → dict, or ``None`` if absent,
    unparseable, or not a JSON object. Shared by the happy path and the
    dropped-action recovery paths so action_input extraction lives in one
    place (the preservation invariant in ``WireFormat.parse``)."""
    block = _find_last_json_block(text)
    if block is None:
        return None
    try:
        parsed = json.loads(text[block[0] : block[1]])
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_prefix_md(text: str) -> ParsedAction:
    """Parse a PREFIX-MD emission into a :class:`ParsedAction`.

    Sentinels are matched strictly: ``^## Thought$`` / ``^## Action$`` /
    ``^## Input$`` — exact word, on its own line. Last-wins policy on
    ``## Action`` (and ``## Input``) so that earlier occurrences inside
    reasoning sub-headings are absorbed into preceding bodies.

    parse_stage policy:
      0 — no action recoverable: no valid ``## Action`` body AND no
          trailing ``action_input`` JSON to fall back on.
      1 — ``## Action`` body is a valid tool name + ``## Input`` JSON
          parses cleanly (full happy path).
      2 — ``## Action`` body is a valid tool name, ``## Input`` JSON
          missing or unparseable (loop sees ``action`` present and
          ``action_input=None`` → schema-mismatch / no-input recovery).
      3 — action slot empty/invalid OR ``## Action`` header absent, BUT a
          trailing ``action_input`` JSON was recovered. ``action=None``,
          ``action_input`` preserved → the loop infers the tool
          (``action_required=False``) or echoes it in the NO_ACTION
          intervention (``action_required=True``). Preserves the
          dropped-action recovery the wire-key prefix was built for.

    Never raises on malformed input — always returns a ``ParsedAction``.
    """
    text = _sanitize_surrogates(text)
    text, thinking = _strip_thinking_blocks(text)
    result = ParsedAction(raw=text, thinking=thinking)

    action_matches = list(_ACTION_HEADER.finditer(text))
    if not action_matches:
        # No ``## Action`` header at all. Still recover a trailing
        # action_input (## Input section or bare trailing dict) so the loop
        # can infer the tool / echo it — parity with react, which has no
        # header concept. Leftover non-JSON text becomes the thought.
        action_input = _extract_json_dict(text)
        if action_input is not None:
            block = _find_last_json_block(text)
            head = _ANY_HEADER.sub("", text[: block[0]]).strip() if block else ""
            result.thought = head or None
            result.action_input = action_input
            result.parse_stage = 3
        return result  # parse_stage stays 0 if nothing recoverable

    last_action = action_matches[-1]

    # Thought section: from FIRST ``## Thought`` to LAST ``## Action``.
    # Earlier ``## Action`` occurrences inside this range are absorbed
    # as reasoning body.
    thought_matches = list(_THOUGHT_HEADER.finditer(text))
    if thought_matches:
        first_thought = thought_matches[0]
        thought_start = first_thought.end()
        thought_end = last_action.start()
        thought_text = (
            text[thought_start:thought_end].strip()
            if thought_end > thought_start
            else ""
        )
    else:
        # No ``## Thought`` heading — the model often reasons in plain
        # prose before ``## Action`` (e.g. "I see the problem... Let me
        # fix it."). Treat that free text as the thought so it counts as
        # reasoning rather than being dropped. thought_required=False also
        # allows the empty case (action straight away).
        thought_text = text[: last_action.start()].strip()

    # Action body + Input section: ``## Input`` headers occurring AFTER
    # the last ``## Action``. The last such Input wins; earlier ones
    # are absorbed into the action body (which we then validate as a
    # single token — sub-header drift will fail validation and the
    # parse_stage=3 path picks it up).
    inputs_after_action = [
        m for m in _INPUT_HEADER.finditer(text) if m.start() > last_action.end()
    ]
    action_body_start = last_action.end()
    if inputs_after_action:
        last_input = inputs_after_action[-1]
        action_body_end = last_input.start()
        input_text = text[last_input.end() :].strip()
    else:
        action_body_end = len(text)
        input_text = ""

    action_body = text[action_body_start:action_body_end].strip()

    # First non-empty line of the action body is the candidate tool name.
    first_line = ""
    for line in action_body.splitlines():
        stripped = line.strip()
        if stripped:
            first_line = stripped
            break

    if not first_line or not _ACTION_NAME.match(first_line):
        # ``## Action`` header present but body empty/invalid. Preserve the
        # Input JSON so the loop can infer the tool (action_required=False)
        # or echo it in the NO_ACTION intervention (action_required=True).
        # Thought is preserved either way.
        result.thought = thought_text or None
        result.action_input = _extract_json_dict(input_text)
        result.parse_stage = 3
        return result

    action = first_line
    action_input = _extract_json_dict(input_text)

    result.thought = thought_text or None
    result.action = action
    result.action_input = action_input
    # Action valid: stage 1 if Input JSON parsed, else 2 (missing/broken
    # input → schema-mismatch / no-input recovery downstream).
    result.parse_stage = 1 if action_input is not None else 2

    return result


# ── Plugin class ─────────────────────────────────────────────


class PrefixMdFormat(WireFormat):
    """PREFIX-MD wire format plugin.

    See module docstring for the wire shape and design rationale.
    Inherits lifecycle defaults from :class:`WireFormat` ABC; the only
    override is :meth:`provider_call_kwargs` to disable the provider's
    JSON mode (the markdown opening ``## `` conflicts with the JSON
    mode's forced ``{`` first token).
    """

    name = "prefix_md"
    # thought is optional: a missing ``## Thought`` is allowed (no
    # NO_THOUGHT retry), and prose reasoning before ``## Action`` is
    # picked up as the thought by the parser.
    thought_required = False
    # action is optional: a dropped/empty ``## Action`` is recovered by the
    # loop via ``infer_action`` on the preserved action_input (wire-key
    # prefix → tool). The parser keeps action_input populated in that case
    # (parse_stage 3); only an unrecoverable emission becomes NO_ACTION.
    action_required = False

    # ─── Prompt ────────────────────────────────────────────────

    def format_rules_anchor(self) -> str:
        return _FORMAT_RULES_ANCHOR

    def format_rules_field_specific(self) -> str:
        # Rule 1 gated on thought_required, Rule 2 on action_required. Both
        # currently resolve to the strong wording (no soft variant), so the
        # section is unchanged; the flags select the clause when softened.
        return (
            f"1. {self._gated_rule(self.thought_required, _THOUGHT_RULE)}\n"
            f"2. {self._gated_rule(self.action_required, _ACTION_RULE)}"
        )

    def render_full_example(self, *, thought, action: str, action_input: str) -> str:
        # ``thought=None`` (skill / agent invocation example) substitutes
        # a short placeholder so the reasoning slot stays visible —
        # teaches the model the slot is required even in invocation-only
        # examples. Same handling as ReAct's placeholder.
        reasoning = thought if thought is not None else "reasoning here"
        return (
            "## Thought\n"
            f"{reasoning}\n"
            "\n"
            "## Action\n"
            f"{action}\n"
            "\n"
            "## Input\n"
            f"{action_input}"
        )

    # ─── Parsing ───────────────────────────────────────────────

    def parse(self, llm_text: str) -> ParsedAction:
        return parse_prefix_md(llm_text)

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

    # ─── PREFIX-MD-specific recovery (NO_THOUGHT) ──────────────
    # Duck-typed — not in the WireFormat ABC. Only plugins with
    # ``thought_required=True`` emit this intervention, and the loop
    # gates the call on that flag.

    def format_no_thought_retry(self, *, prior_content: str = "") -> Intervention:
        """Build the Intervention when the ``## Thought`` body was empty.

        Same failure-grounding shape as ReActFormat's variant: echo the
        prior output so the model sees its own omission, then restate
        the constraint.
        """
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

    # ─── Provider / lifecycle (override) ───────────────────────

    def provider_call_kwargs(self, capabilities) -> dict:
        # An OpenAI-compatible JSON mode forces the first generated token
        # to be ``{``. PREFIX-MD opens with ``## `` so the modes conflict —
        # never request JSON mode, regardless of the model's structured-
        # output capability (forcing it makes omlx/mlx degenerate). The
        # ``capabilities`` arg is accepted for the uniform signature but
        # this plugin's answer is unconditional.
        return {"json_mode": False}
