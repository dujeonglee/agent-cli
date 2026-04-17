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


# ── Group rendering (skill/delegate blocks) ─────────


def _capture_direct(setup_fn) -> str:
    """Capture with direct renderer access (setup_fn gets the renderer)."""
    buf = StringIO()
    console = Console(file=buf, force_terminal=True, width=120)
    r = MinimalRenderer(console)
    setup_fn(r)
    return buf.getvalue()


class TestGroupRendering:
    def test_group_start_shows_top_border(self):
        """group_start prints ┌─ with icon and label."""
        out = _capture_direct(lambda r: r.group_start("skill:plan", icon="🪄"))
        assert "┌─" in out
        assert "🪄" in out
        assert "skill:plan" in out

    def test_group_start_without_icon(self):
        """group_start works without icon."""
        out = _capture_direct(lambda r: r.group_start("delegate"))
        assert "┌─" in out
        assert "delegate" in out

    def test_group_end_success(self):
        """group_end shows ✓ for success with duration."""
        out = _capture_direct(
            lambda r: r.group_end("skill:plan", success=True, duration_s=12.3)
        )
        assert "└─" in out
        assert "✓" in out
        assert "skill:plan" in out
        assert "12.3s" in out

    def test_group_end_failure(self):
        """group_end shows ✗ for failure."""
        out = _capture_direct(lambda r: r.group_end("skill:bad", success=False))
        assert "└─" in out
        assert "✗" in out
        assert "skill:bad" in out

    def test_group_end_no_duration(self):
        """duration_s=0 omits the time display."""
        out = _capture_direct(lambda r: r.group_end("skill:x", success=True))
        assert "└─" in out
        assert "(0" not in out  # No "(0.0s)" shown

    def test_group_contains_nested_output(self):
        """Full cycle: start → push → inner output → pop → end."""

        def run(r):
            r.group_start("skill:plan", icon="🪄")
            r.push_depth()
            r.turn_sep(1)
            r.thought("analyzing", 1)
            r.pop_depth()
            r.group_end("skill:plan", success=True, duration_s=5.0)

        out = _capture_direct(run)
        assert "┌─" in out
        assert "└─" in out
        # Inner content should have │ prefix (depth=1)
        assert "│" in out
        assert "analyzing" in out

    def test_group_respects_current_depth(self):
        """Nested groups (skill → delegate) stack prefixes correctly."""

        def run(r):
            r.group_start("skill:a", icon="🪄")
            r.push_depth()
            r.group_start("delegate:b", icon="🦀")
            r.push_depth()
            r.thought("inner", 1)
            r.pop_depth()
            r.group_end("delegate:b", success=True, duration_s=1.0)
            r.pop_depth()
            r.group_end("skill:a", success=True, duration_s=5.0)

        out = _capture_direct(run)
        # Outer skill at depth 0
        assert "┌─ 🪄 skill:a" in out
        # Inner delegate at depth 1 → prefixed with │
        assert "│" in out
        assert "delegate:b" in out
        # Both groups closed
        assert out.count("└─") == 2

    def test_group_captured_during_parallel(self):
        """group_start/end inside capture mode goes to buffer."""

        def run(r):
            r.start_capture()
            r.group_start("delegate:x", icon="🦀")
            r.thought("captured", 1)
            r.group_end("delegate:x", success=True, duration_s=2.0)
            captured = r.stop_capture()
            # Nothing was printed to console
            # (captured buffer has the events stripped of markup)
            assert captured  # non-empty
            assert any("delegate:x" in line for line in captured)

        _capture_direct(run)


class TestGroupDelegatingFunctions:
    """Test render_group_start / render_group_end wrappers."""

    def test_delegating_functions(self):
        from agent_cli.render import render_group_start, render_group_end

        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=120)
        old = get_renderer()
        set_renderer(MinimalRenderer(console))
        try:
            render_group_start("skill:test", icon="🪄")
            render_group_end("skill:test", success=True, duration_s=1.5)
        finally:
            set_renderer(old)

        out = buf.getvalue()
        assert "┌─" in out
        assert "└─" in out
        assert "skill:test" in out
        assert "1.5s" in out
