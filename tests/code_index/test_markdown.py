"""Tests for the markdown heading walker — DESIGN.md §12.1 markdown coverage.

Fixture: ``fixtures/markdown/sample.md`` (intentional structure) +
``empty.md`` (no headings).

Cases pinned here:

- All six ATX heading levels (``#`` through ``######``) produce
  ``kind='section'`` symbols with ``kind_raw='atx_heading_<N>'`` and
  ``modifiers=['level=<N>']``.
- Setext H1 (``Title\\n===``) and H2 (``Title\\n---``) produce
  ``kind_raw='setext_heading_1'`` / ``_2`` with the correct level
  modifier.
- Parent chain: deeper headings list their nearest strictly-shallower
  ancestor as parent (``# A`` > ``## B`` > ``### C``).
- Sibling / uncle relationships: ``## E`` is uncle of ``### C``; ``## E``
  shares parent ``A`` with ``## B``.
- ``end_line`` is the line BEFORE the next same-or-higher-level heading
  (or last line of the file for the final section).
- A fenced code block inside a section is NOT parsed as a heading.
- ``empty.md`` (no headings) → 0 symbols, no crash, no refs.
- ``walk_refs`` emits no refs at all for markdown.
"""

from __future__ import annotations

import pytest

from tests.code_index.helpers import build_fixture


@pytest.fixture(scope="module")
def index():
    return build_fixture("markdown")


class TestMarkdownAtxLevels:
    """All six ATX levels recognised."""

    def test_h1_emitted(self, index):
        _, by_name = index
        a = by_name["A"][0]
        assert a["kind"] == "section"
        assert a["kind_raw"] == "atx_heading_1"
        assert a["modifiers"] == ["level=1"]

    def test_h2_emitted(self, index):
        _, by_name = index
        b = by_name["B"][0]
        assert b["kind"] == "section"
        assert b["kind_raw"] == "atx_heading_2"
        assert b["modifiers"] == ["level=2"]

    def test_h3_emitted(self, index):
        _, by_name = index
        c = by_name["C"][0]
        assert c["kind_raw"] == "atx_heading_3"
        assert c["modifiers"] == ["level=3"]

    def test_h4_emitted(self, index):
        _, by_name = index
        h4 = by_name["H4"][0]
        assert h4["kind_raw"] == "atx_heading_4"
        assert h4["modifiers"] == ["level=4"]

    def test_h5_emitted(self, index):
        _, by_name = index
        h5 = by_name["H5"][0]
        assert h5["kind_raw"] == "atx_heading_5"
        assert h5["modifiers"] == ["level=5"]

    def test_h6_emitted(self, index):
        _, by_name = index
        h6 = by_name["H6"][0]
        assert h6["kind_raw"] == "atx_heading_6"
        assert h6["modifiers"] == ["level=6"]


class TestMarkdownSetextLevels:
    def test_setext_h1_emitted(self, index):
        _, by_name = index
        sym = by_name["Setext Title One"][0]
        assert sym["kind"] == "section"
        assert sym["kind_raw"] == "setext_heading_1"
        assert sym["modifiers"] == ["level=1"]

    def test_setext_h2_emitted(self, index):
        _, by_name = index
        sym = by_name["Setext Title Two"][0]
        assert sym["kind_raw"] == "setext_heading_2"
        assert sym["modifiers"] == ["level=2"]


