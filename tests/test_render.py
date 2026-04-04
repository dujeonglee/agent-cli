"""Tests for render module — observation compact rendering."""

from __future__ import annotations

from io import StringIO

from rich.console import Console

from agent_cli.render import _render_observation_compact


def _capture_render(content: str, tool_name: str | None = None) -> str:
    """Capture rendered output as plain text."""
    buf = StringIO()
    import agent_cli.render as render_mod

    original = render_mod.console
    render_mod.console = Console(file=buf, force_terminal=True, width=120)
    try:
        _render_observation_compact(content, iteration=1, tool_name=tool_name)
    finally:
        render_mod.console = original
    return buf.getvalue()


class TestObservationCompact:
    def test_success_shows_checkmark(self):
        out = _capture_render("STATUS: success\nRESULT:\nfile contents here")
        assert "✓" in out
        assert "success" in out

    def test_error_shows_x_mark(self):
        out = _capture_render("STATUS: error\nERROR: file not found\nHINT: check path")
        assert "✗" in out
        assert "error" in out

    def test_error_includes_error_detail(self):
        out = _capture_render("STATUS: error\nERROR: permission denied")
        assert "permission denied" in out

    def test_tool_name_displayed(self):
        out = _capture_render("STATUS: success\nRESULT:\nok", tool_name="read_file")
        assert "read_file" in out

    def test_no_tool_name(self):
        out = _capture_render("STATUS: success\nRESULT:\nok")
        assert "✓" in out
        assert "success" in out

    def test_success_status_displayed(self):
        out = _capture_render("STATUS: success\nRESULT:\nok")
        assert "success" in out

    def test_unknown_status_format(self):
        out = _capture_render("some unexpected output without STATUS prefix")
        assert "●" in out
        assert "done" in out

    def test_no_full_result_in_output(self):
        long_result = "x" * 500
        out = _capture_render(f"STATUS: success\nRESULT:\n{long_result}")
        # Should NOT contain the full result — compact mode
        assert long_result not in out

    def test_render_step_delegates_to_compact(self):
        """render_step('observation', ...) should use compact rendering."""
        buf = StringIO()
        import agent_cli.render as render_mod

        original = render_mod.console
        render_mod.console = Console(file=buf, force_terminal=True, width=120)
        try:
            render_mod.render_step(
                "observation",
                "STATUS: success\nRESULT:\ndata",
                iteration=3,
                tool_name="shell",
            )
        finally:
            render_mod.console = original
        out = buf.getvalue()
        assert "✓" in out
        assert "shell" in out
        # Should be compact — no Panel border characters
        assert "╭" not in out
