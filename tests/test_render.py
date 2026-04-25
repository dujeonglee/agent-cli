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


class TestRawRendering:
    def test_raw_non_verbose_is_silent(self):
        """Non-verbose raw() prints nothing — hint lives on the stats line."""
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=120)
        r = MinimalRenderer(console)
        r.raw("some raw LLM text", turn=1, verbose=False)
        assert buf.getvalue() == ""

    def test_raw_verbose_shows_content(self):
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=120)
        r = MinimalRenderer(console)
        r.raw("some raw LLM text", turn=1, verbose=True)
        out = buf.getvalue()
        assert "raw response" in out
        assert "some raw LLM text" in out
        assert "end raw" in out


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


class TestDisplayWidth:
    """Marquee paint depends on `_display_width` matching what the
    terminal actually draws. Underestimating CJK widths causes the
    paint to overflow the terminal, wrap, and stack on new lines
    instead of overwriting in place. Pin the behavior on the chars
    LLMs emit constantly in Korean output."""

    def test_ascii_unchanged(self):
        from agent_cli.render.minimal import _display_width

        assert _display_width("Hello, world!") == 13

    def test_korean_hangul_double_width(self):
        from agent_cli.render.minimal import _display_width

        assert _display_width("안녕하세요") == 10

    def test_ambiguous_chars_count_as_wide(self):
        """`…` `—` `─` `※` `→` are East Asian Ambiguous — macOS Terminal
        and iTerm2 in Korean locale render these as 2 columns. We must
        count them as 2 to match what the terminal draws."""
        from agent_cli.render.minimal import _display_width

        assert _display_width("…") == 2
        assert _display_width("—") == 2
        assert _display_width("─") == 2
        assert _display_width("※") == 2
        assert _display_width("→") == 2

    def test_marquee_overflow_repro(self):
        """Repro of the symptom: '─── 진행 중 ───' was previously counted
        as 15 cols but rendered as 21 in CJK terminals — a 6-col
        underestimate that overflowed any reasonable marquee width."""
        from agent_cli.render.minimal import _display_width

        assert _display_width("─── 진행 중 ───") == 21

    def test_emoji_wide(self):
        from agent_cli.render.minimal import _display_width

        # Single-codepoint emoji is W (Wide) → 2 cols.
        assert _display_width("🤖") == 2

    def test_empty_string(self):
        from agent_cli.render.minimal import _display_width

        assert _display_width("") == 0


class TestTruncateToWidth:
    """Marquee tail-truncation must agree with `_display_width` so that
    the painted line never exceeds the terminal. The "…" prefix is
    itself Ambiguous (2 cols)."""

    def test_no_truncation_when_fits(self):
        from agent_cli.render.minimal import _truncate_to_width

        assert _truncate_to_width("hello", 10) == "hello"

    def test_truncates_long_ascii(self):
        from agent_cli.render.minimal import (
            _display_width,
            _truncate_to_width,
        )

        result = _truncate_to_width("0123456789abcdef", 10)
        assert result.startswith("…")
        assert _display_width(result) <= 10

    def test_truncates_korean_within_budget(self):
        from agent_cli.render.minimal import (
            _display_width,
            _truncate_to_width,
        )

        # 한글 9자 = 18 cols. Budget 10 → must shorten.
        text = "안녕하세요반가워요"
        result = _truncate_to_width(text, 10)
        assert result.startswith("…")
        assert _display_width(result) <= 10

    def test_ambiguous_heavy_string_within_budget(self):
        """The original bug's input — must fit even when packed with
        Ambiguous chars that previously fooled the width calc."""
        from agent_cli.render.minimal import (
            _display_width,
            _truncate_to_width,
        )

        text = "분석 중입니다… 결과: ─── 진행 중 ─── 30% → 60%"
        result = _truncate_to_width(text, 30)
        assert _display_width(result) <= 30


