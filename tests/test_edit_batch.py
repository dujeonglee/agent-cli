"""Batch edit application — several edits to ONE file applied against a single
original read (resolve-all → overlap-reject → bottom-up apply → one write).

Why this exists: edit_file applies one op at a time, reading+writing the file
per op, so a later op's hashline ref (from the model's original read) no longer
matches the already-mutated file → "Hash mismatch". ``apply_edits_batch``
resolves EVERY ref against the same original content, so refs stay valid; it
sorts bottom-up so line indices don't drift, and is all-or-nothing (any overlap
/ bad ref → nothing is written, the model re-emits the whole group). This is
the bottom-up-against-original pattern (cf. NousResearch hashline, the 5-edit-
strategies benchmark).

``apply_edits_batch`` is a PURE function: (path, edits) -> ToolResult. No loop /
ctx / renderer coupling — the loop just routes a run of consecutive same-path
edit_file ops to it.
"""

from __future__ import annotations

from agent_cli.tools.read_file import compute_line_hash


def _ref(n: int, line: str) -> str:
    """Build a valid hashline ref ``N#HH`` for a 1-based line + its content."""
    return f"{n}#{compute_line_hash(n, line)}"


def _write(tmp_path, name, lines):
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n")
    return p


class TestApplyEditsBatch:
    # ── happy path: several edits, one read, bottom-up ──────────────────

    def test_two_replaces_same_file(self, tmp_path):
        from agent_cli.tools.edit_file import apply_edits_batch

        lines = ["a", "b", "c", "d", "e"]  # + trailing "" from the \n
        p = _write(tmp_path, "f.txt", lines)
        edits = [
            {"op": "replace", "pos": _ref(2, "b"), "lines": ["B"]},
            {"op": "replace", "pos": _ref(4, "d"), "lines": ["D"]},
        ]
        result = apply_edits_batch(str(p), edits)
        assert result.success
        assert p.read_text().splitlines() == ["a", "B", "c", "D", "e"]

    def test_insert_then_replace_lower_line_unaffected(self, tmp_path):
        # An insert near the top must NOT break a replace ref lower down:
        # both refs are against the ORIGINAL, applied bottom-up.
        from agent_cli.tools.edit_file import apply_edits_batch

        lines = ["a", "b", "c", "d", "e"]
        p = _write(tmp_path, "f.txt", lines)
        edits = [
            {"op": "append", "pos": _ref(1, "a"), "lines": ["a2"]},  # after line 1
            {"op": "replace", "pos": _ref(5, "e"), "lines": ["E"]},  # line 5 ref
        ]
        result = apply_edits_batch(str(p), edits)
        assert result.success
        assert p.read_text().splitlines() == ["a", "a2", "b", "c", "d", "E"]

    def test_delete_range_and_replace(self, tmp_path):
        from agent_cli.tools.edit_file import apply_edits_batch

        lines = ["a", "b", "c", "d", "e"]
        p = _write(tmp_path, "f.txt", lines)
        edits = [
            {"op": "delete", "pos": _ref(2, "b"), "end": _ref(3, "c")},  # del b,c
            {"op": "replace", "pos": _ref(5, "e"), "lines": ["E"]},
        ]
        result = apply_edits_batch(str(p), edits)
        assert result.success
        assert p.read_text().splitlines() == ["a", "d", "E"]

    def test_single_edit_still_works(self, tmp_path):
        # A 1-edit batch behaves like a normal edit.
        from agent_cli.tools.edit_file import apply_edits_batch

        p = _write(tmp_path, "f.txt", ["a", "b", "c"])
        result = apply_edits_batch(
            str(p), [{"op": "replace", "pos": _ref(2, "b"), "lines": ["B"]}]
        )
        assert result.success
        assert p.read_text().splitlines() == ["a", "B", "c"]

    # ── all-or-nothing: any failure → file untouched ────────────────────

    def test_overlap_rejected_nothing_written(self, tmp_path):
        from agent_cli.tools.edit_file import apply_edits_batch

        lines = ["a", "b", "c", "d", "e"]
        p = _write(tmp_path, "f.txt", lines)
        before = p.read_text()
        edits = [
            {"op": "replace", "pos": _ref(2, "b"), "end": _ref(4, "d"), "lines": ["X"]},
            {"op": "replace", "pos": _ref(3, "c"), "lines": ["Y"]},  # inside 2..4
        ]
        result = apply_edits_batch(str(p), edits)
        assert not result.success
        assert "overlap" in result.error.lower()
        assert p.read_text() == before  # untouched

    def test_hash_mismatch_rejects_whole_batch(self, tmp_path):
        from agent_cli.tools.edit_file import apply_edits_batch

        lines = ["a", "b", "c"]
        p = _write(tmp_path, "f.txt", lines)
        before = p.read_text()
        edits = [
            {"op": "replace", "pos": _ref(1, "a"), "lines": ["A"]},  # valid
            {"op": "replace", "pos": "2#ZZ", "lines": ["B"]},  # bogus hash
        ]
        result = apply_edits_batch(str(p), edits)
        assert not result.success
        # even though edit #1 was valid, nothing is written (all-or-nothing)
        assert p.read_text() == before

    def test_adjacent_ranges_allowed(self, tmp_path):
        # Adjacent (touching but not overlapping) ranges are fine.
        from agent_cli.tools.edit_file import apply_edits_batch

        lines = ["a", "b", "c", "d", "e"]
        p = _write(tmp_path, "f.txt", lines)
        edits = [
            {"op": "replace", "pos": _ref(2, "b"), "end": _ref(3, "c"), "lines": ["X"]},
            {"op": "replace", "pos": _ref(4, "d"), "lines": ["Y"]},  # adjacent, ok
        ]
        result = apply_edits_batch(str(p), edits)
        assert result.success
        assert p.read_text().splitlines() == ["a", "X", "Y", "e"]
