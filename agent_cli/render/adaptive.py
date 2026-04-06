"""Simple & Impactful Renderer - Clean terminal UI."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.box import ROUNDED

from agent_cli.render.base import Renderer


class SimpleRenderer(Renderer):
    """A clean, impactful renderer for terminal output.

    Uses a fixed 80-column layout with strong visual hierarchy
    through color and spacing instead of complex adaptations.
    """

    # Core color palette (5 colors)
    PRIMARY = "cyan"
    SUCCESS = "green"
    WARNING = "yellow"
    ERROR = "red"
    MUTE = "dim"

    def __init__(self, console: Console | None = None):
        self.console = console or Console()
        self.width = 80  # Fixed width for consistent layout

    def _divider(self, style: str = "dim"):
        """Print a horizontal divider."""
        self.console.print(f"[bold {style}]{'─' * self.width}[/]")

    def _styled_panel(self, content: str, title: str, color: str):
        """Create a consistent styled panel."""
        panel = Panel(
            content,
            title=f"[bold {color}]{title}[/]",
            border_style=color,
            box=ROUNDED,
        )
        self.console.print()
        self.console.print(panel)
        self.console.print()

    def header(
        self,
        provider: str,
        model: str,
        max_turns: int,
        skill_name: str = "",
        skill_args: str = "",
    ):
        """Session header banner."""
        self.console.print()
        self.console.print(f"[bold cyan]{'★' * 20} AGENT CLI {'★' * 20}[/]")
        self.console.print(f"[italic dim]Provider: {provider} │ Model: {model}[/]")

        if skill_name:
            args = f"({skill_args})" if skill_args else ""
            self.console.print(f"[dim]Skill: {skill_name}{args}[/]")
        else:
            max_iter = "∞" if max_turns == 0 else max_turns
            self.console.print(f"[dim]Max Iterations: {max_iter}[/]")

        self.console.print(f"[bold cyan]{'★' * 20}{'★' * 20}[/]")
        self.console.print()

    def turn_sep(self, turn: int):
        """Separator between turns."""
        self.console.print()
        self._divider()
        self.console.print(f"[bold yellow]▶ Turn {turn} ▶[/]")
        self._divider()
        self.console.print()

    def thought(self, content: str, turn: int):
        """LLM thought display."""
        self._styled_panel(content, "💭 Thought", self.PRIMARY)

    def action(self, tool_name: str, tool_input: str, turn: int):
        """Tool action display."""
        self._styled_panel(
            f"[dim]Input:[/] {tool_input}", f"⚡ Tool: {tool_name}", self.PRIMARY
        )

    def observation(self, content: str, turn: int, tool_name: str | None = None):
        """Tool result display."""
        first_line = content.split("\n", 1)[0].strip()

        if first_line.startswith("STATUS:"):
            status = first_line.split(":", 1)[1].strip().lower()
            if status == "success":
                color, icon = self.SUCCESS, "✅"
            elif status == "error":
                color, icon = self.ERROR, "❌"
            else:
                color, icon = self.PRIMARY, "●"
        else:
            color, icon = self.SUCCESS, "✅"

        label = f"[{tool_name}]" if tool_name else ""
        f"{icon} {status.title()}{label}"

        self._styled_panel(content, "📊 Observation", color)

    def final(self, content: str, turn: int):
        """Final result display."""
        self._divider()
        self._styled_panel(content, "🏆 Final Result", self.SUCCESS)
        self._divider()

    def error(self, content: str, turn: int):
        """Error display."""
        self._styled_panel(content, "⚠️ Error", self.ERROR)

    def status(self, state: str, message: str, turn: int = 0):
        """Status update with colored indicator."""
        color_map = {
            "running": self.PRIMARY,
            "done": self.SUCCESS,
            "error": self.ERROR,
            "warning": self.WARNING,
        }
        color = color_map.get(state.lower(), self.PRIMARY)

        turn_label = f"[dim](turn {turn})[/]" if turn else ""
        self.console.print(f"  [bold {color}]●[/] {message} {turn_label}")

    def model_detected(self, model: str, capabilities, provider: str, saved_path: str):
        """New model detection notice."""
        self.console.print()
        self.console.print(
            f"[bold cyan]🔍 Model Detected:[/] [cyan]{model}[/] ([dim]{provider}[/])"
        )
        self.console.print(f"  [dim]→ saved to {saved_path}[/]")
        self.console.print()

    def model_loaded(self, model: str, capabilities):
        """Loaded model status."""
        thinking = "✓" if capabilities.supports_thinking else "✗"
        ctx = f"{capabilities.context_window // 1000}k"
        self.console.print(
            f"  [bold green]●[/] [cyan]{model}[/] [dim](ctx={ctx}, thinking={thinking})[/]"
        )

    def raw(self, text: str, turn: int, verbose: bool):
        """Raw LLM response (stub - not implemented in simple mode)."""
        if verbose:
            self._styled_panel(text, f"📝 Raw Response turn {turn}", self.MUTE)
        else:
            self.console.print(
                f"  [dim]📄 raw response turn {turn} (use --verbose to view)[/]"
            )

    def context_dump(self, messages: list[dict], turn: int):
        """Debug context dump (stub - not implemented in simple mode)."""
        self.console.print()
        self.console.print(
            f"[dim]📋 Context Dump turn {turn}, {len(messages)} messages[/]"
        )
        self.console.print()

    def spinner_start(self, message: str = "thinking..."):
        """Start spinner (stub - not implemented in simple mode)."""
        self.console.print(f"  [dim]🤔 {message}[/]")

    def spinner_stop(self):
        """Stop spinner (stub - no-op in simple mode)."""
        pass

    def dispatch_progress(
        self, label: str, turn: int, tool_name: str, detail: str = "", thought: str = ""
    ):
        """Dispatch progress display (stub - not implemented in simple mode)."""
        self.console.print(f"  [dim]{label} [{turn}] ⚡ {tool_name}[/]")
