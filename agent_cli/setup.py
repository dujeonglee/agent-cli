"""First-time setup wizard using Rich TUI.

Guides user through provider, connection, and model selection.
Saves configuration to ~/.agent-cli/config.json or .agent-cli/config.json.
"""

from __future__ import annotations

import json
from pathlib import Path

import requests
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table
from rich.text import Text

from agent_cli.config import save_config

console = Console()

# Provider choices
_PROVIDERS = [
    ("openai", "OpenAI compatible (OpenAI, vLLM, LM Studio, omlx) — default"),
    ("anthropic", "Anthropic"),
]

_DEFAULT_URLS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
}


def _list_openai_models(base_url: str, api_key: str = "") -> list[str]:
    """List model ids from an OpenAI-compatible ``/v1/models`` endpoint.

    Works for omlx, vLLM, LM Studio, and OpenAI itself. ``base_url``
    already includes the ``/v1`` suffix (see ``_DEFAULT_URLS``). Returns
    an empty list on any failure so the caller falls back to manual
    entry.
    """
    try:
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        r = requests.get(f"{base_url.rstrip('/')}/models", headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json().get("data", [])
            return [m["id"] for m in data if isinstance(m, dict) and m.get("id")]
    except Exception:
        pass
    return []


class SetupWizard:
    """Interactive setup wizard for agent-cli configuration."""

    def __init__(self):
        self.console = console

    def run(self) -> dict | None:
        """Run the setup wizard. Returns config dict or None if cancelled."""
        self._welcome()
        self._show_existing_configs()
        provider = self._select_provider()
        base_url, api_key = self._configure_connection(provider)
        default_model = self._select_model(provider, base_url, api_key)
        if not default_model:
            self.console.print("[yellow]No model selected. Setup cancelled.[/]")
            return None

        config = self._build_config(provider, base_url, api_key, default_model)
        if not self._review(config):
            self.console.print("[yellow]Setup cancelled.[/]")
            return None

        self._save(config)
        self._done(config)
        return config

    def _welcome(self) -> None:
        self.console.print()
        self.console.print(
            Panel(
                Text("Agent-CLI Setup", justify="center", style="bold bright_cyan"),
                subtitle="ReAct pattern agent CLI for on-premise LLMs",
                padding=(1, 2),
            )
        )
        self.console.print()

    def _show_existing_configs(self) -> None:
        """Display any existing global or project configs so the user
        can reference them before picking new values. Silent when no
        config exists (first-time setup)."""
        candidates = [
            ("Project", Path.cwd() / ".agent-cli" / "config.json"),
            ("User (global)", Path.home() / ".agent-cli" / "config.json"),
        ]
        entries: list[tuple[str, Path, dict]] = []
        for label, path in candidates:
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(data, dict):
                continue
            entries.append((label, path, data))

        if not entries:
            return

        table = Table(show_header=True, header_style="bold cyan", padding=(0, 1))
        table.add_column("Scope", style="cyan", no_wrap=True)
        table.add_column("Provider")
        table.add_column("Base URL")
        table.add_column("Default Model")
        table.add_column("API Key")
        table.add_column("Path", style="grey46")
        for label, path, data in entries:
            has_key = bool(data.get("api_key"))
            table.add_row(
                label,
                str(data.get("provider") or "(unset)"),
                str(data.get("base_url") or "(unset)"),
                str(data.get("default_model") or "(unset)"),
                "***" if has_key else "(none)",
                str(path),
            )
        self.console.print(
            Panel(
                table,
                title="Existing configuration",
                subtitle="for reference — new choices below can override these",
                border_style="grey46",
            )
        )
        self.console.print()

    def _select_provider(self) -> str:
        self.console.print("[bold]1. Select LLM Provider[/]")
        for i, (key, label) in enumerate(_PROVIDERS, 1):
            self.console.print(f"   [{i}] {label}")

        choice = IntPrompt.ask(
            "   Select",
            default=1,
            choices=[str(i) for i in range(1, len(_PROVIDERS) + 1)],
        )
        provider = _PROVIDERS[choice - 1][0]
        self.console.print(f"   [green]Selected: {provider}[/]\n")
        return provider

    def _configure_connection(self, provider: str) -> tuple[str, str]:
        self.console.print(f"[bold]2. {provider.title()} Connection[/]")

        default_url = _DEFAULT_URLS.get(provider, "https://api.openai.com/v1")
        base_url = Prompt.ask("   Base URL", default=default_url)

        # API key — OpenAI-compatible servers may not need one (omlx /
        # local vLLM), but the prompt allows it; empty stays empty.
        api_key = Prompt.ask("   API Key", password=True, default="")

        self.console.print()
        return base_url, api_key

    def _select_model(self, provider: str, base_url: str, api_key: str) -> str:
        self.console.print("[bold]3. Select Default Model[/]")

        if provider == "openai":
            # OpenAI-compatible (omlx, vLLM, LM Studio, OpenAI) — list via
            # /v1/models so on-prem servers show their real model ids.
            return self._select_openai_model(base_url, api_key)
        # Anthropic has no equivalent listing endpoint here — ask.
        model = Prompt.ask("   Model name", default="claude-sonnet-4-20250514")
        self.console.print()
        return model

    def _select_openai_model(self, base_url: str, api_key: str) -> str:
        models = _list_openai_models(base_url, api_key)
        if not models:
            self.console.print(
                "   [yellow]Could not list models from /v1/models. "
                "Enter model name manually.[/]"
            )
            model = Prompt.ask("   Model name", default="gpt-4o")
            self.console.print()
            return model

        self.console.print("   Available models:")
        for i, m in enumerate(models, 1):
            self.console.print(f"   [{i}] {m}")

        choice = IntPrompt.ask(
            "   Select",
            default=1,
            choices=[str(i) for i in range(1, len(models) + 1)],
        )
        selected = models[choice - 1]
        self.console.print(f"   [green]Selected: {selected}[/]\n")
        return selected

    def _build_config(
        self, provider: str, base_url: str, api_key: str, default_model: str
    ) -> dict:
        return {
            "provider": provider,
            "base_url": base_url,
            "api_key": api_key,
            "default_model": default_model,
        }

    def _review(self, config: dict) -> bool:
        self.console.print("[bold]4. Review[/]")
        table = Table(show_header=False, padding=(0, 2))
        table.add_column("Key", style="cyan")
        table.add_column("Value")
        table.add_row("Provider", config["provider"])
        table.add_row("Base URL", config["base_url"])
        table.add_row("API Key", "***" if config["api_key"] else "(none)")
        table.add_row("Model", config["default_model"])

        self.console.print(Panel(table, title="Configuration"))
        return Confirm.ask("   Save?", default=True)

    def _save(self, config: dict) -> None:
        self.console.print()
        self.console.print("[bold]Save configuration to:[/]")
        self.console.print("   [1] This workspace only (.agent-cli/config.json)")
        self.console.print(
            "   [2] All projects - user default (~/.agent-cli/config.json)"
        )

        choice = IntPrompt.ask("   Select", default=2, choices=["1", "2"])

        if choice == 1:
            path = Path.cwd() / ".agent-cli" / "config.json"
        else:
            path = Path.home() / ".agent-cli" / "config.json"

        save_config(config, path)
        self.console.print(f"   [green]Saved to {path}[/]")

    def _done(self, config: dict) -> None:
        self.console.print()
        self.console.print(
            Panel(
                "[green]Ready![/] Try:\n"
                '  agent-cli run "List files in current directory"\n'
                "  agent-cli web   [dim](interactive browser UI)[/]\n"
                "  agent-cli setup  [dim](to reconfigure)[/]",
                title="Setup Complete",
            )
        )
