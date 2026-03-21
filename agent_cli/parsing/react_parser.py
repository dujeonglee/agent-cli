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
    final_answer: str | None = None
    raw: str = ""
    parse_stage: int = 0  # 0=failed, 1=json.loads, 2=json_repair, 3=regex
    thinking: str | None = None  # Extracted thinking block content


def _sanitize_surrogates(text: str) -> str:
    """Remove unpaired Unicode surrogates that break JSON parsing."""
    return re.sub(r'[\ud800-\udfff]', '', text)


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
    data = repair_json(text)
    if data is not None:
        _populate_from_dict(result, data)
        result.parse_stage = 2
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

    # Try extracting first { ... } block
    m = re.search(r"\{[\s\S]*\}", stripped)
    if m:
        try:
            data = json.loads(m.group())
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

    m = re.search(r'"final_answer"\s*:\s*"((?:[^"\\]|\\.)*)"', text, re.S)
    if m:
        result["final_answer"] = (
            m.group(1).replace('\\"', '"').replace("\\n", "\n")
        )

    return result if result else None


def _populate_from_dict(result: ReActResult, data: dict) -> None:
    """Fill a ReActResult from a parsed dict."""
    result.thought = data.get("thought")
    result.action = data.get("action")
    result.action_input = data.get("action_input")
    result.final_answer = data.get("final_answer") or data.get("final")
