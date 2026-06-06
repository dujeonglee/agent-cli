"""ReAct wire format — the default plugin.

Self-contained module owning every string and behavior that defines the
ReAct wire format: format rules text, recovery wording, parser
adaptation, and lifecycle hooks. Removing this file would remove ReAct
support entirely (with no edits anywhere else needed) — the boundary
the plugin system promises.

Inherits from :class:`agent_cli.wire_formats.base.WireFormat` ABC. The
history pipeline defaults (``serialize_assistant_for_history`` /
``render_assistant_from_history``), identity hooks
(``normalize_assistant_for_messages``, ``render_action_input``), and
provider/lifecycle hooks (``prefill``, ``provider_call_kwargs``,
``format_rules``) all come from the base — ReAct only specifies what
makes its wire shape unique.
"""

from __future__ import annotations

import json
import re

from agent_cli.recovery.intervention import Intervention
from agent_cli.recovery.primitives import echo_prior_output
from agent_cli.wire_formats.base import ParsedAction, WireFormat


# ── ReAct parser ─────────────────────────────────────────────
# 3-stage fallback parser plus its stage-2 JSON repair helper. Lives
# entirely in this module (no ``parsing/`` package, no shared
# ``json_repair`` module) so the whole ReAct format — parser,
# repair, format rules, recovery wording, history rendering — is
# folder-deletable as a single boundary. If a future plugin needs the
# same JSON repair algorithm we re-evaluate sharing at that point;
# pre-emptive extraction would impose ReAct's repair policy on
# wire formats that may want a different recovery strategy.

# Known thinking/reasoning block tag names (case-insensitive)
_THINKING_TAGS = ["think", "thinking", "reasoning", "reflection"]

# Build regex that matches any of the known thinking tags
_THINKING_PATTERN = re.compile(
    r"<(" + "|".join(_THINKING_TAGS) + r")>(.*?)</\1>",
    re.S | re.I,
)


def _sanitize_surrogates(text: str) -> str:
    """Remove unpaired Unicode surrogates that break JSON parsing."""
    return re.sub(r"[\ud800-\udfff]", "", text)


def _strip_thinking_blocks(text: str) -> tuple[str, str | None]:
    """Strip thinking/reasoning blocks from LLM output.

    Handles: <think>...</think>, <thinking>...</thinking>,
             <reasoning>...</reasoning>, <reflection>...</reflection>

    Returns: (text_without_blocks, extracted_thinking_content or None)
    """
    thinking_parts: list[str] = []

    def _collect(match):
        content = match.group(2).strip()
        if content:
            thinking_parts.append(content)
        return ""

    cleaned = _THINKING_PATTERN.sub(_collect, text).strip()

    if thinking_parts:
        return cleaned, "\n\n".join(thinking_parts)
    return text, None


def parse_react(text: str) -> ParsedAction:
    """Parse an LLM response into a :class:`ParsedAction` using 3-stage fallback.

    Stage 0: strip ``<think>...</think>`` and similar thinking blocks.
    Stage 1: ``json.loads`` after markdown-fence strip — fast path.
    Stage 2: :func:`repair_json` — close unterminated strings, brackets,
             fix quotes/unquoted keys/trailing commas, extract embedded block.
    Stage 3: regex field extraction — last-resort recovery.

    Returns a :class:`ParsedAction` with ``parse_stage`` set to the
    successful stage (``0`` if all three failed).
    """
    text = _sanitize_surrogates(text)
    text, thinking = _strip_thinking_blocks(text)
    result = ParsedAction(raw=text, thinking=thinking)

    # Stage 1: Direct JSON parse
    data = _try_json_parse(text)
    if data is not None:
        _populate_from_dict(result, data)
        result.parse_stage = 1
        return result

    # Stage 2: JSON repair
    data, was_truncated = repair_json(text)
    if data is not None:
        _populate_from_dict(result, data)
        result.parse_stage = 2
        result.truncated = was_truncated
        return result

    # Stage 3: Regex extraction
    extracted = _regex_extract(text)
    if extracted:
        _populate_from_dict(result, extracted)
        result.parse_stage = 3
        return result

    return result


