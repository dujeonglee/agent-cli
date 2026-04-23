"""Tests for fuzzy edit matching in tools/edit_file."""

import pytest

from agent_cli.tools.read_file import compute_line_hash
from agent_cli.tools.edit_file import fuzzy_verify_ref, _normalize_for_fuzzy


class TestNormalize:
    def test_tabs_to_spaces(self):
        assert _normalize_for_fuzzy("\thello") == "hello"

    def test_collapse_spaces(self):
        assert _normalize_for_fuzzy("a   b") == "a b"

    def test_smart_quotes(self):
        assert _normalize_for_fuzzy("\u201chello\u201d") == '"hello"'

    def test_em_dash(self):
        assert _normalize_for_fuzzy("a\u2014b") == "a-b"


class TestFuzzyVerifyRef:
    def test_exact_match(self):
        lines = ["def hello():", "    pass"]
        h = compute_line_hash(1, lines[0])
        idx, was_fuzzy = fuzzy_verify_ref(lines, f"1#{h}")
        assert idx == 0
        assert was_fuzzy is False

    def test_fuzzy_match_stale_hash_raises(self):
        """When hash doesn't match, fuzzy raises with re-read guidance."""
        lines = ["def hello():"]
        # Use a wrong hash — should raise with guidance to re-read
        with pytest.raises(RuntimeError, match="Re-read the file"):
            fuzzy_verify_ref(lines, "1#ZZ")

    def test_out_of_range_raises(self):
        lines = ["only one line"]
        with pytest.raises(RuntimeError):
            fuzzy_verify_ref(lines, "5#ZZ")


