# Originally ported from minish.ai/Agent-tools tsindex.py (Apache 2.0).
# See NOTICE for the list of modifications.
"""Incremental index builder (Pass-1 / Pass-2 / SQLite writer).

`build(root, out_path, ...)` walks `root`, classifies each file as
reused (sha1 unchanged from prior index) or changed, runs Pass-1
(`walk_definitions`) on changed files, computes the set of names added
since the prior index, runs Pass-2 (`walk_refs`) on changed files plus
unchanged-but-affected files (those whose stored identifier set
intersects the added names), and writes the result to a fresh SQLite
file via `write_sqlite_index`.

`defs_path=None` (the new default) means no C/C++ preprocessor flags —
upstream defaulted to `Path('tsindex.defs')` relative to cwd. The tool
layer resolves a project-local defs file before calling.

v10.96.0 (docs/code-index/DESIGN.md §21): Pass-2 also yields each file's
scopes and bindings; every row gets an explicit id, and a symbol keeps its
id across builds (copied rows keep theirs, a re-parsed file's symbols take
the old ids by `(qualified_name, kind, kind_raw)` in order), so a resolved
ref elsewhere keeps pointing at it. After the rows are written a third pass
resolves the refs of the re-parsed files and of the files whose recorded
inputs (`resolve_deps`: files read, module names, relative paths, function
names) changed (`_resolve_targets`) — from the tables, without a syntax tree.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
import threading
import time
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

from agent_cli.code_index._sqlite import sqlite3
from agent_cli.code_index.languages import (
    LANGUAGES,
    get_supported_extensions,
    language_of,
)
from agent_cli.code_index.preproc import compute_preproc
from agent_cli.code_index.resolve import function_scopes, resolve_files
from agent_cli.code_index.schema import CODE_NAME_KINDS, SCHEMA_VERSION
from agent_cli.code_index.store import IndexStore, load_index

_PARSER_CACHE: dict[str, object] = {}


# Directories the indexer skips by default. Includes the obvious VCS /
# build-artifact / Python-cache patterns plus agent-cli's own
# infrastructure dirs (``.agent-cli`` holds the index itself; ``.claude``
# stores worktrees, command caches, and other ephemerals that look like
# source files to the walker but shouldn't be indexed as part of the
# project). Without this prune the index can balloon 5–10x with stale
# worktree snapshots when the user runs from a repo with active
# branches.
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        # VCS
        ".git",
        ".hg",
        ".svn",
        # Agent infrastructure
        ".agent-cli",
        ".claude",
        # Python virtualenv / cache
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        # JS / TS
        "node_modules",
        # Build artifacts
        "build",
        "dist",
        "target",
        # Tox
        ".tox",
    }
)


def get_parser(lang: str):
    from tree_sitter import Parser

    if lang not in _PARSER_CACHE:
        _PARSER_CACHE[lang] = Parser(LANGUAGES[lang].grammar_factory())
    return _PARSER_CACHE[lang]


# ---------- build ----------


def iter_source_files(root: Path):
    """Iterate source files matching any registered language extension.

    Walks ``root`` with ``os.walk`` so we can prune common irrelevant
    directories (``_SKIP_DIRS``) — the previous ``rglob('*')`` version
    visited every descendant and made the indexer follow VCS / build /
    worktree dirs that have no business being in the index.
    """
    all_exts = set(get_supported_extensions())
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if p.suffix in all_exts:
                yield p


def build(
    root: Path,
    out_path: Path,
    defs_path: Path | None = None,
    undef_unknown_configs: bool = True,
    force_full: bool = False,
    verbose: bool = True,
) -> dict:
    """색인을 ``root`` 의 지금 상태로 맞춘다 — 돌려주는 값은 통계
    ``{"unchanged", "reused", "changed", "affected", "removed"}``.

    v10.93.0 (docs/code-index/DESIGN.md §18): 파일을 읽기 전에 ``stat`` 만으로
    먼저 가른다. 크기·mtime 이 저장된 것과 같으면 그 파일은 읽지도 sha1 을 재지도
    않고, **모든** 파일이 그대로면 아무것도 쓰지 않고 돌아온다(``unchanged``). 바뀐
    파일이 있을 때만 그 파일(과 새 이름에 영향받는 파일)을 파싱하고, 나머지 행은
    옛 DB 에서 SQL 로 복사해 새 파일에 쓴다(원자 교체는 그대로). 종전엔 쿼리마다
    파일 전체를 읽어 sha1 을 재고 기호·참조 전부를 파이썬 dict 로 올린 뒤 DB 를
    통째로 다시 썼다 — 바뀐 것이 없어도 agent-cli 저장소에서 4.0 s, 그리고
    `edit_file`·`write_file` 의 사후 훅이 같은 길이라 편집마다 그 값이 붙었다."""
    t0 = time.time()

    # C-specific preprocessing context. Other languages ignore unifdef_flags
    # and have a no-op preprocess.
    unifdef_flags, preproc_info, fp = compute_preproc(
        root, defs_path, undef_unknown_configs, verbose
    )

    # Try to load existing index for reuse
    old_store: IndexStore | None = None
    invalidation_reason = None
    if not force_full and out_path.is_file():
        try:
            cand = load_index(out_path)
            if cand.meta.get("schema_version") != SCHEMA_VERSION:
                invalidation_reason = (
                    f"schema_version {cand.meta.get('schema_version')} → "
                    f"{SCHEMA_VERSION}"
                )
            elif cand.meta.get("preproc_fingerprint") != fp:
                invalidation_reason = (
                    "preproc fingerprint changed (defs/auto-undef differs)"
                )
            elif cand.meta.get("root") != str(root.resolve()):
                invalidation_reason = "root path differs"
            else:
                old_store = cand
        except Exception as e:
            invalidation_reason = f"could not read old index: {e}"
    elif force_full:
        invalidation_reason = "--full requested"

    old_files_meta = {f["path"]: f for f in (old_store.files if old_store else [])}

    # stat 선행: (rel, path, size, mtime_ns, lang) — 아직 아무 파일도 읽지 않았다.
    stats: list[tuple[str, Path, int, int, str]] = []
    for p in iter_source_files(root):
        lang = language_of(p)
        if lang is None:
            continue
        st = p.stat()
        stats.append((str(p.relative_to(root)), p, st.st_size, st.st_mtime_ns, lang))

    def _same_stat(old: dict | None, size: int, mtime_ns: int) -> bool:
        return (
            old is not None
            and old.get("size") == size
            and old.get("mtime_ns") == mtime_ns
            and old.get("identifiers") is not None
        )

    if (
        old_store is not None
        and len(stats) == len(old_files_meta)
        and all(
            _same_stat(old_files_meta.get(rel), size, mtime)
            for rel, _, size, mtime, _ in stats
        )
    ):
        if verbose:
            print(f"  [unchanged: {len(stats)} files]", file=sys.stderr)
        return {
            "unchanged": True,
            "reused": len(stats),
            "changed": 0,
            "affected": 0,
            "removed": 0,
        }

    # Walk files, classify as reused vs changed. A file whose size·mtime match
    # is reused without being read; otherwise its sha1 decides.
    raws: dict[str, bytes] = {}

    def raw_of(rel: str, path: Path) -> bytes:
        if rel not in raws:
            raws[rel] = path.read_bytes()
        return raws[rel]

    file_records: list[tuple[str, Path, str, bool, str, int, int]] = []
    n_bytes = 0
    for rel, path, size, mtime, lang in stats:
        old = old_files_meta.get(rel)
        if _same_stat(old, size, mtime):
            file_records.append((rel, path, old["sha1"], True, lang, size, mtime))
            continue
        raw = raw_of(rel, path)
        n_bytes += len(raw)
        h = hashlib.sha1(raw).hexdigest()
        reused = bool(
            old is not None
            and old.get("sha1") == h
            and old.get("identifiers") is not None
        )
        file_records.append((rel, path, h, reused, lang, size, mtime))

    # Per-file data (everything as plain dicts for uniformity)
    syms_by_file: dict[str, list] = {}
    idents_by_file: dict[str, list] = {}
    meta_by_file: dict[str, dict] = {}
    trees: dict[str, tuple] = {}  # rel → (cleaned bytes, tree)

    # Pre-populate reused files — meta only (their rows are copied by SQL);
    # a touched-but-identical file takes the new size·mtime.
    for rel, _, _, reused, _, size, mtime in file_records:
        if reused:
            old = old_files_meta[rel]
            idents_by_file[rel] = old.get("identifiers") or []
            meta_by_file[rel] = {**old, "size": size, "mtime_ns": mtime}

    def parse(rel: str, path: Path, lang: str):
        if rel not in trees:
            raw = raw_of(rel, path)
            spec = LANGUAGES[lang]
            cleaned = spec.preprocess(raw, unifdef_flags) if spec.preprocess else raw
            trees[rel] = (cleaned, get_parser(lang).parse(cleaned))
        return trees[rel]

    def pass1(rel: str, path: Path, lang: str) -> list:
        cleaned, tree = parse(rel, path, lang)
        syms: list = []
        LANGUAGES[lang].walk_definitions(tree.root_node, cleaned, rel, syms)
        syms_by_file[rel] = [asdict(s) for s in syms]
        return syms

    # Pass 1 (defs) for changed files
    for rel, path, h, reused, lang, size, mtime in file_records:
        if reused:
            continue
        raw = raw_of(rel, path)
        syms = pass1(rel, path, lang)
        meta_by_file[rel] = {
            "path": rel,
            "size": size,
            "mtime_ns": mtime,
            "lines": raw.count(b"\n") + 1,
            "sha1": h,
            "has_error": trees[rel][1].root_node.has_error,
            "n_symbols": len(syms),
            "language": lang,
        }

    # Names newly defined since the prior index — only the changed files can
    # add one; the old names come from the DB (no full symbol load).
    new_defined = {
        s["name"]
        for syms in syms_by_file.values()
        for s in syms
        if s["kind"] in CODE_NAME_KINDS
    }
    old_defined: set[str] = old_store.code_names() if old_store else set()
    added_names = new_defined - old_defined

    # Find unchanged files whose ref set could be affected by newly-added names
    # (Option B from the design: only re-Pass2 files that actually mention the
    # new identifier somewhere in their source.)
    affected: set[str] = set()
    if added_names and old_store is not None:
        for rel, _, _, reused, _, _, _ in file_records:
            if not reused:
                continue
            ids = idents_by_file.get(rel)
            if ids and added_names.intersection(ids):
                affected.add(rel)

    # Affected files are re-walked whole (their symbols keep their ids — see
    # `_assign_symbol_ids`); the rest of the reused files are copied by SQL.
    for rel, path, _, _, lang, _, _ in file_records:
        if rel in affected:
            pass1(rel, path, lang)
    parsed = [r for r in file_records if not r[3] or r[0] in affected]
    parsed_set = {r[0] for r in parsed}
    if old_store is not None:
        defined_all = new_defined | old_defined
    else:
        defined_all = new_defined

    ids = _IdAllocator(old_store._conn if old_store is not None else None)
    rows = {"symbols": [], "refs": [], "scopes": [], "bindings": []}
    for rel, path, _, _, lang, _, _ in parsed:
        cleaned, tree = trees[rel]
        spec = LANGUAGES[lang]
        syms = syms_by_file[rel]
        ids.assign_symbols(rel, syms)
        idents: set[str] = set()
        if spec.walk_scopes is not None:
            scopes, binds, new_refs = spec.walk_scopes(
                tree.root_node, cleaned, rel, defined_all, idents
            )
            ref_dicts = [asdict(r) for r in new_refs]
        else:
            new_refs = []
            spec.walk_refs(tree.root_node, cleaned, rel, new_refs, defined_all, idents)
            ref_dicts = [asdict(r) for r in new_refs]
            scopes, binds = function_scopes(
                syms, ref_dicts, tree.root_node.end_point[0] + 1
            )
        _link_rows(rel, syms, scopes, binds, ref_dicts, ids, rows)
        idents_by_file[rel] = sorted(idents)
        meta_by_file[rel]["identifiers"] = idents_by_file[rel]

    # Assemble final ordered lists (deterministic, sorted by file path). Only
    # parsed rows are here; reused files' rows come from the old DB.
    files_meta_list = [meta_by_file[r[0]] for r in file_records]
    copied = [r[0] for r in file_records if r[0] not in parsed_set]
    present = {r[0] for r in file_records}
    removed = sorted(set(old_files_meta) - present)
    n_removed = len(removed)

    n_files = len(file_records)
    n_reused = sum(1 for r in file_records if r[3])
    n_changed = n_files - n_reused
    elapsed = time.time() - t0

    if verbose:
        if old_store is None:
            mode = (
                f"full rebuild ({invalidation_reason})"
                if invalidation_reason
                else "full rebuild (no prior index)"
            )
        else:
            mode = (
                f"incremental: reused {n_reused}, changed {n_changed}, "
                f"affected {len(affected)}, removed {n_removed}"
            )
        print(f"  [{mode}]", file=sys.stderr)

    out = {
        "schema_version": SCHEMA_VERSION,
        "root": str(root.resolve()),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "elapsed_seconds": round(elapsed, 3),
        "preprocessing": preproc_info,
        "preproc_fingerprint": fp,
        "files": files_meta_list,
        "next_ids": ids.next,
        **rows,
    }
    resolved_counts: dict[str, int] = {}

    def resolve(conn) -> None:
        if old_store is None:
            targets = [r[0] for r in file_records]
        else:
            targets = _resolve_targets(
                conn,
                sorted(parsed_set),
                removed,
                _python_files(file_records, old_files_meta),
            )
        resolve_files(conn, targets)
        resolved_counts["files"] = len(targets)

    write_sqlite_index(
        out_path,
        out,
        copy_from=out_path if old_store is not None else None,
        copy_files=copied,
        resolve=resolve,
    )
    if verbose:
        print(
            f"\nIndexed {n_files} files ({n_bytes / 1024:.1f} KiB read) in {elapsed:.2f}s",
            file=sys.stderr,
        )
        print(
            f"  {len(rows['symbols'])} symbols, {len(rows['refs'])} refs parsed; "
            f"{len(copied)} files copied, {resolved_counts.get('files', 0)} resolved",
            file=sys.stderr,
        )
        print(
            f"  wrote {out_path}  ({out_path.stat().st_size / 1024:.1f} KiB)",
            file=sys.stderr,
        )
    return {
        "unchanged": False,
        "reused": n_reused,
        "changed": n_changed,
        "affected": len(affected),
        "removed": n_removed,
        "resolved": resolved_counts.get("files", 0),
    }


def _python_files(file_records, old_files_meta) -> tuple[set[str], set[str]]:
    """``(옛 파이썬 파일들, 새 파이썬 파일들)``."""
    new = {r[0] for r in file_records if r[4] == "python"}
    old = {p for p, m in old_files_meta.items() if m.get("language") == "python"}
    return old, new


class _IdAllocator:
    """행 id 를 명시로 정한다 — 새 행은 옛 DB 의 최댓값 뒤부터, 다시 파싱한 파일의
    기호는 옛 id 를 ``(qualified_name, kind, kind_raw)`` 짝으로 물려받는다. 그래야
    다른 파일의 ``refs.target_id`` 가 본문만 고친 편집 뒤에도 그대로 맞다."""

    def __init__(self, old_conn):
        self.old = old_conn
        self.next: dict[str, int] = {}
        # 지운 행의 id 도 다시 내주지 않는다 — 최고 수위를 meta 에 남긴다
        saved: dict = {}
        if old_conn is not None:
            row = old_conn.execute(
                "SELECT value FROM meta WHERE key = 'next_ids'"
            ).fetchone()
            saved = json.loads(row[0]) if row else {}
        for table in ("symbols", "refs", "scopes", "bindings"):
            top = 0
            if old_conn is not None:
                row = old_conn.execute(f"SELECT MAX(id) FROM {table}").fetchone()
                top = row[0] or 0
            self.next[table] = max(top + 1, saved.get(table, 0))

    def take(self, table: str) -> int:
        i = self.next[table]
        self.next[table] = i + 1
        return i

    def assign_symbols(self, rel: str, syms: list[dict]) -> None:
        pool: dict[tuple, list[int]] = defaultdict(list)
        if self.old is not None:
            for sid, qn, kind, kraw in self.old.execute(
                "SELECT id, qualified_name, kind, kind_raw FROM symbols "
                "WHERE file = ? ORDER BY id",
                (rel,),
            ):
                pool[(qn, kind, kraw)].append(sid)
        for s in syms:
            key = (s.get("qualified_name") or s["name"], s["kind"], s.get("kind_raw"))
            if pool.get(key):
                s["id"] = pool[key].pop(0)
            else:
                s["id"] = self.take("symbols")


def _link_rows(rel, syms, scopes, binds, refs, ids: _IdAllocator, rows) -> None:
    """한 파일의 범위·바인딩·참조에 id 를 붙이고 정의 기호와 잇는다."""
    by_def: dict[tuple[int, str], int] = {}
    for s in syms:
        if s.get("is_definition") and s["kind"] in ("function", "type"):
            by_def.setdefault((s["line"], s["name"]), s["id"])
        rows["symbols"].append(s)
    scope_ids: list[int] = []
    for sc in scopes:
        sid = ids.take("scopes")
        scope_ids.append(sid)
    fn_of: list[int | None] = []
    for i, sc in enumerate(scopes):
        sym = (
            by_def.get((sc.def_line, sc.def_name)) if sc.def_line is not None else None
        )
        parent = scope_ids[sc.parent] if sc.parent is not None else None
        rows["scopes"].append(
            {
                "id": scope_ids[i],
                "file": rel,
                "kind": sc.kind,
                "start_line": sc.start_line,
                "end_line": sc.end_line,
                "parent_id": parent,
                "symbol_id": sym,
            }
        )
        # 감싼 함수 기호 — 람다·컴프리헨션·클래스 본문은 바깥으로
        if sc.kind == "function" and sym is not None:
            fn_of.append(sym)
        elif sc.parent is not None:
            fn_of.append(fn_of[sc.parent])
        else:
            fn_of.append(None)
    for b in binds:
        tgt = None
        if b.kind in ("def", "class"):
            tgt = by_def.get((b.line, b.name))
        rows["bindings"].append(
            {
                "id": ids.take("bindings"),
                "scope_id": scope_ids[b.scope],
                "name": b.name,
                "kind": b.kind,
                "line": b.line,
                "import_module": b.import_module,
                "import_name": b.import_name,
                "symbol_id": tgt,
            }
        )
    for r in refs:
        sc = r.get("scope") or 0
        r["id"] = ids.take("refs")
        r["scope_id"] = scope_ids[sc]
        r["fn_symbol_id"] = fn_of[sc]
        rows["refs"].append(r)


def _surface(conn, schema: str, path: str) -> tuple:
    """다른 파일의 해석이 이 파일에서 읽을 수 있는 것 — 모듈 범위, 클래스 범위와 그 바깥
    범위들(베이스 식을 그 자리에서 찾는다)의 바인딩, 클래스 베이스, def·class 기호 id.
    줄은 순서만 남긴다(위에서 줄을 더해도 같다); 범위·바인딩 id 는 파일 안 순번으로.
    함수 몸 안의 지역은 넣지 않는다 — 클래스를 품지 않은 함수는 바깥에서 안 보인다."""
    scopes = conn.execute(
        f"SELECT id, kind, parent_id, symbol_id, start_line FROM {schema}.scopes "
        f"WHERE file = ? ORDER BY id",
        (path,),
    ).fetchall()
    if not scopes:
        return ()
    parent = {sid: pid for sid, _k, pid, _s, _l in scopes}
    keep: set[int] = set()
    for sid, kind, _pid, _sym, _line in scopes:
        if kind in ("module", "class"):
            cur = sid
            while cur is not None and cur not in keep:
                keep.add(cur)
                cur = parent.get(cur)
    kept = [row for row in scopes if row[0] in keep]
    order = {row[0]: i for i, row in enumerate(kept)}
    binds = []
    for i in range(0, len(kept), 500):
        chunk = [row[0] for row in kept[i : i + 500]]
        marks = ",".join("?" for _ in chunk)
        binds += conn.execute(
            f"SELECT scope_id, name, kind, line, import_module, import_name, "
            f"symbol_id FROM {schema}.bindings WHERE scope_id IN ({marks}) "
            f"ORDER BY id",
            chunk,
        ).fetchall()
    lines = sorted({row[4] for row in kept} | {b[3] for b in binds})
    rank = {ln: i for i, ln in enumerate(lines)}
    class_ids = [row[3] for row in kept if row[1] == "class" and row[3] is not None]
    bases = {}
    for i in range(0, len(class_ids), 500):
        chunk = class_ids[i : i + 500]
        marks = ",".join("?" for _ in chunk)
        bases.update(
            conn.execute(
                f"SELECT id, bases FROM {schema}.symbols WHERE id IN ({marks})", chunk
            ).fetchall()
        )
    return (
        tuple(
            (order[sid], kind, order.get(pid), sym, rank[line], bases.get(sym))
            for sid, kind, pid, sym, line in kept
        ),
        tuple(
            (order[sc], name, kind, rank[line], imod, iname, sym)
            for sc, name, kind, line, imod, iname, sym in binds
        ),
    )


def _module_names(conn, schema: str, path: str) -> dict:
    """모듈 최상위의 이름마다 바인딩 ``[(종류, 모듈, import 이름, 기호)]`` 순서대로 —
    다른 파일이 이 모듈에서 한 이름을 찾을 때 읽는 것(줄은 안 본다)."""
    out: dict = defaultdict(list)
    for name, kind, imod, iname, sym in conn.execute(
        f"SELECT b.name, b.kind, b.import_module, b.import_name, b.symbol_id FROM "
        f"{schema}.bindings b JOIN {schema}.scopes s ON s.id = b.scope_id "
        f"WHERE s.file = ? AND s.kind = 'module' ORDER BY b.id",
        (path,),
    ):
        out[name].append((kind, imod, iname, sym))
    return out


def _function_names(conn, schema: str, files: list[str]) -> set[tuple[str, str]]:
    """그 파일들의 함수 이름 ``(언어, 이름)``."""
    out: set[tuple[str, str]] = set()
    for i in range(0, len(files), 500):
        chunk = files[i : i + 500]
        marks = ",".join("?" for _ in chunk)
        out.update(
            conn.execute(
                f"SELECT DISTINCT language, name FROM {schema}.symbols "
                f"WHERE kind = 'function' AND file IN ({marks})",
                chunk,
            ).fetchall()
        )
    return out


def _has_function(conn, schema: str, lang: str, name: str) -> bool:
    return (
        conn.execute(
            f"SELECT 1 FROM {schema}.symbols WHERE name = ? AND +kind = 'function' "
            f"AND +language = ? LIMIT 1",
            (name, lang),
        ).fetchone()
        is not None
    )


def _resolve_targets(
    conn, parsed: list[str], removed: list[str], py_files: tuple[set, set]
) -> list[str]:
    """다시 해석할 파일 — 다시 파싱한 파일, 그리고 지난 해석이 읽은 것(``resolve_deps``)
    가운데 바뀐 것이 있는 파일: 바깥에 보이는 것(`_surface`)이 달라졌거나 지운 파일,
    대응이 달라진 모듈 이름, 생기거나 사라진 경로, 있고 없음이 바뀐 함수 이름. 해석은
    파서가 쓴 행만 읽으니(다른 파일의 해석 결과는 안 읽는다) 이것으로 전체 재구축과 같다
    — 재노출 사슬·베이스 사슬을 따로 따라갈 필요가 없다(읽은 것이 이미 전이적이다)."""
    # 지운 파일 — 다른 파일에 닿는 길은 모두 모듈 대응(`module_files`)을 거치니
    # 그 파일을 찾던 파일은 아래 `m:`·`p:` 로도 잡힌다
    keys = [f"f:{p}" for p in removed]
    for p in parsed:
        if _surface(conn, "old", p) != _surface(conn, "main", p):
            keys.append(f"f:{p}")
            old_m, new_m = _module_names(conn, "old", p), _module_names(conn, "main", p)
            keys += [
                f"e:{n}:{p}"
                for n in old_m.keys() | new_m.keys()
                if old_m.get(n) != new_m.get(n)
            ]
    old_py, new_py = py_files
    if old_py != new_py:
        from agent_cli.code_index.resolve.python import module_map

        old_map, _ = module_map(old_py)
        new_map, _ = module_map(new_py)
        keys += [
            f"m:{m}"
            for m in old_map.keys() | new_map.keys()
            if old_map.get(m) != new_map.get(m)
        ]
        keys += [f"p:{p}" for p in old_py ^ new_py]
    # 있고 없음이 바뀐 함수 이름 — 바뀔 수 있는 것은 다시 파싱하거나 지운 파일의 이름뿐
    touched = parsed + removed
    for lang, name in sorted(
        _function_names(conn, "old", touched) | _function_names(conn, "main", touched)
    ):
        if _has_function(conn, "old", lang, name) != _has_function(
            conn, "main", lang, name
        ):
            keys.append(f"n:{lang}:{name}")
    out = set(parsed)
    for i in range(0, len(keys), 500):
        chunk = keys[i : i + 500]
        marks = ",".join("?" for _ in chunk)
        out.update(
            r[0]
            for r in conn.execute(
                f"SELECT DISTINCT file FROM resolve_deps WHERE dep IN ({marks})", chunk
            )
        )
    present = {r[0] for r in conn.execute("SELECT path FROM files")}
    return sorted(out & present)


# ---------- storage backend (SQLite) ----------

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL              -- JSON-encoded
);
CREATE TABLE IF NOT EXISTS files (
    path        TEXT PRIMARY KEY,
    size        INTEGER NOT NULL,
    mtime_ns    INTEGER NOT NULL DEFAULT 0,  -- v10.93.0: stat 선행 비교의 근거
    lines       INTEGER NOT NULL,
    sha1        TEXT NOT NULL,
    has_error   INTEGER NOT NULL,
    n_symbols   INTEGER NOT NULL,
    identifiers TEXT NOT NULL,       -- JSON array
    language    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS symbols (
    id             INTEGER PRIMARY KEY,  -- v4: 증분 빌드를 넘어 유지된다
    name           TEXT NOT NULL,
    qualified_name TEXT NOT NULL,    -- display form (parent + sep + name)
    kind           TEXT NOT NULL,    -- function|type|variable|constant|section
    file           TEXT NOT NULL,
    line           INTEGER NOT NULL,
    col            INTEGER NOT NULL,
    end_line       INTEGER NOT NULL,
    is_definition  INTEGER NOT NULL,
    language       TEXT NOT NULL,
    kind_raw       TEXT,             -- original AST node type
    modifiers      TEXT,             -- JSON array
    parent         TEXT,             -- enclosing symbol (class/namespace/module)
    signature      TEXT,
    return_type    TEXT,
    enum_values    TEXT,             -- JSON array
    bases          TEXT              -- v4: JSON array of base-class expressions
);
CREATE INDEX IF NOT EXISTS idx_symbols_name           ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_qualified_name ON symbols(qualified_name);
CREATE INDEX IF NOT EXISTS idx_symbols_kind           ON symbols(kind);
CREATE INDEX IF NOT EXISTS idx_symbols_file           ON symbols(file);
CREATE INDEX IF NOT EXISTS idx_symbols_language       ON symbols(language);
CREATE TABLE IF NOT EXISTS refs (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    kind         TEXT NOT NULL,      -- call|type|name|registration
    file         TEXT NOT NULL,
    line         INTEGER NOT NULL,
    col          INTEGER NOT NULL,
    language     TEXT NOT NULL,
    scope_id     INTEGER NOT NULL,   -- v4: innermost scope of the ref
    fn_symbol_id INTEGER,            -- v4: enclosing function definition
    object       TEXT,               -- v4: `a.b` of `a.b.m()`
    target_id    INTEGER,            -- v4: definition when resolved
    resolution   TEXT NOT NULL       -- v4: resolved|local|candidates|external|unresolved
);
CREATE INDEX IF NOT EXISTS idx_refs_name     ON refs(name);
CREATE INDEX IF NOT EXISTS idx_refs_file     ON refs(file);
CREATE INDEX IF NOT EXISTS idx_refs_kind     ON refs(kind);
CREATE INDEX IF NOT EXISTS idx_refs_language ON refs(language);
CREATE INDEX IF NOT EXISTS idx_refs_target   ON refs(target_id);
CREATE INDEX IF NOT EXISTS idx_refs_fn       ON refs(fn_symbol_id);
CREATE TABLE IF NOT EXISTS scopes (
    id         INTEGER PRIMARY KEY,
    file       TEXT NOT NULL,
    kind       TEXT NOT NULL,        -- module|class|function|comprehension|block
    start_line INTEGER NOT NULL,
    end_line   INTEGER NOT NULL,
    parent_id  INTEGER,
    symbol_id  INTEGER               -- the function/class definition owning it
);
CREATE INDEX IF NOT EXISTS idx_scopes_file   ON scopes(file);
CREATE INDEX IF NOT EXISTS idx_scopes_symbol ON scopes(symbol_id);
CREATE TABLE IF NOT EXISTS bindings (
    id               INTEGER PRIMARY KEY,
    scope_id         INTEGER NOT NULL,
    name             TEXT NOT NULL,
    kind             TEXT NOT NULL,  -- param|local|import|def|class|global|nonlocal
    line             INTEGER NOT NULL,
    import_module    TEXT,
    import_name      TEXT,
    symbol_id        INTEGER         -- the definition a def/class binding names
);
CREATE INDEX IF NOT EXISTS idx_bindings_scope  ON bindings(scope_id);
-- v4: what each file's resolution read — "f:<file>" (its scopes/bindings/bases),
-- "m:<module>", "p:<relative-import path>", "n:<language>:<function name>"
CREATE TABLE IF NOT EXISTS resolve_deps (
    dep  TEXT NOT NULL,
    file TEXT NOT NULL,
    PRIMARY KEY (dep, file)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_resolve_deps_file ON resolve_deps(file);
"""

