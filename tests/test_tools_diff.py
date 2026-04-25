"""Tests for the unified-diff formatter shared by write_file/edit_file.

`format_diff` produces a Rich-marked unified diff string. The renderer
colors `+` lines green and `-` lines red, hunk headers cyan. Truncation
kicks in past `MAX_DIFF_LINES` to keep the LLM observation bounded
when an edit replaces a large file wholesale."""

from __future__ import annotations

from agent_cli.tools._diff import MAX_DIFF_LINES, format_diff


class TestFormatDiff:
    def test_empty_when_unchanged(self):
        assert format_diff("hello", "hello", "f.txt") == ""

    def test_empty_for_both_empty(self):
        assert format_diff("", "", "new.txt") == ""

    def test_added_lines_marked_green(self):
        out = format_diff("a\nb\n", "a\nb\nc\n", "f.txt")
        assert "[green]+c[/green]" in out

    def test_removed_lines_marked_red(self):
        out = format_diff("a\nb\nc\n", "a\nc\n", "f.txt")
        assert "[red]-b[/red]" in out

    def test_hunk_header_marked_cyan(self):
        out = format_diff("a\nb\n", "a\nB\n", "f.txt")
        assert "[cyan]@@" in out

    def test_filename_in_header(self):
        out = format_diff("a\n", "b\n", "src/main.py")
        assert "a/src/main.py" in out
        assert "b/src/main.py" in out

    def test_creating_a_file_shows_all_added(self):
        """Empty old → diff has no `-` lines, every new line is `+`."""
        out = format_diff("", "first\nsecond\n", "new.txt")
        assert "[green]+first[/green]" in out
        assert "[green]+second[/green]" in out
        assert "[red]-" not in out

    def test_truncation_when_diff_exceeds_max(self):
        """A wholesale rewrite of a large file should not bloat the
        observation. Past `MAX_DIFF_LINES` the tail is replaced with
        a single summary line stating how many lines were elided."""
        old = "\n".join(f"old{i}" for i in range(200)) + "\n"
        new = "\n".join(f"new{i}" for i in range(200)) + "\n"
        out = format_diff(old, new, "f.txt")

        rendered_lines = out.splitlines()
        # The truncation summary line is appended on top of the visible
        # cap, so total lines is MAX_DIFF_LINES + 1.
        assert len(rendered_lines) <= MAX_DIFF_LINES + 1
        assert "diff truncated" in rendered_lines[-1]

    def test_rich_markup_in_source_is_escaped(self):
        """If the file contains text that looks like Rich markup (e.g.
        `[bold]`), the formatter must escape it so the renderer doesn't
        consume it as styling."""
        old = "plain text"
        new = "with [bold]markup[/bold]"
        out = format_diff(old, new, "f.txt")
        # `[` should be escaped as `\[` so Rich treats it as a literal.
        assert "\\[bold]" in out
        # And the unescaped form must NOT appear as a paint directive.
        assert "[bold]markup[/bold]" not in out

    def test_filename_header_bold(self):
        out = format_diff("a\n", "b\n", "f.txt")
        assert "[bold]" in out

    def test_no_trailing_newline_preserved(self):
        """unified_diff keeps trailing newlines on its source lines.
        We strip them so the final string doesn't have empty `[red]-[/red]`
        artifacts from the trailing context — verify lines aren't blank."""
        out = format_diff("a\nb\nc", "a\nB\nc", "f.txt")
        for line in out.splitlines():
            # No purely-empty styled markers (e.g. `[red]-[/red]`).
            assert line.strip()
