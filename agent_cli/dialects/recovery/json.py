"""JSON 구제 — 깨진 JSON 을 op 로 되살리는 기계 (Phase 5 S1: 이동만).

세 겹이 한 모듈로 모였다 (docs/dialects/PHASE5.md §4.4):
1. 토큰 수준 수리 — 옛 ``_json_repair`` (이스케이프·괄호 불균형·따옴표).
2. 진단 — 옛 ``_json_diag`` (어디서 깨졌는지, 재시도 힌트용).
3. op 수준 추출·수리 — 옛 ``json_fc`` 157–597 (산문 속 첫 JSON, 다시 열린
   배열 합치기, 이름 없는 op 객체, stage 판정 근거).

코드는 무수정 이동이다 — 등가성 합격선(PHASE5 §7)이 이 모듈을 기준으로 잰다.
엔진의 ``json_native``/``json_in_tag`` 디코더가 쓰는 공용 기계이며, 어느 스펙의
소유물도 아니다.

──────────────────────────── 옛 _json_repair 머리말 ────────────────────────────
JSON structural repair — pure, format-agnostic string→string fixes.

Sibling to ``_json_diag``: same rationale for living off the ``DialectBase``
base (a pure JSON concern, not dialect behaviour, shared by every
JSON-bearing format so each need not carry a private copy that can drift).

Four fixes, all deliberately conservative + bail-if-invalid (the CALLER
re-parses and keeps the result only if it now validates; a wrong guess leaves
the parse failing → diagnostic+retry, never a forced bogus structure):

- :func:`fix_invalid_escapes` — doubles a lone backslash that does NOT begin a
  valid JSON escape (``\\d`` ``\\s`` ``\\x`` ``\\.`` from raw-string regex / char
  classes / Windows paths). The measured dominant backslash-heavy failure:
  json.loads raises ``Invalid \\escape`` on a regex ``[^\\s]``.
- :func:`close_unbalanced` — appends closers for brackets/braces opened and
  never closed (string-aware depth scan). The measured dominant NO_JSON shape:
  a multi-op array the model finished but forgot to close (session 1781336790
  — a 6-op read_file batch missing its `]`).
- :func:`drop_unbalanced_closers` — the mirror: drops closers that match no
  open frame (over-closed payload). Measured shape: a doubled op close brace
  ``[{...}}]`` (session 1783001191 — a 27B shell op → NO_JSON).
- :func:`repair_value_quotes` — a string value/key missing ONE quote (open OR
  close): ``"path": mgt.c"`` / ``"path": "mgt.c}``. Error-position guided, only
  fires on a clear missing-quote signal (a stray quote, or an unterminated
  string with a delimiter before EOF) so bare ``true``/``42`` and genuinely
  truncated mid-value output are left for retry.

──────────────────────────── 옛 _json_diag 머리말 ─────────────────────────────
JSON syntax diagnostics — turn a ``json.JSONDecodeError`` into a
human/model-readable pointer (message + line/column + a caret under the
offending character).

This is a pure JSON-layer utility, NOT dialect behaviour: given any
JSON candidate string it describes the first structural error, knowing
nothing about ReAct vs json_fc. The format-specific part — *which*
substring of the model's emission is the JSON candidate — stays in each
format's ``diagnose_syntax_error`` (which calls this). Kept off the
``DialectBase`` base for exactly that reason; sharing a caret formatter is
a JSON concern, not a coupling between formats.

Used only on the recovery path (NO_JSON), after ``repair_json`` and the
``strict=False`` fallback have both failed — i.e. on the residual,
genuinely-unrepairable emissions. ``strict=False`` here mirrors the
pipeline's tolerance so we report the real structural break (a missing
``]``), not an "Invalid control character" red herring the parser would
have accepted anyway.
"""

from __future__ import annotations

import json
import re

from agent_cli.thinking_tags import TRAILING_THINK_TAG_RE as _TRAILING_THINK_TAG

# ── 1. 토큰 수준 수리 (옛 _json_repair) ──────────────────────────────

_DELIMS = ",}]"
# Valid single-char JSON string escapes (``\uXXXX`` handled separately).
_VALID_ESCAPE = set('"\\/bfnrt')