_SYMBOL_COLS = (
    "id, name, qualified_name, kind, file, line, col, end_line, is_definition, "
    "language, kind_raw, modifiers, parent, signature, return_type, enum_values, bases"
)
_REF_COLS = (
    "id, name, kind, file, line, col, language, scope_id, fn_symbol_id, object, "
    "target_id, resolution"
)
_SCOPE_COLS = "id, file, kind, start_line, end_line, parent_id, symbol_id"
_BINDING_COLS = "id, scope_id, name, kind, line, import_module, import_name, symbol_id"


def _new_tmp_path(path: Path) -> Path:
    """Pick a tmp filename adjacent to ``path`` that's unique per
    process / thread / random suffix.

    Lives in the same directory so the final ``os.replace(tmp, path)``
    is a same-filesystem rename (POSIX-atomic). The pid / tid / nonce
    combination keeps concurrent writers from colliding on a shared
    name even if they happen to land in the same millisecond.
    """
    suffix = f".{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp"
    return path.with_name(path.name + suffix)


def write_sqlite_index(
    path: Path,
    top: dict,
    *,
    copy_from: Path | None = None,
    copy_files: list[str] | None = None,
    resolve=None,
):
    """Replace the SQLite file with a fresh build from `top`.

    v10.93.0: ``copy_from`` 이 있으면 그 DB 를 ATTACH 해 ``copy_files`` 의 행을 SQL 로
    그대로 복사한다 — 파이썬으로 올려 다시 넣지 않는다(agent-cli 저장소의 참조 65만
    행: executemany 1.9 s → 복사 0.2 s). ``top`` 의 ``symbols``·``refs``·``scopes``·
    ``bindings`` 는 다시 파싱한 파일의 행만 든다. v10.96.0: 복사도 새 행도 id 를
    명시한다 — 기호 id 가 빌드를 넘어 유지돼야 해석 결과(``refs.target_id``)가 맞다.
    ``resolve(conn)`` 은 커밋 전에 불린다(옛 DB 는 ``old`` 로 붙어 있다).

    Atomic write contract: the active ``path`` is never opened,
    truncated, or unlinked. We build the full DB at a unique tmp file
    next to ``path``, then ``os.replace`` it on top. Any reader (or
    concurrent writer doing the same dance) sees either the old
    complete DB or the new complete DB — never an in-progress
    half-written state, never a "file disappeared" inode whose
    earlier inhabitant was a SQLite connection.

    The previous implementation called ``path.unlink()`` then
    re-created the file in place. Under parallel delegate workers
    that pattern produced ``sqlite3.OperationalError: disk I/O
    error`` when one worker's unlink ripped the file out from under
    another worker's open connection. Atomic rename eliminates that
    window — there's no reachable state where a connection's
    underlying inode can vanish.
    """
    tmp = _new_tmp_path(path)
    # Make sure the parent dir exists. ``_ensure_index`` already
    # creates ``.agent-cli/`` but defensive mkdir keeps this function
    # self-contained for callers (e.g. tests) that build into a
    # freshly-created directory.
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(tmp))
    attached = False
    try:
        conn.executescript(SQLITE_SCHEMA)
        cur = conn.cursor()
        for k, v in top.items():
            if k in ("files", "symbols", "refs", "scopes", "bindings"):
                continue
            cur.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?)", (k, json.dumps(v))
            )
        cur.executemany(
            "INSERT INTO files(path, size, mtime_ns, lines, sha1, has_error, n_symbols, "
            "identifiers, language) VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (
                    f["path"],
                    f.get("size", 0),
                    f.get("mtime_ns", 0),
                    f.get("lines", 0),
                    f.get("sha1", ""),
                    int(bool(f.get("has_error"))),
                    f.get("n_symbols", 0),
                    json.dumps(f.get("identifiers") or []),
                    f.get("language", "c"),
                )
                for f in top["files"]
            ],
        )
        if copy_from is not None:
            cur.execute("ATTACH DATABASE ? AS old", (str(copy_from),))
            attached = True
        if attached and copy_files:
            cur.execute("CREATE TEMP TABLE keep(path TEXT PRIMARY KEY)")
            cur.executemany(
                "INSERT INTO keep(path) VALUES (?)", [(r,) for r in copy_files]
            )
            for table, cols, where in (
                ("symbols", _SYMBOL_COLS, "file IN (SELECT path FROM keep)"),
                ("refs", _REF_COLS, "file IN (SELECT path FROM keep)"),
                ("scopes", _SCOPE_COLS, "file IN (SELECT path FROM keep)"),
                (
                    "bindings",
                    _BINDING_COLS,
                    (
                        "scope_id IN (SELECT id FROM old.scopes WHERE file IN "
                        "(SELECT path FROM keep))"
                    ),
                ),
                ("resolve_deps", "dep, file", "file IN (SELECT path FROM keep)"),
            ):
                cur.execute(
                    f"INSERT INTO {table}({cols}) SELECT {cols} FROM old.{table} "
                    f"WHERE {where}"
                )
            cur.execute("DROP TABLE keep")
        cur.executemany(
            f"INSERT INTO symbols({_SYMBOL_COLS}) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    s["id"],
                    s["name"],
                    s.get("qualified_name") or s["name"],
                    s["kind"],
                    s["file"],
                    s["line"],
                    s["col"],
                    s["end_line"],
                    int(bool(s.get("is_definition"))),
                    s.get("language", "c"),
                    s.get("kind_raw"),
                    json.dumps(s["modifiers"]) if s.get("modifiers") else None,
                    s.get("parent"),
                    s.get("signature"),
                    s.get("return_type"),
                    json.dumps(s["enum_values"]) if s.get("enum_values") else None,
                    json.dumps(s["bases"]) if s.get("bases") else None,
                )
                for s in top.get("symbols", [])
            ],
        )
        cur.executemany(
            f"INSERT INTO refs({_REF_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    r["id"],
                    r["name"],
                    r["kind"],
                    r["file"],
                    r["line"],
                    r["col"],
                    r.get("language", "c"),
                    r["scope_id"],
                    r.get("fn_symbol_id"),
                    r.get("object"),
                    None,
                    "unresolved",
                )
                for r in top.get("refs", [])
            ],
        )
        cur.executemany(
            f"INSERT INTO scopes({_SCOPE_COLS}) VALUES (?,?,?,?,?,?,?)",
            [
                (
                    sc["id"],
                    sc["file"],
                    sc["kind"],
                    sc["start_line"],
                    sc["end_line"],
                    sc["parent_id"],
                    sc["symbol_id"],
                )
                for sc in top.get("scopes", [])
            ],
        )
        cur.executemany(
            f"INSERT INTO bindings({_BINDING_COLS}) VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    b["id"],
                    b["scope_id"],
                    b["name"],
                    b["kind"],
                    b["line"],
                    b["import_module"],
                    b["import_name"],
                    b["symbol_id"],
                )
                for b in top.get("bindings", [])
            ],
        )
        if resolve is not None:
            resolve(conn)
        conn.commit()
        if attached:
            cur.execute("DETACH DATABASE old")
    except Exception:
        # Build failed somewhere in the inserts. The active ``path``
        # is untouched (we wrote to ``tmp`` only), so the operator
        # still has the prior good index. Just clean up the
        # partial tmp before re-raising.
        conn.close()
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    else:
        conn.close()
        # POSIX ``os.replace`` is atomic when source and target sit
        # on the same filesystem (we placed tmp in path.parent for
        # exactly this reason). Any observer either sees the prior
        # complete DB or the new complete DB at any instant.
        os.replace(tmp, path)
