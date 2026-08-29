"""Tests for agent_cli.providers.capabilities."""

from unittest.mock import MagicMock, patch

import pytest

from agent_cli.config import reload_registry
from agent_cli.providers.capabilities import (
    DEFAULT_CAPABILITIES,
    DEFAULT_CONTEXT_WINDOW,
    MIN_CONTEXT_WINDOW,
    UnsupportedModelError,
    _detect_openai_capabilities,
    _emit_progress,
    get_capabilities,
    set_progress_callback,
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

    def test_unregistered_model(self):
        caps = get_capabilities("unknown-model:latest")
        assert caps == DEFAULT_CAPABILITIES
        assert caps.context_window == DEFAULT_CONTEXT_WINDOW

    def test_default_window_is_not_below_the_supported_minimum(self):
        """The fallback used to be 4096 — below MIN_CONTEXT_WINDOW, which
        detection hard-rejects, and small enough that oversized_cap
        (= window/10) dropped virtually every observation as over-cap."""
        assert DEFAULT_CONTEXT_WINDOW >= MIN_CONTEXT_WINDOW
        assert DEFAULT_CAPABILITIES.context_window == DEFAULT_CONTEXT_WINDOW

    def test_openai_model(self):
        caps = get_capabilities("gpt-4o")
        assert caps.context_window == 128000

    def test_frozen(self):
        caps = get_capabilities("gpt-4o")
        with pytest.raises(AttributeError):
            caps.context_window = 9999  # type: ignore

    def test_thinking_format_field_removed(self):
        """v8.19.0: thinking_format 필드는 제거됨 (요청 미형성 순수 메타).
        dataclass 에 필드가 없고, caps_to_entry 도 키를 쓰지 않으며,
        _detect_thinking 은 bool 만 반환한다."""
        from agent_cli.providers.capabilities import (
            _detect_thinking,
            caps_to_entry,
        )

        caps = get_capabilities("gpt-4o")
        assert not hasattr(caps, "thinking_format")
        assert "thinking_format" not in caps_to_entry(caps)
        # 태그 감지는 supports_thinking 판정용 bool 로 유지.
        assert _detect_thinking("<think>x</think>") is True
        assert _detect_thinking("no tags here") is False

    def test_legacy_thinking_format_key_ignored(self):
        """구 models.json 의 thinking_format 키는 로더가 무시 (무해)."""
        from agent_cli.providers.capabilities import _build_from_entry

        caps = _build_from_entry(
            {"context_window": 32768, "thinking_format": "reasoning"}
        )
        assert caps.context_window == 32768
        assert not hasattr(caps, "thinking_format")

    def test_thinking_budget_field_removed(self):
        """v8.21.0: 정적 thinking_budget 필드 제거 — supports_thinking 단독
        게이트로 전환, 사고 예산은 런타임 effort→budget 번역이 담당.
        구 models.json 의 thinking_budget 키는 로더가 무시(무해)."""
        from agent_cli.providers.capabilities import (
            _build_from_entry,
            caps_to_entry,
        )

        caps = get_capabilities("gpt-4o")
        assert not hasattr(caps, "thinking_budget")
        assert "thinking_budget" not in caps_to_entry(caps)
        # 구 키가 있어도 무시하고 로드.
        legacy = _build_from_entry(
            {"context_window": 32768, "supports_thinking": True, "thinking_budget": 999}
        )
        assert legacy.supports_thinking is True
        assert not hasattr(legacy, "thinking_budget")

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

    @staticmethod
    def _models_resp():
        m = MagicMock(status_code=200)
        m.json.return_value = {"data": [{"id": "local-model", "max_model_len": 32768}]}
        m.raise_for_status.return_value = None
        return m

    @staticmethod
    def _chat_resp(message: dict):
        r = MagicMock(status_code=200)
        r.json.return_value = {"choices": [{"message": message}]}
        r.raise_for_status.return_value = None
        return r

    @patch("agent_cli.providers.capabilities.requests.get")
    @patch("agent_cli.providers.capabilities.requests.post")
    def test_thinking_reprobe_with_enable_thinking(self, mock_post, mock_get):
        """기본 프로브가 사고 미검출이어도, enable_thinking=true 재프로브로 감지
        (Qwen 등 기본 OFF 모델). 2차 요청 body 에 chat_template_kwargs 포함."""
        mock_get.return_value = self._models_resp()
        # 1차: 기본 → 사고 없음. 2차: enable_thinking → <think> 등장.
        mock_post.side_effect = [
            self._chat_resp({"content": "Hello!"}),
            self._chat_resp({"content": "<think>reasoning</think>\nHi"}),
        ]

        from agent_cli.providers.capabilities import _detect_openai_capabilities

        caps = _detect_openai_capabilities("http://localhost:8080/v1", "local-model")
        assert caps is not None
        assert caps.supports_thinking is True
        # 두 번 프로브했고, 2차만 enable_thinking 스위치를 켰다.
        assert mock_post.call_count == 2
        second_body = mock_post.call_args_list[1].kwargs["json"]
        assert second_body["chat_template_kwargs"] == {"enable_thinking": True}
        first_body = mock_post.call_args_list[0].kwargs["json"]
        assert "chat_template_kwargs" not in first_body

    @patch("agent_cli.providers.capabilities.requests.get")
    @patch("agent_cli.providers.capabilities.requests.post")
    def test_thinking_detected_via_reasoning_content(self, mock_post, mock_get):
        """vLLM reasoning 파서: 사고가 content 가 아닌 reasoning_content 필드로 와도
        (content 엔 <think> 없음) 정규화로 감지 — 재프로브 없이 1차에서."""
        mock_get.return_value = self._models_resp()
        mock_post.return_value = self._chat_resp(
            {"content": "Hi", "reasoning_content": "let me think"}
        )

        from agent_cli.providers.capabilities import _detect_openai_capabilities

        caps = _detect_openai_capabilities("http://localhost:8080/v1", "local-model")
        assert caps is not None
        assert caps.supports_thinking is True
        assert mock_post.call_count == 1  # 1차에서 검출 → 재프로브 없음

    @patch("agent_cli.providers.capabilities.requests.get")
    @patch("agent_cli.providers.capabilities.requests.post")
    def test_thinking_reprobe_error_is_tolerated(self, mock_post, mock_get):
        """재프로브가 실패(서버가 chat_template_kwargs 거부 등)해도 검출 전체를
        깨지 않고 '미지원'으로 판정한다 (None 아님)."""
        mock_get.return_value = self._models_resp()
        mock_post.side_effect = [
            self._chat_resp({"content": "Hello!"}),  # 1차: 사고 없음
            Exception("400 chat_template_kwargs rejected"),  # 2차: 재프로브 실패
        ]

        from agent_cli.providers.capabilities import _detect_openai_capabilities

        caps = _detect_openai_capabilities("http://localhost:8080/v1", "local-model")
        assert caps is not None
        assert caps.supports_thinking is False

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
        import agent_cli.config as config_mod
        from agent_cli.main import _prompt_model_capabilities

        monkeypatch.setattr(config_mod, "_GLOBAL_MODELS_PATH", tmp_path / "models.json")

        # context, supports_thinking(y), wire format auto("") — budget 프롬프트는
        # v8.21.0 에서 제거(정적 thinking_budget 필드 삭제).
        inputs = iter(["131072", "y", ""])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))

        caps = _prompt_model_capabilities("test-model")
        assert caps is not None
        assert caps.context_window == 131072
        assert caps.supports_thinking is True

        # Verify saved to file
        import json

        saved = json.loads((tmp_path / "models.json").read_text())
        assert "test-model" in saved["models"]
        assert saved["models"]["test-model"]["context_window"] == 131072

    def test_defaults_on_empty_input(self, monkeypatch, tmp_path):
        """Empty input uses defaults."""
        import agent_cli.config as config_mod
        from agent_cli.main import _prompt_model_capabilities

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


