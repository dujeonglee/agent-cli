# Originally ported from minish.ai/Agent-tools tsindex.py (Apache 2.0).
# See NOTICE for the list of modifications.
"""Markdown heading walker — the 5th-kind `section` symbol producer.

Markdown is the one language where code_index emits ``kind='section'``
rather than function/type/variable/constant. Each heading (ATX `##`
or setext `Title\\n---`) becomes one Symbol whose body spans from the
heading line through the line before the next same-or-higher-level
heading (or EOF). Parent chain is the most recent strictly-shallower
heading.

No refs are emitted — Markdown link/anchor parsing is out of scope for
now (see docs/code-index/DESIGN.md §2 non-goals).

ATX form example::

    ## Setup           → name='Setup',    level=2, kind_raw='atx_heading_2'
    ### Install        → name='Install',  level=3, kind_raw='atx_heading_3',
                                                   parent='Setup'

Setext form example::

    Title              → name='Title',    level=1, kind_raw='setext_heading_1'
    =====

For the `mode='fetch'` shortcut in the code_index tool, the lookup
layer accepts either ``Setup`` (the canonical `name`) or ``## Setup``
(the original marker form); both resolve to the same symbol. That
marker-stripping happens in the tool layer, not here — the walker
output uses the canonical name.
"""

from __future__ import annotations

from agent_cli.code_index.languages import LANGUAGES, LangSpec, noop_preprocess
from agent_cli.code_index.languages._shared import text
from agent_cli.code_index.schema import Symbol


def _lang_markdown():
    import tree_sitter_markdown
    from tree_sitter import Language

    return Language(tree_sitter_markdown.language())


def _atx_level(node) -> int:
    """Return the heading level for an `atx_heading` node, or 0 if unknown.

    The atx_heading carries an ``atx_h<N>_marker`` child where N is 1..6.
    """
    for c in node.children:
        ct = c.type
        if ct.startswith("atx_h") and ct.endswith("_marker"):
            try:
                return int(ct[len("atx_h") : -len("_marker")])
            except ValueError:
                return 0
    return 0


def _setext_level(node) -> int:
    """Return the heading level for a `setext_heading` node, or 0.

    setext h1 is underlined with ``=`` (`setext_h1_underline`), h2 with
    ``-`` (`setext_h2_underline`).
    """
    for c in node.children:
        if c.type == "setext_h1_underline":
            return 1
        if c.type == "setext_h2_underline":
            return 2
    return 0


def _heading_text(node, src: bytes) -> str:
    """Return the heading's visible text — markers stripped, single line.

    For ATX (`## Setup ##`) we drop leading/trailing `#` runs and
    whitespace. For setext we take the first content line (the
    underline is on the next line).
    """
    raw = text(node, src).splitlines()[0]
    return raw.lstrip("# ").rstrip(" #").strip()


def _heading_signature(node, src: bytes) -> str:
    """Original heading line including marker(s) — used by mode='fetch'."""
    return text(node, src).splitlines()[0].rstrip()


def walk_definitions(root, src: bytes, rel: str, syms: list):
    """Collect every heading as a `kind='section'` Symbol.

    Two passes:

    1. Walk the tree (recursive) and gather (line, level, name, signature,
       kind_raw, col) for every heading in document order.
    2. Compute parent and end_line by sweeping the linear list with a
       heading stack — entries with level >= the current heading's level
       are popped (their sections end at the heading's start line minus
       one), then the top of the stack is the new heading's parent.
    """

    # Step 1 — collect headings.
    # Each entry: (start_line, col, level, name, signature, kind_raw, node).
    headings: list[tuple[int, int, int, str, str, str, object]] = []

    def collect(node) -> None:
        nt = node.type
        if nt == "atx_heading":
            lvl = _atx_level(node)
            if lvl > 0:
                headings.append(
                    (
                        node.start_point[0] + 1,
                        node.start_point[1],
                        lvl,
                        _heading_text(node, src),
                        _heading_signature(node, src),
                        f"atx_heading_{lvl}",
                        node,
                    )
                )
        elif nt == "setext_heading":
            lvl = _setext_level(node)
            if lvl > 0:
                headings.append(
                    (
                        node.start_point[0] + 1,
                        node.start_point[1],
                        lvl,
                        _heading_text(node, src),
                        _heading_signature(node, src),
                        f"setext_heading_{lvl}",
                        node,
                    )
                )
        for c in node.children:
            if c.is_named:
                collect(c)

    collect(root)

    if not headings:
        return

    # Step 2 — compute parents and end_lines.
    # Stack entries: (level, name) — parent of a new heading is the name
    # on top of the stack after popping >= entries. end_line is filled
    # in a second forward sweep using the same level rule (next heading
    # with level <= current's level).
    total_lines = len(src.splitlines()) or 1

    # end_line via next-same-or-higher-level lookahead.
    end_lines: list[int] = []
    for i, (start_line, _col, level, _name, _sig, _raw, _node) in enumerate(headings):
        end = total_lines
        for j in range(i + 1, len(headings)):
            nxt_start, _, nxt_level, _, _, _, _ = headings[j]
            if nxt_level <= level:
                end = nxt_start - 1
                break
        end_lines.append(end)

    # parent via stack sweep.
    stack: list[tuple[int, str]] = []
    parents: list[str | None] = []
    for _start_line, _col, level, name, _sig, _raw, _node in headings:
        while stack and stack[-1][0] >= level:
            stack.pop()
        parents.append(stack[-1][1] if stack else None)
        stack.append((level, name))

    for i, (start_line, col, level, name, sig, raw, _node) in enumerate(headings):
        syms.append(
            Symbol(
                name=name,
                kind="section",
                file=rel,
                line=start_line,
                col=col,
                end_line=end_lines[i],
                is_definition=True,
                language="markdown",
                kind_raw=raw,
                modifiers=[f"level={level}"],
                parent=parents[i],
                signature=sig,
            )
        )


def walk_refs(
    root,
    src: bytes,
    rel: str,
    refs: list,
    defined_names: set,
    identifiers_out: set,
    language: str = "markdown",
):
    """Markdown emits no refs.

    Kept as a method to match the LangSpec walker protocol. Link/anchor
    resolution is documented as out-of-scope in DESIGN §2; revisiting it
    would mean adding a `kind='link'` REF_KIND, which we explicitly
    decided against for the initial port.
    """
    return


LANGUAGES["markdown"] = LangSpec(
    name="markdown",
    exts=(".md", ".markdown"),
    grammar_factory=_lang_markdown,
    walk_definitions=walk_definitions,
    walk_refs=walk_refs,
    preprocess=noop_preprocess,
)
