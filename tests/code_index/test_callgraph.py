"""Tests for the call graph (DESIGN §12.4, §19).

v10.94.0: ``callers_of``/``callees_of`` answer one name with SQL; the old
whole-graph ``build_callgraph`` is the oracle ``reference_callgraph`` in
helpers, and ``TestEquivalentToReference`` / ``TestEquivalentOnGeneratedTree``
pin the two to byte-identical output. Covers Counter values, empty-Counter
behaviour for orphan functions, cycle handling, ``build_fn_ranges``
sortedness, and ``containing_fn`` for nested functions plus the BFS
depth-traversal pattern documented in DESIGN §12.4.
"""

from __future__ import annotations

from collections import Counter

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from agent_cli.code_index import Edges, callees_of, callers_of
from agent_cli.code_index.callgraph import build_fn_ranges, containing_fn
from tests.code_index.helpers import (
    FIXTURES,
    build_fixture,
    build_tree,
    reference_callgraph,
)


@pytest.fixture(scope="module")
def python_index():
    store, by_name = build_fixture("python")
    return store, by_name


@pytest.fixture(scope="module")
def cycle_index(tmp_path_factory):
    """Tiny tree with a -> b -> a cycle for cycle-handling tests."""
    root = tmp_path_factory.mktemp("cycle")
    (root / "cycle.py").write_text(
        "def a():\n    return b()\n\n\ndef b():\n    return a()\n"
    )
    return build_tree(root)


@pytest.fixture(scope="module")
def chain_index(tmp_path_factory):
    """Chain a -> b -> c -> d, for BFS depth traversal."""
    root = tmp_path_factory.mktemp("chain")
    (root / "chain.py").write_text(
        "def d():\n    return 4\n\n\n"
        "def c():\n    return d()\n\n\n"
        "def b():\n    return c()\n\n\n"
        "def a():\n    return b()\n"
    )
    return build_tree(root)


class TestBuildCallgraphShape:
    """Return-value shape of ``build_callgraph``."""

    def test_returns_three_dicts(self, python_index):
        store, _ = python_index
        result = reference_callgraph(store)
        assert len(result) == 3
        calls_of, callers_of, sites_of = result
        # All three are dict-like
        assert hasattr(calls_of, "get")
        assert hasattr(callers_of, "get")
        assert hasattr(sites_of, "get")

    def test_calls_of_uses_counter_values(self, python_index):
        store, _ = python_index
        calls_of, _, _ = reference_callgraph(store)
        # The fixture has two distinct `process` methods that each call
        # `helper` once — two unique sites. build_callgraph dedupes the
        # walker's call+name double-emit so the count is exactly 2 (not
        # 4, which would indicate the dedupe regressed).
        assert isinstance(calls_of["process"], Counter)
        assert calls_of["process"]["helper"] == 2

    def test_callers_of_uses_counter_values(self, python_index):
        store, _ = python_index
        _, callers_of, _ = reference_callgraph(store)
        assert isinstance(callers_of["helper"], Counter)
        assert callers_of["helper"]["process"] == 2

    def test_sites_of_records_line_numbers(self, python_index):
        store, _ = python_index
        _, _, sites_of = reference_callgraph(store)
        sites = sites_of[("process", "helper")]
        # Two unique call sites — line 27 (async process) and line 32
        # (sync process). One entry per unique (file, line), with the
        # primary 'call' kind selected over 'name' at the same site.
        assert {(f, ln) for f, ln, _ in sites} == {("app.py", 27), ("app.py", 32)}
        for site in sites:
            assert len(site) == 3
            assert site[2] == "call"  # 'call' wins over 'name' when both present


class TestEmptyCounters:
    """Functions with no callees / no callers return empty Counters (no KeyError)."""

    def test_orphan_callees(self, python_index):
        store, _ = python_index
        calls_of, _, _ = reference_callgraph(store)
        # helper has no callees of its own
        assert calls_of.get("helper", Counter()) == Counter()

    def test_orphan_callers(self, python_index):
        store, _ = python_index
        _, callers_of, _ = reference_callgraph(store)
        # with_defaults in extras.py is not called by anything in the fixture
        assert callers_of.get("with_defaults", Counter()) == Counter()


class TestCycleHandling:
    """``build_callgraph`` must not infinite-loop on cycles."""

    def test_cycle_records_both_directions(self, cycle_index):
        calls_of, callers_of, _ = reference_callgraph(cycle_index)
        # Each of `a` and `b` invokes the other exactly once → one
        # unique site per direction after the call+name dedupe.
        assert calls_of["a"]["b"] == 1
        assert calls_of["b"]["a"] == 1
        assert callers_of["a"]["b"] == 1
        assert callers_of["b"]["a"] == 1

    def test_per_name_queries_on_a_cycle(self, cycle_index):
        assert callees_of(cycle_index, "a").counts == Counter({"b": 1})
        assert callers_of(cycle_index, "a").counts == Counter({"b": 1})


