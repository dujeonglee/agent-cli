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
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

from agent_cli.code_index._sqlite import sqlite3
from agent_cli.code_index.languages import (
    LANGUAGES,
    get_supported_extensions,
    language_of,
)
from agent_cli.code_index.preproc import compute_preproc
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
    refs_by_file: dict[str, list] = {}
    idents_by_file: dict[str, list] = {}
    meta_by_file: dict[str, dict] = {}
    parsed_cleaned: dict[str, bytes] = {}  # cache cleaned bytes for Pass 2

    # Pre-populate reused files — meta only (their rows are copied by SQL);
    # a touched-but-identical file takes the new size·mtime.
    for rel, _, _, reused, _, size, mtime in file_records:
        if reused:
            old = old_files_meta[rel]
            idents_by_file[rel] = old.get("identifiers") or []
            meta_by_file[rel] = {**old, "size": size, "mtime_ns": mtime}

    # Pass 1 (defs) for changed files
    for rel, path, h, reused, lang, size, mtime in file_records:
        if reused:
            continue
        raw = raw_of(rel, path)
        spec = LANGUAGES[lang]
        cleaned = spec.preprocess(raw, unifdef_flags) if spec.preprocess else raw
        parsed_cleaned[rel] = cleaned
        parser = get_parser(lang)
        tree = parser.parse(cleaned)
        syms: list = []
        spec.walk_definitions(tree.root_node, cleaned, rel, syms)
        syms_by_file[rel] = [asdict(s) for s in syms]
        meta_by_file[rel] = {
            "path": rel,
            "size": size,
            "mtime_ns": mtime,
            "lines": raw.count(b"\n") + 1,
            "sha1": h,
            "has_error": tree.root_node.has_error,
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

    # Pass 2 (refs) for changed + affected files
    pass2_set: set[str] = {
        rel for rel, _, _, reused, _, _, _ in file_records if not reused
    }
    pass2_set |= affected
    for rel, path, _, _, lang, _, _ in file_records:
        if rel not in pass2_set:
            continue
        spec = LANGUAGES[lang]
        cleaned = parsed_cleaned.get(rel)
        if cleaned is None:
            raw = raw_of(rel, path)
            cleaned = spec.preprocess(raw, unifdef_flags) if spec.preprocess else raw
        parser = get_parser(lang)
        tree = parser.parse(cleaned)
        new_refs: list = []
        idents: set[str] = set()
        spec.walk_refs(tree.root_node, cleaned, rel, new_refs, new_defined, idents)
        refs_by_file[rel] = [asdict(r) for r in new_refs]
        idents_by_file[rel] = sorted(idents)
        meta_by_file[rel]["identifiers"] = idents_by_file[rel]

    # Assemble final ordered lists (deterministic, sorted by file path). Only
    # parsed rows are here; reused files' rows come from the old DB.
    files_meta_list = [meta_by_file[r[0]] for r in file_records]
    all_symbols = [s for r in file_records for s in syms_by_file.get(r[0], [])]
    all_refs = [x for r in file_records for x in refs_by_file.get(r[0], [])]
    reused_files = [rel for rel, _, _, reused, _, _, _ in file_records if reused]
    present = {r[0] for r in file_records}
    n_removed = len(set(old_files_meta) - present)

    n_files = len(file_records)
    n_reused = len(reused_files)
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
        "symbols": all_symbols,
        "refs": all_refs,
    }
    write_sqlite_index(
        out_path,
        out,
        copy_from=out_path if old_store is not None else None,
        copy_symbols=reused_files,
        copy_refs=[r for r in reused_files if r not in affected],
    )
    if verbose:
        print(
            f"\nIndexed {n_files} files ({n_bytes / 1024:.1f} KiB read) in {elapsed:.2f}s",
            file=sys.stderr,
        )
        print(
            f"  {len(all_symbols)} symbols, {len(all_refs)} refs parsed; "
            f"{n_reused} files copied",
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
    }


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
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
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
    params         TEXT              -- JSON array
);
CREATE INDEX IF NOT EXISTS idx_symbols_name           ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_qualified_name ON symbols(qualified_name);
CREATE INDEX IF NOT EXISTS idx_symbols_kind           ON symbols(kind);
CREATE INDEX IF NOT EXISTS idx_symbols_file           ON symbols(file);
CREATE INDEX IF NOT EXISTS idx_symbols_language       ON symbols(language);
CREATE TABLE IF NOT EXISTS refs (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL,
    kind     TEXT NOT NULL,          -- call|type|name
    file     TEXT NOT NULL,
    line     INTEGER NOT NULL,
    col      INTEGER NOT NULL,
    language TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_refs_name     ON refs(name);
CREATE INDEX IF NOT EXISTS idx_refs_file     ON refs(file);
CREATE INDEX IF NOT EXISTS idx_refs_kind     ON refs(kind);
CREATE INDEX IF NOT EXISTS idx_refs_language ON refs(language);
"""


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
    copy_symbols: list[str] | None = None,
    copy_refs: list[str] | None = None,
):
    """Replace the SQLite file with a fresh build from `top`.

    v10.93.0: ``copy_from`` 이 있으면 그 DB 를 ATTACH 해 ``copy_symbols`` 파일들의
    기호 행과 ``copy_refs`` 파일들의 참조 행을 SQL 로 그대로 복사한다 — 파이썬으로
    올려 다시 넣지 않는다(agent-cli 저장소의 참조 65만 행: executemany 1.9 s →
    복사 0.2 s). ``top["symbols"]``·``top["refs"]`` 는 파싱한 파일의 행만 든다.

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
    try:
        conn.executescript(SQLITE_SCHEMA)
        cur = conn.cursor()
        for k, v in top.items():
            if k in ("files", "symbols", "refs"):
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
        if copy_from is not None and (copy_symbols or copy_refs):
            cur.execute("ATTACH DATABASE ? AS old", (str(copy_from),))
            cur.execute("CREATE TEMP TABLE keep_syms(path TEXT PRIMARY KEY)")
            cur.execute("CREATE TEMP TABLE keep_refs(path TEXT PRIMARY KEY)")
            cur.executemany(
                "INSERT INTO keep_syms(path) VALUES (?)",
                [(r,) for r in copy_symbols or []],
            )
            cur.executemany(
                "INSERT INTO keep_refs(path) VALUES (?)",
                [(r,) for r in copy_refs or []],
            )
            cur.execute(
                "INSERT INTO symbols(name, qualified_name, kind, file, line, col, "
                "end_line, is_definition, language, kind_raw, modifiers, parent, "
                "signature, return_type, enum_values, params) "
                "SELECT name, qualified_name, kind, file, line, col, end_line, "
                "is_definition, language, kind_raw, modifiers, parent, signature, "
                "return_type, enum_values, params FROM old.symbols "
                "WHERE file IN (SELECT path FROM keep_syms) ORDER BY id"
            )
            cur.execute(
                "INSERT INTO refs(name, kind, file, line, col, language) "
                "SELECT name, kind, file, line, col, language FROM old.refs "
                "WHERE file IN (SELECT path FROM keep_refs) ORDER BY id"
            )
            cur.execute("DROP TABLE keep_syms")
            cur.execute("DROP TABLE keep_refs")
        cur.executemany(
            "INSERT INTO symbols(name, qualified_name, kind, file, line, col, end_line, "
            "is_definition, language, kind_raw, modifiers, parent, signature, "
            "return_type, enum_values, params) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
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
                    json.dumps(s["params"]) if s.get("params") else None,
                )
                for s in top["symbols"]
            ],
        )
        cur.executemany(
            "INSERT INTO refs(name, kind, file, line, col, language) VALUES (?,?,?,?,?,?)",
            [
                (
                    r["name"],
                    r["kind"],
                    r["file"],
                    r["line"],
                    r["col"],
                    r.get("language", "c"),
                )
                for r in top["refs"]
            ],
        )
        conn.commit()
        if copy_from is not None and (copy_symbols or copy_refs):
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
