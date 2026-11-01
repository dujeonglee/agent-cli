# Originally ported from minish.ai/Agent-tools tsindex.py (Apache 2.0).
# See NOTICE for the list of modifications.
"""SQLite-backed index reader.

`IndexStore` opens an existing index file (produced by
`agent_cli.code_index.builder.build`) and exposes both dict-style
access (`idx['symbols']`, `idx['root']`) and indexed-lookup methods
(`find_symbols(name=...)`, `find_refs(...)`) that exploit the SQL secondary
indexes, plus the call-graph queries on the stored name resolution
(`refs_to`, `refs_in_functions`, `candidate_refs`, … — v10.96.0) that
`callgraph.callers_of`/`callees_of` use.

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
        d["bases"] = json.loads(d["bases"]) if d.get("bases") else None
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

    # ----- call graph on the stored resolution (v10.96.0, DESIGN §21) -----
    # 가벼운 튜플·좁은 열. 순서는 전부 ``ORDER BY id`` — 사이트·이웃 순서는 참조 id
    # 순이다. ``+kind`` 는 find_symbols 와 같은 planner 함정(통계 없이 kind 인덱스를
    # 고른다) 때문.

    def _defs(self, where: str, params: tuple, *, functions: bool = True) -> list[dict]:
        kind = "AND +kind = 'function' " if functions else ""
        out = []
        for r in self._conn.execute(
            f"SELECT * FROM symbols WHERE {where} {kind}"
            "AND is_definition = 1 ORDER BY id",
            params,
        ):
            d = self._sym_row(r)
            d["id"] = r["id"]
            out.append(d)
        return out

    def definitions(
        self,
        name: str,
        *,
        file: str | None = None,
        functions: bool = True,
        fallback: bool = True,
    ) -> list[dict]:
        """``name`` 의 정의들 (v10.97.0). 맨 이름이면 그 이름 전부; 한정 이름은
        ``qualified_name`` 그대로(``Box.get``·``ns::f``), 아니면 모듈로 한정한 이름
        (``fsio._write``·``agent_cli.fsio.Box.get`` — 파일 경로를 모듈 이름으로 읽어
        ``모듈.한정 이름`` 의 끝이 맞는 것; 없으면 앞이 모듈과 맞고 마지막 조각이 이름인
        것 — 감싼 함수·클래스를 건너뛴 ``fsio._write``), 그것도 없으면 마지막 조각의 맨
        이름으로.
        ``file`` 은 그 파일의 정의만. ``fallback=False`` 면 한정 이름이 맞지 않을 때
        마지막 조각으로 넓히지 않는다(빈 목록)."""
        where, extra = ("", ())
        if file is not None:
            where, extra = (" AND file = ?", (file,))
        if "." not in name and "::" not in name:
            return self._defs("name = ?" + where, (name, *extra), functions=functions)
        exact = self._defs(
            "qualified_name = ?" + where, (name, *extra), functions=functions
        )
        if exact:
            return exact
        norm = name.replace("::", ".")
        leaf = norm.rsplit(".", 1)[-1]
        bare = self._defs("name = ?" + where, (leaf, *extra), functions=functions)
        by_module = [
            d
            for d in bare
            if _ends_with_name(
                f"{_module_of(d['file'])}.{d['qualified_name'].replace('::', '.')}",
                norm,
            )
        ]
        if not by_module:
            # 모듈만 앞에 쓰고 감싼 함수·클래스는 건너뛴 이름 — `fsio._write` 가
            # `agent_cli/fsio.py` 의 `atomic_write_text._write`
            prefix = norm[: -len(leaf) - 1]
            by_module = [
                d for d in bare if _ends_with_name(_module_of(d["file"]), prefix)
            ]
        return by_module or (bare if fallback else [])

    def qualified_name_counts(self, names) -> dict[str, int]:
        """한정 이름마다 그 이름의 정의 수 — 둘 이상이면 출력에 파일을 붙인다."""
        names = list(dict.fromkeys(names))
        out: dict[str, int] = {}
        for i in range(0, len(names), 500):
            chunk = names[i : i + 500]
            marks = ",".join("?" for _ in chunk)
            for qn, n in self._conn.execute(
                f"SELECT qualified_name, COUNT(*) FROM symbols WHERE qualified_name "
                f"IN ({marks}) AND is_definition = 1 GROUP BY qualified_name",
                chunk,
            ):
                out[qn] = n
        return out

    def refs_resolved_to(self, ids, kind: str | None = None) -> list[dict]:
        """``ids`` 정의로 해석된 참조 — 정의 하나를 고른 ``refs``."""
        ids = list(dict.fromkeys(ids))
        out: list[dict] = []
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            marks = ",".join("?" for _ in chunk)
            q = f"SELECT * FROM refs WHERE target_id IN ({marks})"
            params: list = list(chunk)
            if kind is not None:
                q += " AND kind = ?"
                params.append(kind)
            out += [self._ref_row(r) for r in self._conn.execute(q, params)]
        return out

    def function_definitions(self, name: str) -> list[dict]:
        """함수 정의 — `definitions` 의 함수만."""
        return self.definitions(name)

    def function_definitions_exact(self, name: str, language: str) -> list[dict]:
        """맨 이름·언어가 같은 함수 정의 전부 — 이름으로만 맞추는 언어의 한 노드."""
        return self._defs("name = ? AND +language = ?", (name, language))

    def definition_count(self, name: str, language: str) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM symbols WHERE name = ? AND +kind = 'function' "
            "AND is_definition = 1 AND +language = ?",
            (name, language),
        ).fetchone()[0]

    def symbols_by_id(self, ids) -> dict[int, tuple[str, str, str]]:
        """``{id: (qualified_name, name, file)}``."""
        ids = list(dict.fromkeys(ids))
        out: dict[int, tuple[str, str, str]] = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            marks = ",".join("?" for _ in chunk)
            for r in self._conn.execute(
                f"SELECT id, qualified_name, name, file FROM symbols WHERE id IN ({marks})",
                chunk,
            ):
                out[r[0]] = (r[1], r[2], r[3])
        return out

    def function_ids(self, ids) -> set[int]:
        """``ids`` 중 함수 정의인 것."""
        ids = list(dict.fromkeys(ids))
        out: set[int] = set()
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            marks = ",".join("?" for _ in chunk)
            out.update(
                r[0]
                for r in self._conn.execute(
                    f"SELECT id FROM symbols WHERE id IN ({marks}) "
                    "AND +kind = 'function' AND is_definition = 1",
                    chunk,
                )
            )
        return out

    def refs_to(self, target_id: int) -> list[tuple[int, str, int, str]]:
        """``target_id`` 로 해석된 call·name 참조 중 함수 안의 것 ``(fn, file, line, kind)``."""
        return list(
            self._conn.execute(
                "SELECT fn_symbol_id, file, line, kind FROM refs WHERE target_id = ? "
                "AND kind IN ('call', 'name') AND fn_symbol_id IS NOT NULL ORDER BY id",
                (target_id,),
            )
        )

    def candidate_refs(self, name: str, language: str, with_fn: bool = False):
        """이름으로만 맞춘(``candidates``) call·name 참조 중 함수 안의 것 —
        ``(file, line, kind)``, ``with_fn`` 이면 ``(fn, file, line, kind)``."""
        cols = "fn_symbol_id, file, line, kind" if with_fn else "file, line, kind"
        return list(
            self._conn.execute(
                f"SELECT {cols} FROM refs WHERE name = ? AND +language = ? "
                "AND resolution = 'candidates' AND kind IN ('call', 'name') "
                "AND fn_symbol_id IS NOT NULL ORDER BY id",
                (name, language),
            )
        )

    def refs_in_functions(self, fn_ids) -> list[tuple]:
        """함수들 안의 call·name 참조 ``(name, file, line, kind, resolution, target_id)``."""
        fn_ids = list(fn_ids)
        out: list[tuple] = []
        for i in range(0, len(fn_ids), 500):
            chunk = fn_ids[i : i + 500]
            marks = ",".join("?" for _ in chunk)
            out.extend(
                self._conn.execute(
                    f"SELECT name, file, line, kind, resolution, target_id FROM refs "
                    f"WHERE fn_symbol_id IN ({marks}) AND kind IN ('call', 'name') "
                    "ORDER BY id",
                    chunk,
                )
            )
        return out

    def symbols_named(self, name: str) -> list[dict]:
        """이름이 ``name`` 인 기호 전부, 행 순(``ORDER BY id``) — slice 의 ``pick()``
        가 고른다 (v10.94.1; 종전엔 기호 전부를 올려 이름별로 묶었다)."""
        out = []
        for r in self._conn.execute(
            "SELECT * FROM symbols WHERE name = ? ORDER BY id", (name,)
        ):
            d = self._sym_row(r)
            d["id"] = r["id"]
            out.append(d)
        return out

    def symbol(self, symbol_id: int) -> dict | None:
        """기호 하나(``id`` 포함) — slice 가 해석된 이웃 정의를 펼칠 때."""
        r = self._conn.execute(
            "SELECT * FROM symbols WHERE id = ?", (symbol_id,)
        ).fetchone()
        if r is None:
            return None
        d = self._sym_row(r)
        d["id"] = r["id"]
        return d

    def ref_names_in_range(
        self, file: str, start_line: int, end_line: int, kind: str
    ) -> set[str]:
        """그 파일 ``start_line..end_line`` 에서 ``kind`` 참조의 이름들 — slice 의
        types·macros (v10.94.1; 종전엔 참조 전부를 훑었다)."""
        return {
            r[0]
            for r in self._conn.execute(
                "SELECT DISTINCT name FROM refs WHERE file = ? "
                "AND line BETWEEN ? AND ? AND +kind = ?",
                (file, start_line, end_line, kind),
            )
        }

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


def _module_of(path: str) -> str:
    """파일 경로를 점 이름으로 — ``agent_cli/fsio.py`` → ``agent_cli.fsio``,
    ``pkg/__init__.py`` → ``pkg``. 확장자는 무엇이든 뗀다(점으로 한정한 이름을 쓰는
    다른 언어도 같은 규칙)."""
    stem = path.rsplit(".", 1)[0] if "." in path.rsplit("/", 1)[-1] else path
    parts = stem.split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _ends_with_name(full: str, name: str) -> bool:
    """``full`` 이 ``name`` 이거나 점 경계에서 ``name`` 으로 끝나는가."""
    return full == name or full.endswith("." + name)