def _try_json_parse(text: str) -> dict | None:
    """Stage 1: Try direct JSON parse."""
    stripped = strip_markdown_fences(text)

    try:
        data = json.loads(stripped)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, ValueError):
        pass

    # Try extracting first { ... } block using balanced brace extraction
    extracted = _extract_json_block(stripped)
    if extracted != stripped:
        try:
            data = json.loads(extracted)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            pass

    return None


def _regex_extract(text: str) -> dict | None:
    """Stage 3: Extract fields via regex patterns."""
    result: dict = {}

    m = re.search(r'"thought"\s*:\s*"((?:[^"\\]|\\.)*)"', text, re.S)
    if m:
        result["thought"] = m.group(1).replace('\\"', '"').replace("\\n", "\n")

    m = re.search(r'"action"\s*:\s*"((?:[^"\\]|\\.)*)"', text, re.S)
    if m:
        result["action"] = m.group(1).replace('\\"', '"')

    m = re.search(r'"action_input"\s*:\s*(\{[^}]*\})', text, re.S)
    if m:
        try:
            result["action_input"] = json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            result["action_input"] = m.group(1)

    return result if result else None


# Keys that are part of the ReAct protocol or are reserved for internal
# use. They must NEVER be hoisted into action_input even when siblings
# of `action`. Seeding this blacklist defensively:
#   - thought / action / action_input : the ReAct protocol trio
#   - observation: system prompt forbids it in model output; if emitted
#     it's a drift, not a tool arg
#   - reasoning / reflection: thinking-tag variants (already stripped by
#     _strip_thinking_blocks when they appear as tags, but models
#     occasionally emit them as top-level keys too)
#   - role / _meta: added by our own storage / session layer; a confused
#     model could echo them back
_REACT_RESERVED: frozenset[str] = frozenset(
    {
        "thought",
        "action",
        "action_input",
        "observation",
        "reasoning",
        "reflection",
        "role",
        "_meta",
    }
)


# Virtual-tool payload hoisting map.
#
# Some models (observed with qwen3 family) emit responses like:
#   {"thought": "...", "action": "complete", "result": "final answer"}
# where the payload key is at the top level instead of nested inside
# action_input. This is valid JSON and the action name is correct, so
# nothing downstream catches it — the complete handler just sees
# action_input=None and reports "Completed without result".
#
# Entry shape: action_name -> (target_key_in_action_input, top_level_fallback_keys)
# The first matching top-level key's value is placed under target_key.
# For virtual tools we deliberately do NOT fall through to the real-tool
# bundling rule: if none of the known alias keys are present, leave
# action_input as None so the downstream handler can render its "no
# payload" path rather than dispatching with an arbitrary sibling.
_VIRTUAL_TOOL_PAYLOAD_HOIST: dict[str, tuple[str, tuple[str, ...]]] = {
    "complete": ("result", ("result", "answer", "response", "final", "output")),
    "ready_for_review": ("summary", ("summary",)),
    # For ask, _extract_questions in loop.py already treats "questions" and
    # "question" interchangeably, so placing the hoisted value under
    # "questions" is safe regardless of which top-level key the model used.
    "ask": ("questions", ("questions", "question")),
}


def _normalize_action_input(result: ParsedAction, data: dict) -> None:
    """Normalize sibling-emitted tool arguments back into action_input.

    Two layers:

    1. **Virtual tools** (complete / ready_for_review / ask). The payload
       key can drift under several aliases (complete's `result` ↔
       `answer` ↔ `response`); map the first matching alias back to the
       canonical key. If no alias matches, leave action_input=None so
       the downstream handler shows its "no payload" message.

    2. **Real tools and unknown actions**. Bundle every non-reserved
       top-level key into action_input. This catches the pcie_scsc-style
       drift where a model emits:
           {"thought":"...","action":"shell","command":"ls"}
       with `command` as a sibling of `action` rather than nested inside
       action_input. Reserved keys (_REACT_RESERVED) are filtered out so
       protocol fields or meta keys can't poison tool input.

       Layer 2 runs even when ``action`` is absent: a dropped action name
       with bundled siblings (``{"shell_command":"ls"}``) must still
       surface action_input so the loop's ``infer_action`` can recover the
       tool (wire-key prefix → tool) under ``action_required=False`` — the
       parser-side half of the preservation invariant, symmetric with
       prefix_md recovering a trailing Input dict without an action header.

    Precedence rule: if action_input is already present and truthy, use
    it verbatim and ignore any siblings. Empty dicts and None both
    trigger the layer logic.
    """
    if result.action_input:
        return

    # Layer 1: virtual tool alias mapping — action-specific, so only when
    # an action name is present.
    if result.action:
        spec = _VIRTUAL_TOOL_PAYLOAD_HOIST.get(result.action)
        if spec is not None:
            target_key, candidates = spec
            for key in candidates:
                if key in data:
                    result.action_input = {target_key: data[key]}
                    return
            # Known virtual tool with no alias match — leave action_input
            # None rather than bundling stray siblings as payload.
            return

    # Layer 2: real tool / unknown / dropped action sibling bundling.
    extras = {k: v for k, v in data.items() if k not in _REACT_RESERVED}
    if extras:
        result.action_input = extras


