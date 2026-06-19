"""Tests for setup wizard helpers."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch


from agent_cli.setup import (
    SetupWizard,
    _list_models,
)


class TestListOpenAIModels:
    def test_returns_ids(self):
        """omlx/vLLM /v1/models → list of model ids."""
        with patch("agent_cli.setup.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: {
                    "data": [
                        {"id": "Qwen3.6-27B-MLX-8bit", "object": "model"},
                        {"id": "Qwen3.6-35B-A3B-MLX-8bit", "object": "model"},
                    ]
                },
            )
            models = _list_models("http://127.0.0.1:8000/v1")
            assert models == ["Qwen3.6-27B-MLX-8bit", "Qwen3.6-35B-A3B-MLX-8bit"]

    def test_passes_api_key(self):
        with patch("agent_cli.setup.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200, json=lambda: {"data": []}
            )
            _list_models("http://x/v1", api_key="secret")
            headers = mock_get.call_args.kwargs.get("headers", {})
            assert headers.get("Authorization") == "Bearer secret"

    def test_anthropic_uses_xapikey_and_version_headers(self):
        """Anthropic /v1/models needs x-api-key + anthropic-version, not Bearer."""
        with patch("agent_cli.setup.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: {"data": [{"id": "claude-sonnet-4-20250514"}]},
            )
            models = _list_models("http://x/v1", api_key="sk", provider="anthropic")
            headers = mock_get.call_args.kwargs.get("headers", {})
            assert headers.get("x-api-key") == "sk"
            assert headers.get("anthropic-version") == "2023-06-01"
            assert "Authorization" not in headers
            assert models == ["claude-sonnet-4-20250514"]

    def test_anthropic_version_header_sent_even_without_key(self):
        with patch("agent_cli.setup.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200, json=lambda: {"data": []}
            )
            _list_models("http://x/v1", provider="anthropic")
            headers = mock_get.call_args.kwargs.get("headers", {})
            assert headers.get("anthropic-version") == "2023-06-01"
            assert "x-api-key" not in headers  # no key → omit

    def test_empty_on_failure(self):
        with patch("agent_cli.setup.requests.get", side_effect=Exception("fail")):
            assert _list_models("http://x/v1") == []

    def test_empty_on_non_200(self):
        with patch("agent_cli.setup.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=404, json=lambda: {})
            assert _list_models("http://x/v1") == []

    def test_skips_entries_without_id(self):
        with patch("agent_cli.setup.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: {"data": [{"object": "model"}, {"id": "good"}]},
            )
            assert _list_models("http://x/v1") == ["good"]


class TestSelectModelRouting:
    def test_openai_provider_uses_v1_models(self):
        """provider=openai must route to the /v1/models listing path."""
        wiz = SetupWizard()
        with (
            patch(
                "agent_cli.setup._list_models",
                return_value=["m1", "m2"],
            ) as mock_list,
            patch("agent_cli.setup.IntPrompt.ask", return_value=2),
        ):
            selected = wiz._select_model("openai", "http://x/v1", "")
        mock_list.assert_called_once()
        assert selected == "m2"

    def test_openai_falls_back_to_manual_when_empty(self):
        wiz = SetupWizard()
        with (
            patch("agent_cli.setup._list_models", return_value=[]),
            patch("agent_cli.setup.Prompt.ask", return_value="typed-model"),
        ):
            selected = wiz._select_model("openai", "http://x/v1", "")
        assert selected == "typed-model"

    def test_anthropic_provider_lists_models(self):
        """provider=anthropic now also lists models (not just a manual prompt)."""
        wiz = SetupWizard()
        with (
            patch(
                "agent_cli.setup._list_models",
                return_value=["claude-a", "claude-b"],
            ) as mock_list,
            patch("agent_cli.setup.IntPrompt.ask", return_value=2),
        ):
            selected = wiz._select_model("anthropic", "http://x/v1", "k")
        mock_list.assert_called_once()
        # listing was queried with the anthropic provider
        assert (
            mock_list.call_args.args[2] == "anthropic"
            or mock_list.call_args.kwargs.get("provider") == "anthropic"
        )
        assert selected == "claude-b"

    def test_anthropic_falls_back_to_manual_when_empty(self):
        wiz = SetupWizard()
        with (
            patch("agent_cli.setup._list_models", return_value=[]),
            patch("agent_cli.setup.Prompt.ask", return_value="claude-typed"),
        ):
            selected = wiz._select_model("anthropic", "http://x/v1", "")
        assert selected == "claude-typed"


class TestSetupWizardConfig:
    def test_build_config(self):
        """Wizard builds correct config dict."""
        wizard = SetupWizard()
        config = wizard._build_config(
            provider="openai",
            base_url="http://127.0.0.1:8000/v1",
            api_key="",
            default_model="gpt-4o",
        )
        assert config["provider"] == "openai"
        assert config["base_url"] == "http://127.0.0.1:8000/v1"
        assert config["api_key"] == ""
        assert config["default_model"] == "gpt-4o"

    def test_save_config(self, tmp_path):
        """Wizard saves config to file."""
        target = tmp_path / "config.json"
        config = {
            "provider": "openai",
            "base_url": "http://127.0.0.1:8000/v1",
            "api_key": "",
            "default_model": "gpt-4o",
        }
        from agent_cli.config import save_config

        save_config(config, target)

        assert target.exists()
        data = json.loads(target.read_text())
        assert data["provider"] == "openai"
        assert data["default_model"] == "gpt-4o"


class TestShowExistingConfigs:
    """The wizard shows existing configs at startup so the user can
    see what they're about to override. Silent when nothing exists."""

    def _write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_silent_when_no_configs(self, tmp_path, monkeypatch, capsys):
        """First-time setup: nothing to reference, wizard skips the
        panel silently."""
        project = tmp_path / "project"
        project.mkdir()
        monkeypatch.setattr("agent_cli.setup.Path.home", lambda: tmp_path / "home")
        monkeypatch.chdir(project)

        wizard = SetupWizard()
        wizard.console = MagicMock()
        wizard._show_existing_configs()

        # No panel printed when nothing exists.
        wizard.console.print.assert_not_called()

    def test_shows_project_config(self, tmp_path, monkeypatch):
        """Existing .agent-cli/config.json in cwd is surfaced."""
        monkeypatch.setattr("agent_cli.setup.Path.home", lambda: tmp_path / "home")
        project = tmp_path / "project"
        project.mkdir()
        self._write(
            project / ".agent-cli" / "config.json",
            {
                "provider": "openai",
                "base_url": "http://127.0.0.1:8000/v1",
                "api_key": "",
                "default_model": "gpt-4o",
            },
        )
        monkeypatch.chdir(project)

        wizard = SetupWizard()
        wizard.console = MagicMock()
        wizard._show_existing_configs()

        # Panel rendered — check console.print was called.
        assert wizard.console.print.called

    def test_shows_user_config(self, tmp_path, monkeypatch):
        """Existing ~/.agent-cli/config.json is surfaced."""
        home = tmp_path / "home"
        monkeypatch.setattr("agent_cli.setup.Path.home", lambda: home)
        monkeypatch.chdir(tmp_path)  # empty cwd, no project config
        self._write(
            home / ".agent-cli" / "config.json",
            {
                "provider": "anthropic",
                "base_url": "https://api.anthropic.com/v1",
                "api_key": "sk-xxx",
                "default_model": "claude-sonnet-4-20250514",
            },
        )

        wizard = SetupWizard()
        wizard.console = MagicMock()
        wizard._show_existing_configs()

        assert wizard.console.print.called

    def test_api_key_masked_in_display(self, tmp_path, monkeypatch):
        """A populated api_key renders as *** in the table — never the
        raw key value."""
        home = tmp_path / "home"
        monkeypatch.setattr("agent_cli.setup.Path.home", lambda: home)
        monkeypatch.chdir(tmp_path)
        self._write(
            home / ".agent-cli" / "config.json",
            {
                "provider": "openai",
                "base_url": "https://api.openai.com/v1",
                "api_key": "sk-supersecret-do-not-leak",
                "default_model": "gpt-4o",
            },
        )
        wizard = SetupWizard()
        # Real console captured via Rich's string capture.
        from rich.console import Console
        from io import StringIO

        buf = StringIO()
        wizard.console = Console(file=buf, force_terminal=False, width=120)
        wizard._show_existing_configs()
        output = buf.getvalue()
        assert "sk-supersecret-do-not-leak" not in output
        assert "***" in output

    def test_malformed_config_skipped(self, tmp_path, monkeypatch):
        """Unparseable config file doesn't crash the wizard — just
        skipped as if absent."""
        home = tmp_path / "home"
        monkeypatch.setattr("agent_cli.setup.Path.home", lambda: home)
        monkeypatch.chdir(tmp_path)
        bad = home / ".agent-cli" / "config.json"
        bad.parent.mkdir(parents=True)
        bad.write_text("{{{ not valid json ", encoding="utf-8")

        wizard = SetupWizard()
        wizard.console = MagicMock()
        # Must not raise.
        wizard._show_existing_configs()
        # No valid entry → no panel.
        wizard.console.print.assert_not_called()
