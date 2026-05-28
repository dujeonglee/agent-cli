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
        out = _capture(lambda: render_step("observation", "ok", 1, success=True))
        assert "✓" in out
        assert "success" in out

    def test_error_shows_x_mark(self):
        out = _capture(
            lambda: render_step(
                "observation", "ERROR: file not found", 1, success=False
            )
        )
        assert "✗" in out
        assert "error" in out

    def test_error_includes_detail(self):
        out = _capture(
            lambda: render_step(
                "observation", "ERROR: permission denied", 1, success=False
            )
        )
        assert "permission denied" in out

    def test_tool_name_displayed(self):
        out = _capture(
            lambda: render_step(
                "observation", "ok", 1, tool_name="read_file", success=True
            )
        )
        assert "read_file" in out

    def test_no_tool_name(self):
        out = _capture(lambda: render_step("observation", "ok", 1, success=True))
        assert "✓" in out
        assert "success" in out

    def test_compact_no_full_result(self):
        long_result = "x" * 500
        out = _capture(lambda: render_step("observation", long_result, 1, success=True))
        assert long_result not in out

    def test_no_box_characters(self):
        out = _capture(
            lambda: render_step(
                "observation",
                "data",
                3,
                tool_name="shell",
                success=True,
            )
        )
        assert "✓" in out
        assert "shell" in out
        assert "╭" not in out

    def test_diff_rendered_after_summary(self):
        """When write_file/edit_file emit a unified diff in the
        observation, the renderer must show it under the summary line.
        Otherwise the user has no way to see what changed."""
        from agent_cli.tools._diff import format_diff

        diff = format_diff("hello\n", "hello world\n", "f.txt")
        obs = f"File saved: f.txt (12 bytes)\n\n{diff}"
        out = _capture(
            lambda: render_step(
                "observation", obs, 1, tool_name="write_file", success=True
            )
        )
        assert "write_file" in out
        assert "@@" in out
        assert "hello world" in out

    def test_no_diff_when_observation_lacks_one(self):
        """Observations without a unified diff (most tools) render the
        single-line summary only — no spurious diff markers."""
        out = _capture(
            lambda: render_step(
                "observation",
                "ok",
                1,
                tool_name="read_file",
                success=True,
            )
        )
        assert "✓" in out
        assert "--- a/" not in out
        assert "@@" not in out


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
    """The streaming progress indicator is a small ASCII-art talking face
    plus a token estimate: `(•_•) < blah-blah ~N tokens`. Replaces the
    old marquee that scrolled response text — the marquee was hard to
    track when it moved fast and prone to overflow/wrap on resize.
    Frame advancement is throttled to `_FRAME_INTERVAL` so fast streams
    don't blur the animation; rapid chunks within one tick reuse the
    same frame but still grow the token count."""

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
        from agent_cli.render.minimal import _TALK_FRAMES

        assert any(f in out for f in _TALK_FRAMES)
        # Token count: 11 // 4 = 2
        assert "~2 tokens" in out

    def test_frames_advance_when_throttle_window_passes(self, monkeypatch):
        """Frames advance once per `_FRAME_INTERVAL`. We drive
        `time.monotonic` past the interval between chunks so each chunk
        ticks the frame counter; all distinct frames must appear."""
        import time as _time

        from agent_cli.render.minimal import _TALK_FRAMES, _FRAME_INTERVAL

        clock = [0.0]
        monkeypatch.setattr(_time, "monotonic", lambda: clock[0])

        renderer, buf, _ = self._renderer_with_width(80)
        seen = set()
        for _ in range(len(_TALK_FRAMES) + 1):
            renderer.stream_chunk("x")
            clock[0] += _FRAME_INTERVAL + 0.01
            current = buf.getvalue()
            for f in _TALK_FRAMES:
                if f in current:
                    seen.add(f)
        assert set(_TALK_FRAMES).issubset(seen)

    def test_rapid_chunks_share_a_frame(self, monkeypatch):
        """Chunks arriving within the throttle window do NOT advance the
        frame counter — that's the whole point of the throttle. The
        token counter still grows so the user sees activity."""
        import time as _time

        # Frozen clock: the very first chunk ticks once (because
        # `_last_frame_time` starts at 0.0 and the `>=` check fires)
        # but no subsequent chunk can advance.
        monkeypatch.setattr(_time, "monotonic", lambda: 0.0)

        renderer, _, _ = self._renderer_with_width(80)
        renderer.stream_chunk("a")
        first = renderer._stream_chunks
        for _ in range(20):
            renderer.stream_chunk("a")
        assert renderer._stream_chunks == first

    def test_token_count_increases_with_buffer(self):
        renderer, buf, _ = self._renderer_with_width(80)
        renderer.stream_chunk("a" * 100)  # 100 chars → 25 tokens
        renderer.stream_chunk("b" * 100)  # +100 → 50 tokens total

        out = buf.getvalue()
        assert "~25 tokens" in out
        assert "~50 tokens" in out

    def test_line_fits_in_small_terminal(self):
        """Even on a 30-col terminal the painted line must not exceed
        the terminal width. The safety net in `stream_chunk` drops the
        token counter (and truncates further if needed) when the
        full line wouldn't fit."""
        from agent_cli.render.minimal import _display_width

        renderer, buf, _ = self._renderer_with_width(30)
        renderer.stream_chunk("a" * 1000)  # huge buffer → big counter

        out = buf.getvalue()
        last_paint = out.rsplit("\r", 1)[-1]
        import re

        last_paint = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", last_paint)
        assert _display_width(last_paint) < 30

    def test_no_response_text_leaks(self):
        """The old marquee echoed accumulated response text. The new
        indicator must not — sensitive content / model errors stay
        out of the streaming display."""
        renderer, buf, _ = self._renderer_with_width(80)
        secret = "PRIVATE-API-KEY-xyz"
        renderer.stream_chunk(secret)
        assert secret not in buf.getvalue()

    def test_indicator_charset_limited_to_ascii_plus_bullet(self):
        """The indicator is ASCII + `•` (U+2022) only — no CJK, no
        emoji, no variation selectors. `•` is widely supported but
        East Asian Ambiguous, which is why width math accounts for
        Wide/Full/Ambiguous explicitly."""
        from agent_cli.render.minimal import _TALK_FRAMES

        renderer, buf, _ = self._renderer_with_width(80)
        for _ in range(8):
            renderer.stream_chunk("data")

        import re

        out = buf.getvalue()
        out = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", out)
        out = out.replace("\r", "").replace("\n", "")
        # Build the union of legal non-ASCII chars from the frame
        # constants — only those may appear in indicator output.
        non_ascii_allowed = {ch for f in _TALK_FRAMES for ch in f if ord(ch) >= 128}
        for ch in out.strip():
            assert ord(ch) < 128 or ch in non_ascii_allowed, (
                f"Unexpected char in indicator: {ch!r}"
            )

    def test_stream_end_resets_frame_counter(self):
        """A fresh stream starts at frame 0 — the frame counter must
        reset in stream_end so consecutive streams animate identically."""
        renderer, _, _ = self._renderer_with_width(80)
        renderer.stream_chunk("a")
        renderer.stream_chunk("b")
        renderer.stream_chunk("c")
        # After at least one chunk the counter is non-zero (the very
        # first chunk always ticks because `_last_frame_time` starts
        # at 0.0). The exact value depends on timing; what matters is
        # that `stream_end` resets it to 0.
        assert renderer._stream_chunks >= 1
        renderer.stream_end()
        assert renderer._stream_chunks == 0


