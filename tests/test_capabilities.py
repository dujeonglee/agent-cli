"""Tests for agent_cli.providers.capabilities."""

from unittest.mock import MagicMock, patch

import pytest

from agent_cli.config import reload_registry
from agent_cli.providers.capabilities import (
    DEFAULT_CAPABILITIES,
    MIN_CONTEXT_WINDOW,
    UnsupportedModelError,
    get_capabilities,
    set_progress_callback,
    _detect_openai_capabilities,
    _emit_progress,
    _probe_structured_output,
)


def _chat_resp(content: str) -> MagicMock:
    """Build a mock /chat/completions response carrying ``content``."""
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"choices": [{"message": {"content": content}}]}
    resp.raise_for_status.return_value = None
    return resp


@pytest.fixture(autouse=True)
def _clear_cache():
    reload_registry()
    yield
    reload_registry()


class TestGetCapabilities:
    def test_registered_model(self):
        caps = get_capabilities("claude-sonnet-4-20250514")
        assert caps.context_window == 200000
        assert caps.max_output_tokens == 8192
        assert caps.supports_thinking is True
        assert caps.thinking_budget == 16384

    def test_unregistered_model(self):
        caps = get_capabilities("unknown-model:latest")
        assert caps == DEFAULT_CAPABILITIES
        assert caps.context_window == 4096
        assert caps.supports_structured_output is False

    def test_openai_model(self):
        caps = get_capabilities("gpt-4o")
        assert caps.supports_structured_output is True
        assert caps.context_window == 128000

    def test_thinking_format_empty_for_non_thinking(self):
        caps = get_capabilities("gpt-4o")
        assert caps.thinking_format == ""

    def test_thinking_format_empty_for_anthropic(self):
        caps = get_capabilities("claude-sonnet-4-20250514")
        assert caps.thinking_format == ""

    def test_frozen(self):
        caps = get_capabilities("gpt-4o")
        with pytest.raises(AttributeError):
            caps.context_window = 9999  # type: ignore

    def test_static_registry_takes_priority(self):
        """models.json entry should override runtime detection."""
        caps = get_capabilities(
            "gpt-4o", provider="openai", base_url="http://localhost:8000/v1"
        )
        assert caps.context_window == 128000  # from models.json, not runtime

    def test_unregistered_with_runtime_fallback(self):
        """Unregistered model without runtime detection → defaults."""
        caps = get_capabilities("unknown:latest")
        assert caps == DEFAULT_CAPABILITIES


