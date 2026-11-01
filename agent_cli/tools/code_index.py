"""Native ``code_index`` tool entry — agent-cli wrapper over the
``agent_cli.code_index`` package.

Mode dispatch:

  list       file outline (per-file, on-demand parse if outside root)
  fetch      single symbol body in hashline format (edit_file-ready)
  lookup     index-wide symbol lookup by name (+ optional symbol_kind)
  kind       index-wide symbols by symbol_kind
  file       all symbols defined in a single file (index lookup)
  refs       all ref sites for a name (+ optional ref_kind)
  callers    callers of a function (from callgraph)
  callees    callees of a function (from callgraph)
  slice      LLM-context markdown blob (def + optional context)

``build`` 모드는 v10.92.0 에 지웠다 — 매 쿼리가 증분 갱신하므로 할 일이 없었고, 벤치 6회
호출은 전부 색인할 것이 없는 곳에서 "0 symbols" 를 되풀이했다 (docs/tool-review/TRACKER.md).

Indexed root resolution: walk up from cwd looking for an existing
``.agent-cli/`` directory; fall back to cwd. DB lives at
``<root>/.agent-cli/code_index.db`` and is lazy-built on first access
plus per-query incrementally refreshed by ``build()``'s sha1 path.

Paths outside the indexed root fall through to an on-demand parse for
``list`` / ``fetch`` only; index-scoped modes (``lookup``, ``kind``,
``refs``, ``callers``, ``callees``, ``slice``) return a clear error if
the supplied path is out-of-root.
"""

from __future__ import annotations

import re
import threading
from dataclasses import asdict
from pathlib import Path
from typing import ClassVar

from agent_cli.code_index import (
    IndexStore,
    build,
    callees_of,
    callers_of,
    cmd_slice,
    definitions_named,
    load_index,
)
from agent_cli.code_index.builder import get_parser
from agent_cli.code_index.languages import (
    LANGUAGES,
    get_supported_extensions,
    language_of,
)
from agent_cli.code_index.preproc import with_default_version
from agent_cli.code_index.schema import NAME_KINDS, REF_KINDS
from agent_cli.tools.base import Tool
from agent_cli.tools.read_file import format_hashlines_range
from agent_cli.tools.result import ToolResult

# Serialize index builds within a single process. Parallel delegate
# workers can land on ``code_index`` at the same instant, and even
# though ``write_sqlite_index`` is now atomic (race-safe at the
# filesystem layer), letting N workers each run a full incremental
# rebuild simultaneously burns CPU on identical work and amplifies
# lock contention inside SQLite. One build at a time per process —
# the second arrival waits, then trivially re-reads the freshly
# written DB.
_BUILD_LOCK = threading.Lock()


# ----- index root / DB resolution -------------------------------------------


def _resolve_index_root() -> Path:
    """Nearest ancestor that already contains ``.agent-cli/``; else cwd.

    Using an existing ``.agent-cli/`` as the anchor lets multi-subdir
    project layouts work: running ``agent-cli`` from ``src/`` still
    shares an index with a previous run from the project root.
    """
    cwd = Path.cwd()
    for d in [cwd, *cwd.parents]:
        if (d / ".agent-cli").is_dir():
            return d
    return cwd


def _resolve_db_path() -> Path:
    """``<index_root>/.agent-cli/code_index.db``. Creates the parent
    directory if it doesn't yet exist."""
    db = _resolve_index_root() / ".agent-cli" / "code_index.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    return db