class TestThinkingSpinner:
    """`spinner_start` uses `_THINK_FRAMES` — a face plus an
    accumulating thought bubble (`. → o → O → ?  → !`). Frames are
    self-describing so the old default `message="thinking..."` is
    no longer needed; callers can omit the message entirely."""

    def _renderer_with_width(self, width):
        from rich.console import Console
        from agent_cli.render.minimal import MinimalRenderer

        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=width)
        return MinimalRenderer(console), buf, console

    def test_default_message_is_empty(self):
        """Calling `spinner_start()` with no arguments must work — the
        new default is `""`, not `"thinking..."`."""
        from agent_cli.render.base import Renderer
        import inspect

        sig = inspect.signature(Renderer.spinner_start)
        assert sig.parameters["message"].default == ""

    def test_renderer_signature_matches_dispatcher(self):
        """The dispatcher `render_spinner_start` must keep the same
        empty-string default so callers don't need to pass anything."""
        from agent_cli.render import render_spinner_start
        import inspect

        sig = inspect.signature(render_spinner_start)
        assert sig.parameters["message"].default == ""

    def test_think_frames_charset(self):
        """`_THINK_FRAMES` must use only ASCII + `•` (matching the
        same charset constraint as `_TALK_FRAMES`)."""
        from agent_cli.render.minimal import _THINK_FRAMES, _TALK_FRAMES

        talk_non_ascii = {ch for f in _TALK_FRAMES for ch in f if ord(ch) >= 128}
        for frame in _THINK_FRAMES:
            for ch in frame:
                assert ord(ch) < 128 or ch in talk_non_ascii, (
                    f"Unexpected char in think frame: {ch!r}"
                )

    def test_think_frames_progress_through_thought_bubble(self):
        """Sanity check the user-supplied frame sequence: it should
        end with a `?` and then `!` to convey realization → eureka."""
        from agent_cli.render.minimal import _THINK_FRAMES

        assert "?" in _THINK_FRAMES[-2]
        assert "!" in _THINK_FRAMES[-1]


