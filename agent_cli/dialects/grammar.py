"""Decoding grammars — the wire shape as an EBNF the server can enforce.

v9.24.0. The text protocol asks the model for a shape (system prompt) and
repairs what comes back (recovery layer). A server that supports
grammar-constrained decoding (omlx/xgrammar, vLLM, llama.cpp) can instead
mask every token that would leave the shape, so a malformed emission never
exists: no ``<tool_call>`` skeleton stutter, no unknown tool, no reasoning
runaway inside the body (Harbor extract-elf: 32K characters of analysis
inside an open ``<tool_call>``). Prevention where it is available; the
recovery layer stays for servers that cannot enforce.

Each dialect renders its own grammar (:meth:`DialectBase.grammar`) from
the **same tool set the prompt advertises** — the grammar must never allow a
tool the prompt did not describe, nor forbid one it did. This module holds
the format-agnostic pieces: xgrammar-flavoured EBNF for JSON values, the
"text not containing a literal" construction, and the bounded prose rule.

Dialect notes (xgrammar EBNF, the one dialect verified live — 2026-09-25):
``"lit"`` strings, ``[...]`` classes with ``\\`` escapes, ``( )``, ``|``,
``*``/``+``/``?`` and ``{m,n}`` bounded repetition. No ``.`` wildcard —
use ``[^\\n]`` style classes.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

#: Prose (the thought before the first tool call): what it may NOT contain is
#: the format's *opener sequence* (``\n\n[`` / ``\n\n<tool_call>``), never a
#: bare character, and it is NOT length-bounded. Two Harbor A/B lessons
#: (2026-09-25/26) fixed both halves:
#:
#: * The first cut forbade the opener character (``[`` / ``<``) outright. A
#:   thought that needs ``(?<!``, ``b3 << 24`` or ``a[0]`` then has its natural
#:   token masked, the sampler is pushed off the model's distribution, and the
#:   turn either silently means something else (``(?!``, ``>> 8``) or spirals
#:   ("Wait, let me correct that…") to the token cap. Format failures went
#:   1 → 13 and generations of 5–11K tokens appeared.
#: * A turn with NO tool call must stay expressible. The first cuts made the
#:   call mandatory (``root ::= prose "\n\n" call …``): when the model finishes
#:   its prose and wants to end the turn, EOS is masked, and the only legal
#:   continuation is more prose — it drifts to the 32K cap (v3 A/B: 18–24K
#:   tokens in flight, one to three completed calls per trial). The 4000-char
#:   cap had hidden this by forcing the opener. The grammar enforces the
#:   *shape* of a call; "there must be a call" is policy, and the recovery
#:   layer's no-action path already handles it (v9.21 non-recording retry).
#: * The second cut kept a 4000-char cap as bounded *group* repetition
#:   ``( … ){0,4000}``. xgrammar pays for that per token: 2.2 s/token with the
#:   live tokenizer (char-class ``[^<]{0,4000}`` is 0.08 ms, unbounded
#:   ``( … )*`` is free). Two trials finished zero LLM calls in 15 minutes.
#:   A runaway is bounded by ``max_output_tokens`` and the recovery layer
#:   instead — the 32K runaway that motivated grammars was inside a tool
#:   body, which was never length-bounded anyway.


def _rule_name(text: str) -> str:
    """A safe EBNF rule-name fragment from a tool/param name."""
    return "".join(ch if ch.isalnum() else "_" for ch in text)


def _not_containing_alts(
    literal: str | tuple[str, ...], extra_forbidden: str = ""
) -> str:
    """The alternatives (one step of text) that cannot complete ``literal``:
    a character that cannot start it, or a proper prefix of it followed by a
    character that breaks it. Wrapped by the callers in ``( )*`` (unbounded)
    or ``( ){0,n}`` (bounded).

    ``literal`` may be several strings (v9.24.8 — the think region forbids
    both ``</think>`` and ``<tool_call>``): the prefixes form a trie, and
    after each prefix the breaking characters are those that continue NONE
    of the strings. One string yields exactly the former rule."""
    lits = (literal,) if isinstance(literal, str) else tuple(literal)
    firsts = "".join(dict.fromkeys(lit[0] for lit in lits))
    alts = [f"[^{_cls(firsts + extra_forbidden)}]"]
    prefixes = sorted({lit[:i] for lit in lits for i in range(1, len(lit))})
    for prefix in prefixes:
        nxt = "".join(
            dict.fromkeys(
                lit[len(prefix)]
                for lit in lits
                if lit.startswith(prefix) and len(lit) > len(prefix)
            )
        )
        alts.append(f'"{_esc(prefix)}" [^{_cls(nxt + extra_forbidden)}]')
    return " | ".join(alts)


def not_containing(
    name: str, literal: str | tuple[str, ...], extra_forbidden: str = ""
) -> str:
    """Rule ``name`` = any text that does not contain ``literal`` (or any of
    several literals).

    The rule can end anywhere, so text that ends with a prefix of the
    literal (``…</param``) is still accepted — and the closing tag that
    follows is then matched by the caller's rule. ``extra_forbidden`` lists
    characters excluded outright (e.g. ``"`` for a JSON-ish body).

    Known gap (accepted): the prefix chain is not the exact KMP automaton.
    After a proper prefix and a breaking character the loop restarts at
    state 0, so a literal that overlaps itself through the break character
    slips through — for ``</parameter>`` that is ``</</parameter>``, i.e.
    the literal's first character re-used as the break. Exact forms were
    measured: a right-recursive KMP automaton costs xgrammar time linear in
    the text (2.6 ms/token at 30K chars), a loop-with-excursion form 98
    ms/token. The overlap needs the closer's own opener typed twice in a
    row, which no format-following model does, so the chain stays. Line
    and paragraph openers (``prose_rule``) use a different, exact scheme.
    """
    return f"{name} ::= ({_not_containing_alts(literal, extra_forbidden)})*"


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _cls(chars: str) -> str:
    """Characters as they appear inside an EBNF ``[...]`` class."""
    out = []
    for ch in chars:
        if ch in "\\]^-[":
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        else:
            out.append(ch)
    return "".join(out)


#: JSON value rules (RFC 8259, except that strings may hold raw control
#: characters — newlines, tabs — because the parser's last resort re-reads
#: with ``strict=False`` anyway; masking every raw newline of a file body
#: would steer the model line by line for nothing the harness cannot rescue).
#: Names are prefixed ``j_`` so a tool called ``string`` cannot collide.
#:
#: ``j_chars`` inlines the four hex classes of ``\uXXXX`` on purpose. With a
#: ``j_hex`` sub-rule referenced inside the star, xgrammar loses its fast
#: precomputed-mask path for the whole string region and checks the vocabulary
#: token by token: 65 ms/token measured with the live 248K tokenizer (the
#: −37 % throughput of the first A/B), 0.07 ms with the classes inlined.
#: Same strictness either way (short ``\u12`` and ``\q`` are still rejected).
JSON_RULES = r"""
j_value ::= j_object | j_array | j_string | j_number | "true" | "false" | "null"
j_object ::= "{" j_ws ( j_member ( j_ws "," j_ws j_member )* )? j_ws "}"
j_member ::= j_string j_ws ":" j_ws j_value
j_array ::= "[" j_ws ( j_value ( j_ws "," j_ws j_value )* )? j_ws "]"
j_string ::= "\"" j_chars "\""
j_chars ::= ( [^"\\] | "\\" ["\\/bfnrt] | "\\u" [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F] )*
j_number ::= "-"? ( "0" | [1-9] [0-9]* ) ( "." [0-9]+ )? ( [eE] [+\-]? [0-9]+ )?
j_ws ::= [ \t\n\r]*
""".strip()


def json_value_rule(prop_schema: dict) -> str:
    """The JSON rule name for a property's declared type (``j_value`` when
    unknown or polymorphic — the validator still checks the type after)."""
    t = prop_schema.get("type") if isinstance(prop_schema, dict) else None
    if isinstance(t, list):  # ["string", "null"] style — accept any
        return "j_value"
    return {
        "string": "j_string",
        "integer": "j_number",
        "number": "j_number",
        "boolean": '( "true" | "false" )',
        "array": "j_array",
        "object": "j_object",
    }.get(t or "", "j_value")


def think_prefix(thinking_open: bool, forbid: tuple[str, ...] = ()) -> tuple[str, str]:
    """``(root-prefix, rules)`` for a generation that starts inside an open
    ``<think>`` block (the chat template opened it; the model must close it
    before the visible turn). When ``thinking_open`` is False the template
    pre-closed the block and the visible turn starts immediately.

    Verified live: with thinking on and a grammar that lacks this prefix,
    the whole constrained output stays inside the think channel.

    ``forbid`` (v9.24.8): extra strings the think region may not contain —
    xml_fc passes its call opener ``<tool_call>``. A call written inside the
    thinking is never executed (the parser strips thinking first) and it is
    the one shape the harness cannot rescue: live board, a resident agent
    repeated ``<tool_call><function=read_file>…`` inside an unclosed think
    block for 22 minutes (loopback capture). Blocked, the model closes the
    block and calls for real — induction test 0/8 leaks vs 4/8 unblocked,
    all 16 normal stops; cost unchanged (0.0025 ms/token, +0.04 s compile).
    """
    if not thinking_open:
        return "", ""
    return 'think "</think>\\n\\n" ', not_containing("think", ("</think>", *forbid))


def _line_alts(opener: str) -> str:
    """Alternatives for one NON-EMPTY line (no newline inside) that does not
    start with ``opener``: a first character that is not the opener's, or a
    proper prefix of the opener followed by a character that breaks it (or
    the line ending right there). Exact — a line has no room for the
    overlap that limits ``not_containing``."""
    # ``\\n`` below is the two-character EBNF escape, never a Python newline.
    alts = [f"[^\\n{_cls(opener[0])}] [^\\n]*"]
    for i in range(1, len(opener)):
        alts.append(f'"{_esc(opener[:i])}" ( [^\\n{_cls(opener[i])}] [^\\n]* )?')
    return " | ".join(alts)


def prose_rule(name: str, opener: str, *, after_blank_line: bool = False) -> str:
    """Free text (the thought) in which the format's opener never starts a
    call: for xml_fc no LINE may start with ``<tool_call>``; for json_fc
    (``after_blank_line=True``) no line that follows a blank line — or the
    first line — may start with ``[``. So the first such line is
    unambiguously the first tool call, while ``<`` / ``[`` stay free
    everywhere else in the thought (``(?<!``, ``a[0]``, ``- [ ]`` …).

    Built from lines, not from a "text not containing the sequence" chain:
    the chain leaked exactly the common case (``\n\n<tool_call>`` parsed as
    ``"\n" [^<]`` + plain text), and exact automata cost xgrammar time that
    grows with the text (measured 2.6–98 ms/token). Both line forms are
    exact against a Python reference on random strings and flat at
    0.1–0.2 ms/token with the live tokenizer. Unbounded on purpose: a
    bounded group repetition costs seconds per token (module note)."""
    alts = _line_alts(opener)
    nl = "\\n"  # EBNF newline literal (two characters); real "\n" separates rules
    if not after_blank_line:
        # every line: possibly empty, never opener-first
        return f'{name} ::= {name}_l ( "{nl}" {name}_l )*\n{name}_l ::= ( {alts} )?'
    # paragraphs separated by blank runs; a paragraph's first line is never
    # opener-first, its other lines are free; leading/trailing blank lines ok
    return (
        f'{name} ::= ( "{nl}" )* ( {name}_nb ( "{nl}" {name}_any )* '
        f'( "{nl}" "{nl}" ( "{nl}" )* {name}_nb ( "{nl}" {name}_any )* )* '
        f'( "{nl}" )* )?\n'
        f"{name}_any ::= [^{nl}]+\n"
        f"{name}_nb ::= {alts}"
    )


# ── Schema enforcement (v9.24.4) ─────────────────────────────────────
#
# What the grammar enforces beyond the call's SHAPE, and why each is safe
# (costs measured with the live 248K tokenizer — per-token mask time is
# unchanged at ~0.004 ms; the one-time compile grows, and the server caches
# compiled grammars across calls):
#
# * ``enum`` values — a parameter with a declared ``enum`` can only take one
#   of those literals. The model picks among a few words; nothing is invented.
# * ``minLength >= 1`` strings — the value must hold a non-blank character.
#   Declared per parameter, not implied by "required": ``write_file.content``
#   is required yet may legitimately be empty.
# * FORCED presence — a REQUIRED parameter that is ALSO an enum must appear
#   (in any order) before the call can close. Only enum-valued ones: forcing
#   free text would make the model invent a command or path when it meant to
#   stop; forcing a choice among a few words cannot. Free-text required
#   parameters stay with the validator (one nudge, then fixed).
#
# Free-form tools (``allows_extra_keys`` — MCP and the like) are NOT covered:
# their any-key alternative also matches a declared key, so a declared
# constraint can be bypassed through it. Excluding a set of names from the
# wildcard needs a name-exclusion automaton for little gain; the validator
# still enforces the same declarations for them.
#
# Encoding rules that keep the mask on xgrammar's fast path: character
# classes and literal alternations only, the non-blank prefix INLINED (a
# ``body_ne`` sub-rule referencing ``body`` measured 28 ms on some tokens),
# no bounded group repetition.


@dataclass(frozen=True)
class GrammarParam:
    """One tool parameter as the grammar sees it — format-agnostic.

    ``kind``: ``"enum"`` · ``"text_nonempty"`` · ``"text"`` · a JSON type
    name (``"integer"``, ``"number"``, ``"boolean"``, ``"array"``,
    ``"object"``) · ``"any"``."""

    name: str
    kind: str
    prop: dict = field(default_factory=dict, compare=False, hash=False)
    enum: tuple[str, ...] = ()
    required: bool = False

    @property
    def forced(self) -> bool:
        return self.required and self.kind == "enum"


def grammar_params(flat: dict) -> list[GrammarParam]:
    """``flat_param_schemas`` output → :class:`GrammarParam` list."""
    out = []
    for name, (prop, required) in flat.items():
        prop = prop if isinstance(prop, dict) else {}
        t = prop.get("type")
        enum = prop.get("enum")
        if enum and all(isinstance(v, str) for v in enum):
            kind = "enum"
        elif t == "string" and (prop.get("minLength") or 0) >= 1:
            kind = "text_nonempty"
        elif t == "string":
            kind = "text"
        else:
            kind = t if isinstance(t, str) else "any"
        out.append(
            GrammarParam(
                name=name,
                kind=kind,
                prop=prop,
                enum=tuple(enum) if kind == "enum" else (),
                required=bool(required),
            )
        )
    return out


def enum_literals(values: tuple[str, ...], quote: str = "") -> str:
    """``( "a" | "b" )`` — optionally each wrapped in ``quote`` (JSON)."""
    q = _esc(quote)
    return "( " + " | ".join(f'"{q}{_esc(v)}{q}"' for v in values) + " )"


def nonblank_not_containing(literal: str) -> str:
    """Inline expression: text that does not contain ``literal`` and holds at
    least one non-blank character (blank = space, tab, CR, LF).

    Leading blanks, then ONE step of the not-containing automaton whose plain
    character alternative excludes blanks, then the ordinary continuation —
    all inline (see the module note on sub-rule cost)."""
    alts = _not_containing_alts(literal)
    first = literal[0]
    step = alts.replace(f"[^{_cls(first)}]", f"[^ \\t\\r\\n{_cls(first)}]", 1)
    return f"[ \\t\\r\\n]* ( {step} ) ( {alts} )*"


#: JSON string holding at least one non-blank character — inline for the
#: same reason (``j_chars`` is referenced once as a sequence element, the
#: shape ``j_string`` already has). Blank = raw space/tab/CR/LF AND their
#: escapes (backslash + n, t, r, f): the validator strips the DECODED value,
#: so an escaped newline alone must count as blank here too.
JSON_NONBLANK_STRING = (
    r'"\"" ( [ \t\n\r] | "\\" [ntrf] )* ( [^"\\ \t\n\r] | "\\" ["\\/b] | "\\u" '
    r"[0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F] ) j_chars " + r'"\""'
)


def tool_params_expr(
    tool: str, items: dict[str, str], forced: list[str], *, sep: str = ""
) -> tuple[str, list[str]]:
    """The parameter part of one tool rule — shared by every format.

    ``items`` maps a parameter name (or an ``"*"`` wildcard for free-form
    keys) to this format's rendering of ONE occurrence; ``sep`` precedes
    every occurrence (``""`` for xml_fc, the comma for json_fc). Without
    forced parameters: any of them, any number of times, in any order —
    the validator judges required-ness and duplicates. With forced ones:
    the same free repetition around ONE occurrence of each forced
    parameter, in every order (at most a handful — n! alternatives).
    Returns ``(expression, extra_rules)``."""
    if not items:
        return "", []
    any_item = "( " + " | ".join(items.values()) + " )"
    rep = f"( {sep}{any_item} )*"
    if not forced:
        return rep, []
    rep_rule = f"p_{_rule_name(tool)}"
    alts = []
    for perm in itertools.permutations(forced):
        seq = [rep_rule]
        for name in perm:
            seq += [f"{sep}{items[name]}", rep_rule]
        alts.append(" ".join(seq))
    return "( " + " | ".join(alts) + " )", [f"{rep_rule} ::= {rep}"]


def tool_rule_name(tool: str) -> str:
    return f"t_{_rule_name(tool)}"


# ── Terminal calls end the turn (v9.24.6) ─────────────────────────────
#
# A turn may hold several calls (parallel reads, ``reply`` then
# ``complete``), but a TERMINAL tool (``Tool.terminal`` — ``complete``,
# ``run_skill``) ends it: dispatch runs it and never looks at what follows.
# So a call after a terminal one is meaningless — and it is the one shape the
# harness cannot rescue. Live board run (2026-09-27): a resident agent, after
# its ``reply``, emitted ``complete`` and then, instead of EOS, another
# ``<tool_call>`` — five ``complete``s with reworded results, drifting into
# blank ``message`` calls — until the 32K output cap, 25 minutes later. The
# retry finished in one call. Here the only continuation after a terminal
# call's close is whitespace and EOS.


def terminal_tools(names) -> frozenset[str]:
    """Which of ``names`` end a turn — read from ``Tool.terminal``, the same
    declaration dispatch branches on, so the grammar and the loop cannot
    disagree. Names the registry does not know (MCP, test fakes) are not
    terminal."""
    from agent_cli.tools.registry import TOOLS

    return frozenset(
        n for n in names if getattr(TOOLS.get(n), "terminal", False) is True
    )


def call_sequence_rules(seq: str, names: list[str], *, call, sep: str) -> list[str]:
    """Rules for ``seq``: one or more calls, a terminal call only LAST.

    ``call(alts)`` wraps a ``|``-joined set of tool rules into this format's
    single-call shape (xml: the ``<tool_call>`` envelope; json: the op
    object as is). ``sep`` is what goes between two calls. With no terminal
    tool the result is exactly the former ``call ( sep call )*``."""
    term = terminal_tools(names)
    plain = [tool_rule_name(n) for n in names if n not in term]
    final = [tool_rule_name(n) for n in names if n in term]
    lines = []
    if plain:
        lines.append(f"{seq}_p ::= {call(' | '.join(plain))}")
    if final:
        lines.append(f"{seq}_t ::= {call(' | '.join(final))}")
    if plain and final:
        lines.insert(
            0,
            f"{seq} ::= {seq}_t | {seq}_p ( {sep}{seq}_p )* ( {sep}{seq}_t )?",
        )
    elif plain:
        lines.insert(0, f"{seq} ::= {seq}_p ( {sep}{seq}_p )*")
    else:
        lines.insert(0, f"{seq} ::= {seq}_t")
    return lines