class TestDuplicateRefDetection:
    """A: Detect ambiguous multi-edit calls where two edits share a
    hashline reference. The first edit's mutation invalidates the
    later edit's ref, currently resulting in a mid-apply RuntimeError
    that's hard to diagnose. Pre-validate and return a specific error
    instructing the model to combine overlapping edits."""

    def _write(self, tmp_path, lines):
        path = tmp_path / "f.c"
        path.write_text("\n".join(lines))
        return path

    def test_same_pos_twice_rejected(self, tmp_path):
        """pcie_scsc traffic_monitor.c reproducer: replace+append at
        the same pos."""
        from agent_cli.tools.edit_file import tool_edit_file

        lines = ["l1", "l2", "l3"]
        path = self._write(tmp_path, lines)
        h = compute_line_hash(2, lines[1])
        args = {
            "path": str(path),
            "edits": [
                {"op": "replace", "pos": f"2#{h}", "lines": []},
                {"op": "append", "pos": f"2#{h}", "lines": ["new"]},
            ],
        }
        result = tool_edit_file(args)
        assert not result.success
        # Error should name the ambiguity pattern and the offending ref.
        assert "Ambiguous" in result.error or "multiple edits" in result.error
        assert f"2#{h}" in result.error
        # File must be untouched.
        assert path.read_text() == "\n".join(lines)

    def test_end_of_one_edit_equals_pos_of_another_rejected(self, tmp_path):
        """Session 1776946589 line 182 pattern: edit 1 replaces range
        [449#JJ..452#SH], edit 2 appends at 452#SH. The shared 452#SH
        ref falls inside edit 1's replaced range."""
        from agent_cli.tools.edit_file import tool_edit_file

        lines = ["a", "b", "c", "d"]
        path = self._write(tmp_path, lines)
        h2 = compute_line_hash(2, lines[1])
        h3 = compute_line_hash(3, lines[2])
        args = {
            "path": str(path),
            "edits": [
                {"op": "replace", "pos": f"2#{h2}", "end": f"3#{h3}", "lines": ["X"]},
                {"op": "append", "pos": f"3#{h3}", "lines": ["Y"]},
            ],
        }
        result = tool_edit_file(args)
        assert not result.success
        assert f"3#{h3}" in result.error
        assert path.read_text() == "\n".join(lines)

    def test_pos_in_one_end_in_another_rejected(self, tmp_path):
        """Symmetric: edit 1 has the ref as its pos, edit 2 as its end."""
        from agent_cli.tools.edit_file import tool_edit_file

        lines = ["a", "b", "c", "d", "e"]
        path = self._write(tmp_path, lines)
        h1 = compute_line_hash(1, lines[0])
        h3 = compute_line_hash(3, lines[2])
        args = {
            "path": str(path),
            "edits": [
                {"op": "replace", "pos": f"3#{h3}", "lines": ["Z"]},
                {"op": "replace", "pos": f"1#{h1}", "end": f"3#{h3}", "lines": ["X"]},
            ],
        }
        result = tool_edit_file(args)
        assert not result.success
        assert f"3#{h3}" in result.error
        assert path.read_text() == "\n".join(lines)

    def test_single_edit_pos_equals_end_allowed(self, tmp_path):
        """pos==end WITHIN a single edit is a degenerate single-line
        range — not ambiguity. Must still work."""
        from agent_cli.tools.edit_file import tool_edit_file

        lines = ["l1", "l2", "l3"]
        path = self._write(tmp_path, lines)
        h = compute_line_hash(2, lines[1])
        args = {
            "path": str(path),
            "edits": [
                {"op": "replace", "pos": f"2#{h}", "end": f"2#{h}", "lines": ["X"]},
            ],
        }
        result = tool_edit_file(args)
        assert result.success, result.error
        assert path.read_text().splitlines() == ["l1", "X", "l3"]

    def test_two_non_overlapping_edits_succeed(self, tmp_path):
        """Refs at different positions with no duplicates go through
        the normal bottom-up apply path."""
        from agent_cli.tools.edit_file import tool_edit_file

        lines = ["l1", "l2", "l3", "l4"]
        path = self._write(tmp_path, lines)
        h1 = compute_line_hash(1, lines[0])
        h4 = compute_line_hash(4, lines[3])
        args = {
            "path": str(path),
            "edits": [
                {"op": "replace", "pos": f"1#{h1}", "lines": ["A"]},
                {"op": "replace", "pos": f"4#{h4}", "lines": ["Z"]},
            ],
        }
        result = tool_edit_file(args)
        assert result.success, result.error
        assert path.read_text().splitlines() == ["A", "l2", "l3", "Z"]

    def test_three_edits_with_one_duplicate_rejected(self, tmp_path):
        """Mixed case: two unique refs plus one duplicate → still
        rejected, error names the offending ref (not the unique
        ones)."""
        from agent_cli.tools.edit_file import tool_edit_file

        lines = ["l1", "l2", "l3", "l4", "l5"]
        path = self._write(tmp_path, lines)
        h1 = compute_line_hash(1, lines[0])
        h3 = compute_line_hash(3, lines[2])
        args = {
            "path": str(path),
            "edits": [
                {"op": "replace", "pos": f"1#{h1}", "lines": ["A"]},
                {"op": "replace", "pos": f"3#{h3}", "lines": ["C"]},
                {"op": "append", "pos": f"3#{h3}", "lines": ["C+"]},
            ],
        }
        result = tool_edit_file(args)
        assert not result.success
        assert f"3#{h3}" in result.error
        assert f"1#{h1}" not in result.error  # unique ref not flagged


class TestHashMismatchMessage:
    """C: fuzzy_verify_ref's error message must cover both scenarios:
    external modification (re-read needed) AND same-call earlier-edit
    invalidation (combine edits)."""

    def test_message_mentions_reread(self):
        """Pre-validate path — suggests re-reading."""
        with pytest.raises(RuntimeError, match="Re-read"):
            fuzzy_verify_ref(["hello"], "1#ZZ")

    def test_message_mentions_multi_edit_context(self):
        """Apply-time path context — suggests multi-edit interaction."""
        with pytest.raises(RuntimeError, match="multi-edit"):
            fuzzy_verify_ref(["hello"], "1#ZZ")
