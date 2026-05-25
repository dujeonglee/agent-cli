"""Tests for IndexStore.normalize_file_path and abs-path-friendly lookups.

Ported from minish.ai/Agent-tools tests/test_path_normalize.py (Apache 2.0).

Covers:

- Exact relative path (already canonical).
- Absolute path (under index root → strip prefix).
- Absolute path outside root → no match.
- Basename match when unique.
- Basename match when ambiguous → None (caller iterates files).
- Nested relative path.
- ``find_symbols(file=abs)`` works through normalization.
- ``find_refs(file=abs)`` works.
- ``find_refs_in_range(file=abs)`` works.

The nested fixture additionally exercises DESIGN §12.1 "same-name
function in two files" via ``dup_helper`` in both ``sub/mod.py`` and
``other/mod.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.code_index.helpers import build_fixture


@pytest.fixture(scope="module")
def python_index():
    store, by_name = build_fixture("python")
    return store, by_name, Path(store.meta["root"])


@pytest.fixture(scope="module")
def c_index():
    store, by_name = build_fixture("c")
    return store, by_name, Path(store.meta["root"])


@pytest.fixture(scope="module")
def nested_index():
    store, by_name = build_fixture("nested")
    return store, by_name, Path(store.meta["root"])


@pytest.fixture(scope="module")
def js_index():
    store, by_name = build_fixture("javascript")
    return store, by_name, Path(store.meta["root"])


class TestPathNormalization:
    """Use the python fixture (multi-file: app.py, extras.py, error.py)."""

    # ----- normalize_file_path -----

    def test_exact_relative(self, python_index):
        store, _, _ = python_index
        assert store.normalize_file_path("app.py") == "app.py"

    def test_absolute_under_root(self, python_index):
        store, _, root = python_index
        abs_p = str(root / "app.py")
        assert store.normalize_file_path(abs_p) == "app.py"

    def test_absolute_outside_root(self, python_index):
        store, _, _ = python_index
        assert store.normalize_file_path("/tmp/__nothing_here.py") is None

    def test_nonexistent_returns_none(self, python_index):
        store, _, _ = python_index
        assert store.normalize_file_path("nonexistent.py") is None

    def test_empty_string(self, python_index):
        store, _, _ = python_index
        assert store.normalize_file_path("") is None

    # ----- integrated into find_* -----

    def test_find_symbols_with_abs_path(self, python_index):
        store, _, root = python_index
        abs_p = str(root / "app.py")
        hits_abs = store.find_symbols(file=abs_p)
        hits_rel = store.find_symbols(file="app.py")
        assert len(hits_rel) > 0
        assert {s["name"] for s in hits_abs} == {s["name"] for s in hits_rel}

    def test_find_refs_with_abs_path(self, python_index):
        store, _, root = python_index
        abs_p = str(root / "app.py")
        refs_abs = store.find_refs(file=abs_p)
        refs_rel = store.find_refs(file="app.py")
        assert len(refs_abs) == len(refs_rel)

    def test_find_refs_in_range_with_abs_path(self, python_index):
        store, _, root = python_index
        abs_p = str(root / "app.py")
        refs_abs = store.find_refs_in_range(abs_p, 1, 200)
        refs_rel = store.find_refs_in_range("app.py", 1, 200)
        assert len(refs_abs) == len(refs_rel)
        assert len(refs_abs) > 0

    def test_unknown_file_returns_empty(self, python_index):
        store, _, _ = python_index
        assert store.find_symbols(file="/tmp/__no.py") == []
        assert store.find_refs(file="nope.py") == []


class TestAmbiguousSuffixMatch:
    """Use the C fixture (sample.c + sample.h + error.c)."""

    def test_bare_basename_no_parent_returns_self(self, c_index):
        store, _, _ = c_index
        # sample.c and sample.h are at root → exact-match branch wins.
        assert store.normalize_file_path("sample.c") == "sample.c"
        assert store.normalize_file_path("sample.h") == "sample.h"

    def test_abs_for_both_files(self, c_index):
        store, _, root = c_index
        for name in ("sample.c", "sample.h"):
            abs_p = str(root / name)
            assert store.normalize_file_path(abs_p) == name


class TestNestedDirectory:
    """Fixture: tests/code_index/fixtures/nested/

        top.py
        sub/mod.py       (sub_fn, dup_helper)
        sub/mod_dup.py   (dup_in_sub)
        other/mod.py     (other_fn, dup_helper)

    Tests nested-path lookup, suffix uniqueness, ambiguity handling, and
    same-name-across-files (``dup_helper``).
    """

    def test_nested_relative_exact(self, nested_index):
        store, _, _ = nested_index
        assert store.normalize_file_path("sub/mod.py") == "sub/mod.py"

    def test_nested_absolute(self, nested_index):
        store, _, root = nested_index
        abs_p = str(root / "sub" / "mod.py")
        assert store.normalize_file_path(abs_p) == "sub/mod.py"

    def test_bare_basename_ambiguous(self, nested_index):
        # mod.py exists in both sub/ and other/ → ambiguous → None
        store, _, _ = nested_index
        assert store.normalize_file_path("mod.py") is None

    def test_suffix_match_disambiguates(self, nested_index):
        store, _, _ = nested_index
        assert store.normalize_file_path("other/mod.py") == "other/mod.py"

    def test_unique_basename_resolves(self, nested_index):
        store, _, _ = nested_index
        assert store.normalize_file_path("mod_dup.py") == "sub/mod_dup.py"

    def test_find_symbols_with_nested_paths(self, nested_index):
        store, _, root = nested_index
        abs_p = str(root / "sub" / "mod.py")
        s_abs = store.find_symbols(file=abs_p)
        s_rel = store.find_symbols(file="sub/mod.py")
        assert {s["name"] for s in s_abs} == {s["name"] for s in s_rel}
        assert "sub_fn" in {s["name"] for s in s_abs}

    def test_ambiguous_basename_returns_no_symbols(self, nested_index):
        store, _, _ = nested_index
        assert store.find_symbols(file="mod.py") == []

    # ----- DESIGN §12.1: same-name function in two files -----

    def test_same_name_function_in_two_files(self, nested_index):
        store, _, _ = nested_index
        hits = store.find_symbols(name="dup_helper")
        files = sorted(s["file"] for s in hits)
        assert files == ["other/mod.py", "sub/mod.py"]

    def test_same_name_function_both_marked_definitions(self, nested_index):
        store, _, _ = nested_index
        hits = store.find_symbols(name="dup_helper")
        assert all(s["is_definition"] for s in hits)


class TestJsAbsRoundTrip:
    """Round-trip: relative → absolute → relative is stable."""

    def test_abs_round_trip(self, js_index):
        store, _, root = js_index
        abs_p = str(root / "app.js")
        norm = store.normalize_file_path(abs_p)
        assert norm == "app.js"
        abs2 = str(root / norm)
        assert store.normalize_file_path(abs2) == "app.js"
