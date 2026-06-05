"""Pure-Python equivalent of ``unifdef -b -D... -U...``.

We only re-implement the subset ``preproc.py`` actually invokes:

* Flags: ``-DNAME`` / ``-DNAME=VAL`` / ``-UNAME`` — set/unset macros.
* Mode:  ``-b`` — removed lines are replaced with blank lines so the
  output preserves the input's line count (tree-sitter line numbers
  must keep pointing at the original source).
* Directives: ``#if`` / ``#ifdef`` / ``#ifndef`` / ``#elif`` /
  ``#else`` / ``#endif``.
* Expression operators: ``defined(NAME)``, integer literals (dec /
  hex / octal, with C type suffixes), identifier lookup, ``!`` ``~``
  ``-`` (unary), ``* / %``, ``+ -``, ``<< >>``, ``< <= > >=``,
  ``== !=``, ``& ^ |``, ``&& ||``, parentheses.

UNKNOWN propagation (a single sentinel value) lets an unknown
identifier short-circuit through ``&& 0`` / ``|| 1`` without forcing
the whole expression to be opaque. When an ``#if`` / ``#elif`` result
is UNKNOWN, the frame switches to *pass-through*: every line until
the matching ``#endif`` is emitted verbatim and the directives are
not touched — matching real ``unifdef`` behaviour.

The entire ``preproc.preprocess_source`` pipeline already folds
backslash continuations and strips trailing whitespace on directive
lines before calling us, so the walker can treat every directive as a
single, clean line. Mid-file ``#define`` / ``#undef`` are *not*
tracked — agent-cli only feeds CONFIG_* macros through the defs file,
and full mid-file macro tracking would re-introduce a chunk of the
upstream unifdef implementation we deliberately left behind.
"""

from __future__ import annotations

import re
from typing import Optional, Union

# Sentinel for "value is indeterminate". A real integer 0 means false,
# any non-zero int means true, so we need a distinct object for unknown
# rather than overloading None / -1.
_UNKNOWN: object = object()

# A parsed expression result is either an int or the UNKNOWN sentinel.
_Val = Union[int, object]


# ─── Flag parsing ──────────────────────────────────────────────


def _parse_int_literal(s: str) -> int:
    """C-style integer literal: decimal, ``0x...`` hex, ``0...`` octal.

    Strips ``u`` / ``l`` type suffixes (``UL``, ``ULL``, etc.) before
    parsing — the value is the same, the suffix only carries C type
    info that unifdef ignores.
    """
    s = s.strip()
    s = re.sub(r"[uUlL]+$", "", s)
    if not s:
        raise ValueError("empty integer literal")
    if s.lower().startswith("0x"):
        return int(s, 16)
    if s.startswith("0") and len(s) > 1 and all(c in "01234567" for c in s):
        return int(s, 8)
    return int(s)


def parse_flags(flags: list[str]) -> dict[str, Optional[int]]:
    """Convert ``-D``/``-U`` flags into a ``{name: value | None}`` map.

    * ``-DNAME``       → ``name`` defined to ``1`` (unifdef convention)
    * ``-DNAME=VAL``   → ``name`` defined to ``int(VAL)`` if numeric,
                         else to ``None`` (treat opaque values as
                         "defined but value unknown" — same as unifdef)
    * ``-UNAME``       → ``name`` explicitly *undefined* (sentinel
                         ``0`` so ``defined(NAME)`` returns false)

    Returned dict semantics: ``name not in dict`` → UNKNOWN macro;
    ``dict[name] is None`` → defined with opaque value; ``dict[name]``
    is an ``int`` → defined with that integer value; we also store
    sentinel ``0`` for explicitly undefined to keep the table flat.
    """
    defs: dict[str, Optional[int]] = {}
    for f in flags:
        if f.startswith("-D"):
            rest = f[2:]
            if "=" in rest:
                name, val = rest.split("=", 1)
                try:
                    defs[name] = _parse_int_literal(val)
                except ValueError:
                    # Opaque value (string, expression, etc.) — record
                    # as "defined" but value is unknown for evaluation.
                    defs[name] = None
            else:
                defs[rest] = 1
        elif f.startswith("-U"):
            # ``-U`` collides with the "defined opaque" None marker if
            # we used None for both. Use a distinct marker tuple? No —
            # disambiguate via a separate set tracked alongside.
            defs[f"\x00U\x00{f[2:]}"] = 0  # internal sentinel
    return defs


def _is_defined(defs: dict[str, Optional[int]], name: str) -> Optional[bool]:
    """Tri-state defined check: True / False / None (unknown)."""
    if f"\x00U\x00{name}" in defs:
        return False
    if name in defs:
        return True
    return None


