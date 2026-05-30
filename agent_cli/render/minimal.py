"""Minimal indent renderer — no boxes, resize-safe, nested depth support."""

from __future__ import annotations

import threading
from io import StringIO

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text

from agent_cli.render.base import ConfirmOption, Renderer

_MUTED = "grey46"


def _fmt_tok(n: int) -> str:
    """Compact token count: 5234 → '5.2K', 980 → '980'."""
    return f"{n / 1000:.1f}K" if n >= 1000 else str(int(n))


def _format_token_stats(stats: dict) -> str:
    """Render the token-usage dict into the CLI's single-line summary.

    Order: ttft, in (+speed), out (+speed), context %, cumulative out,
    cache. Each part is omitted when its value is zero/absent so other
    providers' lines stay lean.
    """
    parts: list[str] = []
    if stats.get("ttft_ms"):
        parts.append(f"ttft: {stats['ttft_ms']:.0f}ms")
    in_tok = stats.get("in", 0)
    if in_tok:
        sp = stats.get("in_speed", 0)
        parts.append(f"in: {_fmt_tok(in_tok)}" + (f" ({sp:.0f} tok/s)" if sp else ""))
    out_tok = stats.get("out", 0)
    if out_tok:
        sp = stats.get("out_speed", 0)
        parts.append(f"out: {_fmt_tok(out_tok)}" + (f" ({sp:.0f} tok/s)" if sp else ""))
    win = stats.get("context_window", 0)
    if in_tok and win:
        parts.append(
            f"ctx: {_fmt_tok(in_tok)}/{_fmt_tok(win)} ({in_tok / win * 100:.0f}%)"
        )
    if stats.get("total_out"):
        parts.append(f"Σout: {_fmt_tok(stats['total_out'])}")
    if stats.get("cache_read"):
        parts.append(f"cache hit: {_fmt_tok(stats['cache_read'])}")
    if stats.get("cache_write"):
        parts.append(f"cache write: {_fmt_tok(stats['cache_write'])}")
    return " | ".join(parts)


# East Asian "Ambiguous" (A) chars — `…` `—` `─` `※` `→` `《》` etc. — are
# rendered as 2 columns in CJK-locale terminals (macOS Terminal.app and
# iTerm2 with Korean/Japanese/Chinese locale). Counting them as 1 caused
# the marquee to underestimate paint width, overflow the terminal, and
# wrap onto new lines instead of overwriting in place. We assume
# ambiguous = wide so the calculation is correct in CJK terminals and
# only mildly conservative (a column or two of unused tail) elsewhere.
_WIDE_EAW = ("W", "F", "A")

# Streaming animation: a face that "speaks" the response — silent dots,
# then a partial word, then the whole word, then closed-mouth with the
# completed word as a delivery beat. Cycles per chunk but throttled to
# `_FRAME_INTERVAL` so fast streams don't blur it. `•` (U+2022) is East
# Asian Ambiguous; we treat it as 2 cols throughout so width calc agrees
# with how CJK-locale terminals render it.
#
# Each frame is padded with trailing spaces so all frames have the same
# rendered display width — this keeps the `~N tokens` counter that
# follows the frame anchored at a fixed column. Recomputing per-frame
# every chunk would be wasteful, so we pad once at module load.
_TALK_FRAMES_RAW = (
    "(•_•) < ...",
    "(•o•) < blah",
    "(•O•) < blah-blah",
    "(•_•) < blah-blah",
)

# Thinking animation: face + accumulating "hmmm" murmur → "?"
# realization → "!" eureka. Loops back to a fresh "hmm". Used by
# `spinner_start` via Rich Live at 10 fps; the frames are
# self-describing so the old "thinking..." text prefix becomes
# redundant.
_THINK_FRAMES_RAW = (
    "(•_•) hmm",
    "(•_•) hmmm",
    "(•_•) hmmm...",
    "(•_•) hmmm...?",
    "(•_•) hmmm...!",
)

# Cap streaming frame advancement at ~7 fps so multi-character frames
# (the talking face grows letters across frames) stay readable even
# when chunks arrive faster than the eye can track. Counter still
# updates on every chunk — only the visual frame is throttled.
_FRAME_INTERVAL = 0.15

