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
- ~~**D4**~~ → §20 에서 닫음 (v10.95.0). `containing_fn` 은 "시작 줄이 이하인 마지막 함수" 하나만 봤다 — 안쪽 함수가 끝난 뒤
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

---

## 20. 감싼 함수는 가장 안쪽 (v10.95.0)

§19 의 D4. 설계는 Fable 서브에이전트(스크래치 `d4/DESIGN.md`), 리뷰 뒤 D1~D7 승인(2026-11-01).

**결함.** `containing_fn` 은 시작 줄이 line 이하인 **마지막** 함수 하나만 보고 그 함수가 끝났으면
`None` 이었다. 바깥 함수 본문에서 안쪽 함수 정의가 끝난 **뒤** 의 호출은 어느 함수에도 붙지 않았다
— 예 `agent_cli/fsio.py:57,60` 의 `_write()` 두 호출은 `atomic_write_text`(33–60) 의 것인데 bisect 가
안쪽 `_write`(41–54) 에 내려앉아 버려졌다.

**규칙.** line 을 담는 함수 정의 중 **범위가 가장 작은 것**. 범위가 같으면 (시작, 끝, 이름) 순 마지막
(종전 정렬의 동점 규칙과 같다).

**알고리즘.** `build_fn_ranges` 가 (시작, 끝의 역순, 이름) 으로 정렬하고 스택 한 번으로 부모 인덱스를
만들어 `(starts, recs, parents)` 를 돌려준다. `containing_fn` 은 bisect 뒤 그 함수가 끝났으면 부모로
올라간다 — O(log n + 깊이), 깊이 실측 최대 3. 인자 가림은 그 가장 안쪽 함수의 인자만(D3, 종전과 같은
모양).

**언어.** Python walker 만 함수 본문 안으로 내려가 함수 안의 함수를 색인한다. JS/TS(최상위·export 만),
Go(`func_literal` 미방출), Rust(함수 본문의 `fn`·클로저 미방출), Java(지역·익명 클래스·람다 미방출),
C(함수 본문을 건너뜀), C++(람다·지역 클래스 미방출) 는 중첩 레코드가 없어 안쪽 함수 본문의 호출이
지금도 바깥 함수 몫이다 — 결함이 아니라 색인 범위(D5, `TestNestedFunctionsOutsidePython` 이 JS 로 고정).

**영향(구현으로 잰 값, 함수 이름 전부).** 바뀌는 것은 "어느 함수에도 안 붙던 참조가 바깥 함수에
붙는다" 하나 — 다른 함수로 옮겨 가거나 사라지는 간선 **0**.

| | django 사본 | agent-cli(bench/.cache 의 django 사본 포함) |
|---|---|---|
| callers 답이 바뀌는 이름 | 914 / 15,217 (6.0%) | 1,193 / 23,161 (5.2%) |
| callees 답이 바뀌는 이름 | 611 (4.0%) | 949 (4.1%) |
| 잃거나 줄어든 간선 | 0 | 0 |
| 이름당 시간 callers / callees | 0.2 / 0.3 ms (최악 101 ms) | 0.2 / 0.3 ms (최악 109 ms) |

설계 측정(참조 단위): None → 함수 django 8,383 참조(함수 이름 참조의 3.3%), agent-cli 10,943; 함수 →
다른 함수 0, 함수 → None 0; 알고리즘 vs 브루트포스 57만 참조 불일치 0.

