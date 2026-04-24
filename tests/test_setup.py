"""Tests for setup wizard helpers."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch


from agent_cli.setup import (
    SetupWizard,
    _check_ollama_connection,
    _list_ollama_models,
)


class TestCheckOllamaConnection:
    def test_success(self):
        with patch("agent_cli.setup.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: {"version": "0.17.4"},
            )
            ok, version = _check_ollama_connection("http://localhost:11434")
            assert ok is True
            assert "0.17.4" in version

    def test_failure(self):
        with patch("agent_cli.setup.requests.get", side_effect=Exception("refused")):
            ok, version = _check_ollama_connection("http://localhost:11434")
            assert ok is False


class TestListOllamaModels:
    def test_returns_models(self):
        with patch("agent_cli.setup.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: {
                    "models": [
                        {"name": "qwen3:32b", "size": 20_000_000_000},
                        {"name": "llama3:8b", "size": 5_000_000_000},
                    ]
                },
            )
            models = _list_ollama_models("http://localhost:11434")
            assert len(models) == 2
            assert models[0]["name"] == "qwen3:32b"

    def test_empty_on_failure(self):
        with patch("agent_cli.setup.requests.get", side_effect=Exception("fail")):
            models = _list_ollama_models("http://localhost:11434")
            assert models == []


class TestSetupWizardConfig:
    def test_build_config(self):
        """Wizard builds correct config dict."""
        wizard = SetupWizard()
        config = wizard._build_config(
            provider="ollama",
            base_url="http://localhost:11434",
            api_key="",
            default_model="qwen3:32b",
        )
        assert config["provider"] == "ollama"
        assert config["base_url"] == "http://localhost:11434"
        assert config["api_key"] == ""
        assert config["default_model"] == "qwen3:32b"

    def test_save_config(self, tmp_path):
        """Wizard saves config to file."""
        target = tmp_path / "config.json"
        config = {
            "provider": "ollama",
            "base_url": "http://localhost:11434",
            "api_key": "",
            "default_model": "qwen3:32b",
        }
        from agent_cli.config import save_config

        save_config(config, target)

        assert target.exists()
        data = json.loads(target.read_text())
        assert data["provider"] == "ollama"
        assert data["default_model"] == "qwen3:32b"


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
                "provider": "ollama",
                "base_url": "http://localhost:11434",
                "api_key": "",
                "default_model": "qwen3:32b",
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