def _value_of(defs: dict[str, Optional[int]], name: str) -> _Val:
    """Look up a macro's value for bare-identifier expression usage.

    ``-U`` → 0 (matches cpp behaviour for undefined macros in #if).
    ``-D`` with integer value → that int.
    ``-D`` with opaque value → UNKNOWN.
    Otherwise (not defined either way) → UNKNOWN.
    """
    if f"\x00U\x00{name}" in defs:
        return 0
    v = defs.get(name, _UNKNOWN)
    if v is None:
        return _UNKNOWN
    return v


# ─── Expression tokenizer ──────────────────────────────────────

# Order matters: longer operators (``<=``, ``&&``) must match before
# their single-char prefixes.
_TOKEN_RE = re.compile(
    r"""
    \s+
    | (?P<NUMBER>0[xX][0-9a-fA-F]+|0[0-7]*|[1-9][0-9]*)[uUlL]*
    | (?P<IDENT>[A-Za-z_]\w*)
    | (?P<LPAREN>\()
    | (?P<RPAREN>\))
    | (?P<AND>&&)
    | (?P<OR>\|\|)
    | (?P<LSHIFT><<)
    | (?P<RSHIFT>>>)
    | (?P<LE><=)
    | (?P<GE>>=)
    | (?P<EQ>==)
    | (?P<NE>!=)
    | (?P<LT><)
    | (?P<GT>>)
    | (?P<NOT>!)
    | (?P<TILDE>~)
    | (?P<MINUS>-)
    | (?P<PLUS>\+)
    | (?P<STAR>\*)
    | (?P<SLASH>/)
    | (?P<PERCENT>%)
    | (?P<AMP>&)
    | (?P<CARET>\^)
    | (?P<PIPE>\|)
    """,
    re.VERBOSE,
)


def _tokenize(expr: str) -> list[tuple[str, str]]:
    """Lex a #if-style expression into ``(kind, text)`` token pairs.

    Whitespace is skipped silently. An unexpected character raises
    ``ValueError`` — callers (the parser) translate that into the
    "unknown expression → pass-through" pathway so a malformed
    directive doesn't kill the whole index build.
    """
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(expr):
        m = _TOKEN_RE.match(expr, pos)
        if not m:
            raise ValueError(f"unexpected character {expr[pos]!r} at position {pos}")
        kind = m.lastgroup
        if kind is not None:
            tokens.append((kind, m.group()))
        pos = m.end()
    return tokens


# ─── Expression parser + evaluator ─────────────────────────────


def _short_and(a: _Val, b: _Val) -> _Val:
    """``&&`` with UNKNOWN short-circuit: a known-false side wins."""
    a_false = (a is not _UNKNOWN) and (a == 0)
    b_false = (b is not _UNKNOWN) and (b == 0)
    if a_false or b_false:
        return 0
    if a is _UNKNOWN or b is _UNKNOWN:
        return _UNKNOWN
    return 1 if (a and b) else 0


def _short_or(a: _Val, b: _Val) -> _Val:
    """``||`` with UNKNOWN short-circuit: a known-true side wins."""
    a_true = (a is not _UNKNOWN) and (a != 0)
    b_true = (b is not _UNKNOWN) and (b != 0)
    if a_true or b_true:
        return 1
    if a is _UNKNOWN or b is _UNKNOWN:
        return _UNKNOWN
    return 0


def _binop(op: str, a: _Val, b: _Val) -> _Val:
    """Apply a non-short-circuit operator. Either operand UNKNOWN
    propagates UNKNOWN — there's no value the missing side could
    take that would let us settle the result. ``/`` / ``%`` by
    zero is treated as UNKNOWN to match unifdef's behaviour."""
    if a is _UNKNOWN or b is _UNKNOWN:
        return _UNKNOWN
    a_i = int(a)
    b_i = int(b)
    if op == "STAR":
        return a_i * b_i
    if op == "SLASH":
        return _UNKNOWN if b_i == 0 else a_i // b_i
    if op == "PERCENT":
        return _UNKNOWN if b_i == 0 else a_i % b_i
    if op == "PLUS":
        return a_i + b_i
    if op == "MINUS":
        return a_i - b_i
    if op == "LSHIFT":
        return a_i << b_i
    if op == "RSHIFT":
        return a_i >> b_i
    if op == "LT":
        return 1 if a_i < b_i else 0
    if op == "LE":
        return 1 if a_i <= b_i else 0
    if op == "GT":
        return 1 if a_i > b_i else 0
    if op == "GE":
        return 1 if a_i >= b_i else 0
    if op == "EQ":
        return 1 if a_i == b_i else 0
    if op == "NE":
        return 1 if a_i != b_i else 0
    if op == "AMP":
        return a_i & b_i
    if op == "CARET":
        return a_i ^ b_i
    if op == "PIPE":
        return a_i | b_i
    raise ValueError(f"unhandled binary op {op}")