class TestFrameWidthAlignment:
    """All animation frames within a set must render at the same display
    width. Otherwise the content following the frame (token counter for
    talk frames, task label for the parallel-delegate Live panel) would
    jiggle horizontally as the face cycles. We pad once at module load
    rather than per-paint."""

    def test_talk_frames_share_width(self):
        from agent_cli.render.minimal import _TALK_FRAMES, _display_width

        widths = {_display_width(f) for f in _TALK_FRAMES}
        assert len(widths) == 1, f"Talk frames have varying widths: {widths}"

    def test_think_frames_share_width(self):
        from agent_cli.render.minimal import _THINK_FRAMES, _display_width

        widths = {_display_width(f) for f in _THINK_FRAMES}
        assert len(widths) == 1, f"Think frames have varying widths: {widths}"

    def test_padding_preserves_glyphs(self):
        """Padding only adds trailing spaces — no glyphs are lost. The
        raw frames must still appear (left-trimmed) inside the padded
        ones."""
        from agent_cli.render.minimal import (
            _TALK_FRAMES,
            _TALK_FRAMES_RAW,
            _THINK_FRAMES,
            _THINK_FRAMES_RAW,
        )

        for raw, padded in zip(_TALK_FRAMES_RAW, _TALK_FRAMES):
            assert padded.startswith(raw)
            assert padded.rstrip(" ") == raw
        for raw, padded in zip(_THINK_FRAMES_RAW, _THINK_FRAMES):
            assert padded.startswith(raw)
            assert padded.rstrip(" ") == raw

    def test_parallel_delegate_uses_think_frames(self):
        """The parallel-delegate Live panel must reuse ``_THINK_FRAMES``
        so its per-task spinner matches the single-task thinking
        spinner. A regression here would split the user's mental
        model of "agent is working" across two visual languages.

        The Live region is built inside MinimalRenderer (the render
        module owns all UI rendering). After the begin/end-driven
        refactor the panel renderable lives in ``_render_parallel_panel``
        and the Live lifecycle in ``begin_delegate_task`` /
        ``end_delegate_task``. We grep the renderer source to confirm
        the same constant is in use.
        """
        import inspect

        from agent_cli.render import minimal as _minimal

        # ``_render_parallel_panel`` is the body that draws each frame;
        # ``begin_delegate_task`` constructs the FrameClock with the
        # think-frames tuple.
        panel_src = inspect.getsource(_minimal.MinimalRenderer._render_parallel_panel)
        begin_src = inspect.getsource(_minimal.MinimalRenderer.begin_delegate_task)
        combined = panel_src + begin_src
        assert "_THINK_FRAMES" in combined
        # The old Braille frame literal must be gone.
        assert "⣾" not in combined
        # And the delegate tool must NOT host its own Live region
        # anymore — that would re-introduce the architectural leak
        # (UI rendering outside the render module).
        from agent_cli.tools import delegate as _delegate

        delegate_src = inspect.getsource(_delegate)
        assert "from rich.live import Live" not in delegate_src
        assert "rich.live" not in delegate_src

    def test_parallel_delegate_uses_frameclock(self):
        """The throttle/advance logic must live in ``FrameClock`` (the
        shared single-source-of-truth in render.minimal), not be
        copy-pasted into delegate.py or elsewhere. After the begin/
        end-driven refactor the FrameClock instantiation lives in
        ``begin_delegate_task`` and the per-frame call in
        ``_render_parallel_panel``."""
        import inspect

        from agent_cli.render import minimal as _minimal

        begin_src = inspect.getsource(_minimal.MinimalRenderer.begin_delegate_task)
        panel_src = inspect.getsource(_minimal.MinimalRenderer._render_parallel_panel)
        assert "FrameClock" in begin_src
        assert "FrameClock" in panel_src or ".current()" in panel_src
        # The hand-rolled throttle pattern must NOT reappear anywhere.
        from agent_cli.tools import delegate as _delegate

        assert "last_advance" not in inspect.getsource(_delegate)
        assert "last_advance" not in begin_src
        assert "last_advance" not in panel_src


