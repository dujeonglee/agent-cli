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
| ~~`build`~~ | — | v10.92.0 에 삭제 — 매 쿼리가 증분 갱신하므로 할 일이 없었다 (§17). |

~~Each mode gets its own short paragraph in the inline guide.~~ v10.92.0: 안내 블록은 없고
모드마다 한 문장이 `mode` 인자 설명에 있다 (§17). The schema keeps unused params optional per-mode.

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
   that touches the index, refresh the index for the changed files only.
   ~~recompute sha1 of all source files under the root~~ v10.93.0: 파일을
   읽기 전에 크기·mtime 으로 가른다 — 전부 그대로면 아무것도 쓰지 않는다 (§18).
   Cost on 265 files ≈ 50ms (sha1 of every file) → 3,212 files 0.35 s (stat only).
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

- ~~`build_callgraph`~~ v10.94.0: `callers_of`/`callees_of` 의 `Edges` + 옛 알고리즘(오라클 `reference_callgraph`)과의 동치 — fixture 전 이름·생성 트리(§19).
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

---

## 17. 도구 검토 (v10.92.0, docs/tool-review/TRACKER.md)

근거: 벤치 810 세션(약 2만 액션)에서 34회, 실사용 22 세션(229 액션)에서 2회 — 벤치 과제의
`/app` 에는 색인할 코드가 없어(`build` 6회 모두 "0 symbols") 행동은 잴 수 없고, 비용으로 봤다.

- **안내 블록 삭제.** 매 요청 1,689 토큰(설명 44 + 인자 541 + 안내 1,104; GLM 토크나이저) →
  683. 안내의 호출 모양 예시 11개는 v10.77.0 표기 규칙(호출 모양 금지)을 가드가 예시 줄을
  건너뛰어 비껴갔고, 예시의 가짜 경로(`agent_cli/loop.py`·`src/main.c`)를 벤치의 모델이 실제
  경로로 오인해 찔러본 기록이 4건. SQLite 경로·sha1 증분·"no DB write"·"index-scoped" 같은
  내부 사실은 모델의 선택을 바꾸지 않는다. 남긴 사실: 이름 규칙·확장자(설명), 모드마다 한
  문장과 ""list"·"fetch" 는 어떤 파일이든, 나머지는 프로젝트 루트 아래만"(`mode` 인자).
- **`build` 삭제.** 매 쿼리가 갱신하므로 할 일이 없다. `force_full` 은 테스트 헬퍼가 쓰므로
  `build()` 인자로 남는다.
- **빈 결과는 색인의 범위를 말한다** (`_index_scope`·`_no_match`): 색인에 파일이 없으면 "The
  index has no files: nothing under /app has a supported extension.", 있으면 "No symbol named
  "X" among the N indexed symbols (M files under /root)." refs·callers·callees·kind 도 같은 꼴.
  벤치에서 lookup 5·refs 3 이 전부 빈 결과였는데 색인이 비어 있다는 사실은 어디에도 없었다.
- **오류 문구는 표기 규칙대로** — Python repr(`'mode'`, `['build', …]`)을 문장으로.
- 측정은 토큰(1,689 → 683)과 다음 벤치의 "없는 경로" 호출 수(예시 경로 오인 0 기대).
- 남은 구조 항목(별도 PR): 쿼리마다 저장소 전체를 다시 읽고 DB 를 통째로 다시 쓴다 — 이
  저장소(3,212 파일, 23 MB)에서 쿼리 4.0 s, `edit_file`·`write_file` 사후 훅도 4.0 s.

---

## 18. 갱신은 stat 으로 먼저 가른다 (v10.93.0)

§17 의 구조 항목. 종전 `build()` 는 쿼리마다 ① 저장소의 모든 소스 파일을 읽어 sha1 을 재고
② 기존 기호·참조 전부를 파이썬 dict 로 올린 뒤 ③ 바뀐 것이 없어도 SQLite 를 통째로 다시
썼다 — agent-cli 저장소(3,212 파일, 23 MB)에서 쿼리 4.0 s(쓰기 2.2, 로드 1.0, 읽기·sha1
0.6), `edit_file`·`write_file` 의 사후 훅(`post_hook`)도 같은 함수라 편집마다 4.0 s. 기호가
0개인 파일은 재사용 조건(`rel in old_syms_by_file`)에 걸려 매번 다시 파싱됐다(JS 95회).