class _Parser:
    """Precedence-climbing parser for #if expressions.

    Grammar (low → high precedence, mirrors C cpp(1)):

        or       := and  ('||' and)*
        and      := bitor ('&&' bitor)*
        bitor    := bitxor ('|' bitxor)*
        bitxor   := bitand ('^' bitand)*
        bitand   := eq ('&' eq)*
        eq       := cmp (('==' | '!=') cmp)*
        cmp      := shift (('<' | '<=' | '>' | '>=') shift)*
        shift    := add (('<<' | '>>') add)*
        add      := mul (('+' | '-') mul)*
        mul      := unary (('*' | '/' | '%') unary)*
        unary    := ('!' | '~' | '-' | '+') unary | primary
        primary  := NUMBER | IDENT | 'defined' '(' IDENT ')' | 'defined' IDENT | '(' or ')'
    """

    def __init__(self, tokens: list[tuple[str, str]], defs: dict[str, Optional[int]]):
        self.tokens = tokens
        self.pos = 0
        self.defs = defs

    def _peek(self) -> Optional[tuple[str, str]]:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _advance(self) -> tuple[str, str]:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def _expect(self, kind: str) -> tuple[str, str]:
        tok = self._peek()
        if tok is None or tok[0] != kind:
            raise ValueError(f"expected {kind}, got {tok}")
        return self._advance()

    def parse(self) -> _Val:
        v = self._or()
        if self._peek() is not None:
            raise ValueError(
                f"trailing tokens after expression: {self.tokens[self.pos :]}"
            )
        return v

    def _or(self) -> _Val:
        v = self._and()
        while (t := self._peek()) and t[0] == "OR":
            self._advance()
            v = _short_or(v, self._and())
        return v

    def _and(self) -> _Val:
        v = self._bitor()
        while (t := self._peek()) and t[0] == "AND":
            self._advance()
            v = _short_and(v, self._bitor())
        return v

    def _bitor(self) -> _Val:
        v = self._bitxor()
        while (t := self._peek()) and t[0] == "PIPE":
            self._advance()
            v = _binop("PIPE", v, self._bitxor())
        return v

    def _bitxor(self) -> _Val:
        v = self._bitand()
        while (t := self._peek()) and t[0] == "CARET":
            self._advance()
            v = _binop("CARET", v, self._bitand())
        return v

    def _bitand(self) -> _Val:
        v = self._eq()
        while (t := self._peek()) and t[0] == "AMP":
            self._advance()
            v = _binop("AMP", v, self._eq())
        return v

    def _eq(self) -> _Val:
        v = self._cmp()
        while (t := self._peek()) and t[0] in ("EQ", "NE"):
            op = self._advance()[0]
            v = _binop(op, v, self._cmp())
        return v

    def _cmp(self) -> _Val:
        v = self._shift()
        while (t := self._peek()) and t[0] in ("LT", "LE", "GT", "GE"):
            op = self._advance()[0]
            v = _binop(op, v, self._shift())
        return v

    def _shift(self) -> _Val:
        v = self._add()
        while (t := self._peek()) and t[0] in ("LSHIFT", "RSHIFT"):
            op = self._advance()[0]
            v = _binop(op, v, self._add())
        return v

    def _add(self) -> _Val:
        v = self._mul()
        while (t := self._peek()) and t[0] in ("PLUS", "MINUS"):
            op = self._advance()[0]
            v = _binop(op, v, self._mul())
        return v

    def _mul(self) -> _Val:
        v = self._unary()
        while (t := self._peek()) and t[0] in ("STAR", "SLASH", "PERCENT"):
            op = self._advance()[0]
            v = _binop(op, v, self._unary())
        return v

    def _unary(self) -> _Val:
        t = self._peek()
        if t and t[0] in ("NOT", "TILDE", "MINUS", "PLUS"):
            op = self._advance()[0]
            v = self._unary()
            if v is _UNKNOWN:
                return _UNKNOWN
            i = int(v)
            if op == "NOT":
                return 0 if i else 1
            if op == "TILDE":
                return ~i
            if op == "MINUS":
                return -i
            return +i
        return self._primary()

    def _primary(self) -> _Val:
        t = self._peek()
        if t is None:
            raise ValueError("unexpected end of expression")
        kind, text = t
        if kind == "NUMBER":
            self._advance()
            return _parse_int_literal(text)
        if kind == "LPAREN":
            self._advance()
            v = self._or()
            self._expect("RPAREN")
            return v
        if kind == "IDENT":
            if text == "defined":
                self._advance()
                # defined NAME or defined(NAME)
                if (nxt := self._peek()) and nxt[0] == "LPAREN":
                    self._advance()
                    name = self._expect("IDENT")[1]
                    self._expect("RPAREN")
                else:
                    name = self._expect("IDENT")[1]
                state = _is_defined(self.defs, name)
                if state is None:
                    return _UNKNOWN
                return 1 if state else 0
            self._advance()
            return _value_of(self.defs, text)
        raise ValueError(f"unexpected token {t}")