class TestFrameClock:
    """`FrameClock` advances frames at most once per `_FRAME_INTERVAL`
    regardless of how often `current()` is polled. Exercising this in
    isolation guarantees both `spinner_start` and the parallel-delegate
    Live panel get the same cadence."""

    def test_frozen_clock_holds_frame(self, monkeypatch):
        import time as _time

        from agent_cli.render.minimal import FrameClock, _THINK_FRAMES

        monkeypatch.setattr(_time, "monotonic", lambda: 0.0)
        clock = FrameClock(_THINK_FRAMES)
        first = clock.current()
        for _ in range(50):
            assert clock.current() == first

    def test_advances_after_interval(self, monkeypatch):
        import time as _time

        from agent_cli.render.minimal import (
            FrameClock,
            _FRAME_INTERVAL,
            _THINK_FRAMES,
        )

        now = [0.0]
        monkeypatch.setattr(_time, "monotonic", lambda: now[0])
        clock = FrameClock(_THINK_FRAMES)
        seen = set()
        for _ in range(len(_THINK_FRAMES) + 1):
            seen.add(clock.current())
            now[0] += _FRAME_INTERVAL + 0.01
        assert set(_THINK_FRAMES).issubset(seen)


class TestPromptUser:
    """``Renderer.prompt_user`` — chat REPL / setup wizard / ask tool
    all route through here so a web renderer can satisfy the same API
    via form submission instead of stdin."""

    def _make(self):
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=120)
        return MinimalRenderer(console)

    def test_single_line_returns_stripped_input(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _p: "  hello world  ")
        r = self._make()
        assert r.prompt_user("prompt: ", multiline=False) == "hello world"

    def test_single_line_empty_returns_default(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _p: "")
        r = self._make()
        assert r.prompt_user("prompt: ", default="4096", multiline=False) == "4096"

    def test_single_line_eof_propagates(self, monkeypatch):
        # EOF / Ctrl+C propagate so the caller can pick a policy
        # (chat REPL: end session; setup wizard: abort; ask tool:
        # catch + substitute fallback).
        import pytest

        def raise_eof(_p):
            raise EOFError

        monkeypatch.setattr("builtins.input", raise_eof)
        r = self._make()
        with pytest.raises(EOFError):
            r.prompt_user("prompt: ", default="fallback", multiline=False)

    def test_single_line_keyboard_interrupt_propagates(self, monkeypatch):
        import pytest

        def raise_int(_p):
            raise KeyboardInterrupt

        monkeypatch.setattr("builtins.input", raise_int)
        r = self._make()
        with pytest.raises(KeyboardInterrupt):
            r.prompt_user("prompt: ", default="abort", multiline=False)

    def test_multiline_delegates_to_rich_input(self, monkeypatch):
        # ``read_rich_input`` is the existing reader; we just verify
        # we delegate to it and respect default-on-EOF semantics.
        monkeypatch.setattr(
            "agent_cli.input_history.read_rich_input",
            lambda _p, continuation="... ": "  multi  ",
        )
        r = self._make()
        assert r.prompt_user("prompt: ", multiline=True) == "multi"

    def test_multiline_empty_returns_default(self, monkeypatch):
        monkeypatch.setattr(
            "agent_cli.input_history.read_rich_input",
            lambda _p, continuation="... ": "",
        )
        r = self._make()
        assert r.prompt_user("prompt: ", default="def", multiline=True) == "def"


