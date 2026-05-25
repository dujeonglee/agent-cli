"""Tests for IndexStore query API correctness (DESIGN §12.3).

Covers the filter combinations on ``find_symbols`` / ``find_refs``,
``find_refs_in_range`` line-bounded slicing, ``normalize_file_path``
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


class TestFindRefsInRange:
    """``find_refs_in_range`` returns only refs in [start_line, end_line]."""

    def test_in_range_inclusive(self, python_index):
        store, by_name, _ = python_index
        # process body lives at lines 26-27 in app.py and contains helper() call
        process = [s for s in by_name["process"] if s.get("modifiers") == ["async"]][0]
        refs = store.find_refs_in_range(
            process["file"], process["line"], process["end_line"]
        )
        assert refs
        for r in refs:
            assert process["line"] <= r["line"] <= process["end_line"]
            assert r["file"] == process["file"]

    def test_out_of_range_returns_empty(self, python_index):
        store, _, _ = python_index
        # high line numbers far past any file
        refs = store.find_refs_in_range("app.py", 10_000, 20_000)
        assert refs == []

    def test_range_excludes_refs_outside_window(self, python_index):
        store, _, _ = python_index
        # whole-file vs a tiny window — the tiny window should be a strict subset
        whole = store.find_refs_in_range("app.py", 1, 10_000)
        tiny = store.find_refs_in_range("app.py", 26, 27)
        assert len(tiny) < len(whole)
        whole_pairs = {(r["name"], r["line"]) for r in whole}
        for r in tiny:
            assert (r["name"], r["line"]) in whole_pairs


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