def _eval_expr(expr: str, defs: dict[str, Optional[int]]) -> _Val:
    """Evaluate a single ``#if`` / ``#elif`` expression.

    A malformed or unsupported expression is treated as UNKNOWN —
    mirroring unifdef's "pass through anything I can't parse" stance.
    Better to leave a directive intact than to risk wrong pruning.
    """
    try:
        return _Parser(_tokenize(expr), defs).parse()
    except (ValueError, KeyError):
        return _UNKNOWN


# ─── Directive walker ──────────────────────────────────────────

# Three frame modes describe what happens to lines inside this
# branch. ``TAKEN``: emit verbatim. ``NOT_TAKEN``: blank out (``-b``
# semantics). ``PASS_THROUGH``: emit verbatim *and* preserve the
# directives themselves — we couldn't prove inactive, so output stays
# byte-identical to input for this region.
_TAKEN = "TAKEN"
_NOT_TAKEN = "NOT_TAKEN"
_PASS_THROUGH = "PASS_THROUGH"

_DIRECTIVE_RE = re.compile(r"^\s*#\s*([A-Za-z_]\w*)\b(.*)$")


def _blank_line(line: str) -> str:
    """Replace ``line`` with a blank that keeps its line break, so the
    output preserves the input's line count (``-b`` contract)."""
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    return ""


def _strip_trailing_comment(rest: str) -> str:
    """Drop trailing ``/* ... */`` and ``//`` comments from the
    directive tail before passing it to the expression parser. The
    rewriter chain in ``preproc.py`` already removes block comments
    from ``#define`` bodies; this catches the rarer case of a comment
    on an ``#if`` / ``#ifdef`` line itself."""
    rest = re.sub(r"/\*.*?\*/", " ", rest)
    rest = re.sub(r"//.*$", "", rest)
    return rest.strip()


def _any(stack: list[dict], mode: str) -> bool:
    return any(f["mode"] == mode for f in stack)


