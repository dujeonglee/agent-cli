# code_index — agent-cli native code index tool (DESIGN)

**Status**: design accepted, implementation pending (PR-1/2/3)
**Date**: 2026-05-24
**Owner**: Dujeong Lee
**Supersedes**: `agent_cli/tools/symbols.py` (read_symbols), to be removed in PR-3

This document is the contract for porting `tsindex.py` from
`minish.ai/Agent-tools` into agent-cli as a native tool. It captures every
decision that came out of the design conversation so the actual
implementation can proceed without re-litigating choices.

---

## 1. Goals

- Replace the per-call tree-sitter `read_symbols` tool with a **persistent
  SQLite-backed code index** that supports cross-file symbol/ref lookup,
  call graph queries, and LLM-context "slice" rendering.
- Surface the capability as a **single native tool** (`code_index`) with
  mode dispatch, so the model has one entry point.
- Keep the agent-cli on-prem ethos: stdlib SQLite for storage, per-language
  tree-sitter grammars as the only model-side dependency, no external
  services.

## 2. Non-goals

- LSP-level semantic accuracy (this is a syntactic indexer, like the
  original tsindex).
- Multi-root federated index. One project = one DB.
- Watcher-based freshness. Freshness comes from `sha1` recompute on query
  plus a post-hook from agent-cli's own `edit_file`/`write_file`.
- IDE-style incremental parsing within a file. Full file reparse on change.
- Markdown link/anchor refs (`[text](#anchor)`, `[[wikilink]]`). Headings
  only.

---

## 3. Tool surface

Single native tool: **`code_index`**. Mode dispatch:

| mode | params | returns |
|---|---|---|
| `list` | `path` | File outline (definitions + structural symbols with line ranges). Same role as current `read_symbols mode='list'`. |
| `fetch` | `path`, `name` | Body of one symbol from that file. Output uses `LINE#HASH:` hashline format so `edit_file` accepts it directly. |
| `lookup` | `name`, `kind?` | All definitions/declarations across the index for that name. |
| `kind` | `kind` | All symbols of that kind across the index. |
| `file` | `path` | All symbols defined in that file (index-scoped). |
| `refs` | `name`, `kind?` (`call`/`name`/`type`) | All ref sites for that name. |
| `callers` | `name` | Functions that call this one. |
| `callees` | `name` | Functions called by this one. |
| `slice` | `name`, `with_callees?`, `with_callers?`, `with_types?`, `with_macros?`, `depth?`, `max_bytes?` | Markdown blob: def body + optional context. Hashline-wrapped. |
| `build` | (none) | Force a full rebuild. Normally not needed — lazy build covers it. |

Each mode gets its own short paragraph in the inline guide. The schema
keeps unused params optional per-mode.

### 3.1 Rationale for single tool

- Reuses the current `read_symbols` mental model (mode dispatch).
- One entry point keeps the tool list short — the model only has to learn
  one name.
- Tradeoff accepted: the inline guide is longer than per-tool guides
  would be.

---

## 4. Index lifecycle

### 4.1 Storage

- DB path: `<project_root>/.agent-cli/code_index.db`
- "Project root" = current working directory at AgentLoop boot, or the
  nearest ancestor that already contains `.agent-cli/`.
- DB is git-ignored (already covered by existing `.agent-cli/` `.gitignore`
  pattern).

### 4.2 Build trigger

- **Lazy**. First mode that needs the index triggers a build.
- `mode='list' path=X` and `mode='fetch' path=X name=...`:
  - If `X` is inside the indexed root → consult the index (and trigger
    build/refresh first).
  - If `X` is outside → **on-demand parse fallback** (single-file tree-sitter
    parse; results not persisted).
- All cross-file modes (`lookup`, `kind`, `refs`, `callers`, `callees`,
  `slice`) require the index. They never fall back, because the question
  itself is index-scoped.

### 4.3 Freshness

Two-layer freshness:

1. **Per-query incremental** (default tsindex behavior). On every query
   that touches the index, recompute sha1 of all source files under the
   root and incrementally rebuild only the changed ones. Cost on 265 files
   ≈ 50ms.
2. **Edit/write post-hook**. After `edit_file` or `write_file` succeeds
   on a path inside the indexed root, schedule an incremental update for
   *that one file* immediately. Next query is already fresh; the per-query
   sha1 scan still acts as a safety net for external edits.

The watcher option (`watchdog`) was rejected — adds a runtime dependency
and breaks the on-prem minimal-deps invariant.

### 4.4 Invalidation

Inherited from tsindex:

- `meta.schema_version != SCHEMA_VERSION` → full rebuild.
- `meta.root != current_root` → full rebuild.
- `meta.preproc_fingerprint != current` → full rebuild (covers
  `tsindex.defs` changes, auto-undef CONFIG\_\* set changes, etc.).

`SCHEMA_VERSION` gets bumped from tsindex's `4` to a fresh `1` (this is a
new package), reflecting the addition of `kind='section'` and any
agent-cli-specific schema deltas.

---

## 5. Schema

### 5.1 4 → 5 vocab

```
kind ∈ {function, type, variable, constant, section}
                                          ^^^^^^^ NEW
```

`section` represents non-code structural symbols (markdown headings now;
rst/org/asciidoc headings in the future if added). Adding the vocab is
preferred over "stuff it into `type`" because:

- Model-facing semantics stay honest (`kind=section` reads as "document
  section", not "type").
- SQL filters (`WHERE kind='section'`) become first-class instead of
  relying on `language='markdown'` join.
- One bump, then stable. The original 4-vocab was not a hard invariant —
  it's a "we needed 4" decision, not "we must never have 5".

### 5.2 Rest of schema

Inherited verbatim from tsindex (see Agent-tools/PORTING.md §2.2 for the
full SQL DDL):

- `files` table: `path PK, size, lines, sha1, has_error, n_symbols,
  identifiers JSON, language`.
- `symbols` table: `id PK, name, kind, file, line, col, end_line,
  is_definition, language, kind_raw, modifiers JSON, parent, signature,
  return_type, enum_values JSON, params JSON`.
- `refs` table: `id PK, name, kind ∈ {call, name, type}, file, line, col,
  language`.
- `meta` table: `key PK, value` (schema_version, root, built_at,
  preproc_fingerprint, preprocessing JSON).

Indexes on `symbols.name`, `symbols.kind`, `symbols.file`,
`symbols.language` and on the corresponding `refs.*` columns.

### 5.3 Markdown walker — concrete output

For each heading (`#`, `##`, `###`, …):

| field | value |
|---|---|
| `name` | heading text, marker stripped (`Setup`) |
| `kind` | `section` |
| `kind_raw` | `atx_heading_2` / `setext_heading_1` etc. (raw tree-sitter node + level) |
| `file` | `docs/install.md` |
| `line` / `col` / `end_line` | heading line through line before next same-or-higher-level heading (or EOF) |
| `is_definition` | `True` |
| `language` | `markdown` |
| `parent` | enclosing heading text (e.g. `Setup`'s parent is `# Top`) |
| `modifiers` | `["level=2"]` |
| `signature` | full original heading line including marker (`## Setup`) |
| `return_type` / `enum_values` / `params` | `null` |

No `refs` are emitted for markdown.

Lookup convenience: `mode='fetch'` accepts both `name="Setup"` and
`name="## Setup"` (marker auto-stripped before lookup). `mode='lookup'`
matches the canonical `name` (no marker).

---

## 6. Supported languages

Inherited from tsindex (8), plus markdown:

| language | extensions | grammar dep |
|---|---|---|
| C | `.c`, `.h` | `tree-sitter-c` (via `tree-sitter-cpp`'s c grammar — TBD during port; current `read_symbols` reuses cpp grammar for `.c`/`.h`) |
| C++ | `.cpp`, `.cc`, `.cxx`, `.hpp`, `.hh`, `.hxx`, `.h++`, etc. | `tree-sitter-cpp` |
| Python | `.py`, `.pyi` | `tree-sitter-python` |
| Go | `.go` | `tree-sitter-go` (NEW dep) |
| Rust | `.rs` | `tree-sitter-rust` (NEW dep) |
| Java | `.java` | `tree-sitter-java` (NEW dep) |
| JavaScript | `.js`, `.jsx`, `.mjs`, `.cjs` | `tree-sitter-javascript` |
| TypeScript | `.ts`, `.tsx` | `tree-sitter-typescript` |
| Markdown | `.md`, `.markdown` | `tree-sitter-markdown` |

The exact tree-sitter package situation for C will be settled during
PR-1 — current `read_symbols` uses `tree-sitter-cpp` for both, while
tsindex uses dedicated `tree-sitter-c`. Whichever is more reliable on
Linux wheels stays.

---

## 7. C/C++ preprocessing

Ported verbatim from tsindex:

- `preprocess_source()` and the rewrite chain (variadic macro, foreach,
  decl macros, bare attributes, ifdef-zero, define comments, pp trailing
  whitespace, consecutive attrs, pp continuations, type-arg macros).
- `tsindex.defs` config file (kernel-style `#define X 1` / `#define Y 0`
  lines) consumed by `unifdef`.
- Auto-undef of unknown `CONFIG_*` macros (`undef_unknown_configs=True`
  default).
- `unifdef` is a **required system dependency** when working on C/C++
  codebases. agent-cli runs without it for non-C codebases; if a `.c`/`.h`
  file is encountered and `unifdef` is missing, the build falls back to
  raw parse with a warning. README will instruct
  `brew install unifdef` / `apt install unifdef`.

---

## 8. Package structure

```
agent_cli/code_index/
├── __init__.py              # public API: build, load_index, IndexStore
├── schema.py                # SCHEMA_VERSION, Symbol/Ref dataclass, NAME_KINDS
├── builder.py               # build(), Pass-1 (defs) / Pass-2 (refs), incremental
├── store.py                 # IndexStore class, normalize_file_path, query methods
├── callgraph.py             # build_callgraph + helpers
├── slice.py                 # cmd_slice (LLM markdown blob renderer)
├── preproc.py               # unifdef driver + rewrite chain + fingerprint
└── languages/
    ├── __init__.py          # LANGUAGES registry, LangSpec dataclass, lazy factories
    ├── c.py
    ├── cpp.py
    ├── python.py
    ├── go.py
    ├── rust.py
    ├── java.py
    ├── javascript.py
    ├── typescript.py
    └── markdown.py          # NEW

agent_cli/tools/
└── code_index.py            # tool entry: registry schema, mode dispatch,
                             # on-demand fallback, hashline wrapper

tests/code_index/
├── __init__.py
├── fixtures/
│   ├── c/, cpp/, python/, go/, rust/, java/, js/, ts/, markdown/
├── test_c.py                # walker correctness per language
├── test_cpp.py
├── test_python.py
├── test_go.py
├── test_rust.py
├── test_java.py
├── test_javascript.py
├── test_typescript.py
├── test_markdown.py
├── test_path_normalize.py
├── test_builder.py          # incremental, invalidation, sha1 path
├── test_store.py            # IndexStore queries, find_refs_in_range
├── test_callgraph.py
├── test_slice.py
├── test_preproc.py          # unifdef + rewrite chain
├── test_property_walkers.py # hypothesis-based fuzz for walkers
├── test_tool_dispatch.py    # code_index tool mode routing
├── test_tool_on_demand.py   # path outside root → on-demand parse
├── test_tool_hashline.py    # hashline wrapper correctness
└── test_post_hook.py        # edit/write tool → incremental update wiring
```

Every ported file in `agent_cli/code_index/` carries this header:

```python
# Originally ported from minish.ai/Agent-tools tsindex.py (Apache 2.0).
# See NOTICE for the list of modifications.
```

---

## 9. On-demand parse fallback

For `mode='list'` and `mode='fetch'` with a `path` outside the indexed
root:

1. Detect: `path.resolve()` not under `meta.root`.
2. Parse that single file with the appropriate `LangSpec.walk_definitions`
   on the spot. No `walk_refs` (cross-file refs aren't meaningful here
   anyway).
3. Render as if it had come from the index. No persistence.
4. Modes other than `list`/`fetch` return a clear error ("only index-scoped
   for cross-file queries — file is outside the indexed root").

Performance: a single Python/C file parses in 5-50ms. Acceptable as a
fallback; the design accepts that we're paying parse cost on every
out-of-root query.

---

## 10. Hashline format compatibility

`edit_file` consumes the `LINE#HASH:content` format. To keep
`code_index fetch` → `edit_file` workflow intact:

- `mode='fetch'` output: each body line is rendered as `{line:>5}#{hash6}:{content}`,
  same scheme as the current `read_symbols` fetch.
- `mode='slice'` output: each per-section body block uses the same
  hashline format; the markdown headers (`### name — file:lines`) sit
  outside the hashlines, like the current `read_symbols` fetch header.
- `mode='list'` is plain outline output (line numbers + symbol names) —
  no hashlines, same as today.

The hash function is reused from `read_file`/`read_symbols` (existing
`_hash_line` in `agent_cli/tools/`).

---

## 11. Migration plan — three PRs on `feat/code-index`

All three PRs leave the tree on green (`pytest -m 'not ollama_integration'`,
`ruff check`, `ruff format --check`). Each is a single logical commit.

### PR-1 — port code_index package + tests

- Add `agent_cli/code_index/` package (schema/builder/store/callgraph/slice/preproc + per-lang walkers).
- Add `tests/code_index/` suite (per-language fixtures + system integration + property-based).
- Add `LICENSE` (Apache 2.0) and `NOTICE` at repo root.
- pyproject: add `tree-sitter-go`, `tree-sitter-rust`, `tree-sitter-java`.
- README/ARCHITECTURE: **no change yet** — read_symbols still owns the model-facing docs.
- `code_index` is not yet wired into the tool registry. It's importable
  and tested but the LLM can't reach it.

### PR-2 — register `code_index` tool + lifecycle wiring

- `agent_cli/tools/code_index.py`: ToolSchema, mode dispatch, on-demand
  fallback, hashline wrapper.
- `tools/__init__.py` + `tools/registry.py`: register `code_index`.
- `_file_extract.py` `_PATH_TOOLS`: add `code_index` (both tools listed).
- `system_prompt.py`:
  - Add `_build_code_index_inline` (per-mode short guide).
  - `read_file`'s flow steering branches: if `code_index` active, route
    supported extensions to `code_index mode='list'` (parallel to current
    read_symbols branch).
  - `get_supported_extensions()` moves to `code_index/languages/__init__.py`,
    re-exported from current location to keep backward compat for read_symbols.
- `edit_file`/`write_file` post-hook: on success, if path inside index
  root, call `code_index.builder.incremental_update_file(path)`.
- `read_symbols` and `code_index` coexist in this PR. Either tool works;
  guides describe both.
- ARCHITECTURE.md: add section documenting `code_index`. Keep
  `read_symbols` section.
- README: add `code_index` usage. Keep `read_symbols` usage.

### PR-3 — remove read_symbols + cleanup

- Delete `agent_cli/tools/symbols.py`.
- Delete `tests/test_symbols.py`.
- `tools/__init__.py`: drop `tool_read_symbols` import/registration.
- `tools/registry.py`: drop `read_symbols` ToolSchema.
- `_file_extract.py`: drop `"read_symbols"` from `_PATH_TOOLS`.
- `system_prompt.py`: drop `_build_read_symbols_inline` and all
  `read_symbols`-aware branches in `_build_read_file_inline`. The
  fallback `get_supported_extensions` re-export at the old path is also
  removed.
- `read_file.py`: drop the `read_symbols`-mentioning comment.
- README: remove `read_symbols` section.
- ARCHITECTURE.md: remove `read_symbols` references, update LOC numbers.
- Tests: clean up `test_context_compaction`, `test_system_prompt`,
  `test_tools_coverage` references.

---

## 12. Test strategy

This is going to be the most-used tool in agent-cli, so tests get
top-tier coverage.

### 12.1 Per-language walker tests (`test_<lang>.py`)

Each language fixture covers:

- Top-level definitions (function, class/type, variable, constant).
- Nested definitions (method on a class, function inside function, inner
  class).
- Declaration vs definition (C function prototype, Java abstract method,
  Rust trait method).
- Generics / templates (C++ template, Rust generic, TS generic, Java
  generic).
- Async / variadic / default params.
- Receiver/qualified names (Go receiver, Rust impl block, C++
  `ns::Class::method`, Python `self`).
- Refs: `call`, `name` (function pointer / callback), `type`.
- Edge case: same-name function in two files.
- Error files: tree-sitter `has_error=True` recorded but build doesn't
  crash.

Markdown additionally covers:

- ATX (`## Setup`) and setext (`Setup\n-----`) styles.
- All 6 heading levels.
- Nested parent chain (`# A > ## B > ### C` → C's parent is B).
- `end_line` calculation across same-level, deeper, and shallower
  next-headings.
- Code blocks inside section don't get parsed as symbols.

### 12.2 Builder & invalidation (`test_builder.py`)

- Full build from scratch.
- No-op incremental (no file changes → 0 walker calls).
- 1-file modify → only that file rewalked, refs recomputed for files
  that mention names defined in the changed file (Option B re-Pass2).
- File delete: old file's symbols/refs gone.
- File rename: new path indexed, old purged.
- `schema_version` mismatch → full rebuild.
- `meta.root` change → full rebuild.
- `preproc_fingerprint` change (different `tsindex.defs`) → full rebuild.
- `force_full=True` ignores reusable index.

### 12.3 Store queries (`test_store.py`)

- `find_symbols(name=…, kind=…, file=…)` covering each filter combo.
- `find_refs(...)` likewise.
- `find_refs_in_range(file, start, end)`.
- `normalize_file_path` for: exact, absolute, basename, suffix, ambiguous
  (returns None).
- `kind_counts`, `ref_kind_counts`, `top_ref_names`.

### 12.4 Callgraph & slice

- `build_callgraph`: callees/callers/sites map shapes.
- BFS depth traversal.
- Cycle handling.
- `cmd_slice` with each combination of with_callees/callers/types/macros.
- `max_bytes` truncation.

### 12.5 Preproc (`test_preproc.py`)

- `unifdef` available: `tsindex.defs` applied correctly.
- `unifdef` missing: fallback to raw parse + warning.
- Auto-undef of unknown `CONFIG_*` keys.
- Each rewrite function (`rewrite_variadic_macros`, `rewrite_foreach`, …)
  has at least one positive and one negative case.
- Preproc fingerprint stability (same inputs → same fingerprint).

### 12.6 Tool integration (`test_tool_*.py`)

- Mode dispatch routes to correct sub-handler.
- Required-param validation per mode.
- `mode='list'` path inside root → index path.
- `mode='list'` path outside root → on-demand parse path, no DB write.
- `mode='fetch'` produces hashline output that `edit_file` accepts (round-trip test).
- `mode='lookup'` with various filters.
- `mode='refs'` `kind=call`/`name`/`type` filters.
- `mode='callers'` / `callees` with cycles.
- `mode='slice'` end-to-end.
- `mode='build'` forces full rebuild.

### 12.7 Post-hook (`test_post_hook.py`)

- `edit_file` on a tracked file → next `lookup` sees the edit.
- `write_file` adds a new file → next `lookup` sees the new symbol.
- `edit_file` outside indexed root → no-op (no DB write).
- Failed `edit_file` → no DB write.

### 12.8 Property-based (`test_property_walkers.py`)

`hypothesis` generators:

- Random small ASTs (parametrized by language) round-trip through walker
  → defined-names ⊆ identifiers reported by `walk_refs`.
- Random identifier names of varying lengths: never crash a walker, never
  produce a Symbol with `name=""`.
- Random `parent` chains in markdown headings: parent chain is well-formed
  (every parent exists or is None).

`hypothesis` added as a dev-only dep
(`[project.optional-dependencies] dev`).

### 12.9 Total tests target

Existing tsindex test suite has ~50 tests. Adding markdown, system
integration, post-hook, and property-based should put us in the **150-200
test range** for `tests/code_index/`. This is intentional — this tool is
the new core, and the surface is big.

---

## 13. Dependencies

### 13.1 Added to base `dependencies`

```
"tree-sitter-c>=0.23",          # if dedicated grammar is chosen during port
"tree-sitter-go>=0.23",
"tree-sitter-rust>=0.23",
"tree-sitter-java>=0.23",
```

(All existing `tree-sitter-*` deps stay.)

### 13.2 Added to `[project.optional-dependencies] dev`

```
"hypothesis>=6.0",
```

### 13.3 System dependency (documented in README)

- `unifdef` — required for C/C++ codebases. Skipped gracefully (raw parse)
  when missing.

### 13.4 What we did NOT add

- `watchdog` (filesystem watcher): rejected; sha1+post-hook is enough.
- `chonkie`, `model2vec`, `bm25`: alternative semantic indexing libs.
  Out of scope.

---

## 14. Out of scope (explicit non-features)

- LSP hybrid (clangd / pyright / rust-analyzer integration).
- Cross-language calls (Python calling C via cffi, JS calling Rust via
  WASM, etc.). Each language's call graph is intra-language.
- Markdown refs, link resolution, anchor checking.
- Multi-repo federated lookup. One DB = one root.
- Incremental within-file parsing (we reparse the whole file on change).
- Semantic search (vector embeddings).
- Auto-rebuild on a timer or watcher.
- Doc-comment extraction (could be added later as `signature` enrichment).

---

## 15. Cleanup checklist (executed in PR-3)

| Path | Action |
|---|---|
| `agent_cli/tools/symbols.py` | DELETE |
| `tests/test_symbols.py` | DELETE |
| `agent_cli/tools/__init__.py` | drop `tool_read_symbols` import + mapping |
| `agent_cli/tools/registry.py` | drop `"read_symbols"` ToolSchema |
| `agent_cli/context/_file_extract.py:25` | drop `"read_symbols"` from set |
| `agent_cli/tools/read_file.py:99` | drop hashline comment ref |
| `agent_cli/prompts/system_prompt.py` | drop `_build_read_symbols_inline` + read_symbols branches + temp `get_supported_extensions` re-export |
| `tests/test_context_compaction.py` | replace `read_symbols` mentions with `code_index` |
| `tests/test_system_prompt.py` | replace `read_symbols` mentions with `code_index` |
| `tests/test_tools_coverage.py` | replace `read_symbols` mentions with `code_index` |
| `README.md` | remove `read_symbols` section; (code_index already documented in PR-2) |
| `docs/ARCHITECTURE.md` | remove `read_symbols` rows; update LOC numbers |

---

## 16. Open issues / future work

- Whether `tree-sitter-c` deserves a dedicated grammar dep or whether
  `tree-sitter-cpp` continues to cover both. Decide in PR-1.
- Markdown link/anchor refs — pluggable into `walk_refs` if needed.
- Doc comments → `signature` / new `docstring` field. Schema bump.
- Cross-language graph (e.g. JS↔TS↔TSX with imports resolved). Big lift,
  requires module-system semantics.
- `agent-cli code-index` CLI subcommand (mirrors tsindex CLI for human
  use outside the agent loop). Not required for the model surface.