class TestBuildFnRanges:
    """``build_fn_ranges`` shape and sorting."""

    def test_returns_dict_per_file(self, python_index):
        store, _ = python_index
        ranges = build_fn_ranges(store.all_symbols())
        assert isinstance(ranges, dict)
        # python fixture has app.py and extras.py with functions
        assert "app.py" in ranges

    def test_records_sorted_by_start_line(self, python_index):
        store, _ = python_index
        ranges = build_fn_ranges(store.all_symbols())
        for starts, _recs in ranges.values():
            assert starts == sorted(starts)

    def test_only_functions_with_definitions(self, python_index):
        store, _ = python_index
        ranges = build_fn_ranges(store.all_symbols())
        # No non-function symbols (types/variables/constants) appear
        for _starts, recs in ranges.values():
            for _s, _e, name, _params in recs:
                hits = store.find_symbols(name=name)
                assert any(
                    h["kind"] == "function" and h["is_definition"] for h in hits
                ), f"{name} should be a function definition"


class TestContainingFn:
    """``containing_fn`` line lookup, including the nested-function case."""

    def test_line_inside_function_returns_name(self, python_index):
        store, by_name = python_index
        fn_ranges = build_fn_ranges(store.all_symbols())
        helper = by_name["helper"][0]
        result = containing_fn(helper["file"], helper["line"], fn_ranges)
        assert result is not None
        assert result[0] == "helper"

    def test_line_outside_any_function_returns_none(self, python_index):
        store, _ = python_index
        fn_ranges = build_fn_ranges(store.all_symbols())
        # Line 1 of app.py is the module docstring — no enclosing function
        assert containing_fn("app.py", 1, fn_ranges) is None

    def test_unknown_file_returns_none(self, python_index):
        store, _ = python_index
        fn_ranges = build_fn_ranges(store.all_symbols())
        assert containing_fn("does_not_exist.py", 5, fn_ranges) is None

    def test_nested_function_innermost_wins(self, python_index):
        # outer() at line 14-20, inner() defined inside at line 17-18.
        # A line within inner's body should resolve to 'inner', not 'outer'.
        store, by_name = python_index
        fn_ranges = build_fn_ranges(store.all_symbols())
        inner = by_name["inner"][0]
        result = containing_fn(inner["file"], inner["line"] + 1, fn_ranges)
        assert result is not None
        assert result[0] == "inner"


class TestBfsDepthTraversal:
    """Walk ``calls_of`` transitively to verify the BFS depth pattern.

    DESIGN §12.4 calls this out: depth=1 = direct callees, depth=2 also
    pulls in callees of callees, etc. ``cmd_slice`` uses this internally;
    here we exercise the underlying graph directly.
    """

    @staticmethod
    def _reachable(calls_of, start: str, max_depth: int) -> set[str]:
        seen: set[str] = set()
        frontier: list[str] = list(calls_of.get(start, Counter()).keys())
        for _ in range(max_depth):
            next_frontier: list[str] = []
            for fn in frontier:
                if fn in seen:
                    continue
                seen.add(fn)
                next_frontier.extend(calls_of.get(fn, Counter()).keys())
            frontier = next_frontier
            if not frontier:
                break
        return seen

    def test_depth_one_direct_callees(self, chain_index):
        calls_of, _, _ = reference_callgraph(chain_index)
        # a -> {b}
        assert self._reachable(calls_of, "a", 1) == {"b"}

    def test_depth_two_includes_callees_of_callees(self, chain_index):
        calls_of, _, _ = reference_callgraph(chain_index)
        # a -> {b, c}
        assert self._reachable(calls_of, "a", 2) == {"b", "c"}

    def test_depth_three_full_chain(self, chain_index):
        calls_of, _, _ = reference_callgraph(chain_index)
        # a -> {b, c, d}
        assert self._reachable(calls_of, "a", 3) == {"b", "c", "d"}


# ----- v10.94.0: per-name queries ---------------------------------------------


def _assert_same_as_reference(store, names) -> None:
    """이름마다 counts(키 순서까지)·sites(순서·kind)·도구 텍스트가 오라클과 같다."""
    from agent_cli.tools.code_index import _format_edges

    calls_of, callers_ref, sites_of = reference_callgraph(store)
    for n in names:
        got = callers_of(store, n)
        want = callers_ref.get(n, Counter())
        assert list(got.counts.items()) == list(want.items()), ("callers", n)
        for c in want:
            assert got.sites[c] == sites_of[(c, n)], ("callers sites", n, c)
        got = callees_of(store, n)
        want = calls_of.get(n, Counter())
        assert list(got.counts.items()) == list(want.items()), ("callees", n)
        for c in want:
            assert got.sites[c] == sites_of[(n, c)], ("callees sites", n, c)
        ref_edges = Edges(want, {c: sites_of[(n, c)] for c in want})
        assert _format_edges(got) == _format_edges(ref_edges), ("text", n)


