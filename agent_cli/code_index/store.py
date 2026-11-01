# Originally ported from minish.ai/Agent-tools tsindex.py (Apache 2.0).
# See NOTICE for the list of modifications.
"""SQLite-backed index reader.

`IndexStore` opens an existing index file (produced by
`agent_cli.code_index.builder.build`) and exposes both dict-style
access (`idx['symbols']`, `idx['root']`) and indexed-lookup methods
(`find_symbols(name=...)`, `find_refs(...)`) that exploit the SQL secondary
indexes, plus the per-name call-graph queries (`call_sites_by_name`,
`call_sites_in_range`, `function_ranges_in_file`, … — v10.94.0) that
`callgraph.callers_of`/`callees_of` use instead of loading every row.

`load_index(path)` is the convenience constructor used by callers that
don't need to think about the class name.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from agent_cli.code_index._sqlite import sqlite3


class IndexStore:
    """SQLite-backed index reader.

    Supports dict-style access for convenience (`idx['symbols']`, `idx['root']`)
    and indexed-lookup methods (`find_symbols(name=...)`) that use the SQL
    indexes."""

    def __init__(self, path: Path):
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._meta_cache: dict | None = None
        self._files_cache: list | None = None
        self._all_syms: list | None = None
        self._all_refs: list | None = None

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

    def code_names(self) -> set[str]:
        """코드 기호(함수·타입·변수·상수)의 이름 집합 — 증분 빌드가 "새로 생긴 이름" 을
        가를 때 쓴다 (v10.93.0; 종전엔 기호 전부를 dict 로 올려 걸렀다)."""
        from agent_cli.code_index.schema import CODE_NAME_KINDS

        marks = ",".join("?" for _ in CODE_NAME_KINDS)
        return {
            row[0]
            for row in self._conn.execute(
                f"SELECT DISTINCT name FROM symbols WHERE kind IN ({marks})",
                tuple(sorted(CODE_NAME_KINDS)),
            )
        }

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

    def normalize_file_path(self, path: str) -> str | None:
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
            # 다른 등호 절이 있으면 `+kind` — 통계(sqlite_stat1)가 없어 planner 가
            # 행이 가장 많은 idx_symbols_kind 를 고른다(6.4 ms → 0.26 ms, v10.94.0).
            clauses.append("+kind = ?" if clauses or file is not None else "kind = ?")
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

    # ----- per-name call graph (v10.94.0, docs/code-index/DESIGN.md §19) -----
    # 가벼운 튜플·좁은 열 — `_sym_row`/`_ref_row` 의 dict 변환을 피한다. 순서는
    # 전부 ``ORDER BY id``: 콜그래프의 사이트·이웃 순서는 참조 id 순이고, 증분 빌드는
    # 옛 행을 복사한 뒤 새 행을 붙이므로 파일 순이 아니다. ``+kind`` 는 find_symbols
    # 와 같은 planner 함정 때문.

    def function_defs(self, name: str) -> list[tuple[str, int, int]]:
        """이름이 ``name`` 인 함수 **정의**의 ``(file, line, end_line)``."""
        return list(
            self._conn.execute(
                "SELECT file, line, end_line FROM symbols WHERE name = ? "
                "AND +kind = 'function' AND is_definition = 1 ORDER BY id",
                (name,),
            )
        )

    def function_ranges_in_file(self, file: str) -> list[dict]:
        """그 파일의 함수 정의 — `callgraph.build_fn_ranges` 가 읽는 키만 든 dict."""
        return [
            {
                "kind": "function",
                "is_definition": True,
                "file": file,
                "name": r[0],
                "line": r[1],
                "end_line": r[2],
                "params": json.loads(r[3]) if r[3] else None,
            }
            for r in self._conn.execute(
                "SELECT name, line, end_line, params FROM symbols WHERE file = ? "
                "AND +kind = 'function' AND is_definition = 1 ORDER BY id",
                (file,),
            )
        ]

    def is_function_name(self, name: str) -> bool:
        """``name`` 이 함수(정의·선언)로 색인돼 있다."""
        row = self._conn.execute(
            "SELECT 1 FROM symbols WHERE name = ? AND +kind = 'function' LIMIT 1",
            (name,),
        ).fetchone()
        return row is not None

    def function_names_among(self, names) -> set[str]:
        """``names`` 중 함수(정의·선언)로 색인된 것 — 500개씩 끊어 묻는다."""
        names = list(dict.fromkeys(names))
        out: set[str] = set()
        for i in range(0, len(names), 500):
            chunk = names[i : i + 500]
            marks = ",".join("?" for _ in chunk)
            out.update(
                r[0]
                for r in self._conn.execute(
                    f"SELECT DISTINCT name FROM symbols WHERE +kind = 'function' "
                    f"AND name IN ({marks})",
                    chunk,
                )
            )
        return out

    def call_sites_by_name(self, name: str) -> list[tuple[int, str, int, str]]:
        """``name`` 을 부르거나 가리키는 참조의 ``(id, file, line, kind)``."""
        return list(
            self._conn.execute(
                "SELECT id, file, line, kind FROM refs WHERE name = ? "
                "AND kind IN ('call', 'name') ORDER BY id",
                (name,),
            )
        )

    def call_sites_in_range(
        self, file: str, start_line: int, end_line: int
    ) -> list[tuple[int, str, int, str]]:
        """그 파일 ``start_line..end_line`` 의 call·name 참조 ``(id, name, line, kind)``."""
        return list(
            self._conn.execute(
                "SELECT id, name, line, kind FROM refs WHERE file = ? "
                "AND line BETWEEN ? AND ? AND kind IN ('call', 'name') ORDER BY id",
                (file, start_line, end_line),
            )
        )

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
