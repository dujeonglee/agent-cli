"""Minimal indent renderer — no boxes, resize-safe."""

from __future__ import annotations

from rich.console import Console
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text

from agent_cli.render.base import Renderer

_MUTED = "grey46"


class MinimalRenderer(Renderer):
    """Clean indented output with icons, no boxes or color-dependent structure."""

    def __init__(self, console: Console):
        self.con = console
        self._live: Live | None = None

    def header(
        self,
        provider: str,
        model: str,
        max_iter: int,
        skill_name: str = "",
        skill_args: str = "",
    ) -> None:
        self.con.print()
        if skill_name:
            args_label = f"({skill_args})" if skill_args else ""
            self.con.print(
                f"  ● skill:{skill_name}{args_label}  "
                f"[{_MUTED}]{provider} · {model}[/]",
                highlight=False,
            )
        else:
            iter_label = str(max_iter) if max_iter > 0 else "∞"
            self.con.print(
                f"  ● agent-cli  "
                f"[{_MUTED}]{provider} · {model} · max_iter={iter_label}[/]",
                highlight=False,
            )
        self.con.print()

    def iter_sep(self, iteration: int) -> None:
        self.con.print(f"\n[{_MUTED}]→ iter {iteration}[/]\n")

    def thought(self, content: str, iteration: int) -> None:
        self.con.print()
        lines = content.split("\n")
        self.con.print(f"  💭 {lines[0]}", highlight=False)
        for line in lines[1:]:
            self.con.print(f"     {line}", highlight=False)
        self.con.print()

    def action(self, tool_name: str, tool_input: str, iteration: int) -> None:
        self.con.print(f"  ⚡ {tool_name} → {tool_input}", highlight=False)

    def observation(
        self, content: str, iteration: int, tool_name: str | None = None
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

        self.con.print(f"  {icon}{tool_label}  {status}{detail}", highlight=False)

    def final(self, content: str, iteration: int) -> None:
        self.con.print()
        lines = content.split("\n")
        self.con.print(f"  ✅ {lines[0]}", highlight=False)
        for line in lines[1:]:
            self.con.print(f"     {line}", highlight=False)
        self.con.print()

    def error(self, content: str, iteration: int) -> None:
        self.con.print(f"  ✗ {content}", highlight=False)

    def raw(self, text: str, iteration: int, verbose: bool) -> None:
        if not verbose:
            self.con.print(
                f"  [{_MUTED}]📄 raw response iter {iteration} "
                f"(use --verbose to view)[/]"
            )
            return
        self.con.print(f"\n  [{_MUTED}]── raw response iter {iteration} ──[/]")
        for line in text.split("\n"):
            self.con.print(f"  [{_MUTED}]{line}[/]")
        self.con.print(f"  [{_MUTED}]── end raw ──[/]\n")

    def status(self, state: str, message: str, iteration: int = 0) -> None:
        it = f"  iter {iteration}" if iteration else ""
        self.con.print(f"  ● {message}{it}", highlight=False)

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

    def context_dump(self, messages: list[dict], iteration: int) -> None:
        self.con.print(
            f"\n  [{_MUTED}]── context dump "
            f"(iter {iteration}, {len(messages)} msgs) ──[/]"
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
            self.con.print(f"  [{_MUTED}][{i}] {role}: {preview}[/]")
        self.con.print(f"  [{_MUTED}]── end dump ──[/]\n")

    def spinner_start(self, message: str = "thinking...") -> None:
        if self._live is not None:
            return  # Already spinning
        try:
            spinner = Spinner("dots", text=Text(f"  {message}", style=_MUTED))
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
