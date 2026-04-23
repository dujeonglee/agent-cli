"""ReAct response parser with 3-stage fallback.

Stage 0: Strip thinking blocks (<think>, <reasoning>, etc.)
Stage 1: json.loads(strip_markdown(text))      -- fast path
Stage 2: json_repair(text)                      -- fix incomplete/malformed JSON
Stage 3: regex field extraction                  -- last resort
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from agent_cli.parsing.json_repair import repair_json

# Known thinking/reasoning block tag names (case-insensitive)
_THINKING_TAGS = ["think", "thinking", "reasoning", "reflection"]

# Build regex that matches any of the known thinking tags
_THINKING_PATTERN = re.compile(
    r"<(" + "|".join(_THINKING_TAGS) + r")>(.*?)</\1>",
    re.S | re.I,
)


@dataclass
class ReActResult:
    """Parsed ReAct response."""

    thought: str | None = None
    action: str | None = None
    action_input: dict | str | None = None
    raw: str = ""
    parse_stage: int = 0  # 0=failed, 1=json.loads, 2=json_repair, 3=regex
    thinking: str | None = None  # Extracted thinking block content
    truncated: bool = False  # True if JSON was repaired (brackets/strings closed)


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


def parse_react(text: str) -> ReActResult:
    """Parse an LLM response into a ReActResult using 3-stage fallback."""
    text = _sanitize_surrogates(text)
    text, thinking = _strip_thinking_blocks(text)
    result = ReActResult(raw=text, thinking=thinking)

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


def _strip_markdown_fences(text: str) -> str:
    """Remove ```json ... ``` wrapping."""
    stripped = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.I)
    stripped = re.sub(r"\s*```\s*$", "", stripped)
    return stripped


def _try_json_parse(text: str) -> dict | None:
    """Stage 1: Try direct JSON parse."""
    stripped = _strip_markdown_fences(text)

    try:
        data = json.loads(stripped)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, ValueError):
        pass

    # Try extracting first { ... } block using balanced brace extraction
    from agent_cli.parsing.json_repair import _extract_json_block

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
    result = {}

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


# Virtual-tool payload hoisting map.
#
# Some models (observed with qwen3 family) emit responses like:
#   {"thought": "...", "action": "complete", "result": "final answer"}
# where the payload key is at the top level instead of nested inside
# action_input. This is valid JSON and the action name is correct, so
# nothing downstream catches it — the complete handler just sees
# action_input=None and reports "Completed without result".
#
# The drift happens most often on the "final answer" virtual tools
# (complete, ready_for_review) because conceptually they feel like a
# direct reply, not a tool call. Strict JSON Schema would not have
# prevented it either: the old REACT_JSON_SCHEMA only marked `thought`
# as required and placed no additionalProperties cap, so top-level
# `result` was always allowed.
#
# Entry shape: action_name -> (target_key_in_action_input, top_level_fallback_keys)
# The first matching top-level key's value is placed under target_key.
_VIRTUAL_TOOL_PAYLOAD_HOIST: dict[str, tuple[str, tuple[str, ...]]] = {
    "complete": ("result", ("result", "answer", "response", "final", "output")),
    "ready_for_review": ("summary", ("summary",)),
    # For ask, _extract_questions in loop.py already treats "questions" and
    # "question" interchangeably, so placing the hoisted value under
    # "questions" is safe regardless of which top-level key the model used.
    "ask": ("questions", ("questions", "question")),
}


def _hoist_virtual_tool_payload(result: ReActResult, data: dict) -> None:
    """Synthesize action_input from top-level keys when a virtual tool
    response omits action_input. Quiet no-op for non-virtual tools,
    already-populated action_input, or actions without a hoist entry."""
    if not result.action or result.action_input:
        return
    spec = _VIRTUAL_TOOL_PAYLOAD_HOIST.get(result.action)
    if spec is None:
        return
    target_key, candidates = spec
    for key in candidates:
        if key in data:
            result.action_input = {target_key: data[key]}
            return


def _populate_from_dict(result: ReActResult, data: dict) -> None:
    """Fill a ReActResult from a parsed dict."""
    result.thought = data.get("thought")
    result.action = data.get("action")
    result.action_input = data.get("action_input")
    _hoist_virtual_tool_payload(result, data)
