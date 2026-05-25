"""Tests for ``build_callgraph`` and helpers (DESIGN §12.4).

Covers the three-dict return shape of ``build_callgraph``, empty-Counter
behaviour for orphan functions, cycle handling, ``build_fn_ranges``
sortedness, and ``containing_fn`` for nested functions plus the BFS
depth-traversal pattern documented in DESIGN §12.4.
"""

from __future__ import annotations

from collections import Counter

import pytest

from agent_cli.code_index import build_callgraph
from agent_cli.code_index.callgraph import build_fn_ranges, containing_fn
from tests.code_index.helpers import build_fixture, build_tree


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
        result = build_callgraph(store)
        assert len(result) == 3
        calls_of, callers_of, sites_of = result
        # All three are dict-like
        assert hasattr(calls_of, "get")
        assert hasattr(callers_of, "get")
        assert hasattr(sites_of, "get")

    def test_calls_of_uses_counter_values(self, python_index):
        store, _ = python_index
        calls_of, _, _ = build_callgraph(store)
        # The fixture has two distinct `process` methods that each call
        # `helper` once — two unique sites. build_callgraph dedupes the
        # walker's call+name double-emit so the count is exactly 2 (not
        # 4, which would indicate the dedupe regressed).
        assert isinstance(calls_of["process"], Counter)
        assert calls_of["process"]["helper"] == 2

    def test_callers_of_uses_counter_values(self, python_index):
        store, _ = python_index
        _, callers_of, _ = build_callgraph(store)
        assert isinstance(callers_of["helper"], Counter)
        assert callers_of["helper"]["process"] == 2

    def test_sites_of_records_line_numbers(self, python_index):
        store, _ = python_index
        _, _, sites_of = build_callgraph(store)
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
        calls_of, _, _ = build_callgraph(store)
        # helper has no callees of its own
        assert calls_of.get("helper", Counter()) == Counter()

    def test_orphan_callers(self, python_index):
        store, _ = python_index
        _, callers_of, _ = build_callgraph(store)
        # with_defaults in extras.py is not called by anything in the fixture
        assert callers_of.get("with_defaults", Counter()) == Counter()


class TestCycleHandling:
    """``build_callgraph`` must not infinite-loop on cycles."""

    def test_cycle_records_both_directions(self, cycle_index):
        calls_of, callers_of, _ = build_callgraph(cycle_index)
        # Each of `a` and `b` invokes the other exactly once → one
        # unique site per direction after the call+name dedupe.
        assert calls_of["a"]["b"] == 1
        assert calls_of["b"]["a"] == 1
        assert callers_of["a"]["b"] == 1
        assert callers_of["b"]["a"] == 1


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
        for _file, (starts, _recs) in ranges.items():
            assert starts == sorted(starts)

    def test_only_functions_with_definitions(self, python_index):
        store, _ = python_index
        ranges = build_fn_ranges(store.all_symbols())
        # No non-function symbols (types/variables/constants) appear
        for _file, (_starts, recs) in ranges.items():
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
        calls_of, _, _ = build_callgraph(chain_index)
        # a -> {b}
        assert self._reachable(calls_of, "a", 1) == {"b"}

    def test_depth_two_includes_callees_of_callees(self, chain_index):
        calls_of, _, _ = build_callgraph(chain_index)
        # a -> {b, c}
        assert self._reachable(calls_of, "a", 2) == {"b", "c"}

    def test_depth_three_full_chain(self, chain_index):
        calls_of, _, _ = build_callgraph(chain_index)
        # a -> {b, c, d}
        assert self._reachable(calls_of, "a", 3) == {"b", "c", "d"}