def fix_invalid_escapes(text: str) -> tuple[str, bool]:
    """Double any backslash that does NOT begin a valid JSON escape, so the
    model's under-escaped ``\\d`` / ``\\s`` / ``\\x`` / ``\\.`` (raw-string
    regex, ``\\x00`` char classes, Windows paths) parse as the literal backslash
    they meant. The measured dominant backslash-heavy failure: json.loads raises
    ``Invalid \\escape`` on a regex ``[^\\s]`` the model wrote with one backslash.

    Conservative + reversible: VALID escape pairs (``\\"`` ``\\\\`` ``\\n``
    ``\\uXXXX``) are consumed as-is, so already-correct JSON is returned
    unchanged (``changed=False``) and the caller only keeps the result if it now
    validates. String-aware is unnecessary — a backslash outside a string is
    already invalid JSON, so doubling it can't corrupt valid structure."""
    out: list[str] = []
    i = 0
    n = len(text)
    changed = False
    while i < n:
        c = text[i]
        if c == "\\":
            nxt = text[i + 1] if i + 1 < n else ""
            is_u = nxt == "u" and _is_hex4(text[i + 2 : i + 6])
            if nxt in _VALID_ESCAPE or is_u:
                out.append(text[i : i + 2])  # valid escape → keep the pair
                i += 2
            else:
                out.append("\\\\")  # invalid → escape the lone backslash
                changed = True
                i += 1
        else:
            out.append(c)
            i += 1
    return ("".join(out), changed) if changed else (text, False)


def _is_hex4(s: str) -> bool:
    return len(s) == 4 and all(c in "0123456789abcdefABCDEF" for c in s)


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
        elif ch in ("}", "]") and stack and stack[-1] == ch:
            stack.pop()

    if stack:
        return text + "".join(reversed(stack)), True
    return text, False


def drop_unbalanced_closers(text: str) -> tuple[str, bool]:
    """Drop closing brackets/braces that match no open frame — the mirror of
    :func:`close_unbalanced` for OVER-closed payloads. String-aware depth stack:
    a ``}``/``]`` is kept only when it closes the current open frame, else
    dropped as spurious. The measured shape: a model doubling an op's close
    brace, ``[{...}}]`` (session 1783001191 — a 27B shell op emitted with
    ``}}]`` → NO_JSON). Braces inside string values are never counted, so
    content like ``"return {}"`` is untouched, and already-balanced JSON is
    returned unchanged (``changed=False``).

    Returns ``(fixed_text, changed)``; ``changed`` iff at least one closer was
    dropped. bail-if-invalid, same contract as :func:`close_unbalanced`.
    """
    out: list[str] = []
    stack: list[str] = []
    in_string = False
    escape_next = False
    changed = False

    for ch in text:
        if escape_next:
            escape_next = False
            out.append(ch)
            continue
        if ch == "\\":
            if in_string:
                escape_next = True
            out.append(ch)
            continue
        if ch == '"':
            in_string = not in_string
            out.append(ch)
            continue
        if in_string:
            out.append(ch)
            continue
        if ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in ("}", "]"):
            if stack and stack[-1] == ch:
                stack.pop()
            else:
                changed = True  # spurious closer (no matching open) → drop
                continue
        out.append(ch)

    return ("".join(out), changed) if changed else (text, False)


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
        core = token.removeprefix('"')
        core = core.removesuffix('"')
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


