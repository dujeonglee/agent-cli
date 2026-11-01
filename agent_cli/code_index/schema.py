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
    registration  a function stored by a brace initializer — how C fills
               tables of function pointers (`.probe = foo_probe`, `{ f, g }`);
               set by the C resolver when the stored name resolves to a
               function (v10.98.0, docs/code-index/DESIGN.md §22)

Name resolution (v10.96.0, schema v4, docs/code-index/DESIGN.md §21)
--------------------------------------------------------------------

Every ref is resolved once at build time and the result is stored:
`RESOLUTIONS` — `resolved` (one indexed definition, `refs.target_id`),
`local` (a parameter, local variable, class attribute … — not an edge),
`candidates` (matched by name only; the count is taken at query time),
`external` (builtins, modules outside the index, no indexed definition of
that name), `unresolved` (no binding found — usually a parse error). The
scope tree (`scopes`) and what each scope binds (`bindings`) are stored so
the resolution of one file can be redone from the tables alone.

The split exists because the same defined symbol can appear at multiple
ref sites with different roles, and queries like "who *calls* this"
(ref_kind=call) need to be distinguishable from "where is this name
mentioned anywhere" (no filter) or "where is this used as a callback"
(ref_kind=name).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Bumped when the on-disk symbol/ref shape produced by walkers changes
# in a way that older indexes cannot be queried by current code. Loading
# an index whose meta.schema_version mismatches forces a full rebuild
# (see builder.build / store.IndexStore).
#
# v2: added `qualified_name` column. Walker emits the full display form
#     (Python/JS/TS/Java/Go/Rust/Markdown joined by '.', C++ joined by
#     '::', C is flat → equals name). Tool handlers query this column
#     first and fall back to `name` for bare-leaf inputs. See
#     docs/code-index/DESIGN.md for the rationale.
# v3: files.mtime_ns (v10.93.0).
# v4: scopes · bindings · resolve_deps tables, refs.scope_id/fn_symbol_id/object/target_id/
#     resolution, symbols.bases; symbols.params dropped (parameters are
#     bindings now). Symbol ids survive incremental builds (v10.96.0).
SCHEMA_VERSION = 4

# Closed set of values for `symbols.kind`. Adding to this set is an
# intentional design decision (e.g. adding 'section' for markdown) and
# requires bumping SCHEMA_VERSION when downstream tooling expects the
# new value.
NAME_KINDS: frozenset[str] = frozenset(
    {"function", "type", "variable", "constant", "section"}
)

# Subset of NAME_KINDS that participates in cross-file code reference
# resolution. The builder's Pass-2 only adds names of these kinds to the
# `defined_names` set that walk_refs consults — so a Markdown heading
# `## Setup` does NOT cause every Python file mentioning `Setup` to emit
# a `kind='name'` ref pointing at the heading. Section symbols are still
# stored in the index (lookup/kind/file modes return them) — they just
# don't pollute the cross-file ref graph.
CODE_NAME_KINDS: frozenset[str] = NAME_KINDS - {"section"}

# Closed set of values for `refs.kind`. See module docstring for
# semantics. Unlike NAME_KINDS this has not changed from the upstream
# tsindex schema.
REF_KINDS: frozenset[str] = frozenset({"call", "name", "type", "registration"})

#: `refs.resolution` 의 값 (v10.96.0, DESIGN §21).
RESOLUTIONS: frozenset[str] = frozenset(
    {"resolved", "local", "candidates", "external", "unresolved"}
)

#: 범위 규칙이 있는 언어 — 나머지는 함수 범위·인자만 알고 참조는 이름으로만 맞춘다
#: (`candidates`). 언어가 규칙을 얻으면 여기에 더한다.
SCOPE_RESOLVED_LANGUAGES: frozenset[str] = frozenset({"python", "c"})


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
    kind_raw: str | None = None
    modifiers: list[str] | None = None
    parent: str | None = None
    signature: str | None = None
    return_type: str | None = None
    enum_values: list[str] | None = None
    # Parameter names — not stored as a column since v4; the builder turns them
    # into `param` bindings of the function's scope.
    params: list[str] | None = field(default=None)
    # Base-class expressions as written (Python `class K(a.B, C)` → ["a.B", "C"]).
    bases: list[str] | None = None

    # Full display form — what `list` output shows and what the model
    # most naturally passes back. For symbols WITHOUT a parent this
    # equals `name`. For nested symbols this joins parent + native
    # separator + name:
    #
    #   Python / JS / TS / Java / Go / Rust / Markdown : "."  (e.g. "Outer.method")
    #   C++                                            : "::" (e.g. "ns::Foo::bar")
    #   C                                              :  flat → same as `name`
    #
    # Walkers compute this once at emit time. Tool handlers look up by
    # this field first and fall back to `name` for bare-leaf inputs.
    # refs↔defs matching still uses `name` (bare), because refs are
    # extracted from raw source identifiers.
    qualified_name: str = ""


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
    # v4: the innermost scope the ref sits in (an index into the walker's scope
    # list for that file), and `object`: for a `call` ref, the object of
    # `obj.m()` (the chain text "a.b", "<expr>" when it is not a plain
    # name/attribute chain); for a C name stored by a brace initializer
    # (v10.98.0), its designator (".probe", "[]" for a positional value) — it
    # starts with "." or "[" and the ref kind is not `call`; for a C `type` ref
    # naming a struct/union/enum tag, the keyword ("struct") — tags and typedef
    # names are separate namespaces. A member call is therefore
    # `kind = 'call' AND object IS NOT NULL`.
    scope: int | None = None
    object: str | None = None


@dataclass
class Scope:
    """A lexical scope found by a walker (v4). ``parent`` and the ids are
    indexes into the walker's list for one file; the builder turns them into
    table ids. ``def_line``/``def_name`` name the definition that owns the
    scope (a function or class), so the builder can link ``scopes.symbol_id``
    to the symbol Pass-1 emitted on that line."""

    kind: str  # module | class | function | comprehension | block
    start_line: int
    end_line: int
    parent: int | None = None
    def_line: int | None = None
    def_name: str | None = None


@dataclass
class Binding:
    """A name bound in a scope (v4). ``kind`` ∈ param | local | import | def |
    class | global | nonlocal, and for C (v10.98.0) also enumerator | tag |
    typedef | decl (``languages/c.py`` ``walk_scopes``). Imports keep the module
    text and, for ``from m import x``, the imported name (``"*"`` for a star
    import). ``symbol_key`` — ``(line, name, kind_raw)`` of the Pass-1 symbol the
    binding stands for, when (line, name) alone is ambiguous (C: an enumerator
    stands for its enum, ``typedef struct foo {…} foo;`` puts two symbols named
    ``foo`` on one line)."""

    scope: int
    name: str
    kind: str
    line: int
    import_module: str | None = None
    import_name: str | None = None
    symbol_key: tuple | None = None
