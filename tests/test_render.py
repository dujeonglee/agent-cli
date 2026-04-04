"""Tests for render module — pluggable renderer system."""

from __future__ import annotations

from io import StringIO

from rich.console import Console

from agent_cli.render import render_step, set_renderer, get_renderer
from agent_cli.render.minimal import MinimalRenderer


def _capture(fn) -> str:
    """Capture rendered output as plain text."""
    buf = StringIO()
    test_console = Console(file=buf, force_terminal=True, width=120)
    old = get_renderer()
    set_renderer(MinimalRenderer(test_console))
    try:
        fn()
    finally:
        set_renderer(old)
    return buf.getvalue()


class TestObservationCompact:
    def test_success_shows_checkmark(self):
        out = _capture(
            lambda: render_step("observation", "STATUS: success\nRESULT:\nok", 1)
        )
        assert "✓" in out
        assert "success" in out

    def test_error_shows_x_mark(self):
        out = _capture(
            lambda: render_step(
                "observation", "STATUS: error\nERROR: file not found", 1
            )
        )
        assert "✗" in out
        assert "error" in out

    def test_error_includes_detail(self):
        out = _capture(
            lambda: render_step(
                "observation", "STATUS: error\nERROR: permission denied", 1
            )
        )
        assert "permission denied" in out

    def test_tool_name_displayed(self):
        out = _capture(
            lambda: render_step(
                "observation", "STATUS: success\nRESULT:\nok", 1, tool_name="read_file"
            )
        )
        assert "read_file" in out

    def test_no_tool_name(self):
        out = _capture(
            lambda: render_step("observation", "STATUS: success\nRESULT:\nok", 1)
        )
        assert "✓" in out
        assert "success" in out

    def test_unknown_status(self):
        out = _capture(lambda: render_step("observation", "some unexpected output", 1))
        assert "●" in out
        assert "done" in out

    def test_compact_no_full_result(self):
        long_result = "x" * 500
        out = _capture(
            lambda: render_step(
                "observation", f"STATUS: success\nRESULT:\n{long_result}", 1
            )
        )
        assert long_result not in out

    def test_no_box_characters(self):
        out = _capture(
            lambda: render_step(
                "observation",
                "STATUS: success\nRESULT:\ndata",
                3,
                tool_name="shell",
            )
        )
        assert "✓" in out
        assert "shell" in out
        assert "╭" not in out


class TestThoughtRendering:
    def test_thought_icon(self):
        out = _capture(lambda: render_step("thought", "I need to think...", 1))
        assert "💭" in out
        assert "I need to think" in out

    def test_multiline_thought(self):
        out = _capture(lambda: render_step("thought", "Line 1\nLine 2\nLine 3", 1))
        assert "Line 1" in out
        assert "Line 2" in out
        assert "Line 3" in out


class TestActionRendering:
    def test_action_with_tool(self):
        out = _capture(
            lambda: render_step(
                "action", "", 1, tool_name="read_file", tool_input="path.py"
            )
        )
        assert "⚡" in out
        assert "read_file" in out
        assert "path.py" in out


class TestFinalRendering:
    def test_final_icon(self):
        out = _capture(lambda: render_step("final", "All done!", 1))
        assert "✅" in out
        assert "All done!" in out


class TestRendererSwap:
    def test_set_and_get(self):
        old = get_renderer()
        buf = StringIO()
        new = MinimalRenderer(Console(file=buf))
        set_renderer(new)
        assert get_renderer() is new
        set_renderer(old)