바꾼 것:

- **stat 선행.** 걷기에서 `(rel, size, mtime_ns)` 만 모은다. 파일 집합이 같고 모든
  크기·mtime 이 저장된 것과 같으면 **읽지도 쓰지도 않고** `{"unchanged": True}` 로 돌아온다.
  `files` 테이블에 `mtime_ns` 컬럼(SCHEMA_VERSION 3 — 옛 DB 는 한 번 전체 재구축).
- **sha1 은 mtime 이 다른 파일만.** 내용이 같으면 재사용하고 새 mtime 만 메타에 적는다
  (`touch` 한 파일은 다음 호출부터 stat 만으로 걸러진다).
- **바뀐 파일만 파싱, 나머지 행은 SQL 복사.** `write_sqlite_index(copy_from=, copy_symbols=,
  copy_refs=)` 가 옛 DB 를 ATTACH 해 재사용 파일의 기호·참조 행을 `INSERT … SELECT` 로 새
  파일에 옮긴다(원자 교체 계약 그대로). 새 이름에 영향받는 파일(Option B)은 참조만 다시
  쓰고 기호는 복사한다. '옛 이름 집합' 은 `IndexStore.code_names()`(DISTINCT 질의)로.
- `build()` 가 통계 `{"unchanged", "reused", "changed", "affected", "removed"}` 를 돌려준다
  — 테스트와 verbose 출력의 근거.

측정(바뀐 파일 없음 → 한 파일 변경): agent-cli 4.0 s → 0.35 s · (측정 안 함), django 2,633
파일 3.0 s → 0.11 s · 3.05 s → 1.25 s. `callers`·`callees` 의 콜그래프 구성(참조 전부 로드)은
그대로 1.2~1.8 s — 다음 후보 → §19 에서 닫음.

테스트 `tests/code_index/test_builder.py::TestStatFirstRefresh`: 변경 없음 → 읽기·쓰기 0,
touch 만 → 재사용·mtime 갱신·다음은 unchanged, 기호 0개 파일 재사용, 한 파일 변경 → 그
파일만 새 행·나머지 복사·전체 재구축과 동일, 새 이름 → 영향 파일 참조 한 번만, 삭제.

---

## 19. callers·callees 는 물어본 이름만 (v10.94.0)

§18 의 "다음 후보". `callers`/`callees` 는 질의마다 `build_callgraph(store)` 로 참조 65만 행·기호
5.9만 행 전부를 파이썬으로 올려 함수-함수 그래프를 만든 뒤 항목 하나를 읽었다 — agent-cli
1.4~1.6 s(피크 메모리 556 MB), django 1.0 s. 다른 모드는 v10.93.0 이후 0.1~0.35 s.
설계는 Fable 서브에이전트, 리뷰 뒤 D1~D7 승인(2026-11-01).

- **이름 단위 질의.** `callers_of(store, X)`: X 가 함수로 색인돼 있으면 X 를 가리키는 call·name
  참조(`call_sites_by_name`)마다 그 파일의 함수 정의 범위로 감싼 함수를 찾는다.
  `callees_of(store, X)`: X 의 정의마다 그 범위 안 참조(`call_sites_in_range`) 중 감싼 함수가 X
  인 것, 이름이 함수인 것(`function_names_among`)만. 둘 다 `Edges(counts, sites)`.
- **의미는 구성상 같다.** 감싼 함수 판정은 그 파일의 함수 정의(`function_ranges_in_file`)에
  `build_fn_ranges`/`containing_fn` 을 그대로 쓴다 — 중첩·자기 호출·인자 가림·같은 자리
  call+name 하나(`call` 우선)·같은 이름 한 노드. 순서는 **참조 id 순**(옛 dict 삽입 순 = 참조 행
  순) — 증분 빌드가 옛 행을 복사한 뒤 새 행을 붙이므로 질의마다 `ORDER BY id`.
