"""JSON structural repair — pure, format-agnostic string→string fixes.

Sibling to ``_json_diag``: same rationale for living off the ``WireFormat``
base (a pure JSON concern, not wire-format behaviour, shared by every
JSON-bearing format so each need not carry a private copy that can drift).

Currently one fix — :func:`close_unbalanced`. It is deliberately
conservative: it only ever *appends* closers for brackets/braces that were
opened and never closed (string-aware depth scan), so it cannot corrupt an
otherwise-valid payload. Callers re-parse the result and accept it only if
it now validates; a deeper break (a truncated mid-value, an extra closer)
leaves the parse failing, so the caller falls back to diagnostic+retry
rather than forcing a bogus structure. This is the measured repair for the
dominant real NO_JSON shape: a multi-op array the model finished but forgot
to close (session 1781336790 — a 6-op read_file batch missing its `]`).
"""


def close_unbalanced(text: str) -> tuple[str, bool]:
    """Append the closing brackets/braces left open at EOF.

    Returns ``(fixed_text, changed)``. ``changed`` is True iff at least one
    closer was appended. String contents (and escapes) are skipped, so
    brackets inside string values are never counted.
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