class TestStreamMarqueeResize:
    """When the terminal shrinks mid-stream, the previously painted
    line gets retroactively wrapped onto several lines. A bare `\\r`
    only reaches the bottom of those lines, so without cleanup the
    upper rows linger as visible residue. The renderer tracks the
    prior paint width and emits ANSI line-erase sequences before the
    next paint to clear all reflowed rows.

    Tests assert on the specific ANSI escape codes
    (`\\x1b[K` = erase line, `\\x1b[1A` = cursor up) since that's the
    contract with the terminal."""

    def _renderer_with_width(self, width):
        from rich.console import Console
        from agent_cli.render.minimal import MinimalRenderer

        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=width)
        return MinimalRenderer(console), buf, console

    def _set_width(self, console, new_width):
        # Rich's Console.width property reads from `_width` (the value
        # passed at construction). Mutate it to simulate a resize.
        console._width = new_width

    def test_no_resize_no_extra_escapes(self):
        """When width is stable across chunks, the second paint must
        not emit cursor-up sequences — just the regular `\\r` overwrite."""
        renderer, buf, console = self._renderer_with_width(80)
        renderer.stream_chunk("first")
        before = buf.getvalue()
        renderer.stream_chunk(" second")
        delta = buf.getvalue()[len(before) :]
        assert "\x1b[1A" not in delta  # no cursor-up
        # `\x1b[K` may appear from other Rich machinery, but our erase
        # sequence specifically pairs it with cursor-up — the absence
        # of `\x1b[1A` is the meaningful assertion.

    def test_shrink_emits_cleanup_sequence(self):
        """After shrinking from 80 to 40, the previous 79-col paint
        now wraps to ceil(79/40)=2 lines. Cleanup must move up once
        and erase, then erase the (originally bottom) current line —
        exactly one `\\x1b[1A` and at least two `\\x1b[K` per paint."""
        renderer, buf, console = self._renderer_with_width(80)
        renderer.stream_chunk("first chunk")
        before = buf.getvalue()

        self._set_width(console, 40)
        renderer.stream_chunk(" second")
        delta = buf.getvalue()[len(before) :]

        # 2 reflowed lines → 1 cursor-up + 2 line-erases (one per row).
        assert delta.count("\x1b[1A") == 1
        assert delta.count("\x1b[K") == 2

    def test_aggressive_shrink_more_cleanup(self):
        """80 → 20 wraps the 79-col prior paint to ceil(79/20)=4 lines.
        Cleanup must walk up 3 rows, erasing each, then erase the
        starting row → 3 cursor-ups, 4 line-erases."""
        renderer, buf, console = self._renderer_with_width(80)
        renderer.stream_chunk("first")
        before = buf.getvalue()

        self._set_width(console, 20)
        renderer.stream_chunk(" more")
        delta = buf.getvalue()[len(before) :]

        assert delta.count("\x1b[1A") == 3
        assert delta.count("\x1b[K") == 4

    def test_widening_no_cleanup(self):
        """Enlarging the terminal doesn't reflow — the previous narrow
        paint still occupies one line. No cursor-up should fire."""
        renderer, buf, console = self._renderer_with_width(40)
        renderer.stream_chunk("hi")
        before = buf.getvalue()

        self._set_width(console, 100)
        renderer.stream_chunk(" there")
        delta = buf.getvalue()[len(before) :]

        assert "\x1b[1A" not in delta

    def test_stream_end_resets_state(self):
        """After stream_end, the resize-recovery state is cleared so
        the next stream's first chunk doesn't try to erase lines from
        the previous (now-finished) marquee."""
        renderer, _, console = self._renderer_with_width(80)
        renderer.stream_chunk("hello")
        assert renderer._last_term_w == 80
        renderer.stream_end()
        assert renderer._last_term_w == 0
        assert renderer._last_painted_w == 0

        # New stream, immediate shrink — must NOT emit cleanup since
        # state was reset (no prior paint to clean up).
        buf2 = StringIO()
        from rich.console import Console

        renderer.con = Console(file=buf2, force_terminal=True, width=40)
        renderer.stream_chunk("fresh")
        out = buf2.getvalue()
        assert "\x1b[1A" not in out

    def test_capture_mode_skips_paint_and_state(self):
        """In capture mode (parallel delegates), stream_chunk returns
        early. The resize state must not be touched, so a later
        non-capture chunk doesn't compare against ghost state."""
        renderer, buf, console = self._renderer_with_width(80)
        renderer.start_capture()
        try:
            renderer.stream_chunk("captured")
        finally:
            renderer.stop_capture()
        assert renderer._last_term_w == 0
        assert renderer._last_painted_w == 0