- **스키마 그대로**(SCHEMA_VERSION 3): `idx_refs_name`·`idx_refs_file` 이 이미 있다.
- **planner 함정**: `sqlite_stat1` 이 없어 `name = ? AND kind = 'function'` 에 planner 가
  `idx_symbols_kind`(3.3만 행)를 고른다 — 6.4 ms. `+kind` 로 막아 0.26 ms. `find_symbols` 의
  `kind` 절도 다른 등호 절과 함께면 `+kind`. EXPLAIN 가드 테스트.
- **slice** 는 BFS 단계마다 프런티어 이름만 묻는다.
- `build_callgraph` 는 패키지에서 지우고 테스트 오라클 `tests/code_index/helpers.reference_callgraph`
  로 옮겼다(D1). `find_refs_in_range` 삭제(소비자 0, D5). 두 핸들러 출력은 `_format_edges` 하나(D7).

측정(구현, 함수 이름 전부): django 15,217개 — 이름당 평균 callers 0.2 ms·callees 0.3 ms, 최악
`__init__` 96 ms; agent-cli 23,136개 — 0.2/0.3 ms, 최악 `name` 89 ms. **불일치 0**(counts 키
순서·sites 순서·kind·도구 텍스트). 도구 경로 전체(`_ensure_index` 포함): django `callers get`
145 ms·`callees __init__` 197 ms, agent-cli 514·491 ms(갱신 0.35 s 포함; 종전 1.8 s).

테스트: `test_callgraph.py` 의 `TestEdgesSemantics`(자기 호출·인자 가림·콜백 name 사이트·한 줄 두
호출·중첩 함수·**D4 결함 고정**·비함수 이름·같은 이름 병합), `TestEquivalentToReference`(fixture
10개 × 이름 전부), `TestEquivalentOnGeneratedTree`(hypothesis, 이름 알파벳 5개로 충돌 강제);
`test_store.py::TestPerNameCallGraphQueries`(창·id 순·500 경계·증분 빌드 뒤 순서·planner).
사보타주 5종(정렬·call 우선·인자 가림 둘·ORDER BY) 모두 걸림.

남은 것(TRACKER):
- **D4** `containing_fn` 은 "시작 줄이 이하인 마지막 함수" 하나만 본다 — 안쪽 함수가 끝난 뒤
  바깥 본문의 호출은 어느 함수에도 붙지 않는다(Python 클로저·JS/Go 중첩 함수). 고치면 출력이
  바뀌므로 별도 PR, 지금은 테스트로 고정.
- ~~**D6** slice 의 나머지 전체 로드~~ → §19.1 (v10.94.1).
- D3 파일 범위 묶음 질의(`file IN (…)`) — 최악 ~90 ms 를 더 낮출 여지, 보류.

### 19.1 slice 도 색인 전체를 올리지 않는다 (v10.94.1)

§19 의 D6. `cmd_slice` 는 v10.94.0 뒤에도 `pick()` 을 위해 기호 전부(`idx["symbols"]`, 0.2 s)를
이름별로 묶었고, `with_types`·`with_macros` 는 참조 전부(65만 행)를 옵션마다 한 번씩 훑었다
— `as_sql`: 기본 0.22 s, types+macros 0.90 s, with_callees 1.51 s(그래프 포함).

- `pick(name)` → `IndexStore.symbols_named(name)` (`ORDER BY id` — 옛 묶음의 행 순과 같다;
  정렬 키 (정의 여부, 종류, 파일, 줄) 가 같은 동점은 사실상 없어 순서가 결과를 가르진 않는다).
- types·macros → `IndexStore.ref_names_in_range(file, start, end, kind)` (대상 범위 안 `kind` 참조의
  이름 집합; 쓰는 쪽이 정렬한다).
- 결과: 이름당 평균 8~9 ms(django·agent-cli 표본 606 × 옵션 3), 도구 경로 전체 `as_sql`
  with_callees django 213 ms·agent-cli 460 ms(갱신 포함). 출력 불일치 0.
- 테스트 `test_slice.py::TestSliceEquivalentToFullScan` — 기호·참조를 전부 훑는 store 대역
  (`_BruteForceStore`, 옛 방식)과 fixture 10개 × 이름 전부 × 옵션 4종이 같다. 범위 질의의 종류
  거르기를 지우면 7개 실패.