# `chars / 4` matches `agent_cli.context.token_estimator.estimate_tokens`,
# so the streaming counter speaks the same units as the budget plumbing.
_CHARS_PER_TOKEN = 4


def _display_width(text: str) -> int:
    """Calculate display width accounting for CJK double-width characters."""
    import unicodedata

    w = 0
    for ch in text:
        eaw = unicodedata.east_asian_width(ch)
        w += 2 if eaw in _WIDE_EAW else 1
    return w


def _pad_to_width(frames: tuple[str, ...]) -> tuple[str, ...]:
    """Right-pad each frame with spaces so all share the widest frame's
    display width. Keeps following content (token counter, thought
    bubble) anchored at a fixed column instead of jiggling as the face
    cycles through different mouth shapes."""
    target = max(_display_width(f) for f in frames)
    return tuple(f + " " * (target - _display_width(f)) for f in frames)


# Width-aligned frame tuples — the public constants used by the
# renderer. Padding happens once at import; the raw tuples remain
# available for tests that want to verify the underlying glyphs.
_TALK_FRAMES = _pad_to_width(_TALK_FRAMES_RAW)
_THINK_FRAMES = _pad_to_width(_THINK_FRAMES_RAW)


class FrameClock:
    """Time-throttled cyclic frame advancer.

    Single source of truth for the animation cadence: both
    `spinner_start()` (single-task thinking spinner) and the
    parallel-delegate Live panel call this so they advance frames at
    exactly the same rate. `current()` is meant to be called from a
    repaint callback — it returns the frame to draw *now*, advancing
    only if `_FRAME_INTERVAL` has elapsed since the last advance.
    """

    def __init__(self, frames: tuple[str, ...]):
        self._frames = frames
        self._idx = 0
        self._last = 0.0

    def current(self) -> str:
        import time

        now = time.monotonic()
        if now - self._last >= _FRAME_INTERVAL:
            self._idx += 1
            self._last = now
        return self._frames[self._idx % len(self._frames)]


def _truncate_to_width(text: str, max_width: int) -> str:
    """Truncate text from the left to fit within max_width display columns."""
    import unicodedata

    total = _display_width(text)
    if total <= max_width:
        return text
    # Reserve `…`'s actual rendered width (Ambiguous → 2 cols on CJK
    # terminals). Reserving only 1 used to put the truncated string 1
    # col over budget once we started counting Ambiguous as wide.
    target = max_width - _display_width("…")
    if target < 0:
        target = 0
    w = 0
    for i in range(len(text) - 1, -1, -1):
        eaw = unicodedata.east_asian_width(text[i])
        cw = 2 if eaw in _WIDE_EAW else 1
        if w + cw > target:
            return "…" + text[i + 1 :]
        w += cw
    return "…" + text


