"""Tests for agent_cli.providers.compat."""

from unittest.mock import MagicMock, patch

import pytest

from agent_cli.config import reload_registry
from agent_cli.providers.compat import (
    DEFAULT_CAPABILITIES,
    get_capabilities,
    _detect_ollama_capabilities,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    reload_registry()
    yield
    reload_registry()


class TestGetCapabilities:
    def test_registered_model(self):
        caps = get_capabilities("qwen3:32b")
        assert caps.context_window == 32768
        assert caps.max_output_tokens == 4096
        assert caps.supports_structured_output is True
        assert caps.supports_thinking is True
        assert caps.thinking_budget == 4096

    def test_unregistered_model(self):
        caps = get_capabilities("unknown-model:latest")
        assert caps == DEFAULT_CAPABILITIES
        assert caps.context_window == 4096
        assert caps.supports_structured_output is False

    def test_openai_model(self):
        caps = get_capabilities("gpt-4o")
        assert caps.supports_structured_output is True
        assert caps.context_window == 128000

    def test_thinking_format_registered(self):
        caps = get_capabilities("qwen3:32b")
        assert caps.thinking_format == "think"

    def test_thinking_format_empty_for_non_thinking(self):
        caps = get_capabilities("llama3.1:8b")
        assert caps.thinking_format == ""

    def test_thinking_format_empty_for_anthropic(self):
        caps = get_capabilities("claude-sonnet-4-20250514")
        assert caps.thinking_format == ""

    def test_frozen(self):
        caps = get_capabilities("qwen3:32b")
        with pytest.raises(AttributeError):
            caps.context_window = 9999  # type: ignore

    def test_static_registry_takes_priority(self):
        """models.json entry should override runtime detection."""
        caps = get_capabilities(
            "qwen3:32b", provider="ollama", base_url="http://localhost:11434"
        )
        assert caps.context_window == 32768  # from models.json, not runtime

    def test_unregistered_with_runtime_fallback(self):
        """Unregistered model without runtime detection → defaults."""
        caps = get_capabilities("unknown:latest")
        assert caps == DEFAULT_CAPABILITIES


class TestOllamaRuntimeDetection:
    @patch("agent_cli.providers.compat.requests.post")
    def test_detects_capabilities(self, mock_post):
        show_resp = MagicMock()
        show_resp.status_code = 200
        show_resp.json.return_value = {
            "model_info": {"llama.context_length": 8192},
            "details": {"family": "llama", "parameter_size": "8B"},
        }
        show_resp.raise_for_status.return_value = None

        probe_resp = MagicMock()
        probe_resp.status_code = 200
        probe_resp.json.return_value = {
            "message": {"content": "Hello!"},
        }
        probe_resp.raise_for_status.return_value = None

        mock_post.side_effect = [show_resp, probe_resp]

        caps = _detect_ollama_capabilities(
            "http://localhost:11434", "llama3.1:8b-custom"
        )
        assert caps is not None
        assert caps.context_window == 8192
        assert caps.supports_structured_output is True
        assert caps.thinking_format == ""

    @patch("agent_cli.providers.compat.requests.post")
    def test_detects_thinking_model(self, mock_post):
        """Probe-based detection: /api/show for metadata, /api/chat for thinking."""
        show_resp = MagicMock()
        show_resp.status_code = 200
        show_resp.json.return_value = {
            "model_info": {"llama.context_length": 32768},
            "details": {"family": "qwen3"},
        }
        show_resp.raise_for_status.return_value = None

        probe_resp = MagicMock()
        probe_resp.status_code = 200
        probe_resp.json.return_value = {
            "message": {"content": "<think>\nLet me think...\n</think>\nHello!"},
        }
        probe_resp.raise_for_status.return_value = None

        # First call = /api/show, second call = /api/chat (probe)
        mock_post.side_effect = [show_resp, probe_resp]

        caps = _detect_ollama_capabilities("http://localhost:11434", "qwen3:14b")
        assert caps is not None
        assert caps.supports_thinking is True
        assert caps.thinking_budget > 0
        assert caps.thinking_format == "think"

    @patch("agent_cli.providers.compat.requests.post")
    def test_detects_non_thinking_model(self, mock_post):
        """Probe returns no thinking tags → non-thinking model."""
        show_resp = MagicMock()
        show_resp.status_code = 200
        show_resp.json.return_value = {
            "model_info": {"llama.context_length": 8192},
            "details": {"family": "llama"},
        }
        show_resp.raise_for_status.return_value = None

        probe_resp = MagicMock()
        probe_resp.status_code = 200
        probe_resp.json.return_value = {
            "message": {"content": "Hello! How can I help you?"},
        }
        probe_resp.raise_for_status.return_value = None

        mock_post.side_effect = [show_resp, probe_resp]

        caps = _detect_ollama_capabilities("http://localhost:11434", "llama3:8b")
        assert caps is not None
        assert caps.supports_thinking is False
        assert caps.thinking_format == ""

    @patch("agent_cli.providers.compat.requests.post")
    def test_detects_thinking_via_field(self, mock_post):
        """Probe detects thinking via message.thinking field (Qwen3, GLM)."""
        show_resp = MagicMock()
        show_resp.status_code = 200
        show_resp.json.return_value = {
            "model_info": {"qwen3moe.context_length": 262144},
            "details": {"family": "qwen3moe"},
        }
        show_resp.raise_for_status.return_value = None

        probe_resp = MagicMock()
        probe_resp.status_code = 200
        probe_resp.json.return_value = {
            "message": {
                "content": "2 + 2 = 4",
                "thinking": "Let me calculate step by step...",
            },
        }
        probe_resp.raise_for_status.return_value = None

        mock_post.side_effect = [show_resp, probe_resp]

        caps = _detect_ollama_capabilities("http://localhost:11434", "qwen3.5:35b")
        assert caps is not None
        assert caps.supports_thinking is True
        assert caps.thinking_format == "thinking_field"

    @patch("agent_cli.providers.compat.requests.post")
    def test_detects_non_llama_architecture(self, mock_post):
        """Should detect context_length from any architecture prefix (e.g. qwen3next)."""
        show_resp = MagicMock()
        show_resp.status_code = 200
        show_resp.json.return_value = {
            "model_info": {"qwen3next.context_length": 262144},
            "details": {"family": "qwen3next"},
        }
        show_resp.raise_for_status.return_value = None

        probe_resp = MagicMock()
        probe_resp.status_code = 200
        probe_resp.json.return_value = {
            "message": {"content": "<think>reasoning</think>\nHello"},
        }
        probe_resp.raise_for_status.return_value = None

        mock_post.side_effect = [show_resp, probe_resp]

        caps = _detect_ollama_capabilities(
            "http://localhost:11434", "qwen3-coder-next:q8_0"
        )
        assert caps is not None
        assert caps.context_window == 262144

    @patch("agent_cli.providers.compat.requests.post")
    def test_returns_none_on_error(self, mock_post):
        mock_post.side_effect = Exception("Connection refused")
        caps = _detect_ollama_capabilities("http://localhost:11434", "unknown")
        assert caps is None


class TestOpenAICompatRuntimeDetection:
    @patch("agent_cli.providers.compat.requests.get")
    @patch("agent_cli.providers.compat.requests.post")
    def test_detects_thinking_with_context(self, mock_post, mock_get):
        """vLLM: /v1/models returns max_model_len + probe detects thinking."""
        # GET /v1/models → context window
        models_resp = MagicMock()
        models_resp.status_code = 200
        models_resp.json.return_value = {
            "data": [{"id": "local-model", "max_model_len": 32768}],
        }
        models_resp.raise_for_status.return_value = None
        mock_get.return_value = models_resp

        # POST /chat/completions → thinking probe
        probe_resp = MagicMock()
        probe_resp.status_code = 200
        probe_resp.json.return_value = {
            "choices": [{"message": {"content": "<think>reasoning</think>\nHello!"}}],
        }
        probe_resp.raise_for_status.return_value = None
        mock_post.return_value = probe_resp

        from agent_cli.providers.compat import _detect_openai_compat_capabilities

        caps = _detect_openai_compat_capabilities(
            "http://localhost:8080/v1", "local-model"
        )
        assert caps is not None
        assert caps.context_window == 32768
        assert caps.supports_thinking is True
        assert caps.thinking_format == "think"

    @patch("agent_cli.providers.compat.requests.get")
    @patch("agent_cli.providers.compat.requests.post")
    def test_api_key_passed_in_headers(self, mock_post, mock_get):
        """API key should be sent as Bearer token in detection requests."""
        models_resp = MagicMock()
        models_resp.status_code = 200
        models_resp.json.return_value = {
            "data": [{"id": "model", "max_model_len": 8192}],
        }
        models_resp.raise_for_status.return_value = None
        mock_get.return_value = models_resp

        probe_resp = MagicMock()
        probe_resp.status_code = 200
        probe_resp.json.return_value = {
            "choices": [{"message": {"content": "Hello!"}}],
        }
        probe_resp.raise_for_status.return_value = None
        mock_post.return_value = probe_resp

        from agent_cli.providers.compat import _detect_openai_compat_capabilities

        _detect_openai_compat_capabilities(
            "http://localhost:8080/v1", "model", api_key="test-key-123"
        )

        # Verify Authorization header in GET /v1/models
        get_headers = mock_get.call_args.kwargs.get("headers", {})
        assert get_headers.get("Authorization") == "Bearer test-key-123"

        # Verify Authorization header in POST /chat/completions
        post_headers = mock_post.call_args.kwargs.get("headers", {})
        assert post_headers.get("Authorization") == "Bearer test-key-123"

    @patch("agent_cli.providers.compat.requests.get")
    @patch("agent_cli.providers.compat.requests.post")
    def test_no_auth_header_without_key(self, mock_post, mock_get):
        """No Authorization header when api_key is empty."""
        models_resp = MagicMock()
        models_resp.status_code = 200
        models_resp.json.return_value = {"data": []}
        models_resp.raise_for_status.return_value = None
        mock_get.return_value = models_resp

        probe_resp = MagicMock()
        probe_resp.status_code = 200
        probe_resp.json.return_value = {
            "choices": [{"message": {"content": "Hi"}}],
        }
        probe_resp.raise_for_status.return_value = None
        mock_post.return_value = probe_resp

        from agent_cli.providers.compat import _detect_openai_compat_capabilities

        _detect_openai_compat_capabilities(
            "http://localhost:8080/v1", "model", api_key=""
        )

        get_headers = mock_get.call_args.kwargs.get("headers", {})
        assert "Authorization" not in get_headers

    @patch("agent_cli.providers.compat.requests.get")
    @patch("agent_cli.providers.compat.requests.post")
    def test_fallback_context_when_no_models_api(self, mock_post, mock_get):
        """Server without /v1/models → conservative 4096 default."""
        mock_get.side_effect = Exception("Not found")

        probe_resp = MagicMock()
        probe_resp.status_code = 200
        probe_resp.json.return_value = {
            "choices": [{"message": {"content": "Hello!"}}],
        }
        probe_resp.raise_for_status.return_value = None
        mock_post.return_value = probe_resp

        from agent_cli.providers.compat import _detect_openai_compat_capabilities

        caps = _detect_openai_compat_capabilities(
            "http://localhost:8080/v1", "local-model"
        )
        assert caps is not None
        assert caps.context_window == 4096  # default
        assert caps.supports_thinking is False

    @patch("agent_cli.providers.compat.requests.get")
    @patch("agent_cli.providers.compat.requests.post")
    def test_returns_none_on_probe_error(self, mock_post, mock_get):
        mock_get.side_effect = Exception("Connection refused")
        mock_post.side_effect = Exception("Connection refused")

        from agent_cli.providers.compat import _detect_openai_compat_capabilities

        caps = _detect_openai_compat_capabilities("http://localhost:8080/v1", "model")
        assert caps is None

    @patch("agent_cli.providers.compat.requests.get")
    def test_context_window_detection(self, mock_get):
        """Test _detect_openai_context_window directly."""
        models_resp = MagicMock()
        models_resp.status_code = 200
        models_resp.json.return_value = {
            "data": [
                {"id": "other-model", "max_model_len": 8192},
                {"id": "target-model", "max_model_len": 65536},
            ],
        }
        models_resp.raise_for_status.return_value = None
        mock_get.return_value = models_resp

        from agent_cli.providers.compat import _detect_openai_context_window

        ctx = _detect_openai_context_window("http://localhost:8080/v1", "target-model")
        assert ctx == 65536


class TestPromptModelCapabilities:
    def test_saves_user_input(self, monkeypatch, tmp_path):
        """Interactive prompt saves capabilities to models.json."""
        from agent_cli.main import _prompt_model_capabilities
        import agent_cli.config as config_mod

        monkeypatch.setattr(config_mod, "_GLOBAL_MODELS_PATH", tmp_path / "models.json")

        inputs = iter(["131072", "y", "8192"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))

        caps = _prompt_model_capabilities("test-model")
        assert caps is not None
        assert caps.context_window == 131072
        assert caps.supports_thinking is True
        assert caps.thinking_budget == 8192

        # Verify saved to file
        import json

        saved = json.loads((tmp_path / "models.json").read_text())
        assert "test-model" in saved["models"]
        assert saved["models"]["test-model"]["context_window"] == 131072

    def test_defaults_on_empty_input(self, monkeypatch, tmp_path):
        """Empty input uses defaults."""
        from agent_cli.main import _prompt_model_capabilities
        import agent_cli.config as config_mod

        monkeypatch.setattr(config_mod, "_GLOBAL_MODELS_PATH", tmp_path / "models.json")
        monkeypatch.setattr("builtins.input", lambda _: "")

        caps = _prompt_model_capabilities("test-model")
        assert caps is not None
        assert caps.context_window == 4096
        assert caps.supports_thinking is False

    def test_handles_ctrl_c(self, monkeypatch):
        """KeyboardInterrupt returns None."""
        from agent_cli.main import _prompt_model_capabilities

        monkeypatch.setattr(
            "builtins.input", lambda _: (_ for _ in ()).throw(KeyboardInterrupt)
        )

        caps = _prompt_model_capabilities("test-model")
        assert caps is None
