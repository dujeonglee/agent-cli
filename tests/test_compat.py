"""Tests for agent_cli.providers.compat."""

from unittest.mock import MagicMock, patch

import pytest

from agent_cli.config import reload_registry
from agent_cli.providers.compat import (
    DEFAULT_CAPABILITIES,
    get_capabilities,
    set_progress_callback,
    _detect_ollama_capabilities,
    _probe_format_support,
    _emit_progress,
)


def _ok_probe_resp(**extra) -> MagicMock:
    """Mock a successful probe /api/chat response (200 + message.content)."""
    resp = MagicMock()
    resp.status_code = 200
    base_body = {"message": {"content": "ok"}}
    base_body.update(extra)
    resp.json.return_value = base_body
    resp.raise_for_status.return_value = None
    return resp


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

        # 3 calls: /api/show (metadata), thinking probe, format probe.
        mock_post.side_effect = [show_resp, probe_resp, _ok_probe_resp()]

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

        # 3 calls: /api/show, thinking probe, format probe.
        mock_post.side_effect = [show_resp, probe_resp, _ok_probe_resp()]

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

        mock_post.side_effect = [show_resp, probe_resp, _ok_probe_resp()]

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

        mock_post.side_effect = [show_resp, probe_resp, _ok_probe_resp()]

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

        mock_post.side_effect = [show_resp, probe_resp, _ok_probe_resp()]

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
        """Server without /v1/models and an inconclusive overflow probe
        (200 = prompt fit, no number) → 128K conservative default
        (not the old 4096)."""
        mock_get.side_effect = Exception("Not found")

        # Both the overflow probe and the thinking probe hit this 200.
        probe_resp = MagicMock()
        probe_resp.status_code = 200
        probe_resp.text = ""
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
        assert caps.context_window == 131072  # 128K fallback
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


class TestFormatSupportProbe:
    """Unit tests for _probe_format_support in isolation."""

    @patch("agent_cli.providers.compat.requests.post")
    def test_clean_response_returns_true(self, mock_post):
        mock_post.return_value = _ok_probe_resp()
        assert _probe_format_support("http://localhost:11434", "qwen3:32b") is True

    @patch("agent_cli.providers.compat.requests.post")
    def test_http_500_returns_false(self, mock_post, capsys):
        """HTTP 500 (the non-streaming mlx-runner-fail shape)."""
        resp = MagicMock()
        resp.status_code = 500
        mock_post.return_value = resp
        assert _probe_format_support("http://localhost:11434", "mlx-bad:bf16") is False
        captured = capsys.readouterr()
        assert "HTTP 500" in captured.err
        assert "mlx-bad:bf16" in captured.err

    @patch("agent_cli.providers.compat.requests.post")
    def test_200_with_error_body_returns_false(self, mock_post, capsys):
        """The exact shape Ollama emits when mlx runner fails mid-request:
        HTTP 200 plus a body-level {"error": "..."}."""
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"error": "mlx runner failed: pipeline.go:129 msg=..."}
        mock_post.return_value = resp
        assert (
            _probe_format_support(
                "http://localhost:11434", "qwen3.6:35b-a3b-coding-bf16"
            )
            is False
        )
        captured = capsys.readouterr()
        assert "mlx runner failed" in captured.err

    @patch("agent_cli.providers.compat.requests.post")
    def test_connection_error_returns_false(self, mock_post, capsys):
        import requests as _requests

        mock_post.side_effect = _requests.ConnectionError("refused")
        assert _probe_format_support("http://localhost:11434", "model") is False
        captured = capsys.readouterr()
        assert "ConnectionError" in captured.err

    @patch("agent_cli.providers.compat.requests.post")
    def test_non_json_body_returns_false(self, mock_post, capsys):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.side_effect = ValueError("not json")
        mock_post.return_value = resp
        assert _probe_format_support("http://localhost:11434", "model") is False
        captured = capsys.readouterr()
        assert "non-JSON" in captured.err


