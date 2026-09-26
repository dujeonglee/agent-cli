"""Decoding grammars — the wire shape as an EBNF the server can enforce.

v9.24.0. The text protocol asks the model for a shape (system prompt) and
repairs what comes back (recovery layer). A server that supports
grammar-constrained decoding (omlx/xgrammar, vLLM, llama.cpp) can instead
mask every token that would leave the shape, so a malformed emission never
exists: no ``<tool_call>`` skeleton stutter, no unknown tool, no reasoning
runaway inside the body (Harbor extract-elf: 32K characters of analysis
inside an open ``<tool_call>``). Prevention where it is available; the
recovery layer stays for servers that cannot enforce.

Each wire format renders its own grammar (:meth:`WireFormat.grammar`) from
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


def _not_containing_alts(literal: str, extra_forbidden: str = "") -> str:
    """The alternatives (one step of text) that cannot complete ``literal``:
    a character that cannot start it, or a proper prefix of it followed by a
    character that breaks it. Wrapped by the callers in ``( )*`` (unbounded)
    or ``( ){0,n}`` (bounded)."""
    first = literal[0]
    alts = [f"[^{_cls(first + extra_forbidden)}]"]
    for i in range(1, len(literal)):
        prefix = literal[:i]
        nxt = literal[i]
        alts.append(f'"{_esc(prefix)}" [^{_cls(nxt + extra_forbidden)}]')
    return " | ".join(alts)


def not_containing(name: str, literal: str, extra_forbidden: str = "") -> str:
    """Rule ``name`` = any text that does not contain ``literal``.

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


def think_prefix(thinking_open: bool) -> tuple[str, str]:
    """``(root-prefix, rules)`` for a generation that starts inside an open
    ``<think>`` block (the chat template opened it; the model must close it
    before the visible turn). When ``thinking_open`` is False the template
    pre-closed the block and the visible turn starts immediately.

    Verified live: with thinking on and a grammar that lacks this prefix,
    the whole constrained output stays inside the think channel.
    """
    if not thinking_open:
        return "", ""
    return 'think "</think>\\n\\n" ', not_containing("think", "</think>")


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


def tool_rule_name(tool: str) -> str:
    return f"t_{_rule_name(tool)}"