def run_unifdef(text: str, flags: list[str]) -> str:
    """Pure-Python ``unifdef -b`` with the given ``-D``/``-U`` flags.

    Walks the input line-by-line, maintaining a stack of frames per
    nested ``#if``/``#ifdef``/``#ifndef``. Each frame remembers:

      * ``mode``         — TAKEN, NOT_TAKEN, or PASS_THROUGH.
      * ``prior_taken``  — has any previous branch (#if / #elif) in
                           this chain already been taken? Lets
                           #elif/#else fall to NOT_TAKEN once a TAKEN
                           branch has fired.

    Critical semantic match with upstream unifdef: ``PASS_THROUGH``
    does *not* cascade to child frames. A nested ``#ifdef`` inside an
    UNKNOWN frame is still evaluated independently — only its own
    ancestor's NOT_TAKEN status can force it dead. This mirrors the
    behaviour observed against the C implementation: an outer
    header-guard (``#ifndef __FOO_H__``) whose macro isn't on the
    flag list keeps its own directives verbatim but every inner
    ``#ifdef CONFIG_X`` is still resolved against the supplied flags
    just as if the outer frame were taken.

    Output rules per line:

      * non-directive line → blank if any frame in the stack is
        NOT_TAKEN, otherwise keep verbatim. PASS_THROUGH frames do
        *not* affect non-directive lines (their bodies are kept).
      * directive line → blank if the owning frame is TAKEN or
        NOT_TAKEN; keep verbatim if the owning frame is PASS_THROUGH.
        A NOT_TAKEN ancestor always overrides to blank — the
        directive is inside dead code regardless of its own merit.
    """
    defs = parse_flags(flags)
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    stack: list[dict] = []

    def body_emit(line: str) -> str:
        # Non-directive line: dies only if a frame is NOT_TAKEN.
        # PASS_THROUGH frames keep their bodies (the unknown branch
        # might be the live one, so we conservatively retain it).
        return _blank_line(line) if _any(stack, _NOT_TAKEN) else line

    def directive_emit(owning_mode: str, line: str, *, ancestors: list[dict]) -> str:
        # A NOT_TAKEN ancestor pins everything below to dead — even a
        # PASS_THROUGH owning frame can't override that, since the
        # whole region won't be reached at compile time.
        if _any(ancestors, _NOT_TAKEN):
            return _blank_line(line)
        # Otherwise: PASS_THROUGH owning frame keeps its directives
        # verbatim (we can't safely prune them); TAKEN / NOT_TAKEN
        # owning frame blanks them (the directive itself is noise
        # once we've decided which branch is active).
        return line if owning_mode == _PASS_THROUGH else _blank_line(line)

    for line in lines:
        m = _DIRECTIVE_RE.match(line)
        if not m:
            out.append(body_emit(line))
            continue

        directive, rest = m.group(1), m.group(2)
        rest = _strip_trailing_comment(rest)

        if directive in ("if", "ifdef", "ifndef"):
            # New frame. Only NOT_TAKEN ancestors short-circuit
            # evaluation — PASS_THROUGH ancestors leave us free to
            # evaluate this frame independently.
            if _any(stack, _NOT_TAKEN):
                mode = _NOT_TAKEN
            else:
                if directive == "if":
                    val = _eval_expr(rest, defs)
                elif directive == "ifdef":
                    state = _is_defined(defs, rest)
                    val = _UNKNOWN if state is None else (1 if state else 0)
                else:  # ifndef
                    state = _is_defined(defs, rest)
                    val = _UNKNOWN if state is None else (0 if state else 1)
                if val is _UNKNOWN:
                    mode = _PASS_THROUGH
                else:
                    mode = _TAKEN if val else _NOT_TAKEN

            ancestors = list(stack)  # snapshot before push
            stack.append({"mode": mode, "prior_taken": (mode == _TAKEN)})
            out.append(directive_emit(mode, line, ancestors=ancestors))
            continue

        if directive == "elif":
            if not stack:
                out.append(line)
                continue
            top = stack[-1]
            ancestors = stack[:-1]
            if top["mode"] == _PASS_THROUGH:
                # The owning chain is uncertain — keep the #elif
                # verbatim and stay in PASS_THROUGH for the new branch.
                out.append(directive_emit(_PASS_THROUGH, line, ancestors=ancestors))
                continue
            if _any(ancestors, _NOT_TAKEN):
                top["mode"] = _NOT_TAKEN
                out.append(_blank_line(line))
                continue
            if top["prior_taken"]:
                top["mode"] = _NOT_TAKEN
                out.append(directive_emit(_NOT_TAKEN, line, ancestors=ancestors))
                continue
            val = _eval_expr(rest, defs)
            if val is _UNKNOWN:
                # Once the chain becomes uncertain we have to keep
                # this and every subsequent branch verbatim, including
                # the matching #endif — otherwise we'd lose the
                # boundary the downstream tooling needs.
                top["mode"] = _PASS_THROUGH
                out.append(directive_emit(_PASS_THROUGH, line, ancestors=ancestors))
            elif val:
                top["mode"] = _TAKEN
                top["prior_taken"] = True
                out.append(directive_emit(_TAKEN, line, ancestors=ancestors))
            else:
                top["mode"] = _NOT_TAKEN
                out.append(directive_emit(_NOT_TAKEN, line, ancestors=ancestors))
            continue

        if directive == "else":
            if not stack:
                out.append(line)
                continue
            top = stack[-1]
            ancestors = stack[:-1]
            if top["mode"] == _PASS_THROUGH:
                out.append(directive_emit(_PASS_THROUGH, line, ancestors=ancestors))
                continue
            if _any(ancestors, _NOT_TAKEN):
                top["mode"] = _NOT_TAKEN
                out.append(_blank_line(line))
                continue
            if top["prior_taken"]:
                top["mode"] = _NOT_TAKEN
            else:
                top["mode"] = _TAKEN
                top["prior_taken"] = True
            out.append(directive_emit(top["mode"], line, ancestors=ancestors))
            continue

        if directive == "endif":
            if not stack:
                out.append(line)
                continue
            popped = stack.pop()
            out.append(directive_emit(popped["mode"], line, ancestors=stack))
            continue

        # Any other directive (#define, #include, #pragma, ...) is
        # data, not control flow — same emit policy as a normal line.
        out.append(body_emit(line))

    return "".join(out)


__all__ = ["run_unifdef", "parse_flags"]
