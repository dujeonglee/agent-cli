"""Repair incomplete or malformed JSON from LLM output.

Handles common issues:
1. Unclosed strings
2. Missing closing brackets
3. Trailing commas
4. Single quotes instead of double quotes
5. Unquoted keys
6. JSON embedded in surrounding text
"""

from __future__ import annotations

import json
import re


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


def _extract_json_block(text: str) -> str:
    """Find the outermost { ... } block in the text."""
    from agent_cli.parsing.react_parser import _strip_markdown_fences

    text = _strip_markdown_fences(text)

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
