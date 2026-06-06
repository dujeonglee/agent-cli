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
  build      force a full rebuild of the SQLite index

Indexed root resolution: walk up from cwd looking for an existing
``.agent-cli/`` directory; fall back to cwd. DB lives at
``<root>/.agent-cli/code_index.db`` and is lazy-built on first access
plus per-query incrementally refreshed by ``build()``'s sha1 path.

Paths outside the indexed root fall through to an on-demand parse for
``list`` / ``fetch`` only; index-scoped modes (``lookup``, ``kind``,
``refs``, ``callers``, ``callees``, ``slice``, ``build``) return a
clear error if the supplied path is out-of-root.
"""

from __future__ import annotations

import re
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from agent_cli.code_index import (
    IndexStore,
    build,
    build_callgraph,
    cmd_slice,
    load_index,
)
from agent_cli.code_index.builder import get_parser
from agent_cli.code_index.languages import LANGUAGES, language_of
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


def _resolve_defs_path(root: Path) -> Optional[Path]:
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
    next ``code_index`` query sees the change without the model having
    to manually trigger ``mode='build'``. Semantics:

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


def _resolve_symbol(store, name: str, *, file: Optional[str] = None) -> list[dict]:
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


def _on_demand_symbols(file_abs: Path) -> Optional[list[dict]]:
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
    cleaned = spec.preprocess(raw, []) if spec.preprocess else raw
    parser = get_parser(lang)
    tree = parser.parse(cleaned)
    syms: list = []
    # On-demand walks use the file's basename as ``file`` so display
    # output looks reasonable — there's no relative-to-root to compute.
    spec.walk_definitions(tree.root_node, cleaned, file_abs.name, syms)
    return [asdict(s) for s in syms]


# ----- mode handlers --------------------------------------------------------


def _do_list(action_input: dict) -> ToolResult:
    path_str = action_input.get("path")
    if not path_str:
        return ToolResult(False, error="'path' is required for mode='list'")
    path_abs = Path(path_str).resolve()
    if not path_abs.is_file():
        return ToolResult(False, error=f"file not found: {path_str}")
    if language_of(path_abs) is None:
        return ToolResult(
            False,
            error=(
                f"unsupported file extension: {path_abs.suffix}. "
                f"Use read_file for non-code/non-markdown files."
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
    if not path_str:
        return ToolResult(False, error="'path' is required for mode='fetch'")
    if not name:
        return ToolResult(False, error="'name' is required for mode='fetch'")
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
    return ToolResult(True, output=header + "\n" + body)


def _do_lookup(action_input: dict) -> ToolResult:
    name = action_input.get("name")
    if not name:
        return ToolResult(False, error="'name' is required for mode='lookup'")
    symbol_kind = action_input.get("symbol_kind")
    if symbol_kind is not None and symbol_kind not in NAME_KINDS:
        return ToolResult(
            False,
            error=(
                f"invalid symbol_kind: {symbol_kind!r}. Valid: {sorted(NAME_KINDS)}"
            ),
        )

    store, _ = _ensure_index()
    # Dual lookup: try qualified_name first, fall back to bare name.
    # If symbol_kind filter is supplied, apply it to BOTH paths so the
    # caller still gets a kind-narrow result regardless of which match
    # path hits.
    syms = _resolve_symbol(store, name)
    if symbol_kind is not None:
        syms = [s for s in syms if s["kind"] == symbol_kind]
    if not syms:
        return ToolResult(True, output=f"(no symbols match name={name!r})")
    syms_sorted = sorted(syms, key=lambda s: (s["file"], s["line"]))
    return ToolResult(
        True, output="\n".join(_format_symbol_line(s) for s in syms_sorted)
    )


def _do_kind(action_input: dict) -> ToolResult:
    symbol_kind = action_input.get("symbol_kind")
    if not symbol_kind:
        return ToolResult(False, error="'symbol_kind' is required for mode='kind'")
    if symbol_kind not in NAME_KINDS:
        return ToolResult(
            False,
            error=(
                f"invalid symbol_kind: {symbol_kind!r}. Valid: {sorted(NAME_KINDS)}"
            ),
        )

    store, _ = _ensure_index()
    syms = store.find_symbols(kind=symbol_kind)

    search = action_input.get("search")
    if search:
        try:
            regex = re.compile(search)
        except re.error as e:
            return ToolResult(False, error=f"invalid search pattern: {e}")
        syms = [s for s in syms if regex.search(s["name"])]

    if not syms:
        return ToolResult(True, output=f"(no symbols of kind={symbol_kind!r})")
    syms_sorted = sorted(syms, key=lambda s: (s["file"], s["line"]))
    return ToolResult(
        True, output="\n".join(_format_symbol_line(s) for s in syms_sorted)
    )


def _do_file(action_input: dict) -> ToolResult:
    path_str = action_input.get("path")
    if not path_str:
        return ToolResult(False, error="'path' is required for mode='file'")
    path_abs = Path(path_str).resolve()

    store, root = _ensure_index()
    if not _path_in_root(path_abs, root):
        return ToolResult(
            False,
            error=(
                f"mode='file' is index-scoped — path {path_str!r} is outside "
                f"the indexed root ({root}). Use mode='list' for one-off "
                f"out-of-root files."
            ),
        )
    rel = str(path_abs.resolve().relative_to(root.resolve()))
    syms = store.find_symbols(file=rel)
    if not syms:
        return ToolResult(True, output=f"(no symbols in {rel})")
    syms_sorted = sorted(syms, key=lambda s: (s["line"], s["name"]))
    return ToolResult(
        True, output="\n".join(_format_symbol_line(s) for s in syms_sorted)
    )


def _do_refs(action_input: dict) -> ToolResult:
    name = action_input.get("name")
    if not name:
        return ToolResult(False, error="'name' is required for mode='refs'")
    ref_kind = action_input.get("ref_kind")
    if ref_kind is not None and ref_kind not in REF_KINDS:
        return ToolResult(
            False,
            error=(f"invalid ref_kind: {ref_kind!r}. Valid: {sorted(REF_KINDS)}"),
        )

    store, _ = _ensure_index()
    # Refs table stores bare names (because they come from raw source
    # identifiers). If the model passed a qualified form, resolve it
    # via the symbols table to a bare leaf first.
    syms = _resolve_symbol(store, name)
    bare = syms[0]["name"] if syms else _normalize_markdown_name(name)
    refs = store.find_refs(name=bare, kind=ref_kind)
    if not refs:
        filt = f" {ref_kind=}" if ref_kind else ""
        return ToolResult(True, output=f"(no refs to {name!r}{filt})")
    refs_sorted = sorted(refs, key=lambda r: (r["file"], r["line"], r["col"]))
    return ToolResult(True, output="\n".join(_format_ref_line(r) for r in refs_sorted))


def _do_callers(action_input: dict) -> ToolResult:
    name = action_input.get("name")
    if not name:
        return ToolResult(False, error="'name' is required for mode='callers'")
    store, _ = _ensure_index()
    # Callgraph indexes by bare name (refs are bare-name). Resolve
    # qualified → bare via the symbols table.
    syms = _resolve_symbol(store, name)
    bare = syms[0]["name"] if syms else _normalize_markdown_name(name)
    _, callers_of, sites_of = build_callgraph(store)
    callers = callers_of.get(bare)
    if not callers:
        return ToolResult(
            True,
            output=f"(no callers of {name!r} in index — function may not be called)",
        )
    lines = []
    for caller, count in sorted(callers.items(), key=lambda kv: (-kv[1], kv[0])):
        sites = sites_of.get((caller, bare), [])
        site_str = ", ".join(f"{f}:{ln}" for f, ln, _ in sites[:5])
        if len(sites) > 5:
            site_str += f", … ({len(sites)} total)"
        lines.append(f"{caller}  ({count}×)  {site_str}")
    return ToolResult(True, output="\n".join(lines))


def _do_callees(action_input: dict) -> ToolResult:
    name = action_input.get("name")
    if not name:
        return ToolResult(False, error="'name' is required for mode='callees'")
    store, _ = _ensure_index()
    syms = _resolve_symbol(store, name)
    bare = syms[0]["name"] if syms else _normalize_markdown_name(name)
    calls_of, _, sites_of = build_callgraph(store)
    callees = calls_of.get(bare)
    if not callees:
        return ToolResult(
            True,
            output=f"(no callees of {name!r} in index — function may not call anything indexed)",
        )
    lines = []
    for callee, count in sorted(callees.items(), key=lambda kv: (-kv[1], kv[0])):
        sites = sites_of.get((bare, callee), [])
        site_str = ", ".join(f"{f}:{ln}" for f, ln, _ in sites[:5])
        if len(sites) > 5:
            site_str += f", … ({len(sites)} total)"
        lines.append(f"{callee}  ({count}×)  {site_str}")
    return ToolResult(True, output="\n".join(lines))


def _do_slice(action_input: dict) -> ToolResult:
    name = action_input.get("name")
    if not name:
        return ToolResult(False, error="'name' is required for mode='slice'")
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


def _do_build(_action_input: dict) -> ToolResult:
    root = _resolve_index_root()
    db = _resolve_db_path()
    defs = _resolve_defs_path(root)
    with _BUILD_LOCK:
        build(root, db, defs_path=defs, verbose=False, force_full=True)
    store = load_index(db)
    defs_note = f" defconfig: {defs}" if defs else " defconfig: (none)"
    return ToolResult(
        True,
        output=(
            f"Rebuilt index: {store.n_symbols()} symbols, "
            f"{store.n_refs()} refs across {len(store.files)} files. "
            f"Root: {root}.{defs_note}"
        ),
    )


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
    "build": _do_build,
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
    mode = query.get("mode")
    if not mode:
        return ToolResult(
            False,
            error=(f"'mode' is required. Valid: {sorted(_MODES.keys())}"),
        )
    handler = _MODES.get(mode)
    if handler is None:
        return ToolResult(
            False,
            error=(f"unknown mode: {mode!r}. Valid: {sorted(_MODES.keys())}"),
        )
    return handler(query)


def _format_batch(queries: list, results: list[ToolResult]) -> ToolResult:
    """Combine multiple code_index query results into one observation.

    Mirrors :func:`read_file._format_batch` / delegate's parallel formatter:
    per-query header (mode + target) + body (or ERROR), then a summary.
    ``success`` is False only when *every* query failed — a partial success
    stays True (code_index is read-only, so partial results are still useful).
    """
    parts: list[str] = []
    ok = 0
    for i, (q, res) in enumerate(zip(queries, results), 1):
        if isinstance(q, dict):
            label = q.get("mode", "?")
            target = q.get("name") or q.get("path") or ""
            if target:
                label = f"{label} {target}"
        else:
            label = "?"
        parts.append(f"─── [{i}] {label} ───")
        if res.success:
            parts.append(res.output or "(empty)")
            ok += 1
        else:
            parts.append(f"ERROR: {res.error}")
        parts.append("")

    failed = len(queries) - ok
    parts.append(
        f"[code_index batch: {len(queries)} queries, {ok} ok, {failed} failed]"
    )
    combined = "\n".join(parts)
    if ok == 0:
        return ToolResult(False, error=combined)
    return ToolResult(True, output=combined)


def tool_code_index(action_input: dict) -> ToolResult:
    """Run one or more code_index queries. ``action_input["queries"]`` is a
    list of query specs, each consumed by :func:`_dispatch_one`.

    A single-element list returns that query's result verbatim (no batch
    header); multiple queries are combined by :func:`_format_batch`.
    Modes may be mixed within one call (code_index is read-only).
    """
    if not isinstance(action_input, dict):
        return ToolResult(False, error="action_input must be an object")
    queries = action_input.get("queries")
    if not queries or not isinstance(queries, list):
        return ToolResult(
            False,
            error=(
                "code_index requires a non-empty 'code_index_queries' list "
                "(each item: {mode, ...mode-specific args})."
            ),
        )

    results = [_dispatch_one(q) for q in queries]
    if len(results) == 1:
        return results[0]
    return _format_batch(queries, results)


class CodeIndexTool(Tool):
    name = "code_index"
    description = (
        "Code/markdown index queries via persistent tree-sitter SQLite store "
        "(read-only). Provide code_index_queries as a LIST; each item is one "
        "query with its own mode. One call can run many queries (modes may be "
        "mixed). For a single query, pass a one-element list.\n"
        "Modes (per item):\n"
        "  list      - file outline (defs + structural symbols, line ranges) [path]\n"
        "  fetch     - single symbol body, hashline format for edit_file [path, name]\n"
        "  lookup    - find symbol by name across the index [name, symbol_kind?]\n"
        "  kind      - list all symbols of a kind across the index [symbol_kind]\n"
        "  file      - all symbols in a single file (index lookup) [path]\n"
        "  refs      - all ref sites for a name [name, ref_kind?]\n"
        "  callers   - functions that call this one [name]\n"
        "  callees   - functions called by this one [name]\n"
        "  slice     - markdown LLM context: def body + optional callees/callers/"
        "types/macros [name, ...]\n"
        "  build     - force full rebuild (rare - lazy build handles normal cases)\n"
        "Languages: Python, JS/TS, C/C++, Go, Rust, Java, Markdown headings. "
        "Index at <project_root>/.agent-cli/code_index.db, lazy-built and "
        "incrementally refreshed. For 'list'/'fetch' on paths outside the indexed "
        "root: on-demand parse (no DB write). Other modes require the indexed root."
    )
    parameters = {
        "type": "object",
        "properties": {
            "code_index_queries": {
                "type": "array",
                "description": (
                    "List of queries (one or many; modes may be mixed). For a "
                    "single query, pass a one-element list."
                ),
                "items": {
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
                                "build",
                            ],
                            "description": "Operation. See tool description for per-mode params.",
                        },
                        "path": {
                            "type": "string",
                            "description": "File path. Required for list/fetch/file.",
                        },
                        "name": {
                            "type": "string",
                            "description": (
                                "Symbol name (exact, as shown by 'list'). Required for "
                                "fetch/lookup/refs/callers/callees/slice. Markdown 'fetch' "
                                "also accepts the heading with marker (e.g. '## Setup')."
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
                                "Symbol category filter. Optional for lookup. Required for "
                                "kind. 'section' = markdown heading."
                            ),
                        },
                        "ref_kind": {
                            "type": "string",
                            "enum": ["call", "name", "type"],
                            "description": (
                                "Reference site category. Optional for refs. "
                                "call = invocation; name = bare identifier mention "
                                "(callback, pointer); type = identifier in type position."
                            ),
                        },
                        "search": {
                            "type": "string",
                            "description": (
                                "Optional regex (re.search) to filter symbol names. "
                                "Applies to list and kind modes."
                            ),
                        },
                        "with_callees": {
                            "type": "boolean",
                            "description": "slice mode: include callee bodies (transitive up to depth).",
                        },
                        "with_callers": {
                            "type": "boolean",
                            "description": "slice mode: include caller bodies (transitive up to depth).",
                        },
                        "with_types": {
                            "type": "boolean",
                            "description": "slice mode: include types/structs referenced inside target body.",
                        },
                        "with_macros": {
                            "type": "boolean",
                            "description": "slice mode: include function-like macros invoked inside target body.",
                        },
                        "depth": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 5,
                            "description": "slice mode: transitive depth for callees/callers (default 1).",
                        },
                        "max_bytes": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "slice mode: cap output bytes (default unlimited).",
                        },
                    },
                    "required": ["mode"],
                },
            },
        },
        "required": ["code_index_queries"],
    }

    def touched_paths(self, action_input: dict) -> list[str]:
        queries = self.strip_prefix(action_input).get("queries") or []
        return [
            q["path"]
            for q in queries
            if isinstance(q, dict) and isinstance(q.get("path"), str)
        ]

    def _run(self, args: dict, *, session_dir=None) -> ToolResult:
        return tool_code_index(args)
