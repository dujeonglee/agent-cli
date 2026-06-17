"""JSON structural repair — pure, format-agnostic string→string fixes.

Sibling to ``_json_diag``: same rationale for living off the ``WireFormat``
base (a pure JSON concern, not wire-format behaviour, shared by every
JSON-bearing format so each need not carry a private copy that can drift).

Two fixes, both deliberately conservative + bail-if-invalid (the CALLER
re-parses and keeps the result only if it now validates; a wrong guess leaves
the parse failing → diagnostic+retry, never a forced bogus structure):

- :func:`close_unbalanced` — appends closers for brackets/braces opened and
  never closed (string-aware depth scan). The measured dominant NO_JSON shape:
  a multi-op array the model finished but forgot to close (session 1781336790
  — a 6-op read_file batch missing its `]`).
- :func:`repair_value_quotes` — a string value/key missing ONE quote (open OR
  close): ``"path": mgt.c"`` / ``"path": "mgt.c}``. Error-position guided, only
  fires on a clear missing-quote signal (a stray quote, or an unterminated
  string with a delimiter before EOF) so bare ``true``/``42`` and genuinely
  truncated mid-value output are left for retry.
"""

import json

_DELIMS = ",}]"


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


def repair_value_quotes(text: str) -> tuple[str, bool]:
    """Repair a string value/key that is missing ONE of its quotes — the open
    OR the close — anywhere in the JSON.

    Two underlying shapes, distinguished by the strict parser's own error:

    - **Missing OPEN** (``"path": mgt.c"``) → ``Expecting value`` at the bare
      token. Only repaired when the token carries a stray ``"`` (evidence a
      string was intended — so a bare ``true``/``42`` is NOT mis-quoted): drop
      the stray quote and re-quote the token (``"mgt.c"``).
    - **Missing CLOSE** (``"path": "mgt.c}``) → ``Unterminated string`` at the
      open quote → insert a closing ``"`` before the structural delimiter the
      string ran into.

    Error-position guided + bounded loop (fixes several such errors in one
    payload), then the CALLER re-parses and accepts only if it now validates
    (bail-if-invalid, same contract as :func:`close_unbalanced`): a wrong guess
    simply fails to parse and falls through to diagnostic+retry, never forcing
    a bogus op. Returns ``(fixed_text, changed)``.
    """
    start = _first_json_start(text)
    if start is None:
        return text, False
    prefix, body = text[:start], text[start:]
    decoder = json.JSONDecoder()
    changed = False
    for _ in range(16):  # bound: multiple missing quotes in one payload
        try:
            decoder.raw_decode(body)
            break  # parses (ignoring any trailing data) → done
        except json.JSONDecodeError as e:
            repaired = _repair_quote_at(body, e)
            if repaired is None or repaired == body:
                break  # not a missing-quote case / no progress → bail
            body = repaired
            changed = True
    return (prefix + body, True) if changed else (text, False)


def _first_json_start(text: str) -> int | None:
    for i, ch in enumerate(text):
        if ch in "[{":
            return i
    return None


def _scan_to_delim(s: str, pos: int) -> int:
    """First structural delimiter (`, } ]`) at/after ``pos`` (or EOF)."""
    i = pos
    while i < len(s) and s[i] not in _DELIMS:
        i += 1
    return i


def _repair_quote_at(s: str, e: json.JSONDecodeError) -> str | None:
    """Apply ONE targeted quote repair at the parser error, or None if the
    error isn't a recognised missing-quote shape."""
    pos = e.pos
    if e.msg.startswith("Expecting value"):
        # Bare / open-quote-missing scalar value at pos.
        end = _scan_to_delim(s, pos)
        token = s[pos:end].strip()
        if '"' not in token:
            return None  # genuine bare token (true/42/…) — not ours; bail
        core = token[1:] if token.startswith('"') else token
        core = core[:-1] if core.endswith('"') else core
        if not core:
            return None
        return s[:pos] + json.dumps(core) + s[end:]
    if e.msg.startswith("Unterminated string"):
        # Open quote at pos; the string never closed. Shut it before the
        # structural delimiter it ran into — BUT only if such a delimiter
        # exists before EOF. A string that runs to EOF with no delimiter is a
        # genuinely truncated output (cut mid-value), not a missing close
        # quote; force-closing it would fabricate a bogus op, so bail and let
        # the truncation/retry path handle it.
        end = _scan_to_delim(s, pos + 1)
        if end >= len(s):
            return None
        return s[:end] + '"' + s[end:]
    return None
