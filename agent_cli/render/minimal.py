"""Minimal indent renderer — no boxes, resize-safe, nested depth support."""

from __future__ import annotations

from io import StringIO

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.spinner import Spinner
from rich.text import Text

from agent_cli.render.base import Renderer

_MUTED = "grey46"


def _display_width(text: str) -> int:
    """Calculate display width accounting for CJK double-width characters."""
    import unicodedata

    w = 0
    for ch in text:
        eaw = unicodedata.east_asian_width(ch)
        w += 2 if eaw in ("W", "F") else 1
    return w


def _truncate_to_width(text: str, max_width: int) -> str:
    """Truncate text from the left to fit within max_width display columns."""
    import unicodedata

    total = _display_width(text)
    if total <= max_width:
        return text
    # Drop characters from the front until it fits (with … prefix)
    target = max_width - 1  # reserve 1 for …
    w = 0
    for i in range(len(text) - 1, -1, -1):
        eaw = unicodedata.east_asian_width(text[i])
        cw = 2 if eaw in ("W", "F") else 1
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

    @property
    def _prefix(self) -> str:
        """Depth-based prefix for nested rendering."""
        if self._depth == 0:
            return ""
        return "  " + "│   " * self._depth

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
        if self._depth > 0:
            return  # Skip header for nested calls
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
        self._p(f"\n[{_MUTED}]→ turn {turn}[/]\n")

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
        self._p("")
        self._render_markdown("💭", content)
        self._p("")

    def action(self, tool_name: str, tool_input: str, turn: int) -> None:
        display = tool_input[:200] + "..." if len(tool_input) > 200 else tool_input
        self._p(f"  ⚡ {tool_name} → {display}", highlight=False, markup=False)

    def observation(
        self, content: str, turn: int, tool_name: str | None = None
    ) -> None:
        first_line = content.split("\n", 1)[0].strip()
        if first_line.startswith("STATUS:"):
            status = first_line.split(":", 1)[1].strip().lower()
        else:
            status = "done"

        icon = "✓" if status == "success" else "✗" if status == "error" else "●"
        tool_label = f" {tool_name}" if tool_name else ""

        detail = ""
        if status == "error":
            for line in content.split("\n"):
                if line.startswith("ERROR:"):
                    detail = f"  {line}"
                    break

        self._p(f"  {icon}{tool_label}  {status}{detail}", highlight=False)

    def final(self, content: str, turn: int) -> None:
        self._p("")
        self._render_markdown("✅", content)
        self._p("")

    def error(self, content: str, turn: int) -> None:
        self._p(f"  ✗ {content}", highlight=False)

    def raw(self, text: str, turn: int, verbose: bool) -> None:
        if not verbose:
            self._p(
                f"  [{_MUTED}]📄 raw response turn {turn} (use --verbose to view)[/]"
            )
            return
        self._p(f"\n  [{_MUTED}]── raw response turn {turn} ──[/]")
        for line in text.split("\n"):
            self._p(f"  [{_MUTED}]{line}[/]")
        self._p(f"  [{_MUTED}]── end raw ──[/]\n")

    def status(self, state: str, message: str, turn: int = 0) -> None:
        it = f"  turn {turn}" if turn else ""
        self._p(f"  ● {message}{it}", highlight=False)

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
            f"tools={'yes' if capabilities.supports_tool_calling else 'no'}  "
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

    def spinner_start(self, message: str = "thinking...") -> None:
        if self.is_capturing:
            return  # No spinner in capture mode
        if self._live is not None:
            return  # Already spinning
        try:
            prefix = self._prefix
            spinner = Spinner("dots", text=Text(f"{prefix}  {message}", style=_MUTED))
            self._live = Live(
                spinner, console=self.con, refresh_per_second=10, transient=True
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

    def stream_chunk(self, text: str) -> None:
        if self.is_capturing:
            self._capture_line(text)
            return
        if not hasattr(self, "_stream_buf"):
            self._stream_buf = ""
        self._stream_buf += text
        if self.con.file:
            prefix = f"{self._prefix}  ◌ " if self._depth > 0 else "  ◌ "
            max_width = self.con.width - _display_width(prefix) - 1
            # Show the tail of accumulated text (marquee effect)
            visible = self._stream_buf.replace("\n", " ")
            visible = _truncate_to_width(visible, max_width)
            pad = max_width - _display_width(visible)
            self.con.file.write(f"\r{prefix}{visible}{' ' * pad}")
            self.con.file.flush()

    def stream_end(self) -> None:
        self._stream_buf = ""
        if self.is_capturing:
            return
        if self.con.file:
            # Clear the streaming line
            self.con.file.write(f"\r{' ' * self.con.width}\r")
            self.con.file.flush()

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