class TestConfirm:
    """``Renderer.confirm`` — single-line confirmation with options +
    optional comment. Powers the dangerous-command y/n/a prompt today;
    a web renderer would render one button per option."""

    def _make(self):
        from agent_cli.render.base import ConfirmOption

        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=120)
        return MinimalRenderer(console), [
            ConfirmOption(key="y", label="yes", aliases=("yes",)),
            ConfirmOption(key="n", label="no", aliases=("no",)),
            ConfirmOption(key="a", label="always", aliases=("always",)),
        ]

    def test_exact_key_match(self, monkeypatch):
        r, opts = self._make()
        monkeypatch.setattr("builtins.input", lambda _p: "y")
        assert r.confirm("?", opts, default_key="n") == ("y", "")

    def test_alias_match_case_insensitive(self, monkeypatch):
        r, opts = self._make()
        monkeypatch.setattr("builtins.input", lambda _p: "YES")
        assert r.confirm("?", opts, default_key="n") == ("y", "")

    def test_match_preserves_comment(self, monkeypatch):
        r, opts = self._make()
        monkeypatch.setattr("builtins.input", lambda _p: "y  go ahead  ")
        assert r.confirm("?", opts, default_key="n") == ("y", "go ahead")

    def test_empty_returns_default(self, monkeypatch):
        r, opts = self._make()
        monkeypatch.setattr("builtins.input", lambda _p: "   ")
        assert r.confirm("?", opts, default_key="n") == ("n", "")

    def test_eof_returns_default(self, monkeypatch):
        r, opts = self._make()

        def raise_eof(_p):
            raise EOFError

        monkeypatch.setattr("builtins.input", raise_eof)
        assert r.confirm("?", opts, default_key="n") == ("n", "")

    def test_unrecognized_returns_default_with_full_raw_as_comment(self, monkeypatch):
        # User typed something we don't recognize as a yes/no/always —
        # the renderer preserves their full text as a comment so the
        # caller (e.g. shell danger prompt) can surface it to the LLM.
        r, opts = self._make()
        monkeypatch.setattr(
            "builtins.input", lambda _p: "wait, I don't trust this command"
        )
        key, comment = r.confirm("?", opts, default_key="n")
        assert key == "n"
        assert comment == "wait, I don't trust this command"


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
