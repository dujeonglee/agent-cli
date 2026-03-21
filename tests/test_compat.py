"""Tests for agent_cli.providers.compat."""
from unittest.mock import MagicMock, patch

import pytest

from agent_cli.config import reload_registry
from agent_cli.providers.compat import (
    DEFAULT_CAPABILITIES,
    ModelCapabilities,
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
        assert caps.supports_tool_calling is False
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
        assert caps.supports_tool_calling is True
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
        caps = get_capabilities("qwen3:32b", provider="ollama", base_url="http://localhost:11434")
        assert caps.context_window == 32768  # from models.json, not runtime

    def test_unregistered_with_runtime_fallback(self):
        """Unregistered model without runtime detection → defaults."""
        caps = get_capabilities("unknown:latest")
        assert caps == DEFAULT_CAPABILITIES


class TestOllamaRuntimeDetection:
    @patch("agent_cli.providers.compat.requests.post")
    def test_detects_capabilities(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "model_info": {"llama.context_length": 8192},
            "details": {"family": "llama", "parameter_size": "8B"},
        }
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        caps = _detect_ollama_capabilities("http://localhost:11434", "llama3.1:8b-custom")
        assert caps is not None
        assert caps.context_window == 8192
        assert caps.supports_structured_output is True
        assert caps.supports_tool_calling is False
        assert caps.thinking_format == ""

    @patch("agent_cli.providers.compat.requests.post")
    def test_detects_thinking_model(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "model_info": {"llama.context_length": 32768},
            "details": {"family": "qwen3"},
        }
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        caps = _detect_ollama_capabilities("http://localhost:11434", "qwen3:14b")
        assert caps is not None
        assert caps.supports_thinking is True
        assert caps.thinking_budget > 0
        assert caps.thinking_format == "think"

    @patch("agent_cli.providers.compat.requests.post")
    def test_detects_non_llama_architecture(self, mock_post):
        """Should detect context_length from any architecture prefix (e.g. qwen3next)."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "model_info": {"qwen3next.context_length": 262144},
            "details": {"family": "qwen3next"},
        }
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        caps = _detect_ollama_capabilities("http://localhost:11434", "qwen3-coder-next:q8_0")
        assert caps is not None
        assert caps.context_window == 262144

    @patch("agent_cli.providers.compat.requests.post")
    def test_returns_none_on_error(self, mock_post):
        mock_post.side_effect = Exception("Connection refused")
        caps = _detect_ollama_capabilities("http://localhost:11434", "unknown")
        assert caps is None
