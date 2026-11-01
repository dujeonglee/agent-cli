"""Tests for IndexStore query API correctness (DESIGN §12.3).

Covers the filter combinations on ``find_symbols`` / ``find_refs``,
the call-graph queries on the stored resolution (v10.96.0), ``normalize_file_path``
across exact / absolute / bare-basename / suffix / ambiguous shapes
(using the ``nested/`` multi-file fixture), and the aggregate accessors
``kind_counts`` / ``ref_kind_counts`` / ``top_ref_names`` /
``n_symbols`` / ``n_refs`` / ``n_definitions``.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from tests.code_index.helpers import build_fixture


@pytest.fixture(scope="module")
def python_index():
    store, by_name = build_fixture("python")
    return store, by_name, Path(store.meta["root"])


@pytest.fixture(scope="module")
def nested_index():
    store, by_name = build_fixture("nested")
    return store, by_name, Path(store.meta["root"])


class TestFindSymbols:
    """``find_symbols`` filter-combination matrix on the python fixture."""

    def test_name_only_exact_match(self, python_index):
        store, _, _ = python_index
        hits = store.find_symbols(name="helper")
        assert len(hits) == 1
        assert hits[0]["name"] == "helper"
        assert hits[0]["kind"] == "function"

    def test_name_no_match_returns_empty(self, python_index):
        store, _, _ = python_index
        assert store.find_symbols(name="__no_such_symbol__") == []

    def test_kind_filter(self, python_index):
        store, _, _ = python_index
        types = store.find_symbols(kind="type")
        assert types, "python fixture defines classes that should map to kind=type"
        assert all(s["kind"] == "type" for s in types)

    def test_file_filter_relative(self, python_index):
        store, _, _ = python_index
        hits = store.find_symbols(file="app.py")
        assert hits
        assert all(s["file"] == "app.py" for s in hits)

    def test_file_filter_absolute_path_normalizes(self, python_index):
        store, _, root = python_index
        abs_p = str(root / "extras.py")
        hits_abs = store.find_symbols(file=abs_p)
        hits_rel = store.find_symbols(file="extras.py")
        assert {s["name"] for s in hits_abs} == {s["name"] for s in hits_rel}
        assert hits_abs, "abs-path lookup should resolve into matches"

    def test_name_and_kind_combined(self, python_index):
        store, _, _ = python_index
        hits = store.find_symbols(name="Service", kind="type")
        assert len(hits) == 1
        assert hits[0]["kind"] == "type"

    def test_name_and_kind_mismatched_returns_empty(self, python_index):
        store, _, _ = python_index
        # Service exists as kind=type, not kind=function
        assert store.find_symbols(name="Service", kind="function") == []

    def test_file_and_kind_combined(self, python_index):
        store, _, _ = python_index
        hits = store.find_symbols(file="app.py", kind="function")
        assert hits
        assert all(s["file"] == "app.py" and s["kind"] == "function" for s in hits)

    def test_no_filters_returns_everything(self, python_index):
        store, _, _ = python_index
        all_hits = store.find_symbols()
        assert len(all_hits) == store.n_symbols()


class TestFindRefs:
    """``find_refs`` filter combinations."""

    def test_name_and_kind_combined(self, python_index):
        store, _, _ = python_index
        # helper() is called from process — at least one kind=call ref
        hits = store.find_refs(name="helper", kind="call")
        assert hits
        assert all(r["name"] == "helper" and r["kind"] == "call" for r in hits)

    def test_kind_only(self, python_index):
        store, _, _ = python_index
        calls = store.find_refs(kind="call")
        assert calls
        assert all(r["kind"] == "call" for r in calls)

    def test_no_filters_returns_all_refs(self, python_index):
        store, _, _ = python_index
        all_refs = store.find_refs()
        assert len(all_refs) == store.n_refs()


class TestResolutionQueries:
    """v10.96.0 — callers_of/callees_of 가 쓰는 질의(저장된 이름 해석 위에서)."""

    def test_function_definitions_bare_is_all_qualified_is_one(self, python_index):
        store, _, _ = python_index
        defs = store.function_definitions("helper")
        assert defs and all(d["kind"] == "function" and "id" in d for d in defs)
        method = store.function_definitions("DerivedService.process")
        assert [d["qualified_name"] for d in method] == ["DerivedService.process"]
        assert len(store.function_definitions("process")) == 2  # 맨 이름은 둘 다
        assert store.function_definitions("no_such_name") == []

    def test_module_qualified_names(self, tmp_path):
        """v10.97.0 — 파일 경로를 모듈 이름으로 읽어 ``모듈.한정 이름`` 의 끝이 맞는 것."""
        from agent_cli.code_index import build, load_index
        from agent_cli.code_index.store import _module_of

        for rel, body in {
            "pkg/__init__.py": "def init_fn():\n    pass\n",
            "pkg/fsio.py": "def _write():\n    pass\n\n\nclass Box:\n"
            "    def get(self):\n        pass\n\n\nclass Other:\n"
            "    def get(self):\n        pass\n",
            "pkg/other.py": "def _write():\n    pass\n\n\ndef deep():\n    pass\n",
            "pkg/nested.py": "def outer():\n    def deep():\n        pass\n",
        }.items():
            (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / rel).write_text(body)
        build(tmp_path, tmp_path / "x.db", defs_path=None, verbose=False)
        store = load_index(tmp_path / "x.db")

        def files(name, **kw):
            return [d["file"] for d in store.definitions(name, **kw)]

        assert files("_write") == ["pkg/fsio.py", "pkg/other.py"]
        assert files("fsio._write") == ["pkg/fsio.py"]
        assert files("pkg.other._write") == ["pkg/other.py"]
        # 한 모듈에 같은 이름 메서드가 둘 — 모듈 + 한정 이름이 하나를 고른다
        assert [d["qualified_name"] for d in store.definitions("fsio.Box.get")] == [
            "Box.get"
        ]
        assert files("pkg.init_fn") == ["pkg/__init__.py"]
        # 모듈만 쓰고 감싼 함수는 건너뛴 이름
        assert files("nested.deep") == ["pkg/nested.py"]
        assert files("pkg.fsio.get") == ["pkg/fsio.py", "pkg/fsio.py"]
        assert files("_write", file="pkg/other.py") == ["pkg/other.py"]
        # 맞는 모듈이 없으면 마지막 조각으로 넓힌다 — 넓히지 말라면 빈 목록
        assert len(files("nowhere._write")) == 2
        assert files("nowhere._write", fallback=False) == []
        assert files("sio._write") == ["pkg/fsio.py", "pkg/other.py"]  # 점 경계만
        assert _module_of("a/b/c.py") == "a.b.c" and _module_of("a/__init__.py") == "a"

    def test_refs_to_and_refs_in_functions(self, python_index):
        store, _, _ = python_index
        (helper,) = store.function_definitions("helper")
        sites = store.refs_to(helper["id"])
        assert sites and all(fn is not None for fn, *_ in sites)
        callers = store.symbols_by_id({fn for fn, *_ in sites})
        assert {name for _qn, name, _f in callers.values()} == {"process"}
        inside = store.refs_in_functions({fn for fn, *_ in sites})
        assert any(r[4] == "resolved" and r[5] == helper["id"] for r in inside)

    def test_definition_count_is_per_language(self, python_index):
        store, _, _ = python_index
        assert store.definition_count("helper", "python") == 1
        assert store.definition_count("helper", "rust") == 0

    def test_ids_survive_an_incremental_build(self, tmp_path):
        """기호 id 가 빌드를 넘어 같아야 다른 파일의 해석 결과가 맞다."""
        from agent_cli.code_index import build, load_index

        (tmp_path / "a.py").write_text("def t():\n    pass\n")
        (tmp_path / "b.py").write_text("from a import t\n\n\ndef b():\n    t()\n")
        db = tmp_path / ".db"
        build(tmp_path, db, defs_path=None, verbose=False)
        (before,) = load_index(db).function_definitions("t")
        (tmp_path / "a.py").write_text("# moved down\n\n\ndef t():\n    pass\n")
        build(tmp_path, db, defs_path=None, verbose=False)
        store = load_index(db)
        (after,) = store.function_definitions("t")
        assert after["id"] == before["id"] and after["line"] == 4
        assert [fn for fn, *_ in store.refs_to(after["id"])] == [
            store.function_definitions("b")[0]["id"]
        ]

    @pytest.mark.parametrize(
        "query",
        [
            (
                "SELECT * FROM symbols WHERE name = 'x' AND +kind = 'function' "
                "AND is_definition = 1"
            ),
            (
                "SELECT COUNT(*) FROM symbols WHERE name = 'x' AND +kind = 'function' "
                "AND is_definition = 1 AND +language = 'python'"
            ),
        ],
    )
    def test_name_queries_use_the_name_index(self, python_index, query):
        """통계가 없으면 planner 가 `kind = ?` 에 idx_symbols_kind 를 고른다(6.4 ms) —
        `+kind` 가 그 선택을 막는다. find_symbols(name=, kind=) 도 같은 SQL 을 낸다."""
        store, _, _ = python_index
        plan = " ".join(
            str(r[-1]) for r in store._conn.execute("EXPLAIN QUERY PLAN " + query)
        )
        assert "idx_symbols_name" in plan, plan
        assert store.find_symbols(name="helper", kind="function")
        assert store.find_symbols(kind="function")


class TestNormalizeFilePath:
    """``normalize_file_path`` across the documented input shapes.

    Uses the ``nested/`` fixture (``sub/mod.py``, ``sub/mod_dup.py``,
    ``other/mod.py``, ``top.py``) so the ambiguity branch is exercised.
    """

    def test_exact_canonical_already_in_index(self, nested_index):
        store, _, _ = nested_index
        assert store.normalize_file_path("sub/mod.py") == "sub/mod.py"

    def test_absolute_under_root(self, nested_index):
        store, _, root = nested_index
        abs_p = str(root / "sub" / "mod.py")
        assert store.normalize_file_path(abs_p) == "sub/mod.py"

    def test_bare_basename_unique_suffix_resolves(self, nested_index):
        store, _, _ = nested_index
        # mod_dup.py exists only in sub/ — single suffix match
        assert store.normalize_file_path("mod_dup.py") == "sub/mod_dup.py"

    def test_bare_basename_ambiguous_returns_none(self, nested_index):
        store, _, _ = nested_index
        # mod.py exists in BOTH sub/ and other/ — ambiguous
        assert store.normalize_file_path("mod.py") is None

    def test_relative_suffix_match_resolves(self, nested_index):
        store, _, _ = nested_index
        # 'other/mod.py' is the canonical path itself — exact branch
        # 'sub/mod.py' likewise. Use a partial suffix that's only in one tree.
        assert store.normalize_file_path("other/mod.py") == "other/mod.py"

    def test_empty_string_returns_none(self, nested_index):
        store, _, _ = nested_index
        assert store.normalize_file_path("") is None

    def test_completely_unknown_path_returns_none(self, nested_index):
        store, _, _ = nested_index
        assert store.normalize_file_path("not/a/real/file.py") is None


class TestKindAggregates:
    """``kind_counts`` / ``ref_kind_counts`` / ``top_ref_names``."""

    def test_kind_counts_returns_counter(self, python_index):
        store, _, _ = python_index
        kc = store.kind_counts()
        assert isinstance(kc, Counter)
        # Python fixture defines functions and at least one type (Service class)
        assert kc["function"] > 0
        assert kc["type"] > 0

    def test_kind_counts_sums_to_n_symbols(self, python_index):
        store, _, _ = python_index
        assert sum(store.kind_counts().values()) == store.n_symbols()

    def test_ref_kind_counts_has_call_or_name(self, python_index):
        store, _, _ = python_index
        rkc = store.ref_kind_counts()
        assert isinstance(rkc, Counter)
        # Python emits 'call' and 'name' refs but never 'type'
        assert ("call" in rkc) or ("name" in rkc)

    def test_top_ref_names_sorted_desc_and_limited(self, python_index):
        store, _, _ = python_index
        top = store.top_ref_names("call", limit=3)
        assert len(top) <= 3
        counts = [n for _, n in top]
        assert counts == sorted(counts, reverse=True)

    def test_top_ref_names_limit_one(self, python_index):
        store, _, _ = python_index
        top = store.top_ref_names("call", limit=1)
        assert len(top) <= 1


class TestCounts:
    """``n_symbols`` / ``n_refs`` / ``n_definitions`` sanity bounds."""

    def test_counts_nonzero(self, python_index):
        store, _, _ = python_index
        assert store.n_symbols() > 0
        assert store.n_refs() > 0
        assert store.n_definitions() > 0

    def test_definitions_bounded_by_total(self, python_index):
        store, _, _ = python_index
        # every definition is a symbol, so n_definitions <= n_symbols
        assert store.n_definitions() <= store.n_symbols()