#: C 계열 확장자 — defconfig(unifdef) 가 의미 있는 파일.
_C_FAMILY = frozenset(
    {".c", ".h", ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx", ".h++"}
)

#: 선언만 잡혔을 때 관찰 끝에 붙는 한 줄 (v10.76.1). 종전엔 같은 내용이 도구 가이드의
#: 문단(~180 tok)으로 매 요청 실렸다 — C 를 안 다루는 요청에도. 트리거가 생긴 그
#: 관찰에서 말한다(교정은 트리거 직후 — 프롬프트 자리 원칙).
DEFCONFIG_HINT = (
    "[Only declarations matched. In C/C++ a function signature split by "
    "`#ifdef` can parse as an error and drop the definition from the index. "
    "Writing `.agent-cli/defconfig` — lines like `#define CONFIG_FOO 1`, "
    "`#undef CONFIG_BAR`, `#define LINUX_VERSION_CODE 393472` (= 6.1.0; "
    "`KERNEL_VERSION(6, 1, 0)` also works) — makes the index take one branch; "
    "it rebuilds when that file changes. Without it every "
    "`>= KERNEL_VERSION(...)` branch is taken.]"
)


def _defconfig_hint(syms: list[dict], root: Path) -> str:
    """선언만 맞았고, 전부 C 계열이고, defconfig 가 없을 때만 ``DEFCONFIG_HINT``."""
    if not syms or any(s.get("is_definition") for s in syms):
        return ""
    if not all(Path(str(s.get("file", ""))).suffix in _C_FAMILY for s in syms):
        return ""
    if _resolve_defs_path(root) is not None:
        return ""
    return "\n" + DEFCONFIG_HINT


def _resolve_defs_path(root: Path) -> Path | None:
    """Conventional defconfig location: ``<root>/.agent-cli/defconfig``.

    Returned only when the file exists so callers can pass the result
    straight through to ``build(defs_path=...)``. The defconfig wires
    ``#define`` / ``#undef`` lines into ``unifdef -b`` so C source with
    ``#ifdef CONFIG_X`` branches around function signatures parses
    cleanly into a single ``function_definition`` node instead of an
    ERROR run that swallows the definition.
    """
    defs = root / ".agent-cli" / "defconfig"
    return defs if defs.is_file() else None


def _ensure_index() -> tuple[IndexStore, Path]:
    """Run ``build()`` (lazy-creates the DB if missing, incrementally
    refreshes otherwise) and return ``(store, root)``.

    Holds ``_BUILD_LOCK`` for the duration of the build so concurrent
    callers (typically parallel delegate workers each invoking a
    ``code_index`` mode) don't race on a redundant rebuild. The lock
    is *not* held during ``load_index`` — that's a read-only open of
    the just-written file, safe to overlap freely.
    """
    root = _resolve_index_root()
    db = _resolve_db_path()
    with _BUILD_LOCK:
        build(root, db, defs_path=_resolve_defs_path(root), verbose=False)
    return load_index(db), root


def _path_in_root(p: Path, root: Path) -> bool:
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


# ----- post-hook (incremental refresh after edit_file / write_file) ---------


def post_hook(path: str | Path) -> None:
    """Refresh the index after a successful edit_file / write_file.

    Called from ``tool_edit_file`` and ``tool_write_file`` so that the
    next ``code_index`` query sees the change without any action from the
    model. Semantics:

      - If no index DB exists yet, no-op — lazy build will pick the file
        up on the model's first index query.
      - If the path is outside the resolved index root, no-op — we
        don't track files we don't index.
      - Otherwise call ``build()`` with the existing DB; its sha1
        per-file scan re-walks only the one file we just touched
        (~50ms on a 265-file repo from the upstream tsindex benchmark).

    All exceptions are swallowed: the post-hook is best-effort and MUST
    NOT cause the user-facing edit/write to fail because the index
    refresh stumbled. A failed refresh just means the next query will
    pay the lazy-build cost instead.
    """
    try:
        root = _resolve_index_root()
        db = root / ".agent-cli" / "code_index.db"
        if not db.is_file():
            return
        file_abs = Path(path).resolve()
        if not _path_in_root(file_abs, root):
            return
        with _BUILD_LOCK:
            build(root, db, defs_path=_resolve_defs_path(root), verbose=False)
    except Exception:
        # Best-effort: never block the user op.
        return


# ----- shared formatting ----------------------------------------------------


def _format_symbol_line(s: dict) -> str:
    """Outline-style one-liner for ``list`` / ``file`` / ``lookup`` /
    ``kind`` output.

    Uses ``qualified_name`` directly so the displayed form matches what
    the model can pass back to subsequent queries verbatim. For older
    on-demand-parsed symbol dicts that pre-date the v2 schema, the
    fallback to ``name`` keeps display working.
    """
    label = s.get("qualified_name") or s.get("name", "")
    range_str = (
        f":{s['line']}"
        if s["line"] == s["end_line"]
        else f":{s['line']}-{s['end_line']}"
    )
    decl = "" if s.get("is_definition") else " (decl)"
    return f"{label} ({s['kind']}){decl} {s['file']}{range_str}"


def _resolve_symbol(store, name: str, *, file: str | None = None) -> list[dict]:
    """Dual-lookup resolver for model-supplied symbol names.

    The model usually passes the qualified form it saw in ``list``
    output (``AgentLoop._call_llm`` / ``ns::Foo::bar`` / ``A.B.Setup``).
    Internal walkers store ``qualified_name`` for exactly that — try it
    first. If empty, fall back to bare ``name`` lookup so a stripped
    leaf (``_call_llm``) also resolves.

    Returns the matching symbol record list. Empty list = not found.
    Caller decides what to do with multi-match (some modes want a
    single target, others embrace multiple).

    ``file`` (root-relative path) optionally narrows the search to one
    file — used by per-file modes like ``fetch`` so a bare leaf doesn't
    accidentally pick the wrong file's symbol.
    """
    target = _normalize_markdown_name(name)
    kw_q: dict = {"qualified_name": target}
    kw_n: dict = {"name": target}
    if file is not None:
        kw_q["file"] = file
        kw_n["file"] = file
    syms = store.find_symbols(**kw_q)
    if syms:
        return syms
    return store.find_symbols(**kw_n)


def _format_ref_line(r: dict) -> str:
    """One-liner for ``refs`` output."""
    return f"{r['file']}:{r['line']}:{r['col']} {r['kind']} {r['name']}"


def _fetch_body_hashline(file_abs: Path, line: int, end_line: int) -> str:
    """Read the file and return the [line..end_line] window in hashline
    format. 1-indexed inclusive bounds — caller's responsibility."""
    text = file_abs.read_text(encoding="utf-8", errors="replace")
    all_lines = text.splitlines()
    return format_hashlines_range(all_lines, line - 1, end_line)


def _pick_definition(matches: list[dict]) -> dict:
    """Prefer ``is_definition=True`` over declarations when picking one
    symbol from a list of name-matches."""
    defs = [s for s in matches if s.get("is_definition")]
    return defs[0] if defs else matches[0]


def _normalize_markdown_name(name: str) -> str:
    """``## Setup`` → ``Setup``; ``Setup`` → ``Setup``.

    Convenience so the model can pass either the marker form (which
    shows up in raw markdown) or the canonical name (which the walker
    emits) to ``mode='fetch'``. Other languages don't have markers in
    their names so this is a no-op for them.
    """
    stripped = name.lstrip("# ").rstrip(" #").strip()
    return stripped or name


# ----- on-demand parse (path outside indexed root) --------------------------


def _on_demand_symbols(file_abs: Path) -> list[dict] | None:
    """Parse ONE file with its registered walker and return symbols as
    dicts. Returns None if no walker handles the extension."""
    lang = language_of(file_abs)
    if lang is None:
        return None
    try:
        raw = file_abs.read_bytes()
    except OSError:
        return None
    spec = LANGUAGES[lang]
    # 색인 경로와 같은 기본 커널 버전(v9.24.9) — 색인 안팎에서 같은 파일이
    # 다르게 보이지 않게. 파일에 LINUX_VERSION_CODE 가 없으면 무비용(건너뜀).
    cleaned = spec.preprocess(raw, with_default_version([])) if spec.preprocess else raw
    parser = get_parser(lang)
    tree = parser.parse(cleaned)
    syms: list = []
    # On-demand walks use the file's basename as ``file`` so display
    # output looks reasonable — there's no relative-to-root to compute.
    spec.walk_definitions(tree.root_node, cleaned, file_abs.name, syms)
    return [asdict(s) for s in syms]


# ----- mode handlers --------------------------------------------------------


# ── 의미론 검증 (C7, v4.49.0) — mode별 요구사항의 선언 테이블 ─────────
# 이전엔 ~10개 _do_* 핸들러가 _require/_validate_kind 를 제각각 호출했다.
# 단일 함수로 수렴: CodeIndexTool.validate(A5 경로)와 _dispatch_one(직접
# 호출자 방어)이 같은 구현을 소비 — 로직 1곳, 실행 2곳(µs).
_MODE_REQUIRED: dict[str, tuple[str, ...]] = {
    "list": ("path",),
    "fetch": ("path", "name"),
    "lookup": ("name",),
    "kind": ("symbol_kind",),
    "file": ("path",),
    "refs": ("name",),
    "callers": ("name",),
    "callees": ("name",),
    "slice": ("name",),
}


def _quoted(values) -> str:
    """``"a", "b", "c"`` — 값은 큰따옴표 (표기 규칙, docs/prompt-notation)."""
    return ", ".join(f'"{v}"' for v in values)


def _validate_semantics(args: dict) -> str | None:
    """mode 존재/enum + mode별 필수 키 + kind enum — None=통과, str=오류.
    v10.92.0: 문구는 표기 규칙대로(인자 백틱·값 큰따옴표) — 종전엔 Python repr
    (``'mode'``·``['build', …]``)였다."""
    mode = args.get("mode")
    if not mode:
        return f"`mode` is required — one of {_quoted(_MODES)}"
    if mode not in _MODES:
        return f'`mode` "{mode}" is not one of {_quoted(_MODES)}'
    for key in _MODE_REQUIRED.get(mode, ()):
        if not args.get(key):
            return f'`{key}` is required for `mode` "{mode}"'
    symbol_kind = args.get("symbol_kind")
    if (
        mode in ("lookup", "kind")
        and symbol_kind is not None
        and symbol_kind not in NAME_KINDS
    ):
        return (
            f'`symbol_kind` "{symbol_kind}" is not one of {_quoted(sorted(NAME_KINDS))}'
        )
    if mode == "refs":
        ref_kind = args.get("ref_kind")
        if ref_kind is not None and ref_kind not in REF_KINDS:
            return f'`ref_kind` "{ref_kind}" is not one of {_quoted(sorted(REF_KINDS))}'
    return None


def _index_scope(store, root: Path) -> str:
    """빈 결과에 붙는 범위 한 문장 (v10.92.0). 벤치에서 lookup 5·refs 3 이 전부 빈
    결과였는데 색인에 파일이 하나도 없다는 사실은 어디에도 없었다 — 모델은 이름을
    바꿔 다시 찾거나 셸로 갔다."""
    n_files = len(store.files)
    if n_files == 0:
        return (
            f"The index has no files: nothing under {root} has a supported extension."
        )
    return (
        f"among the {store.n_symbols():,} indexed symbols "
        f"({n_files:,} files under {root})"
    )


def _no_match(store, root: Path, what: str) -> str:
    """``what`` 를 찾지 못했다 — 색인이 비었으면 그 사실만, 아니면 범위와 함께."""
    scope = _index_scope(store, root)
    if scope.startswith("The index has no files"):
        return scope
    return f"{what} {scope}."


def _do_list(action_input: dict) -> ToolResult:
    path_str = action_input.get("path")
    path_abs = Path(path_str).resolve()
    if not path_abs.is_file():
        return ToolResult(False, error=f"file not found: {path_str}")
    if language_of(path_abs) is None:
        return ToolResult(
            False,
            error=(
                f"unsupported file extension: {path_abs.suffix}. "
                f"Use `read_file` for non-code/non-markdown files."
            ),
        )

    store, root = _ensure_index()
    if _path_in_root(path_abs, root):
        rel = str(path_abs.resolve().relative_to(root.resolve()))
        syms = store.find_symbols(file=rel)
    else:
        # Out-of-root: parse on the spot, no DB write.
        syms = _on_demand_symbols(path_abs) or []

    search = action_input.get("search")
    if search:
        try:
            regex = re.compile(search)
        except re.error as e:
            return ToolResult(False, error=f"invalid search pattern: {e}")
        syms = [s for s in syms if regex.search(s["name"])]

    if not syms:
        return ToolResult(True, output="(no symbols found)")

    syms_sorted = sorted(syms, key=lambda s: (s["line"], s["name"]))
    return ToolResult(
        True, output="\n".join(_format_symbol_line(s) for s in syms_sorted)
    )


def _do_fetch(action_input: dict) -> ToolResult:
    path_str = action_input.get("path")
    name = action_input.get("name")
    path_abs = Path(path_str).resolve()
    if not path_abs.is_file():
        return ToolResult(False, error=f"file not found: {path_str}")
    if language_of(path_abs) is None:
        return ToolResult(
            False,
            error=f"unsupported file extension: {path_abs.suffix}",
        )

    store, root = _ensure_index()
    target_name = _normalize_markdown_name(name)
    if _path_in_root(path_abs, root):
        rel = str(path_abs.resolve().relative_to(root.resolve()))
        candidates = _resolve_symbol(store, target_name, file=rel)
    else:
        # On-demand path: walker produces dicts with `qualified_name`
        # populated (PR-3 v2 schema) — try the same dual lookup in
        # memory.
        all_syms = _on_demand_symbols(path_abs) or []
        candidates = [s for s in all_syms if s.get("qualified_name") == target_name]
        if not candidates:
            candidates = [s for s in all_syms if s["name"] == target_name]

    if not candidates:
        return ToolResult(False, error=f"symbol not found: {name}")
    chosen = _pick_definition(candidates)

    try:
        body = _fetch_body_hashline(path_abs, chosen["line"], chosen["end_line"])
    except OSError as e:
        return ToolResult(False, error=f"read error: {e}")
    decl = "" if chosen.get("is_definition") else " [declaration]"
    display = chosen.get("qualified_name") or chosen["name"]
    header = (
        f"# {display} ({chosen['kind']}) :{chosen['line']}-{chosen['end_line']}{decl}"
    )
    hint = _defconfig_hint([{**chosen, "file": str(path_abs)}], root)
    return ToolResult(True, output=header + "\n" + body + hint)


def _do_lookup(action_input: dict) -> ToolResult:
    name = action_input.get("name")
    symbol_kind = action_input.get("symbol_kind")

    store, _ = _ensure_index()
    # Dual lookup: try qualified_name first, fall back to bare name.
    # If symbol_kind filter is supplied, apply it to BOTH paths so the
    # caller still gets a kind-narrow result regardless of which match
    # path hits.
    syms = _resolve_symbol(store, name)
    if symbol_kind is not None:
        syms = [s for s in syms if s["kind"] == symbol_kind]
    if not syms:
        what = (
            f'No "{symbol_kind}" named "{name}"'
            if symbol_kind
            else f'No symbol named "{name}"'
        )
        return ToolResult(True, output=_no_match(store, _resolve_index_root(), what))
    syms_sorted = sorted(syms, key=lambda s: (s["file"], s["line"]))
    hint = _defconfig_hint(syms_sorted, _resolve_index_root())
    return ToolResult(
        True, output="\n".join(_format_symbol_line(s) for s in syms_sorted) + hint
    )


def _do_kind(action_input: dict) -> ToolResult:
    symbol_kind = action_input.get("symbol_kind")

    store, root = _ensure_index()
    syms = store.find_symbols(kind=symbol_kind)

    search = action_input.get("search")
    if search:
        try:
            regex = re.compile(search)
        except re.error as e:
            return ToolResult(False, error=f"invalid search pattern: {e}")
        syms = [s for s in syms if regex.search(s["name"])]

    if not syms:
        what = f'No "{symbol_kind}" symbol'
        if search:
            what += f' whose name matches "{search}"'
        return ToolResult(True, output=_no_match(store, root, what))
    syms_sorted = sorted(syms, key=lambda s: (s["file"], s["line"]))
    return ToolResult(
        True, output="\n".join(_format_symbol_line(s) for s in syms_sorted)
    )


def _do_file(action_input: dict) -> ToolResult:
    path_str = action_input.get("path")
    path_abs = Path(path_str).resolve()

    store, root = _ensure_index()
    if not _path_in_root(path_abs, root):
        return ToolResult(
            False,
            error=(
                f'"file" sees only files under {root}; {path_str} is outside it '
                '— "list" reads a file anywhere.'
            ),
        )
    rel = str(path_abs.resolve().relative_to(root.resolve()))
    syms = store.find_symbols(file=rel)
    if not syms:
        return ToolResult(True, output=f"No symbols in {rel}.")
    syms_sorted = sorted(syms, key=lambda s: (s["line"], s["name"]))
    return ToolResult(
        True, output="\n".join(_format_symbol_line(s) for s in syms_sorted)
    )


def _do_refs(action_input: dict) -> ToolResult:
    name = action_input.get("name")
    ref_kind = action_input.get("ref_kind")

    store, root = _ensure_index()
    # Refs table stores bare names (because they come from raw source
    # identifiers). If the model passed a qualified form, resolve it
    # via the symbols table to a bare leaf first.
    syms = _resolve_symbol(store, name)
    bare = syms[0]["name"] if syms else _normalize_markdown_name(name)
    refs = store.find_refs(name=bare, kind=ref_kind)
    if not refs:
        what = f'No "{ref_kind}" use of "{name}"' if ref_kind else f'No use of "{name}"'
        return ToolResult(True, output=_no_match(store, root, what))
    refs_sorted = sorted(refs, key=lambda r: (r["file"], r["line"], r["col"]))
    return ToolResult(True, output="\n".join(_format_ref_line(r) for r in refs_sorted))


#: 정의가 여럿인 이름에서 보여 주는 정의 수.
_MAX_DEFINITIONS = 5


def _sites(sites: list) -> str:
    out = ", ".join(f"{f}:{ln}" for f, ln, _ in sites[:5])
    if len(sites) > 5:
        out += f", … ({len(sites)} total)"
    return out


def _header(edges) -> str:
    d = edges.definitions[0]
    if edges.by_name_only:
        where = (
            f"{d['file']}:{d['line']}-{d['end_line']}"
            if len(edges.definitions) == 1
            else f"any of {len(edges.definitions)} definitions"
        )
        return (
            f"{d['name']} — {where}, matched by name only "
            f"({edges.language} is not scope-resolved)"
        )
    return f"{d['qualified_name']} — {d['file']}:{d['line']}-{d['end_line']}"


def _neighbor_lines(edges) -> list[str]:
    """이웃마다 한 줄 ``name  (N×)  file:line, …`` — 해석된 것 먼저(횟수 많은 순, 같으면
    이름 순), 이름으로만 맞춘 것은 ``?`` 와 정의 수를 단다."""
    lines = []
    known = [
        n for n in edges.neighbors if n.symbol_id is not None or edges.by_name_only
    ]
    guessed = [
        n for n in edges.neighbors if n.symbol_id is None and not edges.by_name_only
    ]
    for n in sorted(known, key=lambda n: (-n.count, n.label)):
        lines.append(f"{n.label}  ({n.count}×)  {_sites(n.sites)}")
    for n in sorted(guessed, key=lambda n: (-n.count, n.label)):
        lines.append(
            f"? {n.label}  ({n.count}×, any of {n.n_definitions} definitions)  "
            f"{_sites(n.sites)}"
        )
    return lines


def _edges_text(store, defs: list[dict], query) -> tuple[str, bool]:
    """``(text, any_edge)`` — 정의마다 머리 + 이웃 줄. 이름으로만 맞추는 언어는 그
    언어의 정의들을 한 덩어리로."""
    from agent_cli.code_index.schema import SCOPE_RESOLVED_LANGUAGES

    groups: list[dict] = []
    seen_langs: set[str] = set()
    for d in defs:
        if d["language"] in SCOPE_RESOLVED_LANGUAGES:
            groups.append(d)
        elif d["language"] not in seen_langs:
            seen_langs.add(d["language"])
            groups.append(d)
    blocks: list[str] = []
    any_edge = False
    unknown_line = None
    for d in groups[:_MAX_DEFINITIONS]:
        edges = query(store, d)
        lines = _neighbor_lines(edges)
        any_edge = any_edge or bool(lines)
        block = [_header(edges), *(lines or ["(none)"])]
        blocks.append("\n".join(block))
        if edges.unknown_sites and unknown_line is None:
            any_edge = True
            unknown_line = (
                f'? "{d["name"]}" on an object of unknown type — any of '
                f"{edges.unknown_definitions} definitions:  {_sites(edges.unknown_sites)}"
            )
    text = "\n\n".join(blocks)
    if len(groups) > _MAX_DEFINITIONS:
        text += (
            f"\n\n… {len(groups) - _MAX_DEFINITIONS} more definitions share this name"
            ' — "lookup" lists them; the qualified name picks one.'
        )
    if unknown_line:
        text += "\n\n" + unknown_line
    return text, any_edge


def _do_callers(action_input: dict) -> ToolResult:
    name = action_input.get("name")
    store, root = _ensure_index()
    defs = definitions_named(store, _normalize_markdown_name(name))
    text, any_edge = _edges_text(store, defs, callers_of) if defs else ("", False)
    if not any_edge:
        return ToolResult(
            True, output=_no_match(store, root, f'No indexed function calls "{name}"')
        )
    return ToolResult(True, output=text)


def _do_callees(action_input: dict) -> ToolResult:
    name = action_input.get("name")
    store, root = _ensure_index()
    defs = definitions_named(store, _normalize_markdown_name(name))
    text, any_edge = _edges_text(store, defs, callees_of) if defs else ("", False)
    if not any_edge:
        return ToolResult(
            True, output=_no_match(store, root, f'"{name}" calls no indexed function')
        )
    return ToolResult(True, output=text)


def _do_slice(action_input: dict) -> ToolResult:
    name = action_input.get("name")
    store, _ = _ensure_index()
    # cmd_slice picks a symbol by bare ``name``; resolve qualified
    # input through the symbols table first.
    syms = _resolve_symbol(store, name)
    bare = syms[0]["name"] if syms else _normalize_markdown_name(name)
    text = cmd_slice(
        store,
        bare,
        with_callees=bool(action_input.get("with_callees", False)),
        with_callers=bool(action_input.get("with_callers", False)),
        with_types=bool(action_input.get("with_types", False)),
        with_macros=bool(action_input.get("with_macros", False)),
        depth=int(action_input.get("depth", 1)),
        max_bytes=int(action_input.get("max_bytes", 0)),
    )
    return ToolResult(True, output=text)


_MODES = {
    "list": _do_list,
    "fetch": _do_fetch,
    "lookup": _do_lookup,
    "kind": _do_kind,
    "file": _do_file,
    "refs": _do_refs,
    "callers": _do_callers,
    "callees": _do_callees,
    "slice": _do_slice,
}


# ----- entry point ----------------------------------------------------------


def _dispatch_one(query: dict) -> ToolResult:
    """Dispatch a single code_index query to its mode handler.

    Per-mode arg requirements are documented in the registry ToolSchema
    description; handlers enforce them and return a descriptive error
    if missing.
    """
    if not isinstance(query, dict):
        return ToolResult(
            False,
            error=f"each query must be an object, got {type(query).__name__}",
        )
    # C7: 의미론 검증은 _validate_semantics 단일 구현 — A5 경로
    # (CodeIndexTool.validate)와 직접 호출자가 같은 것을 소비.
    err = _validate_semantics(query)
    if err:
        return ToolResult(False, error=err)
    return _MODES[query["mode"]](query)


#: 색인하는 확장자 — 설명 한 곳에만 (v10.92.0; 종전엔 안내 블록 끝에).
_EXTENSIONS = ", ".join(get_supported_extensions())


class CodeIndexTool(Tool):
    name = "code_index"
    # v10.92.0 (docs/tool-review/TRACKER.md): 안내 블록(1,104 토큰, 호출 모양 예시
    # 11개)을 지우고 모드 설명을 `mode` 인자 한 곳에 — 모델이 보는 글이 1,689 → 683
    # 토큰. 예시의 가짜 경로(`agent_cli/loop.py`)를 벤치의 모델이 실제 경로로 오인해
    # 찔러본 기록이 4건. SQLite 경로·sha1 증분·"no DB write" 같은 내부 사실은 모델의
    # 선택을 바꾸지 않아 뺐다. 이름 규칙과 확장자는 설명에.
    description = (
        "Query a read-only index of the project's code and markdown; one call runs "
        "one query, chosen by `mode`. Symbol names are written as the language "
        'writes them: "Class.method" in Python, JavaScript and TypeScript, '
        '"ns::Type::method" in C, C++ and Rust, a heading\'s text for markdown '
        '("Setup" or "## Setup"). Indexed files have one of these extensions: '
        f"{_EXTENSIONS}; other files are for `read_file`."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": [
                    "list",
                    "fetch",
                    "lookup",
                    "kind",
                    "file",
                    "refs",
                    "callers",
                    "callees",
                    "slice",
                ],
                "description": (
                    '"list": the outline of one file (`path`), one symbol per line as '
                    "`name (kind) file:start-end`; `search` keeps the names it matches. "
                    '"fetch": the body of one symbol (`path`, `name`) with LINE#HASH '
                    "refs, ready for `edit_file`; a definition wins over a declaration. "
                    '"lookup": where `name` is defined anywhere in the project; '
                    '`symbol_kind` narrows. "kind": every symbol of one `symbol_kind` '
                    'in the project; `search` narrows. "file": every symbol defined in '
                    'one file (`path`). "refs": every place `name` is used; `ref_kind` '
                    'narrows. "callers" and "callees": the functions that call `name`, '
                    "or that `name` calls, under a heading per definition of `name`; a "
                    'line starting with "?" is a call on an object of unknown type, '
                    "which may reach any of the definitions it counts. "
                    '"slice": the body of `name` together with '
                    "its neighbours — `with_callees`, `with_callers`, `with_types`, "
                    '`with_macros` — to `depth`. "list" and "fetch" read any file; the '
                    "other modes see only files under the project root."
                ),
            },
            "path": {
                "type": "string",
                "description": 'File path, for "list", "fetch" and "file".',
            },
            "name": {
                "type": "string",
                "description": (
                    'Symbol name as "list" or "lookup" shows it, for "fetch", "lookup", '
                    '"refs", "callers", "callees" and "slice". A qualified name picks '
                    "one definition when several share the name."
                ),
            },
            "symbol_kind": {
                "type": "string",
                "enum": [
                    "function",
                    "type",
                    "variable",
                    "constant",
                    "section",
                ],
                "description": (
                    'Symbol category: "function", "type", "variable", "constant" or '
                    '"section" (a markdown heading). Required for "kind", optional for '
                    '"lookup".'
                ),
            },
            "ref_kind": {
                "type": "string",
                "enum": ["call", "name", "type"],
                "description": (
                    'Kind of use, for "refs": "call" (an invocation), "name" (the bare '
                    'identifier — a callback, a pointer) or "type" (in a type position).'
                ),
            },
            "search": {
                "type": "string",
                "description": (
                    'A regex; "list" and "kind" keep only the symbols whose name matches.'
                ),
            },
            "with_callees": {
                "type": "boolean",
                "description": (
                    '"slice": include the bodies of the functions it calls, to `depth`.'
                ),
            },
            "with_callers": {
                "type": "boolean",
                "description": (
                    '"slice": include the bodies of the functions that call it, to '
                    "`depth`."
                ),
            },
            "with_types": {
                "type": "boolean",
                "description": '"slice": include the types it uses.',
            },
            "with_macros": {
                "type": "boolean",
                "description": '"slice": include the function-like macros it invokes.',
            },
            "depth": {
                "type": "integer",
                "minimum": 1,
                "maximum": 5,
                "description": (
                    '"slice": how many calls away callees and callers are followed '
                    "(default 1, at most 5)."
                ),
            },
            "max_bytes": {
                "type": "integer",
                "minimum": 0,
                "description": (
                    '"slice": cap on the output size in bytes (default none).'
                ),
            },
        },
        "required": ["mode"],
    }

    oversized_retry_hint = (
        'narrow the query itself: `mode` "fetch" for one symbol (`path` + `name`), '
        '`search` (a regex) to filter "list"/"kind", a specific `path` instead of a '
        'broad dump, or a lower `max_bytes` for "slice".'
    )

    def wrap_single_op(self, flat: dict) -> dict:
        return flat

    def touched_paths(self, action_input: dict) -> list[str]:
        p = self.strip_prefix(action_input).get("path")
        return [p] if isinstance(p, str) and p else []

    def summary_arg(self, action_input: dict) -> str:
        std = self.strip_prefix(action_input)
        return f"{std.get('mode', '')} {std.get('path', '')}".strip()

    def validate(self, args: dict) -> str | None:
        """C7: mode 존재/enum·mode별 필수 키·kind enum — _validate_semantics
        단일 구현 위임 (직접 호출자 경로 _dispatch_one 과 동일 소스)."""
        return _validate_semantics(args)

    def _run(self, args: dict, *, ctx=None) -> ToolResult:
        return _dispatch_one(args)
