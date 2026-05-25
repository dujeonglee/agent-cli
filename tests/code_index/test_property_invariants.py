"""Property-based invariants for the code_index pipeline as a whole.

Where ``test_property_walkers.py`` stresses individual walker outputs,
this file asserts cross-cutting invariants that must hold for ANY
build, regardless of input source: build/load round-trip preserves
records exactly, every emitted kind value is in the closed schema set,
positions are well-formed, and `find_symbols` / `find_refs` return
the same data as `all_symbols` / `all_refs` when not filtered.
"""

from __future__ import annotations

import string
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from agent_cli.code_index import build, load_index
from agent_cli.code_index.schema import NAME_KINDS, REF_KINDS

_IDENT_START = string.ascii_letters + "_"
_IDENT_CONT = string.ascii_letters + string.digits + "_"

_PY_KEYWORDS = frozenset(
    {
        "False",
        "None",
        "True",
        "and",
        "as",
        "assert",
        "async",
        "await",
        "break",
        "class",
        "continue",
        "def",
        "del",
        "elif",
        "else",
        "except",
        "finally",
        "for",
        "from",
        "global",
        "if",
        "import",
        "in",
        "is",
        "lambda",
        "nonlocal",
        "not",
        "or",
        "pass",
        "raise",
        "return",
        "try",
        "while",
        "with",
        "yield",
        "match",
        "case",
    }
)

identifier_st = (
    st.tuples(
        st.text(alphabet=_IDENT_START, min_size=1, max_size=1),
        st.text(alphabet=_IDENT_CONT, min_size=0, max_size=20),
    )
    .map(lambda t: t[0] + t[1])
    .filter(lambda s: s not in _PY_KEYWORDS)
)

# A small but realistic Python source: 0..6 top-level defs/classes
# plus a handful of cross-references. Just enough that the resulting
# build produces both Symbol and Ref records.
_py_unit_st = st.one_of(
    identifier_st.map(lambda n: f"def {n}():\n    pass\n"),
    identifier_st.map(lambda n: f"class {n}:\n    pass\n"),
    st.tuples(identifier_st, identifier_st).map(
        lambda nn: f"def {nn[0]}():\n    return {nn[1]}\n"
    ),
)
py_source_st = st.lists(_py_unit_st, min_size=1, max_size=6).map("".join)


def _build_and_load(tmp_path: Path, source: str) -> object:
    (tmp_path / "sample.py").write_text(source)
    out = tmp_path / ".db"
    build(tmp_path, out, defs_path=None, verbose=False, force_full=True)
    return load_index(out)


class TestRoundTripInvariants:
    @settings(
        max_examples=60, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(source=py_source_st)
    def test_all_symbol_kinds_are_in_schema(self, tmp_path, source):
        idx = _build_and_load(tmp_path, source)
        for s in idx.all_symbols():
            assert s["kind"] in NAME_KINDS

    @settings(
        max_examples=60, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(source=py_source_st)
    def test_all_ref_kinds_are_in_schema(self, tmp_path, source):
        idx = _build_and_load(tmp_path, source)
        for r in idx.all_refs():
            assert r["kind"] in REF_KINDS

    @settings(
        max_examples=60, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(source=py_source_st)
    def test_positions_are_well_formed(self, tmp_path, source):
        # 1-indexed line, 0-indexed col, end_line >= line for every
        # Symbol; line/col same for Ref but no end_line on refs.
        idx = _build_and_load(tmp_path, source)
        for s in idx.all_symbols():
            assert s["line"] >= 1
            assert s["col"] >= 0
            assert s["end_line"] >= s["line"]
        for r in idx.all_refs():
            assert r["line"] >= 1
            assert r["col"] >= 0

    @settings(
        max_examples=60, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(source=py_source_st)
    def test_no_filter_find_returns_everything(self, tmp_path, source):
        # find_symbols() with no filters should be equivalent to
        # all_symbols(). Same for find_refs() vs all_refs(). This pins
        # the no-arg query path used by callers that want a full
        # iteration.
        idx = _build_and_load(tmp_path, source)
        all_s = idx.all_symbols()
        found_s = idx.find_symbols()
        assert sorted(s.items() for s in all_s) == sorted(s.items() for s in found_s)
        all_r = idx.all_refs()
        found_r = idx.find_refs()
        assert sorted(r.items() for r in all_r) == sorted(r.items() for r in found_r)

    @settings(
        max_examples=60, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(source=py_source_st)
    def test_n_symbols_matches_iteration(self, tmp_path, source):
        idx = _build_and_load(tmp_path, source)
        assert idx.n_symbols() == len(idx.all_symbols())
        assert idx.n_refs() == len(idx.all_refs())
        # n_definitions == count where is_definition is True
        defs_via_iter = sum(1 for s in idx.all_symbols() if s["is_definition"])
        assert idx.n_definitions() == defs_via_iter

    @settings(
        max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(source=py_source_st)
    def test_reload_index_yields_identical_data(self, tmp_path, source):
        # Build once, reload twice, compare. This pins the SQLite
        # serialization layer — anything Pickle-incompatible or
        # JSON-mangled on write/read would surface here.
        idx1 = _build_and_load(tmp_path, source)
        syms1 = sorted(s.items() for s in idx1.all_symbols())
        refs1 = sorted(r.items() for r in idx1.all_refs())
        # Reopen the same DB.
        idx2 = load_index(tmp_path / ".db")
        syms2 = sorted(s.items() for s in idx2.all_symbols())
        refs2 = sorted(r.items() for r in idx2.all_refs())
        assert syms1 == syms2
        assert refs1 == refs2

    @settings(
        max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(source=py_source_st)
    def test_filter_by_name_is_consistent(self, tmp_path, source):
        # For every distinct name in the index, find_symbols(name=X)
        # returns exactly the symbols whose name == X.
        idx = _build_and_load(tmp_path, source)
        names = {s["name"] for s in idx.all_symbols()}
        for n in names:
            hit = idx.find_symbols(name=n)
            assert all(s["name"] == n for s in hit)
            assert {(s["file"], s["line"]) for s in hit} == {
                (s["file"], s["line"]) for s in idx.all_symbols() if s["name"] == n
            }

    @settings(
        max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(source=py_source_st)
    def test_kind_counts_sum_to_total(self, tmp_path, source):
        idx = _build_and_load(tmp_path, source)
        assert sum(idx.kind_counts().values()) == idx.n_symbols()
        assert sum(idx.ref_kind_counts().values()) == idx.n_refs()


class TestIncrementalNoOpInvariant:
    """A no-op incremental build (same files, same content) MUST produce
    an index identical to the prior build's symbols/refs."""

    @settings(
        max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(source=py_source_st)
    def test_rebuild_with_no_change_yields_same_data(self, tmp_path, source):
        # First build.
        (tmp_path / "sample.py").write_text(source)
        out = tmp_path / ".db"
        build(tmp_path, out, defs_path=None, verbose=False, force_full=True)
        first = load_index(out)
        first_syms = sorted(s.items() for s in first.all_symbols())
        first_refs = sorted(r.items() for r in first.all_refs())

        # Second build, NOT force_full — sha1 matches, every file
        # should be reused from the existing index. Result equality is
        # the invariant.
        build(tmp_path, out, defs_path=None, verbose=False, force_full=False)
        second = load_index(out)
        second_syms = sorted(s.items() for s in second.all_symbols())
        second_refs = sorted(r.items() for r in second.all_refs())

        assert first_syms == second_syms
        assert first_refs == second_refs
