# Originally ported from minish.ai/Agent-tools tsindex.py (Apache 2.0).
# See NOTICE for the list of modifications.
"""SQLite-backed index reader.

`IndexStore` opens an existing index file (produced by
`agent_cli.code_index.builder.build`) and exposes both dict-style
access (`idx['symbols']`, `idx['root']`) and indexed-lookup methods
(`find_symbols(name=...)`, `find_refs(...)`, `find_refs_in_range(...)`)
that exploit the SQL secondary indexes.

`load_index(path)` is the convenience constructor used by callers that
don't need to think about the class name.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Optional


class IndexStore:
    """SQLite-backed index reader.

    Supports dict-style access for convenience (`idx['symbols']`, `idx['root']`)
    and indexed-lookup methods (`find_symbols(name=...)`) that use the SQL
    indexes."""

    def __init__(self, path: Path):
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._meta_cache: Optional[dict] = None
        self._files_cache: Optional[list] = None
        self._all_syms: Optional[list] = None
        self._all_refs: Optional[list] = None

    # ----- row converters -----

    @staticmethod
    def _file_row(r) -> dict:
        d = dict(r)
        d["has_error"] = bool(d["has_error"])
        d["identifiers"] = json.loads(d["identifiers"] or "[]")
        return d

    @staticmethod
    def _sym_row(r) -> dict:
        d = dict(r)
        d.pop("id", None)
        d["is_definition"] = bool(d["is_definition"])
        d["modifiers"] = json.loads(d["modifiers"]) if d.get("modifiers") else None
        d["enum_values"] = (
            json.loads(d["enum_values"]) if d.get("enum_values") else None
        )
        d["params"] = json.loads(d["params"]) if d.get("params") else None
        return d

    @staticmethod
    def _ref_row(r) -> dict:
        d = dict(r)
        d.pop("id", None)
        return d

    # ----- dict-style back-compat -----

    def __getitem__(self, key):
        if key == "symbols":
            return self.all_symbols()
        if key == "refs":
            return self.all_refs()
        if key == "files":
            return self.files
        return self.meta[key]

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    # ----- accessors -----

    @property
    def meta(self) -> dict:
        if self._meta_cache is None:
            self._meta_cache = {
                row["key"]: json.loads(row["value"])
                for row in self._conn.execute("SELECT key, value FROM meta")
            }
        return self._meta_cache

    @property
    def files(self) -> list:
        if self._files_cache is None:
            self._files_cache = [
                self._file_row(r) for r in self._conn.execute("SELECT * FROM files")
            ]
        return self._files_cache

    def all_symbols(self) -> list:
        if self._all_syms is None:
            self._all_syms = [
                self._sym_row(r) for r in self._conn.execute("SELECT * FROM symbols")
            ]
        return self._all_syms

    def all_refs(self) -> list:
        if self._all_refs is None:
            self._all_refs = [
                self._ref_row(r) for r in self._conn.execute("SELECT * FROM refs")
            ]
        return self._all_refs

    # ----- path normalization -----

    @property
    def _file_set(self) -> set[str]:
        """Set of root-relative paths actually in the index."""
        if not hasattr(self, "_file_set_cache"):
            self._file_set_cache = {f["path"] for f in self.files}
        return self._file_set_cache

    def normalize_file_path(self, path: str) -> Optional[str]:
        """Resolve a file-path-ish string to the canonical root-relative path
        stored in the index. Returns None if no match.

        Accepts:
          1. Exact root-relative path that's already in the index ("ba.c")
          2. Absolute path, if it lives under `meta.root` ("/abs/.../ba.c")
          3. Basename / suffix match — if exactly one file's path ends with
             `/path`, that file is returned ("ba.c" → "kunit/kunit-mock-ba.c"
             only if that's the unique suffix match)

        Ambiguous (multiple suffix matches) → returns None; caller can iterate
        `idx.files` for the full list."""
        if not path:
            return None
        # 1. Already canonical
        if path in self._file_set:
            return path
        # 2. Absolute → strip root prefix
        root_str = self.meta.get("root")
        if root_str:
            try:
                p = Path(path)
                if p.is_absolute():
                    rel = str(p.resolve().relative_to(Path(root_str).resolve()))
                    if rel in self._file_set:
                        return rel
            except (ValueError, OSError):
                pass
        # 3. Suffix match (basename or partial)
        if "/" not in path:
            # bare basename: must end with /<path>
            matches = [f for f in self._file_set if f.endswith("/" + path)]
        else:
            matches = [f for f in self._file_set if f.endswith("/" + path) or f == path]
        if len(matches) == 1:
            return matches[0]
        return None

    def find_symbols(self, *, name=None, qualified_name=None, kind=None, file=None):
        if file is not None:
            file = self.normalize_file_path(file) or file
        clauses, params = [], []
        if name is not None:
            clauses.append("name = ?")
            params.append(name)
        if qualified_name is not None:
            clauses.append("qualified_name = ?")
            params.append(qualified_name)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if file is not None:
            clauses.append("file = ?")
            params.append(file)
        q = "SELECT * FROM symbols"
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        return [self._sym_row(r) for r in self._conn.execute(q, params)]

    def find_refs(self, *, name=None, kind=None, file=None):
        if file is not None:
            file = self.normalize_file_path(file) or file
        clauses, params = [], []
        if name is not None:
            clauses.append("name = ?")
            params.append(name)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if file is not None:
            clauses.append("file = ?")
            params.append(file)
        q = "SELECT * FROM refs"
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        return [self._ref_row(r) for r in self._conn.execute(q, params)]

    def find_refs_in_range(self, file: str, start_line: int, end_line: int):
        """Refs in a contiguous line range of one file (used for callees)."""
        file = self.normalize_file_path(file) or file
        return [
            self._ref_row(r)
            for r in self._conn.execute(
                "SELECT * FROM refs WHERE file = ? AND line BETWEEN ? AND ?",
                (file, start_line, end_line),
            )
        ]

    def kind_counts(self) -> Counter:
        return Counter(
            {
                r["kind"]: r["n"]
                for r in self._conn.execute(
                    "SELECT kind, COUNT(*) AS n FROM symbols GROUP BY kind ORDER BY n DESC"
                )
            }
        )

    def ref_kind_counts(self) -> Counter:
        return Counter(
            {
                r["kind"]: r["n"]
                for r in self._conn.execute(
                    "SELECT kind, COUNT(*) AS n FROM refs GROUP BY kind ORDER BY n DESC"
                )
            }
        )

    def top_ref_names(self, kind: str, limit: int = 10):
        return [
            (r["name"], r["n"])
            for r in self._conn.execute(
                "SELECT name, COUNT(*) AS n FROM refs WHERE kind = ? "
                "GROUP BY name ORDER BY n DESC LIMIT ?",
                (kind, limit),
            )
        ]

    def n_symbols(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]

    def n_refs(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM refs").fetchone()[0]

    def n_definitions(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM symbols WHERE is_definition = 1"
        ).fetchone()[0]


def load_index(path: Path) -> IndexStore:
    return IndexStore(path)