class TestOpenAIRuntimeDetection:
    @patch("agent_cli.providers.capabilities.requests.get")
    @patch("agent_cli.providers.capabilities.requests.post")
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

        from agent_cli.providers.capabilities import _detect_openai_capabilities

        caps = _detect_openai_capabilities("http://localhost:8080/v1", "local-model")
        assert caps is not None
        assert caps.context_window == 32768
        assert caps.supports_thinking is True
        assert caps.thinking_format == "think"

    @patch("agent_cli.providers.capabilities.requests.get")
    @patch("agent_cli.providers.capabilities.requests.post")
    def test_api_key_passed_in_headers(self, mock_post, mock_get):
        """API key should be sent as Bearer token in detection requests."""
        models_resp = MagicMock()
        models_resp.status_code = 200
        models_resp.json.return_value = {
            "data": [{"id": "model", "max_model_len": 32768}],
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

        from agent_cli.providers.capabilities import _detect_openai_capabilities

        _detect_openai_capabilities(
            "http://localhost:8080/v1", "model", api_key="test-key-123"
        )

        # Verify Authorization header in GET /v1/models
        get_headers = mock_get.call_args.kwargs.get("headers", {})
        assert get_headers.get("Authorization") == "Bearer test-key-123"

        # Verify Authorization header in POST /chat/completions
        post_headers = mock_post.call_args.kwargs.get("headers", {})
        assert post_headers.get("Authorization") == "Bearer test-key-123"

    @patch("agent_cli.providers.capabilities.requests.get")
    @patch("agent_cli.providers.capabilities.requests.post")
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

        from agent_cli.providers.capabilities import _detect_openai_capabilities

        _detect_openai_capabilities("http://localhost:8080/v1", "model", api_key="")

        get_headers = mock_get.call_args.kwargs.get("headers", {})
        assert "Authorization" not in get_headers

    @patch("agent_cli.providers.capabilities.requests.get")
    @patch("agent_cli.providers.capabilities.requests.post")
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

        from agent_cli.providers.capabilities import _detect_openai_capabilities

        caps = _detect_openai_capabilities("http://localhost:8080/v1", "local-model")
        assert caps is not None
        assert caps.context_window == 131072  # 128K fallback
        assert caps.supports_thinking is False

    @patch("agent_cli.providers.capabilities.requests.get")
    @patch("agent_cli.providers.capabilities.requests.post")
    def test_returns_none_on_probe_error(self, mock_post, mock_get):
        mock_get.side_effect = Exception("Connection refused")
        mock_post.side_effect = Exception("Connection refused")

        from agent_cli.providers.capabilities import _detect_openai_capabilities

        caps = _detect_openai_capabilities("http://localhost:8080/v1", "model")
        assert caps is None

    @patch("agent_cli.providers.capabilities.requests.get")
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

        from agent_cli.providers.capabilities import _detect_openai_context_window

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

    @patch("agent_cli.providers.capabilities.requests.post")
    def test_cached_capability_silent(self, mock_post):
        """Cache hit (models.json entry) must NOT fire the progress
        callback — probes don't run, user shouldn't see phantom
        messages."""
        messages: list[str] = []
        set_progress_callback(messages.append)
        try:
            caps = get_capabilities("gpt-4o")  # in default_models.json
        finally:
            set_progress_callback(None)
        assert caps.context_window == 128000  # came from registry
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

    @patch("agent_cli.providers.capabilities.requests.post")
    def test_probe_parses_limit_from_overflow_400(self, mock_post):
        from agent_cli.providers.capabilities import _probe_context_window_via_overflow

        mock_post.return_value = self._resp(400, _OMLX_OVERFLOW_400)
        assert _probe_context_window_via_overflow("http://x/v1", "m") == 262144

    @patch("agent_cli.providers.capabilities.requests.post")
    def test_probe_returns_none_when_prompt_fits(self, mock_post):
        """200 means the window exceeds our probe — can't learn exact size."""
        from agent_cli.providers.capabilities import _probe_context_window_via_overflow

        mock_post.return_value = self._resp(200, "")
        assert _probe_context_window_via_overflow("http://x/v1", "m") is None

    @patch("agent_cli.providers.capabilities.requests.post")
    def test_probe_returns_none_on_overflow_without_number(self, mock_post):
        from agent_cli.providers.capabilities import _probe_context_window_via_overflow

        # Classified as overflow, but no parseable number.
        mock_post.return_value = self._resp(400, "context length exceeded")
        assert _probe_context_window_via_overflow("http://x/v1", "m") is None

    @patch("agent_cli.providers.capabilities.requests.post")
    def test_probe_returns_none_on_non_overflow_400(self, mock_post):
        from agent_cli.providers.capabilities import _probe_context_window_via_overflow

        mock_post.return_value = self._resp(400, "invalid_request: unknown field")
        assert _probe_context_window_via_overflow("http://x/v1", "m") is None

    @patch("agent_cli.providers.capabilities.requests.post")
    def test_probe_returns_none_on_connection_error(self, mock_post):
        from agent_cli.providers.capabilities import _probe_context_window_via_overflow

        mock_post.side_effect = Exception("Connection refused")
        assert _probe_context_window_via_overflow("http://x/v1", "m") is None

    @patch("agent_cli.providers.capabilities.requests.get")
    @patch("agent_cli.providers.capabilities.requests.post")
    def test_detect_uses_metadata_and_skips_probe(self, mock_post, mock_get):
        """When /v1/models has max_model_len, no probe POST is sent."""
        from agent_cli.providers.capabilities import _detect_openai_context_window

        mock_get.return_value = self._resp(200, "")
        mock_get.return_value.json.return_value = {
            "data": [{"id": "m", "max_model_len": 32768}]
        }
        assert _detect_openai_context_window("http://x/v1", "m") == 32768
        mock_post.assert_not_called()

    @patch("agent_cli.providers.capabilities.requests.get")
    @patch("agent_cli.providers.capabilities.requests.post")
    def test_detect_falls_back_to_probe(self, mock_post, mock_get):
        """No metadata → probe discovers the real limit (omlx path)."""
        from agent_cli.providers.capabilities import _detect_openai_context_window

        mock_get.return_value = self._resp(200, "")
        mock_get.return_value.json.return_value = {"data": []}  # model absent
        mock_post.return_value = self._resp(400, _OMLX_OVERFLOW_400)
        assert _detect_openai_context_window("http://x/v1", "m") == 262144

    @patch("agent_cli.providers.capabilities.requests.get")
    @patch("agent_cli.providers.capabilities.requests.post")
    def test_detect_falls_back_to_128k_when_probe_fails(self, mock_post, mock_get):
        """No metadata + probe yields nothing → 128K (not the old 4096)."""
        from agent_cli.providers.capabilities import _detect_openai_context_window

        mock_get.return_value = self._resp(200, "")
        mock_get.return_value.json.return_value = {"data": []}
        mock_post.return_value = self._resp(200, "")  # prompt fit → no number
        assert _detect_openai_context_window("http://x/v1", "m") == 131072


class TestModelRejectAndOutputScaling:
    """Auto-detect: output = context_window // 4 (no 4096 cap); context
    below MIN_CONTEXT_WINDOW (16K) is rejected with UnsupportedModelError."""

    @patch("agent_cli.providers.capabilities.requests.get")
    @patch("agent_cli.providers.capabilities.requests.post")
    def test_openai_output_is_context_over_4(self, mock_post, mock_get):
        models = MagicMock(status_code=200)
        models.json.return_value = {"data": [{"id": "big", "max_model_len": 262144}]}
        models.raise_for_status.return_value = None
        mock_get.return_value = models
        probe = MagicMock(status_code=200)
        probe.json.return_value = {"choices": [{"message": {"content": "hi"}}]}
        probe.raise_for_status.return_value = None
        mock_post.return_value = probe
        caps = _detect_openai_capabilities("http://x/v1", "big")
        assert caps.max_output_tokens == 262144 // 4

    @patch("agent_cli.providers.capabilities.requests.get")
    @patch("agent_cli.providers.capabilities.requests.post")
    def test_openai_small_context_rejected(self, mock_post, mock_get):
        models = MagicMock(status_code=200)
        models.json.return_value = {
            "data": [{"id": "tiny", "max_model_len": MIN_CONTEXT_WINDOW - 1}]
        }
        models.raise_for_status.return_value = None
        mock_get.return_value = models
        probe = MagicMock(status_code=200)
        probe.json.return_value = {"choices": [{"message": {"content": "hi"}}]}
        probe.raise_for_status.return_value = None
        mock_post.return_value = probe
        with pytest.raises(UnsupportedModelError):
            _detect_openai_capabilities("http://x/v1", "tiny")

    @patch("agent_cli.providers.capabilities.requests.get")
    @patch("agent_cli.providers.capabilities.requests.post")
    def test_exactly_min_is_accepted(self, mock_post, mock_get):
        """Boundary: context == MIN_CONTEXT_WINDOW is allowed (>= , not >)."""
        models = MagicMock(status_code=200)
        models.json.return_value = {
            "data": [{"id": "edge", "max_model_len": MIN_CONTEXT_WINDOW}]
        }
        models.raise_for_status.return_value = None
        mock_get.return_value = models
        probe = MagicMock(status_code=200)
        probe.json.return_value = {"choices": [{"message": {"content": "hi"}}]}
        probe.raise_for_status.return_value = None
        mock_post.return_value = probe
        caps = _detect_openai_capabilities("http://x/v1", "edge")
        assert caps is not None
        assert caps.max_output_tokens == MIN_CONTEXT_WINDOW // 4


class TestStructuredOutputProbe:
    """Unit tests for _probe_structured_output (json_object + json_schema)."""

    HEADERS = {"Content-Type": "application/json"}

    @patch("agent_cli.providers.capabilities.requests.post")
    def test_both_supported(self, mock_post):
        """json_object returns valid JSON and strict schema conforms exactly."""
        mock_post.side_effect = [
            _chat_resp('{"primary_colors": ["red", "blue", "yellow"]}'),
            _chat_resp('{"colors": ["red", "blue", "yellow"]}'),
        ]
        so, ss = _probe_structured_output("http://x/v1", "m", self.HEADERS)
        assert (so, ss) == (True, True)
        assert mock_post.call_count == 2

    @patch("agent_cli.providers.capabilities.requests.post")
    def test_json_object_only_when_schema_not_conforming(self, mock_post):
        """json_object works, but strict probe returns a non-conforming shape
        (extra/wrong keys) → strict reported False."""
        mock_post.side_effect = [
            _chat_resp('{"anything": 1}'),
            _chat_resp('{"primary_colors": ["red"]}'),  # wrong key → not strict
        ]
        so, ss = _probe_structured_output("http://x/v1", "m", self.HEADERS)
        assert (so, ss) == (True, False)

    @patch("agent_cli.providers.capabilities.requests.post")
    def test_unsupported_when_prose_returned(self, mock_post):
        """Server ignores response_format and answers in prose → both False,
        and the strict probe is never attempted."""
        mock_post.return_value = _chat_resp("The primary colors are red, blue, yellow.")
        so, ss = _probe_structured_output("http://x/v1", "m", self.HEADERS)
        assert (so, ss) == (False, False)
        assert mock_post.call_count == 1  # step B skipped

    @patch("agent_cli.providers.capabilities.requests.post")
    def test_conservative_false_on_error(self, mock_post):
        """Any transport error on the first probe → (False, False)."""
        mock_post.side_effect = Exception("connection refused")
        so, ss = _probe_structured_output("http://x/v1", "m", self.HEADERS)
        assert (so, ss) == (False, False)

    @patch("agent_cli.providers.capabilities.requests.post")
    def test_strict_probe_error_keeps_json_object(self, mock_post):
        """json_object passes, strict probe errors → (True, False)."""
        mock_post.side_effect = [
            _chat_resp('{"colors": ["red"]}'),
            Exception("schema mode unsupported"),
        ]
        so, ss = _probe_structured_output("http://x/v1", "m", self.HEADERS)
        assert (so, ss) == (True, False)


class TestDetectionWiresStructuredFlags:
    """_detect_openai_capabilities should reflect the structured-output probe."""

    @patch("agent_cli.providers.capabilities.requests.get")
    @patch("agent_cli.providers.capabilities.requests.post")
    def test_flags_set_from_probe(self, mock_post, mock_get):
        models = MagicMock(status_code=200)
        models.json.return_value = {"data": [{"id": "m", "max_model_len": 32768}]}
        models.raise_for_status.return_value = None
        mock_get.return_value = models
        # Sequence: thinking probe, json_object probe, json_schema probe.
        mock_post.side_effect = [
            _chat_resp("Hello!"),  # no <think> → thinking False
            _chat_resp('{"colors": ["red", "blue", "yellow"]}'),
            _chat_resp('{"colors": ["red", "blue", "yellow"]}'),
        ]
        caps = _detect_openai_capabilities("http://x/v1", "m")
        assert caps is not None
        assert caps.supports_structured_output is True
        assert caps.supports_strict_schema is True
        assert caps.supports_thinking is False


class TestAnthropicRuntimeDetection:
    """Anthropic capability probe — mirrors the OpenAI probe's logic via the
    shared orchestrator + an Anthropic transport (``/messages``, x-api-key +
    anthropic-version headers, ``content[].text`` response shape)."""

    @patch("agent_cli.providers.capabilities.requests.get")
    @patch("agent_cli.providers.capabilities.requests.post")
    def test_detects_context_and_uses_anthropic_headers(self, mock_post, mock_get):
        mg = MagicMock(status_code=200)
        mg.json.return_value = {"data": [{"id": "claude-x", "max_model_len": 200000}]}
        mg.raise_for_status.return_value = None
        mock_get.return_value = mg

        mp = MagicMock(status_code=200)
        mp.json.return_value = {"content": [{"type": "text", "text": "Hello!"}]}
        mp.raise_for_status.return_value = None
        mock_post.return_value = mp

        from agent_cli.providers.capabilities import _detect_runtime_capabilities

        caps = _detect_runtime_capabilities(
            "anthropic", "http://x/v1", "claude-x", "sk"
        )
        assert caps is not None
        assert caps.context_window == 200000
        # GET /models used anthropic auth (not Bearer)
        gh = mock_get.call_args.kwargs.get("headers", {})
        assert gh.get("x-api-key") == "sk"
        assert gh.get("anthropic-version") == "2023-06-01"
        assert "Authorization" not in gh
        # chat probe hit /messages with anthropic headers
        assert mock_post.call_args.args[0].endswith("/messages")
        ph = mock_post.call_args.kwargs.get("headers", {})
        assert ph.get("x-api-key") == "sk"

    @patch("agent_cli.providers.capabilities.requests.get")
    @patch("agent_cli.providers.capabilities.requests.post")
    def test_thinking_tag_detected_from_messages_content(self, mock_post, mock_get):
        mg = MagicMock(status_code=200)
        mg.json.return_value = {"data": [{"id": "m", "max_model_len": 200000}]}
        mg.raise_for_status.return_value = None
        mock_get.return_value = mg

        mp = MagicMock(status_code=200)
        mp.json.return_value = {
            "content": [{"type": "text", "text": "<think>r</think>\nHi"}]
        }
        mp.raise_for_status.return_value = None
        mock_post.return_value = mp

        from agent_cli.providers.capabilities import _detect_runtime_capabilities

        caps = _detect_runtime_capabilities("anthropic", "http://x/v1", "m", "")
        assert caps.supports_thinking is True
        assert caps.thinking_format == "think"

    @patch("agent_cli.providers.capabilities.requests.get")
    @patch("agent_cli.providers.capabilities.requests.post")
    def test_structured_probe_prompt_only_no_strict(self, mock_post, mock_get):
        mg = MagicMock(status_code=200)
        mg.json.return_value = {"data": [{"id": "m", "max_model_len": 200000}]}
        mg.raise_for_status.return_value = None
        mock_get.return_value = mg

        mp = MagicMock(status_code=200)
        # model emits valid JSON when asked → structured True; strict always False
        mp.json.return_value = {
            "content": [{"type": "text", "text": '{"colors": ["red"]}'}]
        }
        mp.raise_for_status.return_value = None
        mock_post.return_value = mp

        from agent_cli.providers.capabilities import _detect_runtime_capabilities

        caps = _detect_runtime_capabilities("anthropic", "http://x/v1", "m", "")
        assert caps.supports_structured_output is True
        assert caps.supports_strict_schema is False  # no OpenAI strict schema

    @patch("agent_cli.providers.capabilities.requests.get")
    @patch("agent_cli.providers.capabilities.requests.post")
    def test_overflow_probe_via_messages_when_no_metadata(self, mock_post, mock_get):
        mock_get.side_effect = Exception("no /models metadata")  # tier 1 miss

        mp = MagicMock(status_code=400)
        mp.text = "prompt is too long: 250000 tokens > 200000 maximum"
        mp.json.return_value = {"content": [{"type": "text", "text": "x"}]}
        mp.raise_for_status.return_value = None
        mock_post.return_value = mp

        from agent_cli.providers.capabilities import _detect_runtime_capabilities

        caps = _detect_runtime_capabilities("anthropic", "http://x/v1", "m", "")
        assert caps is not None
        # overflow probe hit /messages and parsed the limit
        assert any("/messages" in c.args[0] for c in mock_post.call_args_list)
        assert caps.context_window == 200000
