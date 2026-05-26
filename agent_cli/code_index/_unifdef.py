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


def _any_above(stack: list[dict], mode: str) -> bool:
    """True if any ancestor frame (everything below the top of the
    stack) is in the given mode."""
    return any(f["mode"] == mode for f in stack[:-1])


def _all_taken(stack: list[dict]) -> bool:
    """True if every frame currently in the stack is TAKEN — i.e.,
    we're in a fully-known-active region."""
    return all(f["mode"] == _TAKEN for f in stack)


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

    Output rules per line:

      * directive line (#if/.../#endif) → emit blank in known regions
        (whether taken or not — directives themselves are noise), emit
        verbatim in PASS_THROUGH regions.
      * non-directive line → emit verbatim if every frame is TAKEN
        (or stack empty), blank if any frame is NOT_TAKEN, emit
        verbatim if any frame is PASS_THROUGH (and no NOT_TAKEN
        intervenes — checked frame-by-frame).
    """
    defs = parse_flags(flags)
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    stack: list[dict] = []

    def line_decision() -> str:
        # Output policy for a non-directive line, walking outermost
        # to innermost: the first NOT_TAKEN frame wins (blanks), else
        # the first PASS_THROUGH frame wins (verbatim), else verbatim.
        for f in stack:
            if f["mode"] == _NOT_TAKEN:
                return "blank"
        if any(f["mode"] == _PASS_THROUGH for f in stack):
            return "verbatim"
        return "verbatim"

    for line in lines:
        m = _DIRECTIVE_RE.match(line)
        if not m:
            decision = line_decision()
            out.append(line if decision == "verbatim" else _blank_line(line))
            continue

        directive, rest = m.group(1), m.group(2)
        rest = _strip_trailing_comment(rest)

        if directive in ("if", "ifdef", "ifndef"):
            # New frame. Where the new frame's mode comes from:
            #   * if any ancestor is NOT_TAKEN → mode = NOT_TAKEN
            #     (this whole block is dead anyway, no point eval'ing)
            #   * if any ancestor is PASS_THROUGH → mode = PASS_THROUGH
            #     (parent uncertainty cascades down)
            #   * else evaluate the directive against current defs
            if _any(stack, _NOT_TAKEN):
                mode = _NOT_TAKEN
            elif _any(stack, _PASS_THROUGH):
                mode = _PASS_THROUGH
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

            stack.append({"mode": mode, "prior_taken": (mode == _TAKEN)})
            # Emit the directive line itself.
            if mode == _PASS_THROUGH:
                # Verbatim, so the matching #endif also passes through.
                out.append(line)
            elif _any(stack[:-1], _NOT_TAKEN):
                out.append(_blank_line(line))
            elif _any(stack[:-1], _PASS_THROUGH):
                out.append(line)
            else:
                out.append(_blank_line(line))
            continue

        if directive == "elif":
            if not stack:
                # Stray #elif — emit verbatim, ignore.
                out.append(line)
                continue
            top = stack[-1]
            if top["mode"] == _PASS_THROUGH:
                out.append(line)
                continue
            if _any(stack[:-1], _NOT_TAKEN):
                # Ancestor already says everything inside is dead.
                top["mode"] = _NOT_TAKEN
                out.append(_blank_line(line))
                continue
            if _any(stack[:-1], _PASS_THROUGH):
                out.append(line)
                continue
            if top["prior_taken"]:
                top["mode"] = _NOT_TAKEN
                out.append(_blank_line(line))
                continue
            val = _eval_expr(rest, defs)
            if val is _UNKNOWN:
                # Switching to PASS_THROUGH from here on — we no
                # longer know which branch is the right one, so
                # everything (including the matching #endif) must
                # round-trip unchanged.
                top["mode"] = _PASS_THROUGH
                out.append(line)
            elif val:
                top["mode"] = _TAKEN
                top["prior_taken"] = True
                out.append(_blank_line(line))
            else:
                top["mode"] = _NOT_TAKEN
                out.append(_blank_line(line))
            continue

        if directive == "else":
            if not stack:
                out.append(line)
                continue
            top = stack[-1]
            if top["mode"] == _PASS_THROUGH:
                out.append(line)
                continue
            if _any(stack[:-1], _NOT_TAKEN):
                top["mode"] = _NOT_TAKEN
                out.append(_blank_line(line))
                continue
            if _any(stack[:-1], _PASS_THROUGH):
                out.append(line)
                continue
            if top["prior_taken"]:
                top["mode"] = _NOT_TAKEN
            else:
                top["mode"] = _TAKEN
                top["prior_taken"] = True
            out.append(_blank_line(line))
            continue

        if directive == "endif":
            if not stack:
                out.append(line)
                continue
            popped = stack.pop()
            if popped["mode"] == _PASS_THROUGH:
                out.append(line)
            elif _any(stack, _NOT_TAKEN):
                out.append(_blank_line(line))
            elif _any(stack, _PASS_THROUGH):
                out.append(line)
            else:
                out.append(_blank_line(line))
            continue

        # Any other directive (#define, #include, #pragma, ...) is
        # data, not control flow — same emit policy as a normal line.
        decision = line_decision()
        out.append(line if decision == "verbatim" else _blank_line(line))

    return "".join(out)


__all__ = ["run_unifdef", "parse_flags"]