class TestDetectionWiresProbeResult:
    """Integration: _detect_ollama_capabilities must carry the format probe
    result into the returned ModelCapabilities.supports_structured_output."""

    @patch("agent_cli.providers.compat.requests.post")
    def test_broken_format_sets_false(self, mock_post):
        """Emulate coding-bf16: /api/show succeeds, thinking probe
        succeeds, format probe comes back with the mlx error body."""
        show_resp = MagicMock()
        show_resp.status_code = 200
        show_resp.json.return_value = {
            "model_info": {"qwen3moe.context_length": 262144},
            "details": {"family": "qwen3moe"},
        }
        show_resp.raise_for_status.return_value = None

        thinking_resp = MagicMock()
        thinking_resp.status_code = 200
        thinking_resp.json.return_value = {
            "message": {"content": "ok", "thinking": "pondering"},
        }
        thinking_resp.raise_for_status.return_value = None

        format_fail_resp = MagicMock()
        format_fail_resp.status_code = 200
        format_fail_resp.json.return_value = {"error": "mlx runner failed: ..."}

        mock_post.side_effect = [show_resp, thinking_resp, format_fail_resp]

        caps = _detect_ollama_capabilities(
            "http://localhost:11434", "qwen3.6:35b-a3b-coding-bf16"
        )
        assert caps is not None
        # Probe failure pins the flag to False so live requests skip format.
        assert caps.supports_structured_output is False
        # Thinking detection still works regardless of format probe.
        assert caps.supports_thinking is True


class TestProgressCallback:
    """Runtime detection emits progress messages through a registered
    callback so the CLI can show the user what each probe step is
    doing (cold load + 2 probes can take 20-30s on first run)."""

    def test_emit_noop_when_no_callback(self):
        """Default: no callback registered → _emit_progress is a
        silent no-op. Backward-compat guarantee."""
        set_progress_callback(None)
        _emit_progress("should go nowhere")  # must not raise

    def test_emit_calls_registered_callback(self):
        """With a callback registered, messages flow through."""
        messages: list[str] = []
        set_progress_callback(messages.append)
        try:
            _emit_progress("first")
            _emit_progress("second")
        finally:
            set_progress_callback(None)
        assert messages == ["first", "second"]

    def test_emit_swallows_callback_exceptions(self):
        """A broken progress UI must not derail detection."""

        def broken(_msg):
            raise RuntimeError("ui is on fire")

        set_progress_callback(broken)
        try:
            # Must not propagate the RuntimeError.
            _emit_progress("hello")
        finally:
            set_progress_callback(None)

    @patch("agent_cli.providers.compat.requests.post")
    def test_detect_ollama_emits_step_messages(self, mock_post):
        """Ollama runtime detection emits one message per stage: show,
        thinking probe, format probe, plus a start/end bookend. Pins
        that the probe UI sees each step, not silence until done."""
        show_resp = MagicMock()
        show_resp.status_code = 200
        show_resp.json.return_value = {
            "model_info": {"qwen3.context_length": 32768},
            "details": {"family": "qwen3"},
        }
        show_resp.raise_for_status.return_value = None

        thinking_resp = MagicMock()
        thinking_resp.status_code = 200
        thinking_resp.json.return_value = {"message": {"content": "<think>x</think>4"}}
        thinking_resp.raise_for_status.return_value = None

        mock_post.side_effect = [show_resp, thinking_resp, _ok_probe_resp()]

        messages: list[str] = []
        set_progress_callback(messages.append)
        try:
            caps = _detect_ollama_capabilities("http://localhost:11434", "qwen3:14b")
        finally:
            set_progress_callback(None)

        assert caps is not None
        # Three distinct stages plus a start/completion bookend, all
        # naming the model so the user can tell which probe is running.
        joined = "\n".join(messages)
        assert "metadata" in joined  # /api/show
        assert "thinking" in joined  # thinking probe
        assert "format" in joined  # format probe
        assert any("qwen3:14b" in m for m in messages)

    @patch("agent_cli.providers.compat.requests.post")
    def test_cached_capability_silent(self, mock_post):
        """Cache hit (models.json entry) must NOT fire the progress
        callback — probes don't run, user shouldn't see phantom
        messages."""
        messages: list[str] = []
        set_progress_callback(messages.append)
        try:
            caps = get_capabilities("qwen3:32b")  # in default_models.json
        finally:
            set_progress_callback(None)
        assert caps.context_window == 32768  # came from registry
        assert messages == []  # no probes, no messages
        mock_post.assert_not_called()


