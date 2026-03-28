"""Tests for agent_cli.config."""

import json

import pytest

from agent_cli.config import (
    get_model_entry,
    get_provider_defaults,
    load_config,
    reload_config,
    reload_registry,
    save_config,
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

        result = save_model_entry(
            "new-model:7b",
            {
                "context_window": 8192,
                "max_output_tokens": 2048,
            },
        )

        assert result is True
        assert target.exists()
        data = json.loads(target.read_text())
        assert "new-model:7b" in data["models"]
        assert data["models"]["new-model:7b"]["context_window"] == 8192

    def test_no_overwrite_existing(self, tmp_path, monkeypatch):
        """Existing model should NOT be overwritten."""
        target = tmp_path / "models.json"
        target.write_text(
            json.dumps(
                {
                    "models": {"existing:8b": {"context_window": 4096}},
                }
            )
        )
        monkeypatch.setattr(_config, "_GLOBAL_MODELS_PATH", target)
        monkeypatch.setattr(_config, "_SEARCH_PATHS", [target])

        result = save_model_entry(
            "existing:8b",
            {
                "context_window": 99999,  # different value
            },
        )

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

        global_file.write_text(
            json.dumps(
                {
                    "models": {"my-model": {"context_window": 4096}},
                }
            )
        )
        local_file.write_text(
            json.dumps(
                {
                    "models": {"my-model": {"context_window": 32768}},
                }
            )
        )

        monkeypatch.setattr(_config, "_SEARCH_PATHS", [local_file, global_file])
        reload_registry()

        entry = get_model_entry("my-model")
        assert entry is not None
        assert entry["context_window"] == 32768  # local wins

    def test_global_used_when_no_local(self, tmp_path, monkeypatch):
        """Falls back to global when no project-local file."""
        global_file = tmp_path / "models.json"
        local_file = tmp_path / "nonexistent" / "models.json"  # doesn't exist

        global_file.write_text(
            json.dumps(
                {
                    "models": {"remote-model": {"context_window": 16384}},
                }
            )
        )

        monkeypatch.setattr(_config, "_SEARCH_PATHS", [local_file, global_file])
        reload_registry()

        entry = get_model_entry("remote-model")
        assert entry is not None
        assert entry["context_window"] == 16384


class TestLoadConfig:
    """Test config.json 3-layer merging: env → user → workspace."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        reload_config()
        yield
        reload_config()

    def test_empty_config_when_no_files(self, tmp_path, monkeypatch):
        """No config files → empty dict with defaults."""
        monkeypatch.setattr(
            _config,
            "_CONFIG_PATHS",
            [tmp_path / "ws" / "config.json", tmp_path / "user" / "config.json"],
        )
        # Clear env vars
        for key in [
            "AGENT_CLI_PROVIDER",
            "AGENT_CLI_BASE_URL",
            "AGENT_CLI_API_KEY",
            "AGENT_CLI_MODEL",
        ]:
            monkeypatch.delenv(key, raising=False)

        config = load_config(use_cache=False)
        assert config.get("provider", "") == ""
        assert config.get("default_model", "") == ""

    def test_user_config_loaded(self, tmp_path, monkeypatch):
        """User config (~/.agent-cli/config.json) loaded."""
        user_config = tmp_path / "user" / "config.json"
        user_config.parent.mkdir(parents=True)
        user_config.write_text(
            json.dumps(
                {
                    "provider": "ollama",
                    "base_url": "http://localhost:11434",
                    "default_model": "qwen3:32b",
                }
            )
        )

        monkeypatch.setattr(
            _config,
            "_CONFIG_PATHS",
            [tmp_path / "ws" / "config.json", user_config],
        )
        for key in [
            "AGENT_CLI_PROVIDER",
            "AGENT_CLI_BASE_URL",
            "AGENT_CLI_API_KEY",
            "AGENT_CLI_MODEL",
        ]:
            monkeypatch.delenv(key, raising=False)

        config = load_config(use_cache=False)
        assert config["provider"] == "ollama"
        assert config["default_model"] == "qwen3:32b"

    def test_workspace_overrides_user(self, tmp_path, monkeypatch):
        """Workspace config overrides user config."""
        user_config = tmp_path / "user" / "config.json"
        ws_config = tmp_path / "ws" / "config.json"
        user_config.parent.mkdir(parents=True)
        ws_config.parent.mkdir(parents=True)

        user_config.write_text(
            json.dumps(
                {
                    "provider": "ollama",
                    "base_url": "http://localhost:11434",
                    "default_model": "qwen3:32b",
                }
            )
        )
        ws_config.write_text(
            json.dumps(
                {
                    "default_model": "nemotron:120b",
                }
            )
        )

        monkeypatch.setattr(_config, "_CONFIG_PATHS", [ws_config, user_config])
        for key in [
            "AGENT_CLI_PROVIDER",
            "AGENT_CLI_BASE_URL",
            "AGENT_CLI_API_KEY",
            "AGENT_CLI_MODEL",
        ]:
            monkeypatch.delenv(key, raising=False)

        config = load_config(use_cache=False)
        assert config["provider"] == "ollama"  # from user
        assert config["default_model"] == "nemotron:120b"  # workspace wins

    def test_env_vars_as_base(self, tmp_path, monkeypatch):
        """Environment variables provide base layer."""
        monkeypatch.setattr(
            _config,
            "_CONFIG_PATHS",
            [tmp_path / "ws" / "config.json", tmp_path / "user" / "config.json"],
        )
        monkeypatch.setenv("AGENT_CLI_PROVIDER", "anthropic")
        monkeypatch.setenv("AGENT_CLI_API_KEY", "sk-ant-xxx")
        monkeypatch.delenv("AGENT_CLI_BASE_URL", raising=False)
        monkeypatch.delenv("AGENT_CLI_MODEL", raising=False)

        config = load_config(use_cache=False)
        assert config["provider"] == "anthropic"
        assert config["api_key"] == "sk-ant-xxx"

    def test_file_overrides_env(self, tmp_path, monkeypatch):
        """Config file overrides env vars."""
        user_config = tmp_path / "user" / "config.json"
        user_config.parent.mkdir(parents=True)
        user_config.write_text(json.dumps({"provider": "ollama"}))

        monkeypatch.setattr(
            _config,
            "_CONFIG_PATHS",
            [tmp_path / "ws" / "config.json", user_config],
        )
        monkeypatch.setenv("AGENT_CLI_PROVIDER", "anthropic")

        config = load_config(use_cache=False)
        assert config["provider"] == "ollama"  # file wins over env

    def test_partial_workspace_merge(self, tmp_path, monkeypatch):
        """Workspace with only 1 field merges with user for the rest."""
        user_config = tmp_path / "user" / "config.json"
        ws_config = tmp_path / "ws" / "config.json"
        user_config.parent.mkdir(parents=True)
        ws_config.parent.mkdir(parents=True)

        user_config.write_text(
            json.dumps(
                {
                    "provider": "ollama",
                    "base_url": "http://localhost:11434",
                    "api_key": "",
                    "default_model": "qwen3:32b",
                }
            )
        )
        ws_config.write_text(
            json.dumps(
                {
                    "api_key": "special-key",
                }
            )
        )

        monkeypatch.setattr(_config, "_CONFIG_PATHS", [ws_config, user_config])
        for key in [
            "AGENT_CLI_PROVIDER",
            "AGENT_CLI_BASE_URL",
            "AGENT_CLI_API_KEY",
            "AGENT_CLI_MODEL",
        ]:
            monkeypatch.delenv(key, raising=False)

        config = load_config(use_cache=False)
        assert config["provider"] == "ollama"
        assert config["base_url"] == "http://localhost:11434"
        assert config["api_key"] == "special-key"  # workspace override
        assert config["default_model"] == "qwen3:32b"

    def test_caching(self, tmp_path, monkeypatch):
        """load_config caches result."""
        user_config = tmp_path / "config.json"
        user_config.write_text(json.dumps({"provider": "ollama"}))

        monkeypatch.setattr(_config, "_CONFIG_PATHS", [user_config])
        for key in [
            "AGENT_CLI_PROVIDER",
            "AGENT_CLI_BASE_URL",
            "AGENT_CLI_API_KEY",
            "AGENT_CLI_MODEL",
        ]:
            monkeypatch.delenv(key, raising=False)

        c1 = load_config(use_cache=False)
        c2 = load_config()  # cached
        assert c1 == c2


class TestSaveConfig:
    def test_save_to_user(self, tmp_path):
        """Save config to user path."""
        target = tmp_path / "config.json"
        save_config({"provider": "ollama", "default_model": "qwen3:32b"}, target)

        assert target.exists()
        data = json.loads(target.read_text())
        assert data["provider"] == "ollama"
        assert data["default_model"] == "qwen3:32b"

    def test_save_creates_directory(self, tmp_path):
        """Save creates parent directory if needed."""
        target = tmp_path / "subdir" / "config.json"
        save_config({"provider": "ollama"}, target)
        assert target.exists()

    def test_has_config_false_when_empty(self, tmp_path, monkeypatch):
        """has_config returns False when no config files exist."""
        monkeypatch.setattr(
            _config,
            "_CONFIG_PATHS",
            [tmp_path / "a.json", tmp_path / "b.json"],
        )
        for key in [
            "AGENT_CLI_PROVIDER",
            "AGENT_CLI_BASE_URL",
            "AGENT_CLI_API_KEY",
            "AGENT_CLI_MODEL",
        ]:
            monkeypatch.delenv(key, raising=False)

        from agent_cli.config import has_config

        assert has_config() is False

    def test_has_config_true_with_file(self, tmp_path, monkeypatch):
        """has_config returns True when config file exists."""
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"provider": "ollama"}))

        monkeypatch.setattr(_config, "_CONFIG_PATHS", [cfg])

        from agent_cli.config import has_config

        assert has_config() is True
