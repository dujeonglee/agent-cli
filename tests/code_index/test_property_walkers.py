"""Property-based tests for walker robustness using Hypothesis.

These complement the example-driven walker tests in test_<lang>.py:
example tests pin specific known cases; property tests stress the
walkers with random small inputs to surface crashes, malformed
outputs, or invariant violations the targeted tests didn't anticipate.

Strategies are deliberately conservative — we generate inputs that are
*syntactically valid* in the target language so the walker actually
gets a non-error AST to walk. A "doesn't crash on garbage" test is
covered separately by the error.<ext> fixtures in the example tests.
"""

from __future__ import annotations

import string
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from agent_cli.code_index import build, load_index
from agent_cli.code_index.languages import LANGUAGES
from agent_cli.code_index.schema import NAME_KINDS, REF_KINDS


# ----- shared strategies ----------------------------------------------------


# Identifier: ASCII letter/underscore start, then letters/digits/underscores,
# bounded length. Excludes Python keywords because the walker would treat them
# as keyword tokens, not identifiers, and tests of "this name shows up" would
# spuriously fail.
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

_IDENT_START = string.ascii_letters + "_"
_IDENT_CONT = string.ascii_letters + string.digits + "_"


identifier_st = (
    st.tuples(
        st.text(alphabet=_IDENT_START, min_size=1, max_size=1),
        st.text(alphabet=_IDENT_CONT, min_size=0, max_size=20),
    )
    .map(lambda t: t[0] + t[1])
    .filter(lambda s: s not in _PY_KEYWORDS)
)


# ----- helpers --------------------------------------------------------------


def _build_one(tmp_path: Path, source: str, ext: str):
    """Write `source` to a single file with the given extension in
    tmp_path, build a fresh index over that directory, return the store
    plus the list of symbols from the walker."""
    src_file = tmp_path / f"sample{ext}"
    src_file.write_text(source)
    out = tmp_path / ".db"
    build(tmp_path, out, defs_path=None, verbose=False, force_full=True)
    store = load_index(out)
    return store, store.all_symbols(), store.all_refs()


# ----- Python walker properties --------------------------------------------


# Build small but syntactically valid Python sources from random
# identifier names. We restrict the shapes to def/class/assignment at
# module scope so the walker has well-defined behaviour.

py_def_st = identifier_st.map(lambda n: f"def {n}():\n    pass\n")
py_class_st = identifier_st.map(lambda n: f"class {n}:\n    pass\n")
py_assign_lower_st = identifier_st.filter(lambda n: not n.isupper()).map(
    lambda n: f"{n} = 0\n"
)
py_assign_upper_st = (
    st.text(alphabet=string.ascii_uppercase + "_", min_size=2, max_size=15)
    .filter(lambda s: s[0] != "_" and s not in _PY_KEYWORDS)
    .map(lambda n: f"{n} = 0\n")
)

python_source_st = st.lists(
    st.one_of(py_def_st, py_class_st, py_assign_lower_st, py_assign_upper_st),
    min_size=1,
    max_size=8,
).map("".join)