def _populate_from_dict(result: ParsedAction, data: dict) -> None:
    """Fill a ParsedAction from a parsed dict."""
    result.thought = data.get("thought")
    result.action = data.get("action")
    result.action_input = data.get("action_input")
    _normalize_action_input(result, data)


# ── Prompt section ───────────────────────────────────────────
# The Format Rules section is now composed by the shared builder
# ``_format_rules_builder.build_format_rules`` so that two plugins
# rendering "the same logical content" produce byte-equivalent shared
# text and only differ in the wire-shape-specific fragments returned
# by ``format_rules_anchor`` / ``render_full_example`` /
# ``format_rules_field_specific`` below.

_FORMAT_RULES_ANCHOR = (
    "You MUST output a single JSON object only — no markdown fences, no surrounding\n"
    "text, no `observation` field (it is injected by the system):"
)

# Split into per-field clauses so Rule 1 (thought) can be gated on
# ``thought_required`` via ``WireFormat._gated_rule``. Rule 2 is the
# action_input contract — not an action-presence obligation — so
# ``action_required`` has no clause to soften here (react expresses the
# action requirement through the JSON-envelope anchor, not a numbered rule).
# The composed string is byte-identical to the previous single constant.
_THOUGHT_RULE = (
    "`thought` MUST state purpose (what you want to achieve) and reason "
    "(why this action). Do not leave it empty."
)
_ACTION_INPUT_RULE = (
    "`action_input` MUST match the tool's input schema. For `complete`, "
    "`result` is a plain text string — write the answer directly. Do NOT "
    'wrap it in another JSON envelope like `{"result": "{\\"result\\": ...}"}`.'
)


# ── Recovery fragments ────────────────────────────────────────
# Mirror the bodies in ``recovery/primitives.constrain_format_json`` and
# ``recovery/primitives.constrain_action_required``. Step 3 removes
# those once ``recovery/builders`` start calling the wire format method.
_CONSTRAINT_REMINDER_CALL = (
    "Output ONLY a JSON object: "
    '{"thought": "...", "action": "tool_name", "action_input": {...}}. '
    "No markdown fences, no extra text."
)

_CONSTRAINT_REMINDER_ACTION_REQUIRED = (
    "You MUST include an action. Either use a tool: "
    '{"thought": "...", "action": "tool_name", "action_input": {...}} '
    "or complete the task: "
    '{"thought": "...", "action": "complete", "action_input": {"result": "..."}}'
)

# Opening lines used by the recovery builders to frame the failure
# (parse-fail vs. action-missing). Match the legacy hardcoded strings
# in ``recovery/builders.format_no_json_retry`` and ``…no_action_retry``.
_FAILURE_FRAMING_PARSE_FAIL = "Your response was not valid JSON."
_FAILURE_FRAMING_NO_ACTION = "Your JSON was parsed but has no action."

# Static fallbacks injected when there is nothing meaningful to echo
# back (empty/whitespace prior emission). Match ``constants.RETRY_HINT_*``.
_STATIC_RETRY_HINT_NO_JSON = (
    "Your response was not valid JSON. "
    "Output ONLY a JSON object: "
    '{"thought": "...", "action": "tool_name", "action_input": {...}}. '
    "No markdown fences, no extra text."
)

_STATIC_RETRY_HINT_NO_ACTION = (
    "Your JSON was parsed but has no action. "
    "You MUST include an action. Either use a tool: "
    '{"thought": "...", "action": "tool_name", "action_input": {...}} '
    "or complete the task: "
    '{"thought": "...", "action": "complete", "action_input": {"result": "..."}}'
)


