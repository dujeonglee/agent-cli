"""Tests for the unified-diff formatter shared by write_file/edit_file.

``format_diff`` produces a **plain** standard unified diff (git-diff
text shape, no colour markup or line-number gutter) so the LLM
observation stays clean. Colour is the renderer's job — see
``MinimalRenderer._colorize_diff_line`` (CLI) and ``colorizeDiffBody``
in app.js (web). Truncation kicks in past ``MAX_DIFF_LINES`` to keep the
observation bounded when an edit replaces a large file wholesale."""

from __future__ import annotations

from agent_cli.tools._diff import (
    DIFF_TRUNCATION_PREFIX,
    MAX_DIFF_LINES,
    format_diff,
)


class TestFormatDiff:
    def test_empty_when_unchanged(self):
        assert format_diff("hello", "hello", "f.txt") == ""

    def test_empty_for_both_empty(self):
        assert format_diff("", "", "new.txt") == ""

    def test_added_line_is_plain(self):
        out = format_diff("a\nb\n", "a\nb\nc\n", "f.txt")
        assert "+c" in out
        assert "[green]" not in out  # no Rich markup

    def test_removed_line_is_plain(self):
        out = format_diff("a\nb\nc\n", "a\nc\n", "f.txt")
        assert "-b" in out
        assert "[red]" not in out

    def test_hunk_header_plain(self):
        out = format_diff("a\nb\n", "a\nB\n", "f.txt")
        assert any(line.startswith("@@") for line in out.splitlines())
        assert "[cyan]" not in out

    def test_standard_unified_diff_shape(self):
        out = format_diff("a\nb\n", "a\nB\n", "f.txt")
        lines = out.splitlines()
        assert lines[0].startswith("--- a/f.txt")
        assert lines[1].startswith("+++ b/f.txt")
        assert any(line.startswith("@@") for line in lines)
        # No gutter: a context line starts with a single leading space,
        # not a "   N    N  " number column.
        assert any(line.startswith(" ") for line in lines)

    def test_creating_a_file_shows_all_added(self):
        """Empty old → only the ``---`` header starts with ``-``; every
        content line is ``+``."""
        out = format_diff("", "first\nsecond\n", "new.txt")
        assert "+first" in out
        assert "+second" in out
        body = [ln for ln in out.splitlines() if not ln.startswith("---")]
        assert not any(ln.startswith("-") for ln in body)

    def test_truncation_when_diff_exceeds_max(self):
        old = "\n".join(f"old{i}" for i in range(200)) + "\n"
        new = "\n".join(f"new{i}" for i in range(200)) + "\n"
        out = format_diff(old, new, "f.txt")
        lines = out.splitlines()
        assert len(lines) <= MAX_DIFF_LINES + 1
        assert lines[-1].startswith(DIFF_TRUNCATION_PREFIX)

    def test_source_markup_left_literal(self):
        """Plain diff: source text that looks like Rich markup is kept
        verbatim (no ``\\[`` escaping). The renderer escapes at paint
        time; the LLM observation sees the literal source."""
        out = format_diff("plain", "with [bold]markup[/bold]", "f.txt")
        assert "+with [bold]markup[/bold]" in out
        assert "\\[" not in out

    def test_no_blank_styled_artifacts(self):
        out = format_diff("a\nb\nc", "a\nB\nc", "f.txt")
        for line in out.splitlines():
            assert line.strip()  # no empty lines mid-diff