class MinimalRenderer(Renderer):
    """Clean indented output with icons, no boxes or color-dependent structure.

    Supports nested rendering for skills/delegates via push_depth/pop_depth.
    Each depth level adds a "│ " prefix to all output.
    """

    def __init__(self, console: Console):
        super().__init__()
        self.con = console
        self._live: Live | None = None
        # Marquee resize-recovery state: terminal width and total cols
        # written by the last `stream_chunk`. When the terminal is
        # resized smaller mid-stream, the previous paint reflows onto
        # multiple lines that `\r` alone can't reach. Tracking the prior
        # paint lets us erase exactly those reflowed lines on the next
        # chunk. Reset to 0 in `stream_end`.
        self._last_term_w: int = 0
        self._last_painted_w: int = 0
        # Parallel-delegate orchestration. Owned entirely by
        # MinimalRenderer (was previously driven from tool_delegate
        # via the parallel_live_panel context manager). The Live
        # region starts when the first ``begin_delegate_task`` fires
        # and stops when the last ``end_delegate_task`` fires; the
        # delegate tool only signals lifecycle via begin/end markers
        # now, not the panel context manager or the capture pair.
        #
        # Why ``RLock`` instead of ``Lock``: ``begin_delegate_task``
        # constructs a ``rich.Live(get_renderable=...)`` while
        # holding this lock, and rich.Live's constructor invokes
        # ``get_renderable()`` once SYNCHRONOUSLY to seed the first
        # paint. That callable is ``_render_parallel_panel``, which
        # itself re-acquires the same lock to snapshot per-task
        # state. With a non-reentrant ``threading.Lock`` the same
        # thread blocks on its own held lock — a self-deadlock that
        # surfaces immediately on real terminals (the bug the user
        # reported with 8 parallel delegate tasks hanging). A
        # reentrant ``RLock`` lets the same thread re-acquire, so
        # the constructor's synchronous probe completes and rich
        # gets its initial renderable.
        self._parallel_lock = threading.RLock()
        self._parallel_live: Live | None = None
        self._parallel_clock: FrameClock | None = None
        # task_id → dict with keys: index, agent, task, tid, done,
        # success, duration_s, captured (list[str])
        self._parallel_tasks: dict[str, dict] = {}
        # Registration order for the post-completion dump — the
        # ``_parallel_tasks`` dict's insertion order matches but we
        # keep an explicit list for clarity and stability across
        # future dict-mutation refactors.
        self._parallel_order: list[str] = []

    @property
    def _prefix(self) -> str:
        """Depth-based prefix for nested rendering.

        Depth 0: no prefix (content has its own 2-space indent).
        Depth 1+: │ at column 0, aligned with ┌─/└─ group brackets.
        """
        if self._depth == 0:
            return ""
        return "│ " * self._depth

    def _p(self, text: str, **kwargs) -> None:
        """Print with depth prefix. Captures to buffer if in capture mode."""
        import re

        line = f"{self._prefix}{text}"
        # Strip Rich markup for clean captured text
        clean = re.sub(r"\[/?[^\]]*\]", "", line)
        if self._capture_line(clean):
            return
        self.con.print(line, **kwargs)

    def header(
        self,
        provider: str,
        model: str,
        max_turns: int,
        skill_name: str = "",
        skill_args: str = "",
    ) -> None:
        # Skip header for nested calls (depth>0) or parallel delegates (capture mode).
        # Each AgentLoop calls render_header in _setup(), but only the main loop
        # should show the banner.
        if self._depth > 0 or self.is_capturing:
            return
        self.con.print()
        if skill_name:
            args_label = f"({skill_args})" if skill_args else ""
            self.con.print(
                f"  ● skill:{skill_name}{args_label}  "
                f"[{_MUTED}]{provider} · {model}[/]",
                highlight=False,
            )
        else:
            iter_label = str(max_turns) if max_turns > 0 else "∞"
            self.con.print(
                f"  ● agent-cli  "
                f"[{_MUTED}]{provider} · {model} · max_turns={iter_label}[/]",
                highlight=False,
            )
        self.con.print()

    def turn_sep(self, turn: int) -> None:
        # No-op: turn number is already shown in token stats line
        # (e.g. "● ttft: 200ms | in: 1024 | out: 156  turn 1").
        pass

    def _render_markdown(self, icon: str, content: str) -> None:
        """Render markdown content with icon, respecting depth prefix."""
        buf = StringIO()
        temp = Console(file=buf, width=max(self.con.width - len(self._prefix) - 4, 40))
        temp.print(Markdown(content), highlight=False)
        rendered = buf.getvalue().rstrip("\n")
        lines = rendered.split("\n")
        self._p(f"  {icon} {lines[0]}", highlight=False)
        for line in lines[1:]:
            self._p(f"     {line}", highlight=False)

    def thought(self, content: str, turn: int) -> None:
        # Update live status (first line of thought, shown in parallel progress panel)
        first_line = content.strip().split("\n", 1)[0]
        self.set_thread_status(f"💭 {first_line}")
        self._p("")
        self._render_markdown("💭", content)
        # No trailing blank — let the action/observation pair visually below

    def action(self, tool_name: str, tool_input: str, turn: int) -> None:
        display = tool_input[:200] + "..." if len(tool_input) > 200 else tool_input
        self._p(f"  ⚡ {tool_name} → {display}", highlight=False, markup=False)

    def observation(
        self,
        content: str,
        turn: int,
        tool_name: str | None = None,
        success: bool = True,
    ) -> None:
        status = "success" if success else "error"
        icon = "✓" if success else "✗"
        tool_label = f" {tool_name}" if tool_name else ""

        detail = ""
        if not success:
            for line in content.split("\n"):
                if line.startswith("ERROR:"):
                    detail = f"  {line}"
                    break

        self._p(f"  {icon}{tool_label}  {status}{detail}", highlight=False)

        # `write_file` / `edit_file` append a Rich-marked unified diff
        # to the observation (see agent_cli.tools._diff.format_diff).
        # The summary above only conveys "what tool ran"; the diff is
        # what tells the user *what changed*. The diff is preceded by
        # a blank line and starts with a Rich-styled `--- a/...` header
        # — slicing from that header would split the `[bold]` open tag
        # in half, so we anchor on the full styled prefix instead.
        diff_marker = "[bold]--- a/"
        diff_idx = content.find(diff_marker)
        if diff_idx != -1:
            diff_block = content[diff_idx:].rstrip("\n")
            for line in diff_block.split("\n"):
                self._p(f"     {line}", highlight=False)

    def final(self, content: str, turn: int) -> None:
        self._p("")
        self._render_markdown("✅", content)
        self._p("")

    def error(self, content: str, turn: int) -> None:
        self._p(f"  ✗ {content}", highlight=False)

    def raw(self, text: str, turn: int, verbose: bool) -> None:
        # Non-verbose: stay silent. The per-turn stats line carries a
        # "(use --verbose to view raw response)" hint instead.
        if not verbose:
            return
        self._p(f"\n  [{_MUTED}]── raw response turn {turn} ──[/]")
        for line in text.split("\n"):
            self._p(f"  [{_MUTED}]{line}[/]")
        self._p(f"  [{_MUTED}]── end raw ──[/]\n")

    def thinking(self, text: str, turn: int) -> None:
        # Reasoning content from a provider-side field (Ollama
        # `message.thinking` for Qwen3 family). Caller decides whether
        # to invoke (gated on verbose at the call site).
        if not text:
            return
        self._p(f"\n  [{_MUTED}]── thinking turn {turn} ──[/]")
        for line in text.split("\n"):
            self._p(f"  [{_MUTED}]{line}[/]")
        self._p(f"  [{_MUTED}]── end thinking ──[/]\n")

    def status(self, state: str, message: str, turn: int = 0) -> None:
        it = f"  turn {turn}" if turn else ""
        self._p(f"  ● {message}{it}", highlight=False)

    def token_usage(self, stats: dict, turn: int, verbose: bool = False) -> None:
        msg = _format_token_stats(stats)
        if not msg:
            return
        if not verbose:
            msg += "  (use --verbose to view raw response)"
        self.status("running", msg, turn)

    def model_detected(
        self, model: str, capabilities, provider: str, saved_path: str
    ) -> None:
        yes, no = "✓", "✗"
        thinking_info = (
            f"{yes} (budget: {capabilities.thinking_budget:,}, "
            f"format: {capabilities.thinking_format})"
            if capabilities.supports_thinking
            else no
        )
        self.con.print()
        self.con.print("  ● Model Detected", highlight=False)
        self.con.print(f"    {model} ({provider})", highlight=False)
        self.con.print(
            f"    context={capabilities.context_window:,}  "
            f"output={capabilities.max_output_tokens:,}  "
            f"structured={'yes' if capabilities.supports_structured_output else 'no'}  "
            f"thinking={thinking_info}",
            highlight=False,
        )
        self.con.print(f"    [{_MUTED}]saved to {saved_path}[/]")
        self.con.print()

    def model_loaded(self, model: str, capabilities) -> None:
        yes, no = "✓", "✗"
        thinking = (
            f"thinking={yes}" if capabilities.supports_thinking else f"thinking={no}"
        )
        self.con.print(
            f"  ● {model} (ctx={capabilities.context_window:,}, {thinking})",
            highlight=False,
        )

    def context_dump(self, messages: list[dict], turn: int) -> None:
        self._p(
            f"\n  [{_MUTED}]── context dump (turn {turn}, {len(messages)} msgs) ──[/]"
        )
        for i, m in enumerate(messages):
            role = m.get("role", "?")
            content = m.get("content", "")
            if isinstance(content, str):
                preview = content[:200].replace("\n", "\\n")
                if len(content) > 200:
                    preview += f"... ({len(content)} chars)"
            else:
                preview = str(content)[:200]
            self._p(f"  [{_MUTED}][{i}] {role}: {preview}[/]")
        self._p(f"  [{_MUTED}]── end dump ──[/]\n")

    def spinner_start(self, message: str = "") -> None:
        if self.is_capturing:
            return  # No spinner in capture mode
        if self._live is not None:
            return  # Already spinning
        try:
            prefix = self._prefix
            # `_THINK_FRAMES` are self-describing (face + thought bubble
            # progression), so a `message` is optional — only prepended
            # when callers want to add context (e.g. "loading model").
            # `FrameClock` enforces the shared `_FRAME_INTERVAL` cadence
            # used here AND by the parallel-delegate Live panel.
            clock = FrameClock(_THINK_FRAMES)

            def get_renderable():
                msg = f"{message} " if message else ""
                return Text(f"{prefix}  {msg}{clock.current()}", style=_MUTED)

            self._live = Live(
                get_renderable(),
                console=self.con,
                refresh_per_second=10,
                transient=True,
                get_renderable=get_renderable,
            )
            self._live.start()
        except Exception:
            self._live = None  # Graceful fallback in non-TTY environments

    def spinner_stop(self) -> None:
        if self._live is not None:
            try:
                self._live.stop()
            except Exception:
                pass
            self._live = None

    def group_start(self, label: str, icon: str = "") -> None:
        """Print ┌─ at current depth. Call BEFORE push_depth."""
        icon_part = f"{icon} " if icon else ""
        self._p(f"┌─ {icon_part}{label}", highlight=False)

    def group_end(
        self, label: str, success: bool = True, duration_s: float = 0
    ) -> None:
        """Print └─ at current depth. Call AFTER pop_depth."""
        status = "✓" if success else "✗"
        dur = f" ({duration_s:.1f}s)" if duration_s > 0 else ""
        self._p(f"└─ {status} {label}{dur}", highlight=False)

    def _erase_reflowed_marquee(self) -> None:
        """If the terminal shrank since the last paint, the previous
        paint's content has been retroactively wrapped across multiple
        lines and a bare `\\r` only reaches the bottom one. Compute how
        many lines that paint now occupies at the new width and erase
        them all (current line + N-1 lines above) so the next paint
        starts on a clean line where the original paint began.

        Safe to call when no resize happened — falls through.
        """
        if not (self._last_term_w and self._last_painted_w):
            return
        new_w = self.con.width
        if new_w <= 0 or new_w == self._last_term_w:
            return
        # Ceil division: how many `new_w`-wide rows the prior paint now
        # occupies after the terminal's reflow.
        wrap_count = max(1, (self._last_painted_w + new_w - 1) // new_w)
        if wrap_count <= 1:
            return  # widened (or no change) — `\r` + pad still cleans up
        f = self.con.file
        for _ in range(wrap_count - 1):
            f.write("\r\x1b[K\x1b[1A")
        f.write("\r\x1b[K")

    def stream_chunk(self, text: str) -> None:
        import time

        if self.is_capturing:
            # Skip streaming in capture mode (parallel delegates).
            # The talking-face progress indicator is for live TTY only.
            return
        if not hasattr(self, "_stream_buf"):
            self._stream_buf = ""
            self._stream_chunks = 0
            self._last_frame_time = 0.0
        self._stream_buf += text
        # Frame advancement is time-throttled so multi-char talking
        # frames stay readable. The counter (below) still updates on
        # every chunk, so the user still sees activity even between
        # frame ticks.
        now = time.monotonic()
        if now - self._last_frame_time >= _FRAME_INTERVAL:
            self._stream_chunks += 1
            self._last_frame_time = now
        if self.con.file:
            self._erase_reflowed_marquee()
            prefix = f"{self._prefix}  " if self._depth > 0 else "  "
            frame = _TALK_FRAMES[self._stream_chunks % len(_TALK_FRAMES)]
            tokens = len(self._stream_buf) // _CHARS_PER_TOKEN
            line = f"{prefix}{frame} ~{tokens} tokens"
            # Narrow-terminal safety net: if frame+counter wouldn't fit,
            # drop the counter; if even the face wouldn't fit, truncate.
            # Keeps the indicator from ever wrapping onto a new line.
            avail = self.con.width - 1
            if _display_width(line) > avail:
                line = f"{prefix}{frame}"
                if _display_width(line) > avail:
                    line = _truncate_to_width(line, avail)
            self.con.file.write(f"\r{line}")
            pad = max(0, self.con.width - _display_width(line) - 1)
            self.con.file.write(" " * pad)
            self.con.file.flush()
            self._last_term_w = self.con.width
            self._last_painted_w = _display_width(line) + pad

    def stream_end(self) -> None:
        self._stream_buf = ""
        self._stream_chunks = 0
        if self.is_capturing:
            return
        if self.con.file:
            # Resize may have left reflowed remnants — clean those up
            # before the single-line clear below.
            self._erase_reflowed_marquee()
            self.con.file.write(f"\r{' ' * self.con.width}\r")
            self.con.file.flush()
            self._last_term_w = 0
            self._last_painted_w = 0

    def dispatch_progress(
        self,
        label: str,
        turn: int,
        tool_name: str,
        detail: str = "",
        thought: str = "",
    ) -> None:
        # Stop any active spinner before printing progress
        self.spinner_stop()

        # Delegate 🦀, skill 🪄, other ⚡
        if "delegate" in label:
            action_icon = "🦀"
        elif "skill:" in label:
            action_icon = "🪄"
        else:
            action_icon = "⚡"

        if thought:
            t = thought.replace("\n", " ").strip()
            self._p(
                f"  [{_MUTED}]{label} [{turn}] 💭 {t}[/]",
                highlight=False,
            )
        if tool_name == "complete":
            self._p(
                f"  [{_MUTED}]{label} [{turn}] ✅ {tool_name}{detail}[/]",
                highlight=False,
            )
        else:
            self._p(
                f"  [{_MUTED}]{label} [{turn}] {action_icon} {tool_name}:{detail}[/]",
                highlight=False,
            )

    # ── User input ──────────────────────────────────

    def prompt_user(
        self,
        prompt: str,
        *,
        default: str = "",
        multiline: bool = True,
        continuation: str = "... ",
        context: str = "",
    ) -> str:
        """CLI implementation — readline + paste detection for multi-line,
        single ``input()`` call for one-line prompts.

        ``EOFError`` / ``KeyboardInterrupt`` propagate to the caller —
        different consumers want different policy (chat REPL ends the
        session, setup wizard aborts back to caller, ask tool
        substitutes ``"(no response)"``), so the renderer doesn't
        impose one. Empty input → ``default`` substitution.

        Decode errors get swallowed by ``read_rich_input`` (one-shot
        warning + empty) so the user can retype without aborting.

        ``context`` is ignored — callers that announce pre-input text
        (e.g. ``_handle_ask``'s coloured question block) print it
        directly via ``console.print`` so Rich markup survives.
        """
        del context  # CLI prints announcements separately for color.
        if multiline:
            from agent_cli.input_history import read_rich_input

            value = read_rich_input(prompt, continuation=continuation).strip()
        else:
            try:
                value = input(prompt).strip()
            except UnicodeDecodeError:
                # Mirror read_rich_input's behaviour: drop the broken
                # paste and let the caller treat it as no input. EOF /
                # KbInt deliberately not caught — see method docstring.
                value = ""
        return value if value else default

    def confirm(
        self,
        prompt: str,
        options: list[ConfirmOption],
        *,
        default_key: str,
    ) -> tuple[str, str]:
        """CLI implementation — read one line and match the first token
        case-insensitively against each option's ``key`` and aliases.

        Empty / EOF / Ctrl+C → ``(default_key, "")``. Unrecognized first
        token → ``(default_key, full_raw_line)`` so the user's typed
        intent is preserved as a comment (e.g. shell danger prompt
        surfaces "I don't trust this" as a comment alongside the
        implicit deny).
        """
        try:
            raw = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            return (default_key, "")
        if not raw:
            return (default_key, "")

        parts = raw.split(maxsplit=1)
        first_lower = parts[0].lower()
        comment = parts[1].strip() if len(parts) > 1 else ""

        for opt in options:
            candidates = (opt.key.lower(), *(a.lower() for a in opt.aliases))
            if first_lower in candidates:
                return (opt.key, comment)

        # Unrecognized — preserve the full raw input as comment so the
        # user's intent surfaces upstream (e.g. tool result observation).
        return (default_key, raw)

    # ── Parallel delegate panel (begin/end-driven) ────

    def _render_parallel_panel(self) -> Text:
        """Build the multi-task progress region for the Live refresh.

        Reads ``_parallel_tasks`` under ``_parallel_lock`` so a
        mid-frame mutation (worker finishing) can't corrupt the
        drawn state. The per-task status line is fetched from the
        base ``_thread_status`` dict via ``get_thread_status(tid)``
        — same source the previous panel implementation polled, so
        no behavioural change.

        ``_parallel_clock.current()`` advances the animation frame
        at the throttled cadence shared with ``spinner_start``.
        """
        with self._parallel_lock:
            snapshot = [
                (tid, dict(self._parallel_tasks[tid])) for tid in self._parallel_order
            ]
        lines = [Text(f"Running {len(snapshot)} tasks in parallel:", style="grey46")]
        frame = self._parallel_clock.current() if self._parallel_clock else "..."
        for _tid, t in snapshot:
            label = (
                f"[{t['index'] + 1}] {t['agent']}: {t['task']}"
                if t["agent"]
                else f"[{t['index'] + 1}] {t['task']}"
            )
            if t["done"]:
                icon = "✓" if t["success"] else "✗"
                lines.append(
                    Text(
                        f"  {icon} {label} ({t['duration_s']:.1f}s)",
                        style="grey46",
                    )
                )
            else:
                status = self.get_thread_status(t["tid"])
                lines.append(Text(f"  {frame} {label}", style="grey46"))
                lines.append(Text(f"       {status}", style="grey46"))
        return Text("\n").join(lines)

    def begin_delegate_task(
        self,
        *,
        task_id: str,
        index: int,
        agent: str,
        task_text: str,
    ) -> None:
        """Register a worker thread into the live panel + start
        capturing its output. First call starts the ``rich.Live``
        region; subsequent calls just register their card.

        Called from inside the worker thread (i.e. the thread that
        will run ``_run_single`` next), so ``start_capture()`` here
        attaches the capture buffer to the correct thread without
        the caller needing a separate ``render_start_capture()``.
        """
        # The Live region's ``start()`` synchronously calls
        # ``get_renderable`` once to seed the first paint —
        # ``_render_parallel_panel`` then tries to acquire
        # ``_parallel_lock`` to snapshot state. If we did
        # ``live.start()`` while STILL holding ``_parallel_lock``,
        # the same thread would block on its own non-reentrant
        # ``threading.Lock`` and the whole worker would hang. So we
        # construct the Live + register state under the lock, but
        # release the lock BEFORE calling ``start()``.
        new_live: Live | None = None
        with self._parallel_lock:
            if self._parallel_live is None and self.con.is_terminal:
                # First task on a real terminal — bring the panel up.
                # ``transient=True`` so the live region clears on stop
                # and the captured output dump owns the terminal space
                # afterwards. When ``is_terminal`` is False (unit tests
                # with StringIO, headless redirected runs) we skip the
                # Live region entirely: the panel would have nowhere
                # to render and rich's refresh thread can hang on
                # output-blocking writes against a non-tty backend.
                # Task state and capture machinery still run so the
                # captured-output dump in ``end_delegate_task`` works
                # the same way.
                self._parallel_clock = FrameClock(_THINK_FRAMES)
                # Static placeholder as the initial renderable —
                # ``get_renderable`` (the callable below) is the
                # source of truth on every refresh tick. ``start()``
                # below will fire ``get_renderable`` once
                # synchronously to draw frame 0; the lock is
                # released by then so re-acquisition is safe.
                new_live = Live(
                    Text(""),
                    console=self.con,
                    refresh_per_second=8,
                    transient=True,
                    get_renderable=self._render_parallel_panel,
                )
                self._parallel_live = new_live
            if not self._parallel_order:
                # Fresh dump order for this parallel set (first task
                # of a new fan-out, regardless of whether the Live
                # region is active).
                self._parallel_order = []
            self._parallel_tasks[task_id] = {
                "index": index,
                "agent": agent,
                "task": task_text,
                "tid": threading.get_ident(),
                "done": False,
                "success": None,
                "duration_s": 0.0,
                "status": "",
                "captured": [],
            }
            self._parallel_order.append(task_id)
        # Outside the lock: ``start()`` does a synchronous first-
        # paint via ``get_renderable``, which re-acquires the
        # ``_parallel_lock``. Calling it after the ``with`` block
        # avoids the self-deadlock.
        if new_live is not None:
            new_live.start()
        # ``start_capture`` writes to ``self._captures`` under the
        # base lock; safe to call outside the panel lock.
        self.start_capture()

    def end_delegate_task(
        self,
        *,
        task_id: str,
        success: bool,
        duration_s: float,
        error: str = "",
    ) -> None:
        """Finalise a worker thread: stop its capture, mark its card
        done. When this is the LAST outstanding task, tear down the
        Live region and dump the captured output in registration
        order so the user sees per-task transcripts after the panel
        clears.
        """
        captured = self.stop_capture()
        should_stop = False
        with self._parallel_lock:
            state = self._parallel_tasks.get(task_id)
            if state is not None:
                state["done"] = True
                state["success"] = success
                state["duration_s"] = duration_s
                state["captured"] = captured
                if error:
                    state["error"] = error
            # All tasks in this set are done?
            if all(self._parallel_tasks[tid]["done"] for tid in self._parallel_order):
                should_stop = True
        if not should_stop:
            return
        # Tear down outside the lock — ``Live.stop`` joins the
        # refresh thread, which could otherwise contend with our
        # lock if it's mid-render.
        live = self._parallel_live
        self._parallel_live = None
        self._parallel_clock = None
        if live is not None:
            live.stop()
        # Replay each task's captured output wrapped in the group
        # framing the user would have seen if the worker ran live —
        # ``[N] agent: task`` opening header, indented body, closing
        # header with ✓/✗ and duration. This was previously the
        # delegate tool's job after the parallel_live_panel exited;
        # now the renderer owns the whole presentation so the tool
        # layer only needs to signal begin/end.
        with self._parallel_lock:
            order_snapshot = list(self._parallel_order)
            task_snapshot = {
                tid: dict(self._parallel_tasks[tid]) for tid in order_snapshot
            }
        for tid in order_snapshot:
            t = task_snapshot[tid]
            task_text = (t["task"] or "")[:40]
            agent = t["agent"]
            label = (
                f"[{t['index'] + 1}] {agent}: {task_text}"
                if agent
                else f"[{t['index'] + 1}] {task_text}"
            )
            self.group_start(label, icon="🦀")
            self.push_depth()
            prefix = self._prefix
            for line in t["captured"]:
                self.con.print(f"{prefix}{line}", highlight=False)
            self.pop_depth()
            self.group_end(
                label, success=bool(t["success"]), duration_s=t["duration_s"]
            )
        # Reset registration state for the next parallel set.
        with self._parallel_lock:
            for tid in order_snapshot:
                self._parallel_tasks.pop(tid, None)
            self._parallel_order = []

    # ── Ask-tool announcement ────────────────────────

    def announce_ask(self, questions: list[str], *, prefix: str = "") -> None:
        """Print the ``Agent asks:`` header + each question in colour.

        Mirrors the plain-text version that ``loop.py`` passes to
        ``prompt_user(context=...)`` for web surfaces; the CLI needs
        the spelled-out version because terminals don't echo the
        prompt context back to the user.
        """
        # Local import to keep `C` palette resolution lazy — the
        # render/__init__ module sets up `_renderer` at import time
        # and we don't want a hard circular dependency.
        from agent_cli.render import C

        accent = C["accent"]
        self.con.print(f"{prefix}\n{prefix}[{accent}]Agent asks:[/]")
        if len(questions) > 1:
            for i, q in enumerate(questions, 1):
                self.con.print(f"{prefix}  {i}. {q}")
        else:
            self.con.print(f"{prefix}  {questions[0]}")