# ── 2. 진단 (옛 _json_diag) ───────────────────────────────────────────

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
    object/array attempt (starts with ``{`` or ``[``). Our dialects only
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


# ── 3. op 수준 추출·수리 (옛 json_fc 157–597) ──────────────────────────

# A JSON value can only begin with these characters, so a ``[``/``{`` followed
# by anything else is prose, not an op — checked BEFORE the balanced scan both
# to reject it and to keep the scan cheap (an unmatched opener otherwise costs a
# walk to EOF, and prose is full of them: `arr[0]`, `- [ ]`, `{ passive: false }`).
_JSON_VALUE_START = set('{["-0123456789tfn')


def _json_spans(body: str) -> list[tuple[int, int]]:
    """Every top-level balanced ``[...]`` / ``{...}`` span in ``body``.

    The string-aware walk is the one :func:`_extract_first_json` has always
    used; the difference is that it does not stop at the first span. Openers
    that never balance are skipped (scan resumes at the next character), so a
    stray brace cannot swallow the rest of the text.
    """
    opens = {"[": "]", "{": "}"}
    out: list[tuple[int, int]] = []
    i, n = 0, len(body)
    while i < n:
        c = body[i]
        if c not in opens:
            i += 1
            continue
        # Cheap prefilter: the next non-space character must be able to start a
        # JSON value (for ``{`` a key string, or an empty object).
        j = i + 1
        while j < n and body[j].isspace():
            j += 1
        nxt = body[j] if j < n else ""
        allowed = ('"', "}") if c == "{" else tuple(_JSON_VALUE_START) + ("]",)
        if nxt not in allowed:
            i += 1
            continue
        close = opens[c]
        depth = 0
        in_str = escape = False
        end = -1
        for k in range(i, n):
            ch = body[k]
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
            elif ch == c:
                depth += 1
            elif ch == close:
                depth -= 1
                if depth == 0:
                    end = k + 1
                    break
        if end < 0:
            # A prefiltered opener that never balances = DAMAGED op JSON (an
            # unclosed array is the single most common repair case). Stop here
            # instead of resuming inside it: the next balanced thing would be
            # the array's own first element, which would then masquerade as a
            # complete bare 1-op and silently drop every later op. Leaving the
            # region unclaimed sends it to the repair machinery, where
            # ``close_unbalanced`` restores the whole batch (stage 2).
            break
        out.append((i, end))
        i = end
    return out


def _op_signature(value) -> int:
    """How op-like a parsed payload is: 1 = certain, 2 = structural, 0 = not.

    1 — carries an ``action`` key (an op, or a batch containing one).
    2 — a bare dict WITHOUT ``action``: an op whose action field the model
        dropped (the loop recovers it via ``infer_action`` on the preserved
        input, ``action_required=False`` — tests/test_dropped_field_recovery).
        Only the dict form needs this tier: an actionless ARRAY still starts
        with ``[{``, so the fallback anchor finds it, whereas a bare
        ``{"path": …}`` matches no opener pattern and would otherwise be lost.
    0 — anything else: ``[1]``, ``[]``, ``["a"]``, a bare string/number, and an
        array of dicts with no action anywhere. This is the ``any("action")``
        guard PHASE4 §3.1 called for, applied at SPAN SELECTION time — the turn
        parser already had it, but by then the wrong span had been handed over.
    """
    if isinstance(value, dict):
        return 1 if "action" in value else 2
    if isinstance(value, list) and value and all(isinstance(e, dict) for e in value):
        return 1 if any("action" in e for e in value) else 0
    return 0


# Fallback anchor for text whose op JSON is BROKEN (nothing parses, so span
# selection cannot help): the last plausible op opener. Prose braces sit before
# it, so the repair machinery starts on the real payload instead of on
# `board[i][j]`. Whitespace-tolerant (``[\n  {``).
_OP_OPENER = re.compile(r'\[\s*\{|\{\s*"action"')


def _op_anchor(text: str) -> int:
    """Where the op payload starts, or -1 when there is no candidate at all.

    The best op-signature tier wins, and within that tier the FIRST span. Note
    what does the work here: it is the TIER, not the direction. Prose braces are
    tier 0, so they are gone before position is even consulted — which means
    every historical position-based semantic survives untouched:

    * ``[{op1}]\\n[{op2}]`` (array reopened per op) — both tier 1, anchor on the
      first, so ``_merge_reopened_op_arrays`` still sees the whole group.
    * op array, prose, op array — both tier 1, anchor on the first: the
      deliberately conservative "no merge across prose, first array wins"
      contract (test_prose_between_arrays_defense).
    * trailing ``</think>`` / closing prose after the ops — tier 0, ignored.

    Scanning from the back instead would have satisfied the leading-prose cases
    while breaking both bullet-1 and bullet-2, so direction is not the fix.

    With nothing parseable (genuinely broken JSON), falls back to the first
    plausible op opener — prose braces do not match ``[{`` / ``{"action"``, so
    the repair machinery starts on the payload rather than on ``board[i][j]`` —
    and finally to the first brace, the historical behaviour.
    """
    best_start, best_tier = -1, 3
    for start, end in _json_spans(text):
        try:
            value = json.loads(text[start:end])
        except json.JSONDecodeError:
            continue
        tier = _op_signature(value)
        if tier and tier < best_tier:
            best_tier, best_start = tier, start
        if best_tier == 1:
            break  # a certain op; nothing later can outrank it
    if best_start >= 0:
        return best_start
    match = _OP_OPENER.search(text)
    if match:
        return match.start()
    return next((i for i, c in enumerate(text) if c in "[{"), -1)


def _extract_first_json(body: str, *, strict: bool = True):
    """Parse the first balanced ``[...]`` or ``{...}`` from body, or None.

    ``strict`` is forwarded to ``json.loads``: with ``strict=False`` the parser
    accepts literal control characters (raw newlines/tabs) inside string values
    — a leniency :func:`_extract_op_json` uses as a last-resort repair (see
    there). The balanced-brace scan itself is unaffected (it already tracks
    strings), so only the final ``json.loads`` behaviour changes."""
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else body
        if body.rfind("```") > 0:
            body = body[: body.rfind("```")]
    opens = {"[": "]", "{": "}"}
    start = next((i for i, c in enumerate(body) if c in opens), -1)
    if start < 0:
        return None
    open_c, close_c = body[start], opens[body[start]]
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(body)):
        c = body[i]
        if in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == open_c:
            depth += 1
        elif c == close_c:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(body[start : i + 1], strict=strict)
                except json.JSONDecodeError:
                    return None
    return None


def _merge_reopened_op_arrays(text: str) -> tuple[str, bool]:
    """Merge multiple top-level op-arrays the model emitted as SEPARATE arrays
    instead of one. Measured shape: a 27B write_file batch split across three
    lines, each its own ``[{...}`` (session 1783129061 — the model reopened the
    array per op rather than emitting one ``[op, op, op]``).

    The tell is a structural ``}`` (an op close) followed — outside any string —
    by an optional array close ``]``, whitespace, then ``[{`` (a new op-array
    open). In valid JSON a ``}`` is only ever followed by ``,`` ``]`` ``}``, so
    ``}`` … ``[{`` at object level is unambiguously a spurious re-open →
    rewrite the boundary to ``},{`` so the ops fold into one array. String-aware
    (braces/brackets inside a ``content`` value never trigger it), and a no-op
    when the pattern is absent (``changed=False``). Compose with
    :func:`close_unbalanced` for the array's own missing ``]``.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    in_str = escape = False
    changed = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "}":
            j = i + 1
            while j < n and text[j].isspace():
                j += 1
            if j < n and text[j] == "]":  # optional close of the prior array
                j += 1
                while j < n and text[j].isspace():
                    j += 1
            if j + 1 < n and text[j] == "[" and text[j + 1] == "{":
                out.append("},{")  # fold the re-opened array into this one
                i = j + 2
                changed = True
                continue
        out.append(ch)
        i += 1
    return ("".join(out), changed) if changed else (text, False)


def _repair_anonymous_op_objects(text: str, *, drop_close: bool) -> str:
    """Remove the spurious ``{`` the model inserts in object-KEY position when
    it wraps an op's params in an anonymous nested object (DESIGN Exp 8). Two
    shapes are seen, and the model is consistent within one emission:

      A. ``{"action": X, {params}}``  (anon AND op both close — 27B)
      B. ``{"action": X, {params}``   (one ``}`` — the model reuses the anon
         close AS the op close — 35B; the array then has N unbalanced ``{``)

    The repair removes the spurious ``{`` either way. ``drop_close`` switches
    the two:
      - ``True``  → variant A: the anon ``{`` opens a frame whose matching
        ``}`` is ALSO dropped, leaving the op's own ``}`` (``{X, params}``).
      - ``False`` → variant B: the anon ``{`` opens NO frame, so the single
        ``}`` that follows closes the op (``{X, params}``).
    The caller tries both and keeps whichever parses (``_extract_op_json``).

    Context- and string-aware single pass: only a ``{`` where an object key is
    expected (object start / right after a comma at object level) is treated as
    the bug; a ``{`` after ``:`` (a legit nested value) or inside an array
    element is left alone, and braces inside string literals (C code in a
    ``content`` value) never affect matching. No-op when the pattern is absent.
    """
    out: list[str] = []
    frames: list[dict] = []  # {"type": "obj"|"arr", "expect_key": bool, "drop": bool}
    in_str = escape = False
    for ch in text:
        if in_str:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            if frames and frames[-1]["type"] == "obj":
                frames[-1]["expect_key"] = False  # the key/value string starts
            out.append(ch)
        elif ch == "{":
            top = frames[-1] if frames else None
            if top and top["type"] == "obj" and top["expect_key"]:
                # spurious anonymous-object open in key position → drop it.
                if drop_close:
                    # variant A: track it so its matching `}` is dropped too.
                    frames.append({"type": "obj", "expect_key": True, "drop": True})
                # variant B: push nothing — the op frame keeps absorbing the
                # params and its own `}` (the next one) closes it.
            else:
                frames.append({"type": "obj", "expect_key": True, "drop": False})
                out.append(ch)
        elif ch == "}":
            top = frames.pop() if frames else {"drop": False}
            if not top.get("drop"):
                out.append(ch)
            if frames and frames[-1]["type"] == "obj":
                frames[-1]["expect_key"] = False
        elif ch == "[":
            frames.append({"type": "arr", "expect_key": False, "drop": False})
            out.append(ch)
        elif ch == "]":
            if frames:
                frames.pop()
            if frames and frames[-1]["type"] == "obj":
                frames[-1]["expect_key"] = False
            out.append(ch)
        elif ch == ":":
            if frames and frames[-1]["type"] == "obj":
                frames[-1]["expect_key"] = False
            out.append(ch)
        elif ch == ",":
            if frames and frames[-1]["type"] == "obj":
                frames[-1]["expect_key"] = True
            out.append(ch)
        else:
            out.append(ch)
    return "".join(out)


def _op_count(parsed) -> int:
    """Number of dict-shaped ops in an extracted payload (bare object = 1)."""
    if isinstance(parsed, list):
        return sum(1 for x in parsed if isinstance(x, dict))
    return 1 if isinstance(parsed, dict) else 0


def _extract_op_json(text: str):
    """``_extract_first_json`` with an anonymous-nested-object repair fallback.

    Returns ``(parsed, repaired)`` — ``repaired`` is True iff the strict parse
    failed but the unwrap repair recovered it (parse_stage 2). Tries both op
    shapes — ``{"action":X, {params}}`` (A) and ``{"action":X, {params}`` (B) —
    and keeps whichever parses."""
    parsed = _extract_first_json(text)
    if parsed is not None:
        # The first array parsed cleanly — but the model may have split the
        # batch into several SEPARATE top-level arrays (`[{op1}] [{op2}]`), and
        # `_extract_first_json` stops at the first, silently dropping the rest
        # (measured happy-path op loss — no error, parse_stage would be 1). If
        # folding whitespace-adjacent re-opened arrays yields MORE ops, take the
        # merged parse as a drift recovery (parse_stage 2). Conservative: the
        # fold only fires on `}` …`[{` (never a valid single array's `},{`), and
        # only when it strictly gains ops — trailing `</think>`/prose never
        # matches, so that defense (first-array-wins) is preserved.
        merged, changed = _merge_reopened_op_arrays(text)
        if changed:
            remerged = _extract_first_json(merged)
            if remerged is not None and _op_count(remerged) > _op_count(parsed):
                return remerged, True
        return parsed, False
    # Strict parse failed: the JSON is broken. A reasoning model often leaks a
    # trailing ``</think>`` after the array, which defeats the unclosed-array
    # closer below (it appends ``]`` past the tag). Drop trailing think tags so
    # the repair path sees clean JSON. Done only AFTER the strict parse, so valid
    # JSON carrying ``<think>`` inside a string value is preserved.
    text = _TRAILING_THINK_TAG.sub("", text)
    for drop_close in (True, False):
        fixed = _repair_anonymous_op_objects(text, drop_close=drop_close)
        if fixed != text:
            # Compose with merge + close_unbalanced: the model often forgets BOTH
            # the params-wrapping AND the array's ``]`` — and sometimes splits the
            # batch into several separate ``[{...}`` arrays (measured — session
            # 1783129061, a 27B write_file batch: three lines of
            # `[{"action":X, {params}}` with no trailing `]`). The unwrap yields
            # `[{...}` per op (still unclosed / multi-array), so fold re-opened
            # arrays into one and append the missing closer before re-parsing.
            merged = _merge_reopened_op_arrays(fixed)[0]
            seen: set[str] = set()
            for base in (fixed, merged):
                for cand in (base, close_unbalanced(base)[0]):
                    if cand in seen:
                        continue
                    seen.add(cand)
                    for strict in (True, False):
                        parsed = _extract_first_json(cand, strict=strict)
                        if parsed is not None:
                            return parsed, True
    # Last resort: the model emitted literal control chars (raw newlines/tabs)
    # inside a string value — common in big `result`/`content` markdown blobs
    # written without `\n` escaping — which strict json.loads rejects
    # ("Invalid control character"). Re-parse leniently; a recovery, so
    # repaired=True keeps parse_stage 2 as the signal that JSON was non-strict.
    # Note: strict=False ONLY relaxes control chars — genuinely broken JSON
    # (missing value, bad brace) still returns None, so this never force-parses
    # garbage into bogus ops.
    parsed = _extract_first_json(text, strict=False)
    if parsed is not None:
        return parsed, True
    # Invalid escapes: the model under-escaped a regex ``\s`` / ``\d`` / ``\x`` /
    # ``\.`` (raw strings, char classes, Windows paths) → strict json.loads raises
    # "Invalid \escape" (the measured dominant backslash-heavy failure). Double
    # the lone backslashes and re-parse; compose with the unclosed-array closer so
    # a payload that is BOTH under-escaped AND missing its ``]`` still recovers.
    esc_fixed, changed = fix_invalid_escapes(text)
    if changed:
        parsed = _extract_first_json(esc_fixed, strict=False)
        if parsed is not None:
            return parsed, True
        closed, ch2 = close_unbalanced(esc_fixed)
        if ch2:
            parsed = _extract_first_json(closed, strict=False)
            if parsed is not None:
                return parsed, True
    # Last resort: a well-formed op array the model finished but forgot to
    # close (measured dominant NO_JSON shape — session 1781336790, a 6-op
    # read_file batch missing its trailing `]`). `_extract_first_json` returns
    # None on an unclosed array (depth never returns to 0), so close the
    # unbalanced brackets at EOF (string-aware, depth-stack → deterministic
    # closer) and re-parse (strict, then control-char-lenient). Accept only if
    # it now validates; a deeper break (truncated mid-op) keeps it None →
    # diagnostic+retry, never a forced bogus op.
    closed, changed = close_unbalanced(text)
    if changed:
        for strict in (True, False):
            parsed = _extract_first_json(closed, strict=strict)
            if parsed is not None:
                return parsed, True
    # Last resort: an OVER-closed payload — the model doubled an op's close
    # brace (`[{...}}]`, session 1783001191 — a 27B shell op emitted `}}]` →
    # NO_JSON). The mirror of close_unbalanced: drop the spurious closers
    # (string-aware, so content braces are safe) and re-parse, composed with
    # close_unbalanced so a payload BOTH over-closed early AND unclosed at EOF
    # still recovers. bail-if-invalid: a wrong drop falls through to retry.
    dropped, changed = drop_unbalanced_closers(text)
    if changed:
        for cand in (dropped, close_unbalanced(dropped)[0]):
            for strict in (True, False):
                parsed = _extract_first_json(cand, strict=strict)
                if parsed is not None:
                    return parsed, True
    # Last resort: a string value/key missing ONE quote (open or close) —
    # ``"path": mgt.c"`` / ``"path": "mgt.c}``. Error-position-guided requote
    # (string-aware), composed with close_unbalanced so a payload that is BOTH
    # quote-broken AND unclosed still recovers. Accept only if it validates
    # (bail-if-invalid) → a wrong guess falls through to diagnostic+retry.
    requoted, changed = repair_value_quotes(text)
    if changed:
        for cand in (requoted, close_unbalanced(requoted)[0]):
            for strict in (True, False):
                parsed = _extract_first_json(cand, strict=strict)
                if parsed is not None:
                    return parsed, True
    return None, False