class TestEdgesSemantics:
    """옛 build_callgraph 의 규칙이 이름 단위 질의에서도 그대로인지 — 사례별로."""

    @pytest.fixture(scope="class")
    def idx(self, tmp_path_factory):
        root = tmp_path_factory.mktemp("sem")
        (root / "m.py").write_text(
            "def helper():\n    return 1\n\n\n"
            "def self_call():\n    return self_call()\n\n\n"
            "def shadow(helper):\n    return helper()\n\n\n"
            "def register(fn):\n    return fn\n\n\n"
            "def uses_callback():\n    return register(helper)\n\n\n"
            "def twice():\n    helper(); helper()\n\n\n"
            "def outer():\n"
            "    def inner():\n        return helper()\n"
            "    return helper()\n\n\n"
            "class Service:\n    pass\n"
        )
        return build_tree(root)

    def test_self_call_is_not_an_edge(self, idx):
        assert "self_call" not in callers_of(idx, "self_call").counts
        assert callees_of(idx, "self_call").counts == Counter()

    def test_a_parameter_with_the_same_name_shadows(self, idx):
        assert "shadow" not in callers_of(idx, "helper").counts

    def test_a_callback_reference_is_a_name_site(self, idx):
        edges = callers_of(idx, "helper")
        assert edges.counts["uses_callback"] == 1
        assert [k for _f, _l, k in edges.sites["uses_callback"]] == ["name"]

    def test_two_calls_on_one_line_are_one_site_of_kind_call(self, idx):
        edges = callers_of(idx, "helper")
        assert edges.counts["twice"] == 1
        assert [k for _f, _l, k in edges.sites["twice"]] == ["call"]

    def test_a_call_inside_a_nested_function_belongs_to_it(self, idx):
        assert callers_of(idx, "helper").counts["inner"] == 1
        assert "helper" in callees_of(idx, "inner").counts

    def test_a_call_after_the_nested_function_is_dropped(self, idx):
        """알려진 결함(D4, docs/code-index/DESIGN.md §19) — 안쪽 함수가 끝난 뒤 바깥
        본문의 호출은 어느 함수에도 붙지 않는다. 고칠 땐 출력이 바뀐다는 뜻이라
        여기서 고정해 둔다."""
        assert "outer" not in callers_of(idx, "helper").counts
        assert "helper" not in callees_of(idx, "outer").counts

    def test_a_non_function_name_has_no_edges(self, idx):
        assert callers_of(idx, "Service").counts == Counter()
        assert callees_of(idx, "Service").counts == Counter()
        assert callers_of(idx, "no_such_name").counts == Counter()

    def test_same_named_functions_are_one_node(self):
        store, _ = build_fixture("nested")
        _assert_same_as_reference(store, ["dup_helper"])
        defs = {f for f, _s, _e in store.function_defs("dup_helper")}
        assert defs == {"sub/mod.py", "other/mod.py"}


class TestEquivalentToReference:
    """fixture 마다 함수 이름 전부 + 함수 아닌 이름 몇 개 — 옛 전체 그래프와 같다."""

    @pytest.mark.parametrize(
        "lang", sorted(p.name for p in FIXTURES.iterdir() if p.is_dir())
    )
    def test_every_name(self, lang):
        store, by_name = build_fixture(lang)
        _assert_same_as_reference(store, sorted(by_name))


_SMALL_NAMES = ["a", "b", "c", "d", "e"]


@st.composite
def _py_tree(draw):
    """작은 이름 알파벳으로 충돌을 강제한 Python 파일 1~4개 — 호출·콜백·자기 호출·
    인자 가림·중첩 def 뒤의 호출을 섞는다."""
    files = {}
    for i in range(draw(st.integers(1, 4))):
        chunks = []
        for _ in range(draw(st.integers(1, 6))):
            fn = draw(st.sampled_from(_SMALL_NAMES))
            param = draw(st.sampled_from(["x", *_SMALL_NAMES]))
            body = []
            for _ in range(draw(st.integers(0, 4))):
                callee = draw(st.sampled_from(_SMALL_NAMES))
                body.append(
                    draw(st.sampled_from([f"{callee}()", f"keep({callee})", "pass"]))
                )
            if draw(st.booleans()):
                inner = draw(st.sampled_from(_SMALL_NAMES))
                body.append(f"def {inner}():\n        return {fn}()")
                body.append(f"{draw(st.sampled_from(_SMALL_NAMES))}()")
            lines = "\n    ".join(body or ["pass"])
            chunks.append(f"def {fn}({param}):\n    {lines}\n")
        files[f"m{i}.py"] = "\n\n".join(chunks)
    return files


class TestEquivalentOnGeneratedTree:
    @settings(
        max_examples=30,
        deadline=None,
        suppress_health_check=[
            HealthCheck.function_scoped_fixture,
            HealthCheck.too_slow,
        ],
    )
    @given(files=_py_tree())
    def test_every_name(self, tmp_path_factory, files):
        root = tmp_path_factory.mktemp("gen")
        for name, text in files.items():
            (root / name).write_text(text)
        store = build_tree(root)
        _assert_same_as_reference(store, [*_SMALL_NAMES, "keep", "x"])
