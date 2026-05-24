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
import sqlite3
import sys
import time
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from agent_cli.code_index.languages import (
    LANGUAGES,
    get_supported_extensions,
    language_of,
)
from agent_cli.code_index.preproc import compute_preproc
from agent_cli.code_index.schema import CODE_NAME_KINDS, SCHEMA_VERSION
from agent_cli.code_index.store import IndexStore, load_index

_PARSER_CACHE: dict[str, object] = {}


def get_parser(lang: str):
    from tree_sitter import Parser

    if lang not in _PARSER_CACHE:
        _PARSER_CACHE[lang] = Parser(LANGUAGES[lang].grammar_factory())
    return _PARSER_CACHE[lang]


# ---------- build ----------


def iter_source_files(root: Path):
    """Iterate source files matching any registered language extension.

    `get_supported_extensions()` triggers the lazy walker import as a
    side effect, so this function works regardless of whether any
    walker module was imported before now.
    """
    all_exts = set(get_supported_extensions())
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix in all_exts:
            yield p


def build(
    root: Path,
    out_path: Path,
    defs_path: Optional[Path] = None,
    undef_unknown_configs: bool = True,
    force_full: bool = False,
    verbose: bool = True,
):
    t0 = time.time()

    # C-specific preprocessing context. Other languages ignore unifdef_flags
    # and have a no-op preprocess.
    unifdef_flags, preproc_info, fp = compute_preproc(
        root, defs_path, undef_unknown_configs, verbose
    )

    # Try to load existing index for reuse
    old_store: Optional[IndexStore] = None
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

    # Group old per-file data
    old_files_meta = {f["path"]: f for f in (old_store.files if old_store else [])}
    old_syms_by_file: dict[str, list] = defaultdict(list)
    old_refs_by_file: dict[str, list] = defaultdict(list)
    if old_store:
        for s in old_store.all_symbols():
            old_syms_by_file[s["file"]].append(s)
        for r in old_store.all_refs():
            old_refs_by_file[r["file"]].append(r)

    # Walk files, classify as reused vs changed. Skip files whose extension
    # doesn't match any registered language.
    file_records = []  # list of (rel, raw_bytes, sha1, reused: bool, lang: str)
    n_bytes = 0
    for p in iter_source_files(root):
        lang = language_of(p)
        if lang is None:
            continue
        rel = str(p.relative_to(root))
        raw = p.read_bytes()
        n_bytes += len(raw)
        h = hashlib.sha1(raw).hexdigest()
        old = old_files_meta.get(rel)
        reused = bool(
            old
            and old.get("sha1") == h
            and old.get("identifiers") is not None
            and rel in old_syms_by_file
        )
        file_records.append((rel, raw, h, reused, lang))

    # Per-file data (everything as plain dicts for uniformity)
    syms_by_file: dict[str, list] = {}
    refs_by_file: dict[str, list] = {}
    idents_by_file: dict[str, list] = {}
    meta_by_file: dict[str, dict] = {}
    parsed_cleaned: dict[str, bytes] = {}  # cache cleaned bytes for Pass 2

    # Pre-populate reused files
    for rel, _, _, reused, _ in file_records:
        if reused:
            syms_by_file[rel] = old_syms_by_file[rel]
            refs_by_file[rel] = old_refs_by_file[rel]
            idents_by_file[rel] = old_files_meta[rel].get("identifiers") or []
            meta_by_file[rel] = old_files_meta[rel]

    # Pass 1 (defs) for changed files
    for rel, raw, h, reused, lang in file_records:
        if reused:
            continue
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
            "size": len(raw),
            "lines": raw.count(b"\n") + 1,
            "sha1": h,
            "has_error": tree.root_node.has_error,
            "n_symbols": len(syms),
            "language": lang,
        }

    # Compute new and old defined-name sets
    new_defined = {
        s["name"]
        for syms in syms_by_file.values()
        for s in syms
        if s["kind"] in CODE_NAME_KINDS
    }
    old_defined: set[str] = set()
    if old_store:
        for s in old_store.all_symbols():
            if s["kind"] in CODE_NAME_KINDS:
                old_defined.add(s["name"])
    added_names = new_defined - old_defined

    # Find unchanged files whose ref set could be affected by newly-added names
    # (Option B from the design: only re-Pass2 files that actually mention the
    # new identifier somewhere in their source.)
    affected: set[str] = set()
    if added_names and old_store is not None:
        for rel, _, _, reused, _ in file_records:
            if not reused:
                continue
            ids = idents_by_file.get(rel)
            if ids and added_names.intersection(ids):
                affected.add(rel)

    # Pass 2 (refs) for changed + affected files
    pass2_set: set[str] = {rel for rel, _, _, reused, _ in file_records if not reused}
    pass2_set |= affected
    for rel, raw, _, _, lang in file_records:
        if rel not in pass2_set:
            continue
        spec = LANGUAGES[lang]
        cleaned = parsed_cleaned.get(rel)
        if cleaned is None:
            cleaned = spec.preprocess(raw, unifdef_flags) if spec.preprocess else raw
        parser = get_parser(lang)
        tree = parser.parse(cleaned)
        new_refs: list = []
        idents: set[str] = set()
        spec.walk_refs(tree.root_node, cleaned, rel, new_refs, new_defined, idents)
        refs_by_file[rel] = [asdict(r) for r in new_refs]
        idents_by_file[rel] = sorted(idents)
        meta_by_file[rel]["identifiers"] = idents_by_file[rel]

    # Assemble final ordered lists (deterministic, sorted by file path)
    files_meta_list = [meta_by_file[r[0]] for r in file_records]
    all_symbols = [s for r in file_records for s in syms_by_file[r[0]]]
    all_refs = [x for r in file_records for x in refs_by_file[r[0]]]

    n_files = len(file_records)
    n_reused = sum(1 for _, _, _, reused, _ in file_records if reused)
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
                f"affected {len(affected)}"
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
    write_sqlite_index(out_path, out)
    if verbose:
        print(
            f"\nIndexed {n_files} files ({n_bytes / 1024:.1f} KiB) in {elapsed:.2f}s",
            file=sys.stderr,
        )
        print(f"  {len(all_symbols)} symbols, {len(all_refs)} refs", file=sys.stderr)
        print(
            f"  wrote {out_path}  ({out_path.stat().st_size / 1024:.1f} KiB)",
            file=sys.stderr,
        )


