"""Tests for agent_cli.config."""
import json

import pytest

from agent_cli.config import (
    get_model_entry,
    get_provider_defaults,
    reload_registry,
    save_model_entry,
)
import agent_cli.config as _config


@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear registry cache before each test."""
    reload_registry()
    yield
    reload_registry()


class TestGetModelEntry:
    def test_known_model(self):
        """Models from ~/.agent-cli/models.json should load."""
        entry = get_model_entry("qwen3:32b")
        assert entry is not None
        assert entry["provider"] == "ollama"
        assert entry["context_window"] == 32768

    def test_unknown_model(self):
        assert get_model_entry("nonexistent-model") is None

    def test_anthropic_model(self):
        entry = get_model_entry("claude-sonnet-4-20250514")
        assert entry is not None
        assert entry["supports_tool_calling"] is True


class TestGetProviderDefaults:
    def test_ollama_defaults(self):
        defaults = get_provider_defaults("ollama")
        assert defaults.base_url == "http://localhost:11434"
        assert defaults.default_model == "qwen3:32b"

    def test_openai_defaults(self):
        defaults = get_provider_defaults("openai")
        assert "api.openai.com" in defaults.base_url

    def test_anthropic_defaults(self):
        defaults = get_provider_defaults("anthropic")
        assert "anthropic.com" in defaults.base_url

    def test_unknown_provider_fallback(self):
        defaults = get_provider_defaults("unknown_provider")
        assert defaults.base_url == "http://localhost:11434"
        assert defaults.default_model == ""


class TestSaveModelEntry:
    def test_save_new_model(self, tmp_path, monkeypatch):
        """New model should be saved to global models.json."""
        target = tmp_path / "models.json"
        monkeypatch.setattr(_config, "_GLOBAL_MODELS_PATH", target)
        monkeypatch.setattr(_config, "_SEARCH_PATHS", [target])

        result = save_model_entry("new-model:7b", {
            "context_window": 8192,
            "max_output_tokens": 2048,
        })

        assert result is True
        assert target.exists()
        data = json.loads(target.read_text())
        assert "new-model:7b" in data["models"]
        assert data["models"]["new-model:7b"]["context_window"] == 8192

    def test_no_overwrite_existing(self, tmp_path, monkeypatch):
        """Existing model should NOT be overwritten."""
        target = tmp_path / "models.json"
        target.write_text(json.dumps({
            "models": {"existing:8b": {"context_window": 4096}},
        }))
        monkeypatch.setattr(_config, "_GLOBAL_MODELS_PATH", target)
        monkeypatch.setattr(_config, "_SEARCH_PATHS", [target])

        result = save_model_entry("existing:8b", {
            "context_window": 99999,  # different value
        })

        assert result is False
        data = json.loads(target.read_text())
        assert data["models"]["existing:8b"]["context_window"] == 4096  # unchanged

    def test_creates_directory(self, tmp_path, monkeypatch):
        """Should create ~/.agent-cli/ if it doesn't exist."""
        target = tmp_path / "subdir" / "models.json"
        monkeypatch.setattr(_config, "_GLOBAL_MODELS_PATH", target)
        monkeypatch.setattr(_config, "_SEARCH_PATHS", [target])

        save_model_entry("test:1b", {"context_window": 2048})

        assert target.exists()


class TestSearchPathPriority:
    def test_project_local_overrides_global(self, tmp_path, monkeypatch):
        """Project .agent-cli/models.json should override ~/.agent-cli/models.json."""
        global_dir = tmp_path / "global"
        local_dir = tmp_path / "local"
        global_file = global_dir / "models.json"
        local_file = local_dir / "models.json"
        global_dir.mkdir()
        local_dir.mkdir()

        global_file.write_text(json.dumps({
            "models": {"my-model": {"context_window": 4096}},
        }))
        local_file.write_text(json.dumps({
            "models": {"my-model": {"context_window": 32768}},
        }))

        monkeypatch.setattr(_config, "_SEARCH_PATHS", [local_file, global_file])
        reload_registry()

        entry = get_model_entry("my-model")
        assert entry is not None
        assert entry["context_window"] == 32768  # local wins

    def test_global_used_when_no_local(self, tmp_path, monkeypatch):
        """Falls back to global when no project-local file."""
        global_file = tmp_path / "models.json"
        local_file = tmp_path / "nonexistent" / "models.json"  # doesn't exist

        global_file.write_text(json.dumps({
            "models": {"remote-model": {"context_window": 16384}},
        }))

        monkeypatch.setattr(_config, "_SEARCH_PATHS", [local_file, global_file])
        reload_registry()

        entry = get_model_entry("remote-model")
        assert entry is not None
        assert entry["context_window"] == 16384