class TestPromptWireFormatBinding:
    """바인딩 UX ② (multi-wire-format): 대화형 모델 등록 지점에 wire format
    질문 한 줄 — 엔트리 생성 위치 = 발생 원인 위치. auto/빈 입력 = 필드
    미기록 (해석 체인 위임), 등록된 이름만 수용 (오타는 재질문)."""

    def _saved_entry(self, tmp_path):
        import json

        saved = json.loads((tmp_path / "models.json").read_text())
        return saved["models"]["test-model"]

    def test_named_format_saved_as_binding(self, monkeypatch, tmp_path):
        import agent_cli.config as config_mod
        from agent_cli.main import _prompt_model_capabilities

        monkeypatch.setattr(config_mod, "_GLOBAL_MODELS_PATH", tmp_path / "models.json")
        inputs = iter(["131072", "n", "xml_fc"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))

        caps = _prompt_model_capabilities("test-model")
        assert caps is not None
        assert self._saved_entry(tmp_path)["wire_format"] == "xml_fc"

    def test_auto_omits_field(self, monkeypatch, tmp_path):
        import agent_cli.config as config_mod
        from agent_cli.main import _prompt_model_capabilities

        monkeypatch.setattr(config_mod, "_GLOBAL_MODELS_PATH", tmp_path / "models.json")
        inputs = iter(["4096", "n", "auto"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))

        assert _prompt_model_capabilities("test-model") is not None
        assert "wire_format" not in self._saved_entry(tmp_path)

    def test_empty_omits_field(self, monkeypatch, tmp_path):
        import agent_cli.config as config_mod
        from agent_cli.main import _prompt_model_capabilities

        monkeypatch.setattr(config_mod, "_GLOBAL_MODELS_PATH", tmp_path / "models.json")
        monkeypatch.setattr("builtins.input", lambda _: "")

        assert _prompt_model_capabilities("test-model") is not None
        assert "wire_format" not in self._saved_entry(tmp_path)

    def test_unknown_name_reprompts_until_valid(self, monkeypatch, tmp_path):
        import agent_cli.config as config_mod
        from agent_cli.main import _prompt_model_capabilities

        monkeypatch.setattr(config_mod, "_GLOBAL_MODELS_PATH", tmp_path / "models.json")
        inputs = iter(["4096", "n", "xml_fx", "json_fc"])  # 오타 → 재질문
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))

        assert _prompt_model_capabilities("test-model") is not None
        assert self._saved_entry(tmp_path)["wire_format"] == "json_fc"

    def test_case_normalized(self, monkeypatch, tmp_path):
        import agent_cli.config as config_mod
        from agent_cli.main import _prompt_model_capabilities

        monkeypatch.setattr(config_mod, "_GLOBAL_MODELS_PATH", tmp_path / "models.json")
        inputs = iter(["4096", "n", "XML_FC"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))

        assert _prompt_model_capabilities("test-model") is not None
        assert self._saved_entry(tmp_path)["wire_format"] == "xml_fc"


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

    @patch("agent_cli.providers.capabilities.requests.get")
    @patch("agent_cli.providers.capabilities.requests.post")
    def test_thinking_reprobe_with_thinking_block(self, mock_post, mock_get):
        """2단계 재프로브 (transport 공통 계약 — 리뷰 §4.2 수리): ① 기본
        프로브 미검출 → ② thinking 블록을 켠 재프로브에서 사고가 나오면
        supports_thinking=True. 재프로브 요청은 Anthropic 방언의 스위치
        (thinking 블록, budget 하한 1024 + max_tokens 증액)를 실어야 한다."""
        mg = MagicMock(status_code=200)
        mg.json.return_value = {"data": [{"id": "m", "max_model_len": 200000}]}
        mg.raise_for_status.return_value = None
        mock_get.return_value = mg

        plain = MagicMock(status_code=200)
        plain.json.return_value = {"content": [{"type": "text", "text": "Hi"}]}
        plain.raise_for_status.return_value = None
        thinky = MagicMock(status_code=200)
        thinky.json.return_value = {
            "content": [
                {"type": "thinking", "thinking": "pondering"},
                {"type": "text", "text": "Hi"},
            ]
        }
        thinky.raise_for_status.return_value = None
        mock_post.side_effect = [plain, thinky]

        from agent_cli.providers.capabilities import _detect_runtime_capabilities

        caps = _detect_runtime_capabilities("anthropic", "http://x/v1", "m", "")
        assert caps.supports_thinking is True
        # 재프로브 body 에 thinking 스위치 + budget 만큼 증액된 max_tokens
        reprobe_body = mock_post.call_args_list[1].kwargs["json"]
        assert reprobe_body["thinking"] == {"type": "enabled", "budget_tokens": 1024}
        assert reprobe_body["max_tokens"] == 512 + 1024
        # 1차 프로브는 스위치 없음
        assert "thinking" not in mock_post.call_args_list[0].kwargs["json"]

    @patch("agent_cli.providers.capabilities.requests.get")
    @patch("agent_cli.providers.capabilities.requests.post")
    def test_thinking_reprobe_failure_tolerated(self, mock_post, mock_get):
        """재프로브 실패(비사고 모델이 thinking 블록에 400 등)는 관용 —
        검출 전체를 깨지 않고 supports_thinking=False 로 강등 (OpenAI
        transport 와 동형 계약)."""
        mg = MagicMock(status_code=200)
        mg.json.return_value = {"data": [{"id": "m", "max_model_len": 200000}]}
        mg.raise_for_status.return_value = None
        mock_get.return_value = mg

        plain = MagicMock(status_code=200)
        plain.json.return_value = {"content": [{"type": "text", "text": "Hi"}]}
        plain.raise_for_status.return_value = None
        mock_post.side_effect = [plain, Exception("400 thinking not supported")]

        from agent_cli.providers.capabilities import _detect_runtime_capabilities

        caps = _detect_runtime_capabilities("anthropic", "http://x/v1", "m", "")
        assert caps is not None
        assert caps.supports_thinking is False

    def test_anthropic_text_skips_leading_thinking_block(self):
        """_anthropic_text 는 첫 **text 타입** 블록을 찾는다 — thinking 활성
        응답(blocks[0]=thinking)에서 위치 고정 인덱싱이 빈 문자열을 돌려주던
        것의 수리."""
        from agent_cli.providers.capabilities import _anthropic_text

        assert (
            _anthropic_text(
                {
                    "content": [
                        {"type": "thinking", "thinking": "r"},
                        {"type": "text", "text": "Hello"},
                    ]
                }
            )
            == "Hello"
        )
        assert _anthropic_text({"content": [{"type": "text", "text": "x"}]}) == "x"
        assert _anthropic_text({"content": []}) == ""

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