class TestStreamTalkingFace:
    """The streaming progress indicator is a 4-frame ASCII talking face
    plus a token estimate: `(._.) ~N tokens`. Replaces the old marquee
    that scrolled response text — the marquee was hard to track when it
    moved fast and prone to overflow/wrap on resize. The fixed-width
    face is universally compatible (ASCII only) and the line stays
    short enough that overflow can't happen on any reasonable terminal."""

    def _renderer_with_width(self, width):
        from rich.console import Console
        from agent_cli.render.minimal import MinimalRenderer

        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=width)
        return MinimalRenderer(console), buf, console

    def test_first_chunk_paints_face_and_token_count(self):
        renderer, buf, _ = self._renderer_with_width(80)
        renderer.stream_chunk("hello world")  # 11 chars → ~2 tokens

        out = buf.getvalue()
        # Some frame from the cycle must appear.
        from agent_cli.render.minimal import _TALK_FRAMES

        assert any(f in out for f in _TALK_FRAMES)
        # Token count: 11 // 4 = 2
        assert "~2 tokens" in out

    def test_frames_cycle_across_chunks(self):
        """Each chunk advances to the next frame. After 4 chunks all
        4 frames have appeared; after 5 chunks the cycle has wrapped."""
        from agent_cli.render.minimal import _TALK_FRAMES

        renderer, buf, _ = self._renderer_with_width(80)
        seen = []
        for i in range(4):
            renderer.stream_chunk("x")
            # The latest frame is the last one drawn — it's the one
            # at position (i+1) % 4. We collect them all from buf.
            current = buf.getvalue()
            for f in _TALK_FRAMES:
                if f in current and f not in seen:
                    seen.append(f)

        # All four distinct mouth shapes should have been painted at
        # least once across the 4 chunks. (._.) and (.o.) are repeated
        # in the cycle but the SET of distinct shapes is 3: closed,
        # small-open, wide-open.
        distinct = set(_TALK_FRAMES)
        assert distinct.issubset(set(seen))

    def test_token_count_increases_with_buffer(self):
        renderer, buf, _ = self._renderer_with_width(80)
        renderer.stream_chunk("a" * 100)  # 100 chars → 25 tokens
        renderer.stream_chunk("b" * 100)  # +100 → 50 tokens total

        out = buf.getvalue()
        assert "~25 tokens" in out
        assert "~50 tokens" in out

    def test_line_fits_in_small_terminal(self):
        """Even on a 30-col terminal, the painted line never exceeds
        the terminal width — the talking-face line is fixed and short."""
        from agent_cli.render.minimal import _display_width

        renderer, buf, _ = self._renderer_with_width(30)
        renderer.stream_chunk("a" * 1000)  # huge buffer, big counter

        # Pull the latest paint between `\r` markers.
        out = buf.getvalue()
        last_paint = out.rsplit("\r", 1)[-1]
        # Strip ANSI escapes (none expected here on first paint, but
        # be safe in case Rich adds any).
        import re

        last_paint = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", last_paint)
        # Width of all printed columns must fit terminal width.
        assert _display_width(last_paint) < 30

    def test_no_response_text_leaks(self):
        """The old marquee echoed accumulated response text. The new
        indicator must not — sensitive content / model errors stay
        out of the streaming display."""
        renderer, buf, _ = self._renderer_with_width(80)
        secret = "PRIVATE-API-KEY-xyz"
        renderer.stream_chunk(secret)
        assert secret not in buf.getvalue()

    def test_pure_ascii_output(self):
        """No CJK / emoji / variation selectors — the painted indicator
        must be pure ASCII so it renders identically everywhere."""
        renderer, buf, _ = self._renderer_with_width(80)
        for _ in range(8):
            renderer.stream_chunk("data")

        # Pull just the painted indicator — strip ANSI + surrounding
        # whitespace and check every char is in printable ASCII.
        import re

        out = buf.getvalue()
        out = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", out)
        out = out.replace("\r", "").replace("\n", "")
        for ch in out.strip():
            assert ord(ch) < 128, f"Non-ASCII char in indicator: {ch!r}"

    def test_stream_end_resets_chunk_counter(self):
        """A fresh stream starts at frame 0 — the per-chunk counter
        must reset in stream_end so consecutive streams animate the
        same way."""
        renderer, _, _ = self._renderer_with_width(80)
        renderer.stream_chunk("a")
        renderer.stream_chunk("b")
        renderer.stream_chunk("c")
        assert renderer._stream_chunks == 3
        renderer.stream_end()
        assert renderer._stream_chunks == 0


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