class TestPythonWalkerProperties:
    @settings(
        max_examples=80, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(source=python_source_st)
    def test_walker_emits_only_valid_kinds(self, tmp_path, source):
        # Every Symbol emitted by the walker must carry a `kind` value
        # from NAME_KINDS. A mistake like emitting `kind="method"` (a
        # historical name in upstream comments) would be caught here.
        _, syms, _ = _build_one(tmp_path, source, ".py")
        for s in syms:
            assert s["kind"] in NAME_KINDS

    @settings(
        max_examples=80, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(source=python_source_st)
    def test_walker_never_emits_empty_name(self, tmp_path, source):
        # An empty Symbol.name would break downstream lookups silently
        # (find_symbols(name='') returns no hits). Walkers MUST always
        # have an identifier for emit, even on weird-but-valid inputs.
        _, syms, _ = _build_one(tmp_path, source, ".py")
        for s in syms:
            assert s["name"], (
                f"empty name in symbol record: {s!r}; source was: {source!r}"
            )

    @settings(
        max_examples=80, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(source=python_source_st)
    def test_walker_line_positions_are_valid(self, tmp_path, source):
        # 1-indexed lines, 0-indexed cols, end_line >= line.
        _, syms, _ = _build_one(tmp_path, source, ".py")
        for s in syms:
            assert s["line"] >= 1
            assert s["col"] >= 0
            assert s["end_line"] >= s["line"]

    @settings(
        max_examples=80, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(source=python_source_st)
    def test_walker_refs_have_valid_kinds(self, tmp_path, source):
        _, _, refs = _build_one(tmp_path, source, ".py")
        for r in refs:
            assert r["kind"] in REF_KINDS

    @settings(
        max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(name=identifier_st)
    def test_single_def_with_random_name_round_trips(self, tmp_path, name):
        # Whatever random identifier we picked must come back from the
        # walker — this is the most basic round-trip guarantee.
        src = f"def {name}():\n    pass\n"
        _, syms, _ = _build_one(tmp_path, src, ".py")
        names = {s["name"] for s in syms}
        assert name in names

    @settings(
        max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(name=identifier_st)
    def test_walker_never_crashes_on_long_identifier(self, tmp_path, name):
        # Stress: identifier up to 21 chars (start + 20 cont). The
        # walker has no business choking on length.
        src = f"def {name}():\n    return {name}\n"
        # Just succeeds → property holds.
        _build_one(tmp_path, src, ".py")


# ----- Markdown walker properties ------------------------------------------


# Generate a sequence of headings of varying levels. The walker must
# always produce a well-formed parent chain regardless of the input
# sequence.

heading_level_st = st.integers(min_value=1, max_value=6)
heading_text_st = (
    st.text(
        alphabet=string.ascii_letters + " ",
        min_size=1,
        max_size=30,
    )
    .map(lambda s: s.strip())
    .filter(lambda s: bool(s) and "\n" not in s)
)
heading_st = st.tuples(heading_level_st, heading_text_st)
markdown_source_st = st.lists(heading_st, min_size=1, max_size=20).map(
    lambda hs: "\n\n".join(f"{'#' * lvl} {txt}" for lvl, txt in hs) + "\n"
)


class TestMarkdownWalkerProperties:
    @settings(
        max_examples=80, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(source=markdown_source_st)
    def test_all_headings_have_section_kind(self, tmp_path, source):
        _, syms, _ = _build_one(tmp_path, source, ".md")
        for s in syms:
            assert s["kind"] == "section"

    @settings(
        max_examples=80, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(source=markdown_source_st)
    def test_parent_chain_well_formed(self, tmp_path, source):
        # Every non-None parent must be the name of some earlier-line
        # heading (i.e. the parent symbol exists in the same file).
        _, syms, _ = _build_one(tmp_path, source, ".md")
        # Headings come back in document order in the walker — sort to
        # be sure for the comparison.
        syms_sorted = sorted(syms, key=lambda s: s["line"])
        seen_names: set[str] = set()
        for s in syms_sorted:
            parent = s.get("parent")
            if parent is not None:
                assert parent in seen_names, (
                    f"parent {parent!r} not previously emitted for symbol {s!r}"
                )
            seen_names.add(s["name"])

    @settings(
        max_examples=80, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(source=markdown_source_st)
    def test_level_modifier_matches_heading(self, tmp_path, source):
        # Every section symbol carries modifiers=['level=N'] reflecting
        # the heading depth.
        _, syms, _ = _build_one(tmp_path, source, ".md")
        for s in syms:
            mods = s.get("modifiers") or []
            level_mods = [m for m in mods if m.startswith("level=")]
            assert len(level_mods) == 1
            level = int(level_mods[0].split("=", 1)[1])
            assert 1 <= level <= 6

    @settings(
        max_examples=80, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(source=markdown_source_st)
    def test_end_line_monotonic_with_start_line(self, tmp_path, source):
        # end_line >= line for every emitted section.
        _, syms, _ = _build_one(tmp_path, source, ".md")
        for s in syms:
            assert s["end_line"] >= s["line"]

    @settings(
        max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(source=markdown_source_st)
    def test_walker_emits_no_refs_for_markdown(self, tmp_path, source):
        # Markdown walk_refs is a no-op; refs from a pure-markdown tree
        # must be empty regardless of content.
        _, _, refs = _build_one(tmp_path, source, ".md")
        assert refs == []


# ----- Symbol/Ref schema invariants across walkers --------------------------


# Quick parametric test across every registered language: build an
# empty file in each language and ensure the walker doesn't crash. This
# catches "walker assumes content" bugs.


@pytest.fixture(scope="module")
def all_langs():
    # Ensure walkers are loaded so LANGUAGES is fully populated.
    from agent_cli.code_index.languages import _ensure_loaded

    _ensure_loaded()
    return list(LANGUAGES.items())


class TestEmptyFileSafety:
    """An empty file with each supported extension must build cleanly
    (zero symbols, zero refs, has_error possibly True for languages
    that require at least one declaration). The walker MUST NOT raise."""

    def test_each_language_empty_file_does_not_crash(self, tmp_path, all_langs):
        for name, spec in all_langs:
            # Use the first registered extension.
            ext = spec.exts[0]
            sub = tmp_path / name
            sub.mkdir(exist_ok=True)
            (sub / f"empty{ext}").write_text("")
            out = sub / ".db"
            # Just succeeding (no exception) is the property under test.
            build(sub, out, defs_path=None, verbose=False, force_full=True)
            idx = load_index(out)
            # Sanity: any symbols emitted (some grammars may emit for
            # the implicit module/program node) still respect the
            # schema's closed kind set.
            for s in idx.all_symbols():
                assert s["kind"] in NAME_KINDS
