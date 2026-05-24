# Originally ported from minish.ai/Agent-tools tsindex.py (Apache 2.0).
# See NOTICE for the list of modifications.
"""Schema constants and data shapes shared across the code_index package.

The agent-cli port bumps `SCHEMA_VERSION` to 1 (independent versioning
from the upstream tsindex's 4) and introduces a fifth kind, `section`,
for non-code structural symbols (markdown headings now; other structured
docs in the future). The 4-vocab → 5-vocab change is the only
schema-level deviation from the upstream walker output contract; the
SQLite DDL itself (see store.py) remains identical.

Kinds
-----

`NAME_KINDS` — the closed set of values written to `symbols.kind`:

    function   callable definitions (function, method, lambda, fn-like macro)
    type       shape/contract definitions (class, struct, typedef, interface, trait)
    variable   runtime storage (globals, class fields, mutable bindings)
    constant   compile-time constants (#define X 5, const N = 10, UPPER_SNAKE)
    section    document structural symbol (markdown heading — new)

`REF_KINDS` — the closed set of values written to `refs.kind`:

    call       invocation site (`X(...)` form)
    name       bare identifier mention (callback, function pointer, macro arg)
    type       identifier in type position (var/param type, generic arg)

The split exists because the same defined symbol can appear at multiple
ref sites with different roles, and queries like "who *calls* this"
(ref_kind=call) need to be distinguishable from "where is this name
mentioned anywhere" (no filter) or "where is this used as a callback"
(ref_kind=name).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Bumped when the on-disk symbol/ref shape produced by walkers changes
# in a way that older indexes cannot be queried by current code. Loading
# an index whose meta.schema_version mismatches forces a full rebuild
# (see builder.build / store.IndexStore).
SCHEMA_VERSION = 1

# Closed set of values for `symbols.kind`. Adding to this set is an
# intentional design decision (e.g. adding 'section' for markdown) and
# requires bumping SCHEMA_VERSION when downstream tooling expects the
# new value.
NAME_KINDS: frozenset[str] = frozenset(
    {"function", "type", "variable", "constant", "section"}
)

# Closed set of values for `refs.kind`. See module docstring for
# semantics. Unlike NAME_KINDS this has not changed from the upstream
# tsindex schema.
REF_KINDS: frozenset[str] = frozenset({"call", "name", "type"})


@dataclass
class Symbol:
    """A definition or declaration site.

    `kind` ∈ NAME_KINDS. `kind_raw` preserves the original tree-sitter
    node name (e.g. 'preproc_function_def', 'arrow_function',
    'atx_heading_2') so finer-grained queries are still possible without
    polluting the normalized vocabulary.

    `is_definition` is False for C/Java/Rust forward declarations and
    similar header-only declarations; the symbol is still recorded so
    callgraph and refs can resolve names.
    """

    name: str
    kind: str
    file: str
    line: int
    col: int
    end_line: int
    is_definition: bool
    language: str

    # Optional / language-specific enrichment. None when not applicable.
    kind_raw: Optional[str] = None
    modifiers: Optional[list[str]] = None
    parent: Optional[str] = None
    signature: Optional[str] = None
    return_type: Optional[str] = None
    enum_values: Optional[list[str]] = None
    params: Optional[list[str]] = field(default=None)


@dataclass
class Ref:
    """A usage site.

    `kind` ∈ REF_KINDS (call/name/type). One defined `Symbol` may have
    many `Ref`s; one Ref points to exactly one name in one location.
    """

    name: str
    kind: str
    file: str
    line: int
    col: int
    language: str