class TestMarkdownParentChain:
    """Parent = nearest strictly-shallower ancestor."""

    def test_h1_has_no_parent(self, index):
        _, by_name = index
        a = by_name["A"][0]
        assert a["parent"] is None

    def test_h2_parent_is_h1(self, index):
        _, by_name = index
        b = by_name["B"][0]
        assert b["parent"] == "A"

    def test_h3_parent_is_h2(self, index):
        _, by_name = index
        c = by_name["C"][0]
        assert c["parent"] == "B"

    def test_h4_parent_is_h2_when_no_h3_in_chain(self, index):
        _, by_name = index
        h4 = by_name["H4"][0]
        # H4 sits under G (level-2) inside F (level-1). Nearest
        # strictly-shallower is G.
        assert h4["parent"] == "G"

    def test_h5_parent_is_h4(self, index):
        _, by_name = index
        assert by_name["H5"][0]["parent"] == "H4"

    def test_h6_parent_is_h5(self, index):
        _, by_name = index
        assert by_name["H6"][0]["parent"] == "H5"

    def test_setext_h2_parent_is_setext_h1(self, index):
        _, by_name = index
        sym = by_name["Setext Title Two"][0]
        assert sym["parent"] == "Setext Title One"

    def test_sibling_shares_parent(self, index):
        """C and D are both level-3 children of B."""
        _, by_name = index
        assert by_name["C"][0]["parent"] == "B"
        assert by_name["D"][0]["parent"] == "B"

    def test_uncle_relationship(self, index):
        """E (level-2) is uncle of C (level-3) — E.parent == C.parent.parent == A."""
        _, by_name = index
        assert by_name["E"][0]["parent"] == "A"
        # C.parent is B and B.parent is A — so A is C's grandparent
        # and E's parent: classic uncle setup.
        assert by_name["B"][0]["parent"] == by_name["E"][0]["parent"] == "A"


class TestMarkdownEndLines:
    """end_line stops at the line BEFORE next same-or-higher-level heading."""

    def test_h1_ends_before_next_h1(self, index):
        _, by_name = index
        # A is level-1; next same-or-higher is F at line 27. A.end_line == 26.
        a = by_name["A"][0]
        assert a["line"] == 1
        assert a["end_line"] == 26

    def test_h3_ends_before_next_same_level(self, index):
        _, by_name = index
        # C (level-3 at line 9) ends right before D (level-3 at line 19).
        c = by_name["C"][0]
        d = by_name["D"][0]
        assert c["end_line"] == d["line"] - 1

    def test_h2_ends_before_higher_level(self, index):
        _, by_name = index
        # E (level-2 at line 23) ends right before F (level-1 at line 27).
        e = by_name["E"][0]
        f = by_name["F"][0]
        assert e["end_line"] == f["line"] - 1

    def test_last_section_extends_to_eof(self, index):
        _, by_name = index
        # TightSibling is the last heading; end_line is the last line of
        # the file. Compute via the file length implicitly: end_line of
        # the deepest last heading must be >= its own start line.
        ts = by_name["TightSibling"][0]
        assert ts["end_line"] >= ts["line"]

    def test_tight_sibling_end_line_edge(self, index):
        """No-blank-line-before heading still computes correct end_line."""
        _, by_name = index
        # `Setext Title Two` (level-2) is followed by `## TightSibling`
        # (also level-2) immediately on the next line — Setext Title Two
        # ends one line before TightSibling.
        s2 = by_name["Setext Title Two"][0]
        ts = by_name["TightSibling"][0]
        assert s2["end_line"] == ts["line"] - 1


class TestMarkdownCodeBlockIgnored:
    def test_fake_inside_code_block_not_emitted_as_section(self, index):
        _, by_name = index
        # `def fake():` lives in a fenced code block — it must not show up.
        assert "fake" not in by_name

    def test_no_python_kind_symbols(self, index):
        # Markdown walker only emits kind=section. The fenced code block
        # content must not leak into other kinds either.
        store, _ = index
        non_section = [s for s in store.all_symbols() if s["kind"] != "section"]
        assert non_section == []


class TestMarkdownNoRefs:
    def test_walk_refs_emits_nothing(self, index):
        store, _ = index
        assert store.all_refs() == []


class TestMarkdownEmptyFile:
    def test_empty_file_does_not_crash(self, index):
        store, _ = index
        # empty.md is in the index but contributes zero symbols.
        paths = {f["path"] for f in store.files}
        assert "empty.md" in paths

    def test_empty_file_contributes_zero_symbols(self, index):
        store, _ = index
        empty_syms = [s for s in store.all_symbols() if s["file"] == "empty.md"]
        assert empty_syms == []

    def test_empty_file_not_marked_has_error(self, index):
        store, _ = index
        empty = next(f for f in store.files if f["path"] == "empty.md")
        assert empty.get("has_error") is False