# Verified live against an omlx server (Qwen3.6-27B-MLX-8bit, 2026-05-30).
_OMLX_OVERFLOW_400 = (
    "Prompt too long: 360012 tokens exceeds max context window of 262144 tokens"
)


class TestContextWindowProbe:
    """PR C — detect-time context-window discovery via overflow probe.

    Covers _probe_context_window_via_overflow in isolation plus the
    _detect_openai_context_window tier ordering (metadata → probe →
    128K fallback)."""

    def _resp(self, status, text=""):
        r = MagicMock()
        r.status_code = status
        r.text = text
        return r

    @patch("agent_cli.providers.compat.requests.post")
    def test_probe_parses_limit_from_overflow_400(self, mock_post):
        from agent_cli.providers.compat import _probe_context_window_via_overflow

        mock_post.return_value = self._resp(400, _OMLX_OVERFLOW_400)
        assert _probe_context_window_via_overflow("http://x/v1", "m") == 262144

    @patch("agent_cli.providers.compat.requests.post")
    def test_probe_returns_none_when_prompt_fits(self, mock_post):
        """200 means the window exceeds our probe — can't learn exact size."""
        from agent_cli.providers.compat import _probe_context_window_via_overflow

        mock_post.return_value = self._resp(200, "")
        assert _probe_context_window_via_overflow("http://x/v1", "m") is None

    @patch("agent_cli.providers.compat.requests.post")
    def test_probe_returns_none_on_overflow_without_number(self, mock_post):
        from agent_cli.providers.compat import _probe_context_window_via_overflow

        # Classified as overflow, but no parseable number.
        mock_post.return_value = self._resp(400, "context length exceeded")
        assert _probe_context_window_via_overflow("http://x/v1", "m") is None

    @patch("agent_cli.providers.compat.requests.post")
    def test_probe_returns_none_on_non_overflow_400(self, mock_post):
        from agent_cli.providers.compat import _probe_context_window_via_overflow

        mock_post.return_value = self._resp(400, "invalid_request: unknown field")
        assert _probe_context_window_via_overflow("http://x/v1", "m") is None

    @patch("agent_cli.providers.compat.requests.post")
    def test_probe_returns_none_on_connection_error(self, mock_post):
        from agent_cli.providers.compat import _probe_context_window_via_overflow

        mock_post.side_effect = Exception("Connection refused")
        assert _probe_context_window_via_overflow("http://x/v1", "m") is None

    @patch("agent_cli.providers.compat.requests.get")
    @patch("agent_cli.providers.compat.requests.post")
    def test_detect_uses_metadata_and_skips_probe(self, mock_post, mock_get):
        """When /v1/models has max_model_len, no probe POST is sent."""
        from agent_cli.providers.compat import _detect_openai_context_window

        mock_get.return_value = self._resp(200, "")
        mock_get.return_value.json.return_value = {
            "data": [{"id": "m", "max_model_len": 32768}]
        }
        assert _detect_openai_context_window("http://x/v1", "m") == 32768
        mock_post.assert_not_called()

    @patch("agent_cli.providers.compat.requests.get")
    @patch("agent_cli.providers.compat.requests.post")
    def test_detect_falls_back_to_probe(self, mock_post, mock_get):
        """No metadata → probe discovers the real limit (omlx path)."""
        from agent_cli.providers.compat import _detect_openai_context_window

        mock_get.return_value = self._resp(200, "")
        mock_get.return_value.json.return_value = {"data": []}  # model absent
        mock_post.return_value = self._resp(400, _OMLX_OVERFLOW_400)
        assert _detect_openai_context_window("http://x/v1", "m") == 262144

    @patch("agent_cli.providers.compat.requests.get")
    @patch("agent_cli.providers.compat.requests.post")
    def test_detect_falls_back_to_128k_when_probe_fails(self, mock_post, mock_get):
        """No metadata + probe yields nothing → 128K (not the old 4096)."""
        from agent_cli.providers.compat import _detect_openai_context_window

        mock_get.return_value = self._resp(200, "")
        mock_get.return_value.json.return_value = {"data": []}
        mock_post.return_value = self._resp(200, "")  # prompt fit → no number
        assert _detect_openai_context_window("http://x/v1", "m") == 131072