**오라클(D4).** `tests/code_index/helpers.reference_callgraph` 는 이제 `containing_fn` 을 import 하지 않고
담는 범위 중 가장 작은 것을 직접 찾는 `_brute_containing` 을 쓴다 — 종전엔 같은 함수를 공유해 결함까지
같았고, 동치 테스트가 이 결함을 원리상 잡지 못했다. 이제 생성 트리 테스트(`_py_tree` 가 "중첩 def 뒤의
호출" 을 섞는다)가 새 규칙을 검사한다.

테스트: `TestContainingFn` 합성 범위(안쪽 뒤·깊이 2·형제 사이·같은 줄에 끝남·같은 시작·동일 범위),
`TestBuildFnRanges` 부모가 자식을 담는지, 반전된 `test_a_call_after_the_nested_function_belongs_to_the_outer`,
`TestClosureFixture`(새 fixture `python/closures.py` — 클로저·깊이 3·데코레이터·클래스 본문·메서드 안
지역 함수의 손으로 쓴 정답), `TestNestedFunctionsOutsidePython`(새 `javascript/nested.js`), slice 한 단언.
사보타주: 부모를 전부 -1(옛 동작) → 11개 실패, 정렬 키를 (시작, 끝) 으로 → 1개 실패.

남은 것(TRACKER): 인자 가림을 체인 합집합으로 — 클로저가 잡은 바깥 함수 인자(`def wrap(view):
def wrapper(): view()`) 가 지금은 같은 이름의 색인된 함수로의 거짓 간선이 된다(django 61 쌍, agent-cli 81 쌍).

---

## 21. 이름 해석은 색인에 (v10.96.0, 스키마 4)

사용자 결정(2026-11-01, "B안으로 가자"): 이름 기반 근사를 하나씩 고치는 일(§19 D4·§20 의 인자 가림 합집합)을
멈추고, 색인에 범위·바인딩을 저장해 빌드 때 참조를 한 번 해석한다. 설계는 Fable(스크래치 `scope/DESIGN.md`),
리뷰에서 둘을 바꿨다 — 규칙 없는 언어도 인자를 바인딩으로 넣어 오늘의 가림을 유지, 편집 비용을 출시 조건으로.

**뿌리 원인 셋.** ① 참조가 바인딩 종류를 몰랐다(인자·지역·import·def 구분 없음) ② 노드가 맨 이름이었다(모든
`get` 이 한 노드) ③ `obj.m()` 은 구문으로 못 푼다. ①·② 는 저장으로, ③ 은 라벨(`candidates`)로.

**스키마 4.** `scopes(file, kind, start_line, end_line, parent_id, symbol_id)`, `bindings(scope_id, name, kind,
line, import_module, import_name, symbol_id)` — `symbol_id` 는 def·class 바인딩의 기호, `resolve_deps(dep, file)` —
파일마다 해석이 읽은 것, `refs` 에 `scope_id`·`fn_symbol_id`·`object`·
`target_id`·`resolution`, `symbols` 에 `bases`, `params` 열 삭제(인자는 바인딩). 기호 id 는 빌드를 넘어 유지 —
복사 행은 id 명시, 다시 파싱한 파일은 `(qualified_name, kind, kind_raw)` 짝으로 옛 id, 지운 id 는 다시 안 씀
(meta `next_ids`). 그래서 본문만 고친 편집 뒤에 다른 파일의 `target_id` 가 한 행도 안 바뀐다.

**파이프라인.** Pass-1 정의 → Pass-2 범위·바인딩·참조(Python `walk_scopes`, 그 밖 `walk_refs` +
`resolve.function_scopes`) → 표에 쓰기 → Pass-3 `resolve_files`(표만 읽는다).

**증분 — 해석이 읽은 것을 적는다.** 해석은 파서가 쓴 행(범위·바인딩·클래스 베이스·파일 목록·함수 이름)만 읽고
다른 파일의 해석 결과는 읽지 않는다. 그래서 파일마다 해석하면서 **실제로 읽은 것**을 `resolve_deps` 에 적어 두면,
다음 빌드는 그 가운데 바뀐 것이 있는 파일만 다시 해석하면 된다 — 전체 재구축과 같은 답이 구성으로 보장된다.
적는 것: `f:<파일>`(그 파일의 범위·바인딩을 통째로 — 클래스 멤버·베이스를 찾을 때), `e:<이름>:<파일>`(그 모듈
최상위의 한 이름 — `from F import x`·`F.x`·`*` 를 거친 조회; `*` 자체도 `e:*:<파일>`), `m:<모듈>`·`p:<경로>`(모듈
이름·상대 import 후보 경로를 찾아본 것), `n:<언어>:<이름>`(그 이름의 함수가 있는지로 `candidates`/`external` 을
가른 것). 재노출·`*`·베이스 사슬을 따라 읽은 것이 전이적으로 들어간다 — 한 해석 안의 메모(`follow_import`·
`resolve_in_module`)는 결과와 함께 그 계산이 읽은 것을 든다. 이 전제("해석은 표의 함수")가 메모에서도 지켜지도록
**진입점에 기댄 결과는 해석 묶음을 넘어 남기지 않는다**: 계산 중이던 다른 메모에 걸린(순환) 결과는 들어온 길에
따라 값이 다르니 — 다른 파일이 받으면 해석 묶음(증분은 바뀐 파일만)에 따라 답이 갈린다. 그런 메모는 기댄 메모 키를
함께 적어, 그 메모가 계산 중인 동안만 쓰고(그 계산이 끝나면 지운다), 적중하면 기댄 것도 바깥으로 옮긴다. **import
깊이 상한은 없다** — 상한이 있으면 얕은 진입에서 깨끗하게 메모된 값을 깊은 진입이 받아(새로 계산했으면 잘렸을 것)
상한 자체가 묶음에 기댄다. 끝나는 것은 "계산 중" 절단이 보장한다(키가 유한하고 한 키는 스택에 한 번만 오른다).
재노출 사슬이 파이썬 재귀 한도에 닿으면(약 140단 — 100단은 해석된다) 잘라서 답을 내지 않고 빌드가 분명한 오류로
실패한다(있던 색인은 그대로). 곧바로 버리면 `*`
순환 안에서 같은 계산을 가지마다 다시 해 비용이 지수로 는다(서로를 `*` 로 import 하는 12 모듈: 수십 초 → 0.03 s).
읽은 것은 메모 여부와 상관없이 바깥으로 올라가고, 순환으로 걸린 메모는 스택 아래에서 계산 중이라 끝나며 같은 것을
다 담는다. 바뀐 것(`builder._resolve_targets`): 다시 파싱한 파일의 바깥 표면
(`_surface` — 모듈·클래스 범위와 그 바깥 범위의 바인딩, 베이스, def·class 기호 id; 줄은 순위만, 함수 몸의 지역은
빼서 본문 편집은 표면을 안 바꾼다)이 달라졌으면 `f:` 와 달라진 최상위 이름의 `e:`, 지운 파일의 `f:`, 파이썬 파일
집합이 바뀌면 대응이 달라진 모듈의 `m:` 과 생기거나 사라진 경로의 `p:`, 다시 파싱하거나 지운 파일의 함수 이름 중
있고 없음이 바뀐 것의 `n:`. 복사하는 파일은 `resolve_deps` 행도 복사한다.

처음엔 "바깥에 보이는 것이 바뀐 파일을 import 하는 파일" 을 사례마다 규칙으로 넓혔다(바뀐 이름·모듈째 import·
클래스 id 추적·재노출 고정점·모듈 대응 diff). 리뷰 네 번에 걸쳐 매번 새 모양(두 단계 재노출, `models.Model`, 모듈
객체 재노출, 클래스 없는 재노출 파일을 거친 손자 베이스)에서 증분 ≠ 전체가 나왔고, 넓힐수록 비용이 컸다(django
`timezone.py` 에 함수 하나 추가가 1,097 파일). 읽은 것 기록으로 바꾸자 그 규칙들이 모두 사라지고 같은 편집이 1 파일.

**Python 규칙**(`resolve/python.py` docstring): 함수 범위는 평평(대입 앞 참조도 지역), 인자·지역 → `local`;
클래스 범위는 그 본문의 직접 참조에만; `global`/`nonlocal`; 모듈·클래스 본문은 줄 이전의 마지막 바인딩, 함수
안에서 온 참조는 최종 바인딩; 바인딩 없으면 내장 → `external`, 아니면 `unresolved`. import 는 소스 루트(색인
루트 + 최상위 패키지의 부모)로 파일을 찾고, 상대 import 는 파일 디렉터리에서; 이름이 직접 묶여 있지 않으면
서브모듈이 `*` 보다 먼저, `*` 가 여럿이면 정의를 내놓는 쪽. 속성 체인은 모듈·클래스로 따라가고 `self`/`cls` 는
메서드의 클래스 + 베이스(깊이 우선, 6단); 그 밖의 객체는 `candidates`(그 이름의 Python 함수가 없으면 `external`).
기본값·어노테이션·데코레이터·베이스·컴프리헨션 첫 iterable 은 바깥 범위. 지역 별칭(`g = f; g()`)은 따라가지
않는다(값 추론 없음 — 간선 하나를 잃지만 거짓 간선은 없다).

**다른 언어.** 함수 정의마다 함수 범위(가장 안쪽 규칙의 중첩) + 인자 바인딩. 참조는 감싼 함수들의 인자면
`local`, 아니면 그 이름의 함수가 그 언어에 있으면 `candidates`, 없으면 `external` — v10.95.0 의 인자 가림과 같은
결과에 바깥 함수 인자까지(합집합). 출력 머리가 "matched by name only (… is not scope-resolved)".

**출력.** callers·callees 는 정의마다 머리(`Box.get — a.py:6-7`)와 해석된 이웃 줄(한정 이름, 같은 한정 이름이
둘이면 `@file`), callers 끝에 `? "get" on an object of unknown type — any of N definitions: …`, callees 의 이름으로만
맞춘 이웃은 `? save  (2×, any of 12 definitions)  …`. 맨 이름은 정의 전부(최대 5 + "… N more"), 한정 이름은 하나.
slice BFS 는 정의 단위, `?` 는 따라가지 않는다. 자기 호출·모듈 수준 참조는 종전처럼 뺀다. 설명은 `mode` 에 두 문장,
`name` 에 한 문장.

**planner 함정.** `sqlite_stat1` 이 없어 `name = ? AND language = ?` 에 planner 가 `idx_symbols_language` 를,
`kind = ?` 에 `idx_symbols_kind` 를 고른다 — `+kind`·`+language` 로 막는다(EXPLAIN 가드 테스트 둘).

**측정**(구현):

| | django 사본(2,633 파일) | agent-cli 자체(412 파일) |
|---|---|---|
| 전체 빌드 | 7.4 s | 2.6 s |
| 색인 크기 | 99.1 MB (종전 98.8; `resolve_deps` 7.3만 행) | 29.2 MB |
| Python 참조 해석 분포 | resolved 29.4% · local 29.0% · external 24.4% · candidates 17.1% · unresolved 0.1% | resolved 22.7% · local 38.2% · external 24.9% · candidates 14.1% |
| callers+callees (흔한 이름 200개) | 중앙값 0.4 ms · 최대 22 ms | 0.2 ms · 12 ms |
| 증분: 바뀐 것 없음 / 본문 편집 | 0.11 s / 1.10 s (v10.95.0 1.25 s) | 0.01 s / 0.35 s |
| 증분: 새 파이썬 파일 | 1.10 s (다시 해석 1 파일) | — |
| 증분: 허브·`timezone`·`db/__init__`·`db/models/__init__` 에 함수 추가 | 1.1~1.2 s (각 1 파일; 규칙 방식 2·1,097·1,068·851) | 0.36 s |
| 증분: 가장 많이 import 되는 이름 재바인딩(`django.test.TestCase`) | 1.72 s (325 파일) | — |
| 증분: `Model` 에 메서드 추가 | 1.79 s (299 파일; 규칙 방식 1,660) | — |

위 편집 일곱 모두 증분 = 전체(참조마다 해석·대상 비교, 차이 0). 이름 단위(`e:`)가 없으면 허브에 함수 하나 추가가
691 파일 — 모듈 최상위 조회는 그 이름의 바인딩만 보니 이름 단위로 적는다.

**정확도 — jedi 대조**(django, 무작위 1,000 자리, 스크래치 venv 의 jedi 0.20.1; 스크립트 `scope/impl/jedi_check.py`):

| | `f()` 400 | `obj.m()` 400 | 맨 이름 200 |
|---|---|---|---|
| resolved · jedi 와 일치 | 303/303 | 114/114 | 37/37 |
| candidates · jedi 정답 포함 | — | 64/64 | — |
| local/external 인데 jedi 는 정의를 찾음 | 1 (`try/except` 대체 대입 — 규칙상 local) | 0 | 0 |
| 종전(이름만)의 답 크기 평균 | 3.4 정의 | 20.4 정의 | 6.9 정의 |

첫 대조에서 불일치 4건을 고쳤다: `from pkg import functions` 의 서브모듈이 `*` 보다 먼저, `import *` 가 여럿일 때
첫 것이 모른다고 해도 다음 것을 본다(django gis `models/__init__`).

**테스트.** `test_resolution.py` — `fixtures/scoping` 패키지의 손으로 쓴 정답 34개(규칙마다 하나),
독립 오라클 `py_oracle`(CPython `ast`+`symtable`: def 몸 안 맨 이름의 local/follow 판정; def 이면서 따로 대입된
이름은 symtable 이 가르지 못해 뺀다)과 fixture·생성 트리(hypothesis 30) 비교, 증분 = 전체 재구축(본문·줄 이동·
함수 삭제·추가·재노출 삭제·베이스 변경·파일 삭제 7종, 기호 id 를 (file, qualified_name, line) 으로 정규화).
`test_callgraph.py`·`test_store.py`·`test_tool_dispatch.py` 는 새 질의·출력. 사보타주 8종 모두 걸린다 — 클래스 범위를 메서드가 봄, 함수 지역을 대입 뒤부터만, `global`·`nonlocal` 무시, `self`/`cls` 특별 취급 없음, 상대 import 한 단계 덜, dependents 비우기, 다시 파싱한 기호에 새 id(처음엔 클래스 범위·`global`·`nonlocal` 셋이 안 걸려 그 사례를 fixture 에 더했다). 알려진 것: 정의가 사라진 이름의 맨
식별자 참조는 그 파일을 다시 파싱할 때까지 남는다(다시 해석돼 `unresolved` — 간선 아님; 종전부터).

**리뷰(Fable, 1회차) 뒤 고친 것.** ① 증분 ≠ 전체 다섯 부류 — 다른 모듈에 이미 있는 이름을 새로 정의(식별자 비교가
아무것도 안 걷는다), 모듈째 import(`from pkg import core`·`import pkg.core`), 손자 클래스, 새 모듈 파일, `__init__.py`
추가로 바뀐 소스 루트, 같은 이름 def 를 하나 더 → `_changed_names` 가 생긴 이름도·목록 순서도 보고, 의존 질의에
모듈째·미해석 import, 클래스 세 단계, 모듈 대응 변화(처음엔 파일 집합이 바뀌면 전부 다시 해석 — django 2.97 s →
대응이 달라진 모듈을 import 하는 파일만 1.25 s); `*` import 의 모듈 파일도 적는다. ② `import a.b.c as x` 가 `a` 로
풀렸다(바인딩 이름으로 별칭을 가른다). ③ py-tree-sitter 는 접근마다 새 Node 를 만들어 `is` 비교가 늘 거짓 —
`from X import …` 의 모듈 이름이 가짜 바인딩이 됐고(뒤의 `import pkg` 를 덮음), for·with 타깃이 참조로도 나왔다 →
`==`. ④ `*args: int`·`**kw: str` 가 안 묶였고 어노테이션 참조가 둘 → 고침; match 의 `case P(y=h)`·`*rest` 캡처를
묶는다. ⑤ 생성 트리 30 예제 중 15 가 `global` 위치 때문에 SyntaxError 로 건너뛰어졌다 → 맨 앞에 두어 0 건, 비교
277 참조; fixture 오라클 판정 수 ≥ 40 단언. 리뷰 사례 6종은 `test_review_edits_incremental_equals_full`·
`test_import_forms_and_aliases`·`test_walker_binding_sites` 로 고정. **리뷰 2회차.** 1회차 7건 해소 확인, 남은 한 뿌리 — 의존 전파가 한 단계라 재노출·`*` 사슬·재노출을 거친 클래스·4단
베이스 체인에서 증분 ≠ 전체 → 위의 고정점·클래스 id 추적. 사례 5종을 `REVIEW_EDITS` 에 더했다(사보타주: 고정점을 끄면
3, 클래스를 한 단계로 하면 2 실패). 보이지 않는 차이로 남긴 것: 그 이름의 마지막 파이썬 함수가 지워지면 다른 파일의
`obj.m()` 이 `candidates` 로 남는다(전체는 `external`) — `callees` 는 정의 수 0 을 숨기고 `callers` 는 정의가 없으니 출력은
같다(4회차에서 풀림). **리뷰 3회차.** 2회차 사례 19종 모두 같음 확인, 새로 셋 — ① import 바인딩 갱신이 `COALESCE` 로 이전 빌드의
기호 id 를 남겨 두 단계 위의 재노출 삭제가 안 보였다 → 그대로 덮어쓴다. ② 모듈 속성으로 쓴 베이스(`from pkg import
models; class Foo(models.Model)` — django 관용구, 그 모듈은 클래스 없는 재노출 `__init__`)에 클래스 전파가 안 닿았다 →
클래스 파일·재노출 파일을 모듈째 import 한 파일도 프런티어에 넣고, 클래스 id 가 비어도 새로 닿은 파일이 없을 때까지
돈다. ③ 모듈 객체를 재노출하는 파일(`from pkg import core` 를 다른 파일이 `f.core.helper()` 로)이 고정점에서 "움직임"
이 안 됐다 → 다시 해석된 파일을 가리키는 모듈 객체 import 가 있으면 움직임. 사례 5종 추가, 사보타주 4종 모두 걸림.
**리뷰 4회차(Opus — Fable 주간 한도).** 3회차 수정 위에서 새로 두 부류(모듈 객체 재노출의 대상만 바뀜 — 모듈
바인딩엔 거쳐 온 파일이 안 남는다; 클래스 없는 재노출 파일에서 베이스가 바뀌면 손자 이하가 낡음)와 비용(모듈 객체
import 를 따라 번지는 "움직임" 규칙) → 규칙을 더 넓히는 대신 위의 "읽은 것 기록" 으로 바꿨다. 리뷰 시나리오 전부
(1~4회차 46종) 같음, R6(마지막 함수 삭제)도 함수 이름 기록으로 풀렸다. 사례 10종 추가(재노출·베이스·순환 메모·상대
경로·베이스 식만·`*` 추가·마지막 함수). 사보타주 — 이름 단위 기록·`*` 기록·메모 합치기·순환 표시 펼치기·함수 이름·
모듈 이름·상대 경로·통째 읽기 기록, 표면의 베이스·클래스 범위, 복사, 이름 키 — 열두 가지 모두 걸린다(줄 순위는 바인딩
순서와 겹쳐 관찰되지 않는 안전한 여분). **리뷰 5회차(Opus).** 기록 누락·표면 구멍은 없음, 남은 한 부류 — 메모가
진입점에 기댄 값을 담아 해석 묶음·순서에 따라 답이 갈림(`*` 순환에서 먼저 들어온 쪽이 자르는 자리, 깊이 상한에서 잘린
값을 상한 안쪽 진입이 받음; django 에선 발현 안 함) → 잘린 값을 메모하지 않기(6·7회차에 다듬음). 같은 표를 한꺼번에·거꾸로·하나씩
해석해 답이 같은지 단언하는 테스트(fixture 전체 + 두 사례), 사보타주 3종(잘린 값 메모·순환 표시·깊이 표시) 걸림.
django 빌드·편집 비용 변화 없음. **리뷰 6회차(Opus).** 정확성 문제 없음(위 논리 확인), 비용 회귀 하나 — 기댄 메모를
곧바로 버려 `*` 순환에서 지수 → 기댄 것이 살아 있는 동안만 남긴다. 무작위 import 그래프 60개(`*`·이름 import·순환)를
섞은 순서·하나씩 해석해 답이 같은지, `*` 12 모듈 순환이 5 s 안인지 단언; 사보타주 — 적중 때 기댄 것 안 옮김·프레임
끝에 안 지움·파일을 넘어 남김·곧바로 버림·전부 메모 — 모두 걸린다. **리뷰 7회차(Opus).** 깊이 상한이 남아 있는 한
묶음 의존(위) — 리뷰어 퍼즈에서 증분 ≠ 전체 400 seed 중 6 → 상한을 없앴다. 무작위 그래프 생성기에 긴 사슬(8~12단)·
별칭·모듈째 import·클래스를 넣고 150 seed 를 다섯 묶음 구성(한꺼번에·섞어·거꾸로·하나씩·셋씩)으로; 100단 사슬 해석,
재귀 한도를 넘는 사슬의 분명한 실패. 새 테스트 넷 모두 옛 판에서 실패. 리뷰어 퍼즈: 묶음 독립 600 seed 참조 차이 0,
증분 400 seed 차이 0. 남긴 것(낮음): 순환 안에서는 기록된 읽은 것이 묶음에 따라 하나 더 붙기도 한다(참조 답은 같고,
더 붙는 쪽뿐이라 과잉 포함 — 엄밀하게는 순환을 강연결 요소 단위로 풀어야 한다). 색인을 만드는 속성 테스트의 hypothesis 마감(200 ms)을 껐다 — 예제마다 `build()` 가 돌아 부하에 따라 간헐 실패.
회귀 아님으로 남긴 것: 조건부 def(`try: from x
import f / except: def f()`)는 Pass-1 이 if/try 안을 안 봐 기호가 없다(v10.95.0 도 같다).

**남은 것.** PR-2 `refs` 해석 태그·`path` 로 정의 고르기(`fsio._write` 처럼 모듈로 한정한 이름은 아직 맨 이름으로
떨어진다), PR-3 C(static·블록 범위·헤더 짝·`registration`), 다음 언어(JS/TS·Go·Rust), Java·C++ 는 언어 서버 쪽.