# Recovery framings emitted by THIS plugin. Format-agnostic prefixes
# (``"You have called"``, ``"You were asked to:"``, plus the interrupt
# notice) live in ``wire_formats._FORMAT_AGNOSTIC_USER_PREFIXES`` and
# are unioned with this list at consume time via
# ``wire_formats.all_system_user_prefixes()``. Includes the NO_THOUGHT
# framing because thought is a required field in this format.
_SYSTEM_USER_PREFIXES = (
    "Your response was not valid JSON.",
    "Your JSON was parsed but has no action.",
    "Your JSON was missing the 'thought' field.",
)


# ── NO_THOUGHT recovery (ReAct-only) ──────────────────────────
# ``thought`` is a required field in the ReAct schema, so emitting
# an action without it triggers a retry. The constraint and framing
# live here because no other plugin shares them — envelope plugins
# carry thought as preceding free text, where its absence is valid.
_NO_THOUGHT_FRAMING = "Your JSON was missing the 'thought' field."

_NO_THOUGHT_CONSTRAINT = (
    "Your JSON must include a 'thought' field stating purpose "
    "(what you want to achieve) and reason (why this specific action). "
    "Do not omit it."
)


class ReActFormat(WireFormat):
    """Reference plugin — preserves pre-plugin behavior.

    Inner shape: ``{"thought": ..., "action": ..., "action_input": ...}``.
    Parser: 3-stage fallback (``parse_react``). Recovery: the
    long-standing ``constrain_format_json`` / ``constrain_action_required``
    text. Provider quirks: none (JSON mode stays active when
    capabilities support it).

    Inherits the history pipeline defaults from ``WireFormat``:
    ``serialize_assistant_for_history`` runs ``parse_react`` and stores
    structured fields; ``render_assistant_from_history`` re-emits those
    fields through ``render_full_example`` (JSON wire shape). Identity
    defaults for ``normalize_assistant_for_messages``, ``prefill``,
    ``provider_call_kwargs``, and ``render_action_input`` also apply —
    ReAct doesn't need overrides for any of them.

    The class has no constructor parameters: registration is a single
    ``register(ReActFormat())`` call at module import time (see
    ``agent_cli/wire_formats/__init__.py``).
    """

    name = "react"
    # Both fields optional, unified with prefix_md: a missing thought or a
    # dropped action no longer forces a retry by themselves. A dropped
    # action is recovered via infer_action on the preserved action_input
    # (the parser keeps non-reserved siblings bundled even without an
    # action — see _normalize_action_input). NO_THOUGHT / NO_ACTION
    # recovery still fires for any plugin that sets these True (tested via
    # a synthetic plugin so the True paths stay covered).
    thought_required = False
    action_required = False

    # ─── Prompt ────────────────────────────────────────────────

    def format_rules_anchor(self) -> str:
        return _FORMAT_RULES_ANCHOR

    def format_rules_field_specific(self) -> str:
        # Rule 1 (thought) gated on thought_required; currently resolves to
        # the strong wording (no soft variant) so the section is unchanged.
        return (
            f"1. {self._gated_rule(self.thought_required, _THOUGHT_RULE)}\n"
            f"2. {_ACTION_INPUT_RULE}"
        )

    def render_full_example(self, *, thought, action: str, action_input: str) -> str:
        # ReAct shape: a single JSON object. ``thought=None`` (skill /
        # agent invocation example) substitutes a short placeholder so
        # the reasoning slot stays visible — teaches the model the slot
        # is required even in invocation-only examples.
        reasoning = thought if thought is not None else "reasoning here"
        return (
            "{"
            f'"thought": "{reasoning}", '
            f'"action": "{action}", '
            f'"action_input": {action_input}'
            "}"
        )

    # ─── Parsing ───────────────────────────────────────────────

    def parse(self, llm_text: str) -> ParsedAction:
        return parse_react(llm_text)

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

    # ─── ReAct-only recovery (NO_THOUGHT) ──────────────────────
    # Not part of the WireFormat base interface — only plugins with
    # ``thought_required=True`` emit this intervention, and the loop
    # gates the call on that flag. Plugins where thought is preceding
    # free text (not a structured schema field) set
    # ``thought_required=False`` and never reach this method. Adding
    # it to the base would force every plugin to implement a no-op,
    # so duck typing wins here.

    def format_no_thought_retry(self, *, prior_content: str = "") -> Intervention:
        """Build the Intervention when an action was emitted without
        the required ``thought`` field.

        Same failure-grounding shape as the format_no_*_retry builders
        in ``recovery.wf_recovery``: echo the prior output so the model
        sees its own omission, then restate the constraint. Inlined
        rather than promoted to a primitive — adding a primitive for a
        single caller violates the "primitive reused by ≥2 failures"
        anti-patchwork invariant in DESIGN.md §4.
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


# ── Stage 2 helper: repair malformed JSON ────────────────────
# Used by ``parse_react`` stage 2. Kept module-public (no underscore
# prefix) because ``test_json_repair`` exercises it directly — that
# test is part of the ReAct plugin's coverage. Other plugins import
# this only if they explicitly opt in to ReAct's repair policy; new
# plugins are encouraged to define their own recovery rather than
# share by default.
#
# Handles common issues seen in small-model output:
#   1. Unclosed strings
#   2. Missing closing brackets
#   3. Trailing commas
#   4. Single quotes instead of double quotes
#   5. Unquoted keys
#   6. JSON embedded in surrounding text (markdown fences, prose)


def repair_json(text: str) -> tuple[dict | None, bool]:
    """Attempt to repair malformed JSON text into a valid dict.

    Returns (parsed_dict, was_truncated).
    was_truncated is True if brackets/strings had to be closed.
    """
    cleaned = _extract_json_block(text)
    cleaned = _fix_quotes(cleaned)
    cleaned = _fix_unquoted_keys(cleaned)
    cleaned = _fix_trailing_commas(cleaned)
    cleaned, str_closed = _fix_unclosed_strings(cleaned)
    cleaned, brackets_closed = _fix_missing_brackets(cleaned)
    was_truncated = str_closed or brackets_closed

    try:
        result = json.loads(cleaned)
        if isinstance(result, dict):
            return result, was_truncated
    except (json.JSONDecodeError, ValueError):
        pass

    return None, False


def strip_markdown_fences(text: str) -> str:
    """Remove `````json ... ````` wrapping."""
    stripped = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.I)
    stripped = re.sub(r"\s*```\s*$", "", stripped)
    return stripped


def _extract_json_block(text: str) -> str:
    """Find the outermost { ... } block in the text."""
    text = strip_markdown_fences(text)

    start = text.find("{")
    if start == -1:
        return text

    depth = 0
    in_string = False
    escape_next = False
    last_close = -1

    for i in range(start, len(text)):
        ch = text[i]
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            if in_string:
                escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            last_close = i
            if depth == 0:
                return text[start : i + 1]

    if last_close > start:
        return text[start : last_close + 1]
    return text[start:]


def _fix_quotes(text: str) -> str:
    """Replace single-quoted strings with double-quoted strings."""
    result = []
    in_double = False
    in_single = False
    escape_next = False

    for ch in text:
        if escape_next:
            result.append(ch)
            escape_next = False
            continue
        if ch == "\\":
            result.append(ch)
            escape_next = True
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            result.append(ch)
        elif ch == "'" and not in_double:
            in_single = not in_single
            result.append('"')
        else:
            result.append(ch)

    return "".join(result)


def _fix_unquoted_keys(text: str) -> str:
    """Add double quotes around unquoted JSON keys."""
    return re.sub(
        r"([{,]\s*)([a-zA-Z_]\w*)(\s*:)",
        r'\1"\2"\3',
        text,
    )


def _fix_trailing_commas(text: str) -> str:
    """Remove trailing commas before } or ]."""
    return re.sub(r",\s*([}\]])", r"\1", text)


def _fix_unclosed_strings(text: str) -> tuple[str, bool]:
    """Close unclosed string literals at end of text.

    Returns (fixed_text, was_closed).
    """
    in_string = False
    escape_next = False

    for ch in text:
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string

    if in_string:
        return text + '"', True

    return text, False


def _fix_missing_brackets(text: str) -> tuple[str, bool]:
    """Add missing closing brackets/braces.

    Returns (fixed_text, was_closed).
    """
    stack: list[str] = []
    in_string = False
    escape_next = False

    for ch in text:
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            if in_string:
                escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in ("}", "]"):
            if stack and stack[-1] == ch:
                stack.pop()

    if stack:
        return text + "".join(reversed(stack)), True

    return text, False