# ---------- storage backend (SQLite) ----------

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL              -- JSON-encoded
);
CREATE TABLE IF NOT EXISTS files (
    path        TEXT PRIMARY KEY,
    size        INTEGER NOT NULL,
    lines       INTEGER NOT NULL,
    sha1        TEXT NOT NULL,
    has_error   INTEGER NOT NULL,
    n_symbols   INTEGER NOT NULL,
    identifiers TEXT NOT NULL,       -- JSON array
    language    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS symbols (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL,     -- function|type|variable|constant|section
    file          TEXT NOT NULL,
    line          INTEGER NOT NULL,
    col           INTEGER NOT NULL,
    end_line      INTEGER NOT NULL,
    is_definition INTEGER NOT NULL,
    language      TEXT NOT NULL,
    kind_raw      TEXT,              -- original AST node type
    modifiers     TEXT,              -- JSON array
    parent        TEXT,              -- enclosing symbol (class/namespace/module)
    signature     TEXT,
    return_type   TEXT,
    enum_values   TEXT,              -- JSON array
    params        TEXT               -- JSON array
);
CREATE INDEX IF NOT EXISTS idx_symbols_name     ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_kind     ON symbols(kind);
CREATE INDEX IF NOT EXISTS idx_symbols_file     ON symbols(file);
CREATE INDEX IF NOT EXISTS idx_symbols_language ON symbols(language);
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


def write_sqlite_index(path: Path, top: dict):
    """Replace the SQLite file with a fresh build from `top`."""
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
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
            "INSERT INTO files(path, size, lines, sha1, has_error, n_symbols, identifiers, language) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    f["path"],
                    f.get("size", 0),
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
        cur.executemany(
            "INSERT INTO symbols(name, kind, file, line, col, end_line, is_definition, "
            "language, kind_raw, modifiers, parent, signature, return_type, enum_values, params) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    s["name"],
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
    finally:
        conn.close()
