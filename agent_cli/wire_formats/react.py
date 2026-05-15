"""ReAct wire format — the default plugin.

Self-contained module owning every string and behavior that defines the
ReAct wire format: format rules text, recovery wording, parser
adaptation, and lifecycle hooks. Removing this file would remove ReAct
support entirely (with no edits anywhere else needed) — the boundary
the plugin system promises.

During the multi-step refactor (Steps 2 → 5) the same strings still
live in ``prompts/system_prompt.FORMAT_RULES``, ``constants.py``, and
``recovery/primitives.py`` because callers haven't been migrated yet.
That duplication is intentional and short-lived: each later step
removes one of those legacy locations as its caller switches to
``wire_format.<method>()``. The final state has every string in this
module only.
"""

from __future__ import annotations

import json
import re

from agent_cli.recovery.intervention import Intervention
from agent_cli.recovery.primitives import echo_prior_output
from agent_cli.wire_formats.base import ParsedAction


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

    Precedence rule: if action_input is already present and truthy, use
    it verbatim and ignore any siblings. Empty dicts and None both
    trigger the layer logic.
    """
    if not result.action or result.action_input:
        return

    # Layer 1: virtual tool alias mapping.
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

    # Layer 2: real tool / unknown action sibling bundling.
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

_FORMAT_RULES_FIELD_SPECIFIC = (
    "1. `thought` MUST state purpose (what you want to achieve) and reason (why this action). Do not leave it empty.\n"
    "2. `action_input` MUST match the tool's input schema."
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


class ReActFormat:
    """Reference plugin — preserves pre-plugin behavior.

    Inner shape: ``{"thought": ..., "action": ..., "action_input": ...}``.
    Parser: 3-stage fallback (``parse_react``). Recovery: the
    long-standing ``constrain_format_json`` / ``constrain_action_required``
    text. Provider quirks: none (JSON mode stays active when
    capabilities support it).

    The class deliberately has no constructor parameters: registration
    is a single ``register(ReActFormat())`` call at module import time
    (see ``agent_cli/wire_formats/__init__.py``).
    """

    name = "react"
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
        # ReAct nests action_input as a JSON dict verbatim — identity.
        # The inline-guide builder feeds in already-JSON strings; no
        # transformation needed. A future plugin where action_input is
        # not a JSON dict overrides this hook.
        return action_input

    def render_full_example(self, *, thought, action: str, action_input: str) -> str:
        # ReAct shape: a single JSON object. ``thought=None`` (skill /
        # agent invocation example) substitutes a short placeholder so
        # the field stays visible — matches envelope's "reasoning here"
        # handling so both plugins teach the same contract: the
        # reasoning slot is always present.
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
    # Not part of the WireFormat Protocol — only plugins with
    # ``thought_required=True`` emit this intervention, and the loop
    # gates the call on that flag. Envelope plugins set
    # ``thought_required=False`` and never reach this method. Adding it
    # to the Protocol would force every plugin to implement a no-op,
    # so duck typing wins here.

    def format_no_thought_retry(self, *, prior_content: str = "") -> Intervention:
        """Build the Intervention when an action was emitted without
        the required ``thought`` field.

        Same failure-grounding shape as the format_no_*_retry builders
        in ``recovery/builders``: echo the prior output so the model
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

    # ─── Provider / lifecycle ──────────────────────────────────

    def prefill(self) -> str:
        # No prefill — ReAct's prior produces ReAct shape on its own.
        # Envelope plugins override this to force their wire shape from
        # the first generated token.
        return ""

    def provider_call_kwargs(self) -> dict:
        # ReAct is fully JSON-shaped; keep capability-driven
        # ``format=json`` mode active by passing no overrides.
        return {}

    # ─── History / context-window policy ──────────────────────
    # All three history-pipeline knobs are now plugin-owned:
    # ``normalize_assistant_for_messages`` (H3),
    # ``serialize_assistant_for_history`` (H2), and
    # ``render_assistant_from_history`` (H4). ``manager._to_natural_
    # language`` keeps only the user / tool branches; the assistant
    # branch is delegated here.

    def normalize_assistant_for_messages(self, raw: str) -> str:
        # ReAct: raw IS the on-the-wire shape, so identity preserves
        # self-reinforcement (the model's prior teaches the format
        # we want it to keep emitting). Envelope plugins re-render
        # to repair drift at the boundary.
        return raw

    def serialize_assistant_for_history(self, raw_text: str) -> dict:
        # JSON-parse the ReAct emission and surface
        # ``thought / action / action_input`` as top-level fields so
        # ``manager._to_natural_language`` can dispatch on them. Falls
        # back to bare ``content`` when the text isn't parseable so
        # corrupt emissions still survive in the log for postmortem.
        try:
            data = json.loads(raw_text)
            if isinstance(data, dict) and ("thought" in data or "action" in data):
                data["role"] = "assistant"
                return data
        except (json.JSONDecodeError, TypeError):
            pass
        return {"role": "assistant", "content": raw_text}

    def render_assistant_from_history(self, record: dict) -> dict:
        # Round-trip the structured history record back to the ReAct
        # wire shape: the JSON object the model originally emitted.
        # ``serialize_assistant_for_history`` parsed that JSON into
        # ``thought / action / action_input`` top-level fields; here we
        # re-emit those fields as a single JSON string so the model's
        # next turn sees the same wire shape regardless of whether the
        # turn came from the live buffer or from history.jsonl. Self-
        # reinforcement of the wire format survives the overflow
        # recovery / session resume boundary.
        #
        # Differences from the original emission are limited to JSON
        # normalization (key order = thought→action→action_input,
        # default ``json.dumps`` spacing). Semantic content is
        # preserved verbatim.
        if "thought" not in record and "action" not in record:
            # Fallback: a record that ``serialize_assistant_for_history``
            # could not parse and stored as bare ``content``.
            return {"role": "assistant", "content": record.get("content", "")}

        payload: dict = {}
        if "thought" in record:
            payload["thought"] = record["thought"]
        if "action" in record:
            payload["action"] = record["action"]
        if "action_input" in record:
            payload["action_input"] = record["action_input"]
        return {
            "role": "assistant",
            "content": json.dumps(payload, ensure_ascii=False),
        }


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
