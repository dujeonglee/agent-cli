"""JSON syntax diagnostics — turn a ``json.JSONDecodeError`` into a
human/model-readable pointer (message + line/column + a caret under the
offending character).

This is a pure JSON-layer utility, NOT wire-format behaviour: given any
JSON candidate string it describes the first structural error, knowing
nothing about ReAct vs md_array. The format-specific part — *which*
substring of the model's emission is the JSON candidate — stays in each
format's ``diagnose_syntax_error`` (which calls this). Kept off the
``WireFormat`` base for exactly that reason; sharing a caret formatter is
a JSON concern, not a coupling between formats.

Used only on the recovery path (NO_JSON), after ``repair_json`` and the
``strict=False`` fallback have both failed — i.e. on the residual,
genuinely-unrepairable emissions. ``strict=False`` here mirrors the
pipeline's tolerance so we report the real structural break (a missing
``]``), not an "Invalid control character" red herring the parser would
have accepted anyway.
"""

import json

# How many characters of context to show on each side of the error column.
# Long single-line JSON (the model's usual shape) is windowed to this so the
# snippet stays readable while the caret stays aligned to the local window.
_WINDOW = 40
_INDENT = "    "


def describe_json_error(json_text: str | None) -> str | None:
    """Return a multi-line diagnostic for the first JSON syntax error in
    ``json_text``, or ``None`` if it parses cleanly / is blank.

    Shape::

        Expecting ',' delimiter (line 1, column 9)
            {"a": 1 "b": 2}
                   ^

    Returns ``None`` unless the candidate actually *looks like* a JSON
    object/array attempt (starts with ``{`` or ``[``). Our wire formats only
    ever emit objects/arrays, so a candidate that doesn't start that way is
    bare prose, not malformed JSON — pointing a caret at "Expecting value,
    column 1" there is noise; the generic "output ONLY JSON" hint already
    covers it.
    """
    if not json_text or not json_text.strip():
        return None
    if json_text.lstrip()[:1] not in ("{", "["):
        return None
    try:
        json.loads(json_text, strict=False)
    except json.JSONDecodeError as e:
        return _render(e, json_text)
    return None


def _render(e: json.JSONDecodeError, text: str) -> str:
    header = f"{e.msg} (line {e.lineno}, column {e.colno})"

    pos = min(e.pos, len(text))
    line_start = text.rfind("\n", 0, pos) + 1
    line_end = text.find("\n", pos)
    if line_end == -1:
        line_end = len(text)
    line = text[line_start:line_end]
    col = pos - line_start  # 0-based column within the offending line

    seg_start = max(0, col - _WINDOW)
    seg = line[seg_start : col + _WINDOW]
    lead = "..." if seg_start > 0 else ""
    tail = "..." if (col + _WINDOW) < len(line) else ""

    snippet = _INDENT + lead + seg + tail
    caret = _INDENT + " " * (len(lead) + (col - seg_start)) + "^"
    return f"{header}\n{snippet}\n{caret}"
