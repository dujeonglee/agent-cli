"""Fancy renderer — rich visual output with colors, boxes, and animations."""

from __future__ import annotations

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text
from rich.table import Table
from rich.box import ROUNDED, DOUBLE

from agent_cli.render.base import Renderer


# Color scheme for the fancy renderer
_FANCY = {
    "primary": "bright_cyan",
    "secondary": "bright_magenta",
    "accent": "bright_yellow",
    "success": "bright_green",
    "warning": "bright_yellow",
    "error": "bright_red",
    "info": "blue",
    "muted": "grey50",
    "thought": "cyan",
    "action": "green",
    "observation": "medium_purple3",
    "final": "yellow",
    "separator": "bright_white",
}


class FancyRenderer(Renderer):
    """Fancy visual renderer with rich formatting, colors, and animations."""

    def __init__(self, console: Console):
        self.con = console
        self._live: Live | None = None

    def _divider(self, style: str = _FANCY["separator"]):
        """Print a horizontal divider."""
        self.con.print(
            f"[bold {style}]╀───────────────────────────────────────────────╀[/]"
        )

    def _header_box(self, title: str, provider: str, model: str, **details):
        """Render a styled header box."""
        content = Text()
        content.append(" 🤖 Agent CLI ", style=f"bold {_FANCY['primary']}")
        content.append(f"│ {provider} · {model}", style=f"italic {_FANCY['muted']}")

        for key, value in details.items():
            if value:
                content.append(f"  │  {key}: {value}", style=f"{_FANCY['muted']}")

        panel = Panel(
            content,
            title="[bold]✨ Session Started[/]",
            border_style=_FANCY["primary"],
            box=DOUBLE,
        )
        self.con.print(panel)

    def header(
        self,
        provider: str,
        model: str,
        max_turns: int,
        skill_name: str = "",
        skill_args: str = "",
    ) -> None:
        """Session or skill start banner with fancy styling."""
        self.con.print()

        details = {}
        if skill_name:
            details["Skill"] = f"{skill_name}{f'({skill_args})' if skill_args else ''}"
        elif max_turns > 0:
            details["Max Iterations"] = str(max_turns)
        else:
            details["Max Iterations"] = "∞"

        self._header_box("", provider, model, **details)
        self._divider()
        self.con.print()

    def turn_sep(self, turn: int) -> None:
        """Separator between turns with fancy styling."""
        self.con.print()
        self._divider()
        self.con.print(
            f"[bold {_FANCY['secondary']}]▶ Turn {turn} ▶[/]", highlight=False
        )
        self._divider()
        self.con.print()

    def thought(self, content: str, turn: int) -> None:
        """LLM reasoning/thought with styled box."""
        self.con.print()
        panel = Panel(
            content,
            title=f"[bold {_FANCY['thought']}]💭 Thought[/]",
            border_style=_FANCY["thought"],
            box=ROUNDED,
        )
        self.con.print(panel)

    def action(self, tool_name: str, tool_input: str, turn: int) -> None:
        """Tool call with styled display."""
        self.con.print()
        content = Text()
        content.append(" ⚡ Tool Call: ", style=f"bold {_FANCY['action']}")
        content.append(f"{tool_name}", style=f"bold {_FANCY['action']}")
        content.append(" → ", style=_FANCY["muted"])
        content.append(tool_input, style=_FANCY["muted"])

        panel = Panel(
            content,
            title=f"[bold {_FANCY['action']}]🛠 Action[/]",
            border_style=_FANCY["action"],
            box=ROUNDED,
        )
        self.con.print(panel)

    def observation(
        self, content: str, turn: int, tool_name: str | None = None
    ) -> None:
        """Tool result with status styling."""
        first_line = content.split("\n", 1)[0].strip()
        if first_line.startswith("STATUS:"):
            status = first_line.split(":", 1)[1].strip().lower()
        else:
            status = "done"

        if status == "success":
            status_style = _FANCY["success"]
            icon = "✅"
        elif status == "error":
            status_style = _FANCY["error"]
            icon = "❌"
        else:
            status_style = _FANCY["info"]
            icon = "●"

        tool_label = f" [{tool_name}]" if tool_name else ""
        content_text = Text()
        content_text.append(
            f"{icon} {status}{tool_label}", style=f"bold {status_style}"
        )

        # Add error details if present
        for line in content.split("\n"):
            if line.startswith("ERROR:"):
                content_text.append(f"\n  Error: {line}", style=_FANCY["error"])
                break

        panel = Panel(
            content_text,
            title=f"[bold {status_style}]📊 Observation[/]",
            border_style=status_style,
            box=ROUNDED,
        )
        self.con.print()
        self.con.print(panel)

    def final(self, content: str, turn: int) -> None:
        """Final answer with celebratory styling."""
        self.con.print()
        self._divider()
        panel = Panel(
            content,
            title=f"[bold {_FANCY['success']}]🏆 Final Result[/]",
            border_style=_FANCY["success"],
            box=DOUBLE,
        )
        self.con.print(panel)
        self._divider()
        self.con.print()

    def error(self, content: str, turn: int) -> None:
        """Error message with error styling."""
        self.con.print()
        panel = Panel(
            content,
            title=f"[bold {_FANCY['error']}]⚠️ Error[/]",
            border_style=_FANCY["error"],
            box=ROUNDED,
        )
        self.con.print(panel)

    def raw(self, text: str, turn: int, verbose: bool) -> None:
        """Raw LLM response panel (verbose only)."""
        if not verbose:
            # Hint lives on the per-turn stats line in non-verbose mode.
            return
        self.con.print()

        content = Text()
        content.append(f"── raw response turn {turn} ──\n", style=_FANCY["muted"])
        for line in text.split("\n"):
            content.append(f"{line}\n", style=_FANCY["muted"])
        content.append("── end raw ──", style=_FANCY["muted"])

        panel = Panel(
            content,
            title=f"[bold {_FANCY['muted']}]📝 Raw Response[/]",
            border_style=_FANCY["muted"],
            box=ROUNDED,
        )
        self.con.print(panel)

    def status(self, state: str, message: str, turn: int = 0) -> None:
        """Status update with colored badge."""
        state_colors = {
            "running": _FANCY["info"],
            "done": _FANCY["success"],
            "error": _FANCY["error"],
            "warning": _FANCY["warning"],
        }
        style = state_colors.get(state.lower(), _FANCY["info"])

        it_label = f"[italic {_FANCY['muted']}](turn {turn})[/]" if turn else ""
        self.con.print(f"  [bold {style}]●[/] {message} {it_label}", highlight=False)

    def model_detected(
        self, model: str, capabilities, provider: str, saved_path: str
    ) -> None:
        """Newly detected model info with table."""
        self.con.print()

        # Create a table for model capabilities
        table = Table(box=ROUNDED, border_style=_FANCY["primary"])
        table.add_column("Capability", style=_FANCY["primary"])
        table.add_column("Status", style=_FANCY["secondary"])

        # Thinking capability
        if capabilities.supports_thinking:
            thinking_status = f"✓ (budget: {capabilities.thinking_budget:,}, format: {capabilities.thinking_format})"
        else:
            thinking_status = "✗"
        table.add_row("Thinking", thinking_status)

        # Context window
        table.add_row("Context Window", f"{capabilities.context_window:,} tokens")

        # Output tokens
        table.add_row("Max Output", f"{capabilities.max_output_tokens:,} tokens")

        # Structured output
        table.add_row(
            "Structured Output",
            "✓" if capabilities.supports_structured_output else "✗",
        )

        # Tool calling
        table.add_row(
            "Tool Calling",
            "✓" if capabilities.supports_tool_calling else "✗",
        )

        panel = Panel(
            table,
            title=f"[bold {_FANCY['primary']}]🔍 Model Detected: {model} ({provider})[/]",
            border_style=_FANCY["primary"],
            box=DOUBLE,
        )
        self.con.print(panel)
        self.con.print(f"  [italic {_FANCY['muted']}]* saved to {saved_path}[/]")
        self.con.print()

    def model_loaded(self, model: str, capabilities) -> None:
        """Loaded model one-liner with styling."""
        thinking = "✓ thinking" if capabilities.supports_thinking else "✗ thinking"
        self.con.print(
            f"  [bold {_FANCY['success']}]●[/] "
            f"[cyan]{model}[/] "
            f"[italic {_FANCY['muted']}](ctx={capabilities.context_window:,}, {thinking})[/]",
            highlight=False,
        )

    def context_dump(self, messages: list[dict], turn: int) -> None:
        """Debug context window dump with table."""
        self.con.print()
        self._divider()
        self.con.print(
            f"[bold {_FANCY['muted']}]📋 Context Dump[/] "
            f"[italic]turn {turn}, {len(messages)} messages[/]",
            highlight=False,
        )
        self._divider()

        for i, m in enumerate(messages):
            role = m.get("role", "?")
            content = m.get("content", "")
            if isinstance(content, str):
                preview = content[:100].replace("\n", "\\n")
                if len(content) > 100:
                    preview += f"... ({len(content)} chars)"
            else:
                preview = str(content)[:100]

            role_style = {
                "system": _FANCY["warning"],
                "user": _FANCY["action"],
                "assistant": _FANCY["observation"],
            }.get(role, _FANCY["muted"])

            self.con.print(
                f"  [bold {role_style}][{i}] {role}:[/] {preview}", highlight=False
            )

        self._divider()
        self.con.print()

    def spinner_start(self, message: str = "thinking...") -> None:
        """Start a spinner animation with fancy style."""
        if self._live is not None:
            return  # Already spinning
        try:
            # Use a more visually appealing spinner
            spinner = Spinner(
                "bouncingBar",
                text=Text(f"  🤔 {message}", style=_FANCY["primary"]),
                style=_FANCY["primary"],
            )
            self._live = Live(
                spinner, console=self.con, refresh_per_second=10, transient=True
            )
            self._live.start()
        except Exception:
            self._live = None  # Graceful fallback in non-TTY environments

    def spinner_stop(self) -> None:
        """Stop the spinner animation."""
        if self._live is not None:
            try:
                self._live.stop()
            except Exception:
                pass
            self._live = None

    def dispatch_progress(
        self,
        label: str,
        turn: int,
        tool_name: str,
        detail: str = "",
        thought: str = "",
    ) -> None:
        self.spinner_stop()
        if thought:
            t = thought.replace("\n", " ").strip()
            self.con.print(
                f"  [dim]{label} [{turn}] 💭 {t}[/]",
                highlight=False,
            )
        if tool_name == "complete":
            icon = "✅"
        elif "delegate" in label:
            icon = "🦀"
        elif "skill:" in label:
            icon = "🪄"
        else:
            icon = "⚡"
        self.con.print(
            f"  [dim]{label} [{turn}] {icon} {tool_name}{detail}[/]",
            highlight=False,
        )
