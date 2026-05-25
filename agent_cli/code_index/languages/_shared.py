# Originally ported from minish.ai/Agent-tools tsindex.py (Apache 2.0).
# See NOTICE for the list of modifications.
"""Cross-walker helpers.

Currently holds only `text(node, src)`, which every walker module uses
to slice the raw source bytes by a tree-sitter node's byte range. Lives
here so the walkers stay free of cross-imports between languages.
"""

from __future__ import annotations


def text(node, src: bytes) -> str:
    """Return the raw source spanned by a tree-sitter node as a string.

    Decodes UTF-8 with `errors='replace'` so a stray invalid byte in a
    preprocessed C file does not crash the walker. The walkers treat
    these results as syntactically meaningful (identifier names,
    signatures) — the replacement character is acceptable as a last
    resort because we still get a stable string for downstream code.
    """
    return src[node.start_byte : node.end_byte].decode("utf-8", "replace")


def qualify(parent: str | None, name: str, sep: str = ".") -> str:
    """Compose a symbol's display form from its parent chain + leaf name.

    Used by every walker to fill the ``qualified_name`` field on Symbol.
    The ``parent`` value is already a chain in the walkers that nest
    (Python / JS / TS / Java join with '.', C++ with '::') so this
    helper just concatenates one more level on top.

    For top-level symbols (parent is None) the qualified form equals
    the bare name.
    """
    return f"{parent}{sep}{name}" if parent else name
