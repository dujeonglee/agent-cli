"""First-time setup wizard using Rich TUI.

Guides the user through one model server: its name, provider, connection and
default model. Saves it under ``endpoints`` in ~/.agent-cli/config.json.
"""

from __future__ import annotations

import requests
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table
from rich.text import Text

from agent_cli.providers.credentials import (
    Credentials,
    ExtraHeaderError,
    credentials_from_config,
)

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


def _list_models(
    base_url: str, credentials: Credentials, provider: str = "openai"
) -> list[str]:
    """List model ids from a provider's ``/models`` endpoint.

    OpenAI-compatible (omlx, vLLM, LM Studio, OpenAI) and Anthropic both expose
    ``GET {base_url}/models`` returning ``{data:[{id,...}]}`` — only the auth
    headers differ (OpenAI: ``Authorization: Bearer``; Anthropic: ``x-api-key``
    + ``anthropic-version``). ``base_url`` already includes the ``/v1`` suffix
    (see ``_DEFAULT_URLS``). Returns [] on any failure so the caller falls back
    to manual entry.
    """
    try:
        headers = credentials.headers(provider)
        r = requests.get(f"{base_url.rstrip('/')}/models", headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json().get("data", [])
            return [m["id"] for m in data if isinstance(m, dict) and m.get("id")]
    except Exception:
        pass
    return []


def _wizard_credentials(api_key: str, extra_headers) -> Credentials:
    """모델 목록 조회용 — 기존 추가 헤더가 규칙을 어기면 헤더 없이 묻는다(마법사는
    깨진 설정을 고치는 길이라 멈추지 않는다; 부팅이 그 오류를 이름과 함께 알린다)."""
    try:
        return credentials_from_config(
            {"extra_headers": extra_headers}, api_key=api_key
        )
    except ExtraHeaderError:
        return Credentials(api_key=api_key)


def _saved_endpoints() -> tuple[dict[str, dict], str | None, str]:
    """``(저장된 서버들, 기본 서버 이름, 못 읽었을 때의 사유)`` — 마법사는 깨진 설정을 고치는
    길이라 못 읽어도 멈추지 않는다(저장하면 바뀐다)."""
    from agent_cli.config import ConfigError, default_endpoint_name, list_endpoints

    try:
        return list_endpoints(), default_endpoint_name(), ""
    except ConfigError as e:
        return {}, None, str(e)


class SetupWizard:
    """Interactive setup wizard — creates or edits one model server (endpoint).

    서버 하나를 만들거나 고친다(v10.144.0). 서버 이름은 사용자가 정하고 바꾸지 않는다.
    추가 헤더(`extra_headers`)는 묻지 않는다 — 고치는 서버의 것을 그대로 두고(저장이 그
    키를 건드리지 않는다) 모델 목록 조회에도 싣는다. 옛 모양 설정은 읽지 않고, 저장 직전에
    `config.retire_legacy_files` 가 ``.v1`` 로 이름만 바꿔 남긴다."""

    def __init__(self):
        self.console = console

    def run(self) -> dict | None:
        """Run the setup wizard. Returns the saved server values (with ``name``) or None."""
        self._welcome()
        saved, default, unreadable = _saved_endpoints()
        self._show_existing(saved, default, unreadable)
        name = self._select_name(saved)
        existing = saved.get(name, {})
        provider = self._select_provider()
        base_url, api_key = self._configure_connection(provider, existing)
        key_for_listing = api_key if api_key else str(existing.get("api_key") or "")
        default_model = self._select_model(
            provider,
            base_url,
            _wizard_credentials(key_for_listing, existing.get("extra_headers")),
        )
        if not default_model:
            self.console.print("[yellow]No model selected. Setup cancelled.[/]")
            return None

        config = self._build_config(provider, base_url, api_key, default_model)
        if not api_key and existing.get("api_key"):
            del config["api_key"]  # 빈 입력 = 저장된 키 유지
        make_default = default is None or default not in saved or name == default
        if not make_default:
            make_default = Confirm.ask(
                f"   Use '{name}' as the default server?", default=False
            )
        if not self._review(name, config, existing, make_default):
            self.console.print("[yellow]Setup cancelled.[/]")
            return None

        self._save(name, config, make_default)
        self._done()
        return {"name": name, **config}

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

    def _show_existing(
        self, saved: dict[str, dict], default: str | None, unreadable: str
    ) -> None:
        """저장된 서버 표(키는 ``***``) — 없으면 조용하다. 옛 모양 파일·못 읽은 파일은 한 줄."""
        from agent_cli.config import legacy_config_present

        if unreadable:
            self.console.print(
                f"Could not read the existing config {unreadable}. "
                "Saving at the end of setup keeps it as a .v1 file.\n",
                style="yellow",
                markup=False,
                highlight=False,
                soft_wrap=True,  # 경로를 rich 가 80칸에서 끊지 않게
            )
        elif legacy_config_present():
            self.console.print(
                "Settings saved before 10.144.0 are not read. Saving at the end of "
                "setup keeps the old files as config.json.v1 / models.json.v1.\n",
                style="yellow",
                markup=False,
                highlight=False,
                soft_wrap=True,
            )
        if not saved:
            return

        table = Table(show_header=True, header_style="bold cyan", padding=(0, 1))
        table.add_column("Server", style="cyan", no_wrap=True)
        table.add_column("Provider")
        table.add_column("Base URL")
        table.add_column("Default Model")
        table.add_column("API Key")
        for name, data in sorted(saved.items()):
            label = f"{name} (default)" if name == default else name
            table.add_row(
                Text(label),
                Text(str(data.get("provider") or "openai")),
                Text(str(data.get("base_url") or "(unset)")),
                Text(str(data.get("default_model") or "(unset)")),
                "***" if data.get("api_key") else "(none)",
            )
        self.console.print(
            Panel(
                table,
                title="Saved servers",
                subtitle="enter an existing name to edit it, or a new name to add one",
                border_style="grey46",
            )
        )
        self.console.print()

    def _select_name(self, saved: dict[str, dict]) -> str:
        from agent_cli.config import ENDPOINT_NAME_RE

        self.console.print("[bold]1. Server name[/]")
        self.console.print(
            "   [dim]Lowercase letters, digits and '-'. Rooms remember a server by "
            "this name, so it cannot be renamed later.[/]"
        )
        while True:
            name = Prompt.ask("   Name").strip()
            if ENDPOINT_NAME_RE.match(name):
                break
            self.console.print("   [yellow]Use lowercase letters, digits and '-'.[/]")
        action = "Editing" if name in saved else "New server"
        self.console.print(f"   [green]{action}: {name}[/]\n")
        return name

    def _select_provider(self) -> str:
        self.console.print("[bold]2. Select LLM Provider[/]")
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

    def _configure_connection(
        self, provider: str, existing: dict | None = None
    ) -> tuple[str, str]:
        existing = existing or {}
        self.console.print(f"[bold]3. {provider.title()} Connection[/]")

        default_url = str(existing.get("base_url") or "") or _DEFAULT_URLS.get(
            provider, "https://api.openai.com/v1"
        )
        base_url = Prompt.ask("   Base URL", default=default_url)

        # API key — OpenAI-compatible servers may not need one (omlx /
        # local vLLM), but the prompt allows it; empty stays empty, or keeps the
        # saved key when editing a server.
        label = (
            "   API Key (empty keeps the saved key)"
            if existing.get("api_key")
            else "   API Key"
        )
        api_key = Prompt.ask(label, password=True, default="")

        self.console.print()
        return base_url, api_key

    def _select_model(
        self, provider: str, base_url: str, credentials: Credentials
    ) -> str:
        self.console.print("[bold]4. Select Default Model[/]")
        # Both providers expose ``/models`` (omlx serves the same models under
        # both APIs; real Anthropic has GET /v1/models too) — list them so the
        # user picks a real id instead of typing. Falls back to manual entry
        # when listing fails (no key / unsupported endpoint).
        if provider == "anthropic":
            return self._select_model_from_list(
                base_url, credentials, "anthropic", "claude-sonnet-4-20250514"
            )
        return self._select_model_from_list(base_url, credentials, "openai", "gpt-4o")

    def _select_model_from_list(
        self,
        base_url: str,
        credentials: Credentials,
        provider: str,
        manual_default: str,
    ) -> str:
        models = _list_models(base_url, credentials, provider)
        if not models:
            self.console.print(
                "   [yellow]Could not list models from /models. "
                "Enter model name manually.[/]"
            )
            model = Prompt.ask("   Model name", default=manual_default)
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

    def _review(
        self, name: str, config: dict, existing: dict, make_default: bool
    ) -> bool:
        self.console.print("[bold]5. Review[/]")
        table = Table(show_header=False, padding=(0, 2))
        table.add_column("Key", style="cyan")
        table.add_column("Value")
        table.add_row("Server", Text(name + (" (default)" if make_default else "")))
        table.add_row("Provider", Text(config["provider"]))
        table.add_row("Base URL", Text(config["base_url"]))
        has_key = bool(config.get("api_key") or existing.get("api_key"))
        table.add_row("API Key", "***" if has_key else "(none)")
        table.add_row("Model", Text(config["default_model"]))

        self.console.print(Panel(table, title="Configuration"))
        return Confirm.ask("   Save?", default=True)

    def _save(self, name: str, config: dict, make_default: bool) -> None:
        """옛 모양 파일을 ``.v1`` 로 남기고, 서버 하나를 저장하고, 필요하면 기본으로 지정한다."""
        from agent_cli.config import (
            config_path,
            retire_legacy_files,
            save_endpoint,
            set_default_endpoint,
        )

        for kept in retire_legacy_files():
            self.console.print(
                f"   Kept the old file as {kept}",
                style="grey46",
                markup=False,
                highlight=False,
                soft_wrap=True,
            )
        save_endpoint(name, config)
        if make_default:
            set_default_endpoint(name)
        self.console.print(
            f"   Saved server '{name}' to {config_path()}",
            style="green",
            markup=False,
            highlight=False,
            soft_wrap=True,
        )

    def _done(self) -> None:
        self.console.print()
        self.console.print(
            Panel(
                "[green]Ready![/] Try:\n"
                '  agent-cli run "List files in current directory"\n'
                "  agent-cli web   [dim](interactive browser UI)[/]\n"
                "  agent-cli endpoints  [dim](saved servers)[/]\n"
                "  agent-cli setup  [dim](to add or edit a server)[/]",
                title="Setup Complete",
            )
        )
