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
