"""Tests for provider adapters (mocked HTTP)."""

import threading
from unittest.mock import MagicMock, patch

import pytest

from agent_cli.providers import create_provider
from agent_cli.providers.anthropic import AnthropicProvider
from agent_cli.providers.base import CallSettings, LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.providers.http import interruptible_lines
from agent_cli.providers.openai import OpenAIProvider


@pytest.fixture
def caps_structured():
    return ModelCapabilities(
        context_window=32768,
        max_output_tokens=4096,
        supports_thinking=False,
    )


@pytest.fixture
def caps_basic():
    return ModelCapabilities(
        context_window=4096,
        max_output_tokens=2048,
        supports_thinking=False,
    )


def _mock_response(json_data, status_code=200):
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = json_data
    mock.raise_for_status.return_value = None
    return mock


class TestAnthropicProvider:
    @patch("agent_cli.providers.anthropic.requests.post")
    def test_call_sends_correct_request(self, mock_post, caps_structured):
        mock_post.return_value = _mock_response(
            {
                "content": [{"type": "text", "text": '{"thought": "hi"}'}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "stop_reason": "end_turn",
            }
        )

        provider = AnthropicProvider("https://api.anthropic.com/v1", "test-key")
        result = provider.call(
            messages=[{"role": "user", "content": "hello"}],
            system="system prompt",
            model="claude-sonnet-4-20250514",
            capabilities=caps_structured,
        )

        assert isinstance(result, LLMResponse)
        assert result.content == '{"thought": "hi"}'
        assert result.usage.input_tokens == 10
        assert result.stop_reason == "stop"  # P0-1: end_turn → 정규화 어휘

        call_kwargs = mock_post.call_args
        assert "x-api-key" in call_kwargs.kwargs["headers"]
        assert call_kwargs.kwargs["json"]["max_tokens"] == 4096

    @patch("agent_cli.providers.anthropic.requests.post")
    def test_multiple_text_blocks_accumulate(self, mock_post, caps_structured):
        """다중 text/thinking 블록은 누산 — 마지막 블록만 잔존하던 종전 동작의
        수리(리뷰 §4.2). 스트리밍 경로(델타 연결)와 동형."""
        mock_post.return_value = _mock_response(
            {
                "content": [
                    {"type": "thinking", "thinking": "step1 "},
                    {"type": "text", "text": "part1 "},
                    {"type": "thinking", "thinking": "step2"},
                    {"type": "text", "text": "part2"},
                ],
                "stop_reason": "end_turn",
            }
        )
        provider = AnthropicProvider("https://api.anthropic.com/v1", "k")
        result = provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="s",
            model="claude-sonnet-4-20250514",
            capabilities=caps_structured,
        )
        assert result.content == "part1 part2"
        assert result.thinking == "step1 step2"

    @patch("agent_cli.providers.anthropic.requests.post")
    def test_inline_think_isolated_from_content(self, mock_post, caps_structured):
        """인라인 <think> 격리 — OpenAI 경로와 동형(리뷰 §4.2 수리). Anthropic-
        호환 로컬 서버(omlx 등)가 태그를 content 에 흘리면 thinking 으로 분리,
        thinking 블록이 함께 있으면 합류."""
        mock_post.return_value = _mock_response(
            {
                "content": [
                    {"type": "text", "text": "<think>inline reasoning</think>Answer"}
                ],
                "stop_reason": "end_turn",
            }
        )
        provider = AnthropicProvider("https://api.anthropic.com/v1", "k")
        result = provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="s",
            model="qwen-on-omlx",
            capabilities=caps_structured,
        )
        assert result.content == "Answer"
        assert result.thinking == "inline reasoning"

    @patch("agent_cli.providers.anthropic.requests.post")
    def test_system_sent_with_cache_control(self, mock_post, caps_structured):
        """System prompt is wrapped in a content block with cache_control."""
        mock_post.return_value = _mock_response(
            {
                "content": [{"type": "text", "text": "{}"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "stop_reason": "end_turn",
            }
        )
        provider = AnthropicProvider("https://api.anthropic.com/v1", "k")
        provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="my system",
            model="claude-sonnet-4-20250514",
            capabilities=caps_structured,
        )
        body = mock_post.call_args.kwargs["json"]
        assert body["system"] == [
            {
                "type": "text",
                "text": "my system",
                "cache_control": {"type": "ephemeral"},
            }
        ]

    @patch("agent_cli.providers.anthropic.requests.post")
    def test_cache_usage_fields_parsed(self, mock_post, caps_structured):
        """Both cache_creation and cache_read tokens flow through to TokenUsage."""
        mock_post.return_value = _mock_response(
            {
                "content": [{"type": "text", "text": "{}"}],
                "usage": {
                    "input_tokens": 5,
                    "output_tokens": 2,
                    "cache_creation_input_tokens": 100,
                    "cache_read_input_tokens": 50,
                },
                "stop_reason": "end_turn",
            }
        )
        provider = AnthropicProvider("https://api.anthropic.com/v1", "k")
        result = provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="claude-sonnet-4-20250514",
            capabilities=caps_structured,
        )
        assert result.usage.cache_creation_input_tokens == 100
        assert result.usage.cache_read_input_tokens == 50
        # input_tokens stays separate — billable/occupancy total is the sum
        assert result.usage.input_tokens == 5
        assert result.usage.total_input_tokens == 5 + 100 + 50

    @patch("agent_cli.providers.anthropic.requests.post")
    def test_cache_usage_fields_default_zero(self, mock_post, caps_structured):
        """When server omits cache fields, TokenUsage defaults to 0."""
        mock_post.return_value = _mock_response(
            {
                "content": [{"type": "text", "text": "{}"}],
                "usage": {"input_tokens": 5, "output_tokens": 2},
                "stop_reason": "end_turn",
            }
        )
        provider = AnthropicProvider("https://api.anthropic.com/v1", "k")
        result = provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="claude-sonnet-4-20250514",
            capabilities=caps_structured,
        )
        assert result.usage.cache_creation_input_tokens == 0
        assert result.usage.cache_read_input_tokens == 0
        # no cache → total_input_tokens == input_tokens (omlx/non-cache parity)
        assert result.usage.total_input_tokens == result.usage.input_tokens == 5

    @patch("agent_cli.providers.anthropic.requests.post")
    def test_interrupt_check_breaks_stream(self, mock_post, caps_structured):
        # Parity with the openai provider: a user interrupt mid-generation
        # closes the Anthropic SSE stream and skips the rest. The flag goes True
        # once the first text delta has been received (driven off on_chunk, so
        # independent of reader-thread timing), so the trailing delta is never
        # read.
        sse = [
            b'data: {"type":"message_start","message":{"usage":{"input_tokens":5}}}',
            b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"partial "}}',
            b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"NEVER_READ"}}',
            b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}',
        ]
        r = MagicMock()
        r.iter_lines.return_value = iter(sse)
        r.raise_for_status.return_value = None
        mock_post.return_value = r

        seen: list[str] = []

        provider = AnthropicProvider("https://api.anthropic.com/v1", "k")
        result = provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="claude-sonnet-4-20250514",
            capabilities=caps_structured,
            on_chunk=seen.append,
            interrupt_check=lambda: len(seen) >= 1,
        )
        assert result.stop_reason == "interrupted"
        assert result.content == "partial "
        assert "NEVER_READ" not in result.content
        r.close.assert_called_once()

    @patch("agent_cli.providers.anthropic.requests.post")
    def test_degeneration_check_breaks_stream(self, mock_post, caps_structured):
        # Parity with the openai provider: the early-break optimization is the
        # dialect's is_degenerate predicate, which is provider-independent.
        # As the streamed text accumulates into a runaway it returns True
        # mid-stream → the Anthropic stream closes, the trailing chunk is never
        # read, and the truncated content carries stop_reason="degenerate_runaway".
        sse = [
            b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"## Thought\\n## Action\\n"}}',
            b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"## Thought\\n## Action\\n"}}',
            b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"NEVER_READ"}}',
            b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}',
        ]
        r = MagicMock()
        r.iter_lines.return_value = iter(sse)
        r.raise_for_status.return_value = None
        mock_post.return_value = r

        provider = AnthropicProvider("https://api.anthropic.com/v1", "k")
        result = provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="claude-sonnet-4-20250514",
            capabilities=caps_structured,
            on_chunk=lambda c: None,
            degeneration_check=lambda t: t.count("## Action") >= 2,
        )
        assert result.stop_reason == "degenerate_runaway"
        assert "NEVER_READ" not in result.content
        r.close.assert_called_once()


class TestStopReasonNormalization:
    """P0-1: LLMResponse.stop_reason 은 루프 어휘로 정규화 — Anthropic 원어
    ``max_tokens`` 가 그대로 흐르면 loop 의 출력-절단 가드(``== "length"``)가
    무발화해 잘린 write_file/shell 이 디스패치되던 실사고의 계약 고정."""

    def _call(self, caps, stop_reason):
        with patch("agent_cli.providers.anthropic.requests.post") as mock_post:
            mock_post.return_value = _mock_response(
                {
                    "content": [{"type": "text", "text": "x"}],
                    "stop_reason": stop_reason,
                }
            )
            provider = AnthropicProvider("https://api.anthropic.com/v1", "k")
            return provider.call(
                messages=[{"role": "user", "content": "hi"}],
                system="s",
                model="m",
                capabilities=caps,
            )

    def test_max_tokens_maps_to_length(self, caps_structured):
        # 절단 가드가 실제로 발화하게 되는 핵심 매핑.
        assert self._call(caps_structured, "max_tokens").stop_reason == "length"

    def test_end_turn_and_stop_sequence_map_to_stop(self, caps_structured):
        assert self._call(caps_structured, "end_turn").stop_reason == "stop"
        assert self._call(caps_structured, "stop_sequence").stop_reason == "stop"

    def test_unknown_and_none_pass_through(self, caps_structured):
        assert self._call(caps_structured, "tool_use").stop_reason == "tool_use"
        assert self._call(caps_structured, None).stop_reason is None

    @patch("agent_cli.providers.anthropic.requests.post")
    def test_streaming_path_also_normalized(self, mock_post, caps_structured):
        # 스트리밍 경로(message_delta 의 stop_reason)도 같은 매핑을 타야 한다.
        sse = [
            b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hi"}}',
            b'data: {"type":"message_delta","delta":{"stop_reason":"max_tokens"},"usage":{"output_tokens":2}}',
        ]
        r = MagicMock()
        r.iter_lines.return_value = iter(sse)
        r.raise_for_status.return_value = None
        mock_post.return_value = r
        provider = AnthropicProvider("https://api.anthropic.com/v1", "k")
        result = provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="s",
            model="m",
            capabilities=caps_structured,
            on_chunk=lambda c: None,
        )
        assert result.stop_reason == "length"


class TestDegenerationTrigger:
    """P0-4: 조기종료 게이트 문자는 wire shape 소유 — 종전 '#' 하드코딩은
    xml_fc(<tool_call> 반복 러너웨이)에서 조기종료를 구조적으로 무발화시켰다."""

    def test_dialects_declare_trigger(self):
        from agent_cli.dialects import get as get_wire

        assert get_wire("json_fc").degeneration_trigger == "#"
        assert get_wire("xml_fc").degeneration_trigger == "<"

    def test_llm_caller_passes_wire_trigger(self):
        # llm.py 가 wire 의 트리거를 provider.call 로 전달하는 배선 고정.
        import inspect

        from agent_cli.loop import llm as llm_mod

        src = inspect.getsource(llm_mod)
        assert "degeneration_trigger" in src
        assert "getattr(\n                    self.cfg.dialect" in src

    @patch("agent_cli.providers.anthropic.requests.post")
    def test_custom_trigger_fires_early_stop(self, mock_post, caps_structured):
        # '#' 없는 xml 러너웨이 청크 — 트리거 '<' 로 조기종료가 걸려야 한다
        # (종전 하드코딩에선 predicate 자체가 호출되지 않아 무발화).
        sse = [
            b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"<tool_call></tool_call>"}}',
            b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"<tool_call></tool_call>"}}',
            b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"NEVER_READ"}}',
            b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}',
        ]
        r = MagicMock()
        r.iter_lines.return_value = iter(sse)
        r.raise_for_status.return_value = None
        mock_post.return_value = r
        provider = AnthropicProvider("https://api.anthropic.com/v1", "k")
        result = provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="s",
            model="m",
            capabilities=caps_structured,
            on_chunk=lambda c: None,
            degeneration_check=lambda t: t.count("<tool_call>") >= 2,
            degeneration_trigger="<",
        )
        assert result.stop_reason == "degenerate_runaway"
        assert "NEVER_READ" not in result.content


class TestOpenAIProvider:
    @patch("agent_cli.providers.openai.requests.post")
    def test_thinking_switch_is_always_explicit_and_effort_never_sent(
        self, mock_post, caps_thinking
    ):
        """v10.72.0: 세션 사고 스위치(on/off) → ``chat_template_kwargs.enable_thinking``
        으로 **항상 명시**(Qwen 템플릿은 스위치가 없으면 사고를 켠다, v9.24.8).
        ``reasoning_effort`` 는 어느 경우에도 보내지 않는다 — 종전엔 켬이면 기본
        ``medium`` 을 실었는데 GLM 템플릿이 그것을 ``Max`` 로 읽었다(실측)."""
        r = MagicMock()
        r.raise_for_status.return_value = None
        r.json.return_value = {
            "choices": [{"message": {"content": "[]"}, "finish_reason": "stop"}]
        }
        mock_post.return_value = r
        provider = OpenAIProvider("https://api.openai.com/v1", "k")
        for flag in (False, True):
            provider.call(
                messages=[{"role": "user", "content": "hi"}],
                system="s",
                model="m",
                capabilities=caps_thinking,
                settings=CallSettings(thinking=flag),
            )
            body = mock_post.call_args.kwargs["json"]
            assert body["chat_template_kwargs"] == {"enable_thinking": flag}
            assert "reasoning_effort" not in body
        # settings 를 안 주면 CallSettings 기본 = 끔 (ctx 기본과 같다)
        provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="s",
            model="m",
            capabilities=caps_thinking,
        )
        body = mock_post.call_args.kwargs["json"]
        assert body["chat_template_kwargs"] == {"enable_thinking": False}

    @patch("agent_cli.providers.openai.requests.post")
    def test_switch_not_sent_when_not_supported(self, mock_post, caps_structured):
        """v8.21.1: supports_thinking=False 면 켬 요청도 무시 — 비추론 모델에
        chat_template_kwargs 를 보내 400 나는 것 방지."""
        r = MagicMock()
        r.raise_for_status.return_value = None
        r.json.return_value = {
            "choices": [{"message": {"content": "[]"}, "finish_reason": "stop"}]
        }
        mock_post.return_value = r
        provider = OpenAIProvider("https://api.openai.com/v1", "k")
        provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="s",
            model="m",
            capabilities=caps_structured,  # supports_thinking=False
            settings=CallSettings(thinking=True),
        )
        body = mock_post.call_args.kwargs["json"]
        assert "reasoning_effort" not in body
        assert "chat_template_kwargs" not in body

    @patch("agent_cli.providers.openai.requests.post")
    def test_degeneration_check_breaks_stream(self, mock_post, caps_structured):
        # As the streamed text accumulates into a runaway, degeneration_check
        # returns True mid-stream → the provider closes the stream and never
        # reads the trailing chunks (token/latency saving). The truncated
        # content is returned with stop_reason="degenerate_runaway".
        sse = [
            b'data: {"choices":[{"delta":{"content":"## Thought\\n## Action\\n"}}]}',
            b'data: {"choices":[{"delta":{"content":"## Thought\\n## Action\\n"}}]}',
            b'data: {"choices":[{"delta":{"content":"NEVER_READ"}}]}',
            b"data: [DONE]",
        ]
        r = MagicMock()
        r.iter_lines.return_value = iter(sse)
        r.raise_for_status.return_value = None
        mock_post.return_value = r

        chunks: list[str] = []
        provider = OpenAIProvider("https://api.openai.com/v1", "test-key")
        result = provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="m",
            capabilities=caps_structured,
            on_chunk=chunks.append,
            degeneration_check=lambda t: t.count("## Action") >= 2,
        )
        assert "NEVER_READ" not in result.content  # later chunk never read
        assert result.stop_reason == "degenerate_runaway"
        r.close.assert_called_once()

    @patch("agent_cli.providers.openai.requests.post")
    def test_no_degeneration_check_consumes_full_stream(
        self, mock_post, caps_structured
    ):
        sse = [
            b'data: {"choices":[{"delta":{"content":"## Thought\\nx\\n"}}]}',
            b'data: {"choices":[{"delta":{"content":"## Action\\nshell"}}]}',
            b"data: [DONE]",
        ]
        r = MagicMock()
        r.iter_lines.return_value = iter(sse)
        r.raise_for_status.return_value = None
        mock_post.return_value = r
        provider = OpenAIProvider("https://api.openai.com/v1", "test-key")
        result = provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="m",
            capabilities=caps_structured,
            on_chunk=lambda c: None,
        )
        assert "## Action\nshell" in result.content

    @patch("agent_cli.providers.openai.requests.post")
    def test_interrupt_check_breaks_stream(self, mock_post, caps_structured):
        # User interrupt (Ctrl+C / web stop) mid-generation. interruptible_lines
        # polls interrupt_check before fetching each SSE line; here the flag goes
        # True once the first chunk has been received (driven off on_chunk, so
        # the assertion is independent of reader-thread timing), so the trailing
        # line is never read. (Content has NO '#', so this isn't degeneration.)
        sse = [
            b'data: {"choices":[{"delta":{"content":"partial "}}]}',
            b'data: {"choices":[{"delta":{"content":"NEVER_READ"}}]}',
            b"data: [DONE]",
        ]
        r = MagicMock()
        r.iter_lines.return_value = iter(sse)
        r.raise_for_status.return_value = None
        mock_post.return_value = r

        seen: list[str] = []

        provider = OpenAIProvider("https://api.openai.com/v1", "test-key")
        result = provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="m",
            capabilities=caps_structured,
            on_chunk=seen.append,
            interrupt_check=lambda: len(seen) >= 1,
        )
        assert result.stop_reason == "interrupted"
        assert result.content == "partial "
        assert "NEVER_READ" not in result.content
        r.close.assert_called_once()

    @patch("agent_cli.providers.openai.requests.post")
    def test_no_interrupt_consumes_full_stream(self, mock_post, caps_structured):
        # interrupt_check that never fires → stream consumed fully, no close,
        # normal stop_reason from the server (not "interrupted").
        sse = [
            b'data: {"choices":[{"delta":{"content":"hello "}}]}',
            b'data: {"choices":[{"delta":{"content":"world"},"finish_reason":"stop"}]}',
            b"data: [DONE]",
        ]
        r = MagicMock()
        r.iter_lines.return_value = iter(sse)
        r.raise_for_status.return_value = None
        mock_post.return_value = r
        provider = OpenAIProvider("https://api.openai.com/v1", "test-key")
        result = provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="m",
            capabilities=caps_structured,
            on_chunk=lambda c: None,
            interrupt_check=lambda: False,
        )
        assert result.content == "hello world"
        assert result.stop_reason != "interrupted"

    @patch("agent_cli.providers.openai.requests.post")
    def test_without_structured_output(self, mock_post, caps_basic):
        mock_post.return_value = _mock_response(
            {
                "choices": [
                    {"message": {"content": "plain text"}, "finish_reason": "stop"}
                ],
            }
        )

        provider = OpenAIProvider("http://localhost:8080/v1", "")
        result = provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="local-model",
            capabilities=caps_basic,
        )

        body = mock_post.call_args.kwargs["json"]
        assert "response_format" not in body
        assert result.content == "plain text"

    @patch("agent_cli.providers.openai.requests.post")
    def test_api_key_sets_auth_header(self, mock_post, caps_basic):
        """Non-empty API key → Authorization header present."""
        mock_post.return_value = _mock_response(
            {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            }
        )
        provider = OpenAIProvider("http://localhost:8080/v1", "my-key")
        provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="m",
            capabilities=caps_basic,
        )
        headers = mock_post.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer my-key"

    @patch("agent_cli.providers.openai.requests.post")
    def test_empty_api_key_skips_auth_header(self, mock_post, caps_basic):
        """Empty API key → no Authorization header (local servers)."""
        mock_post.return_value = _mock_response(
            {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            }
        )
        provider = OpenAIProvider("http://localhost:8080/v1", "")
        provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="m",
            capabilities=caps_basic,
        )
        headers = mock_post.call_args.kwargs["headers"]
        assert "Authorization" not in headers


@pytest.fixture
def caps_thinking():
    return ModelCapabilities(
        context_window=32768,
        max_output_tokens=4096,
        supports_thinking=True,
    )


@pytest.fixture
def caps_no_thinking():
    return ModelCapabilities(
        context_window=32768,
        max_output_tokens=4096,
        supports_thinking=False,
    )


class TestThinkingSwitch:
    """v10.72.0: 사고는 on/off 둘뿐 — Anthropic 은 켬이면 ``thinking: {type: adaptive}``
    (현행 Claude 는 ``budget_tokens`` 를 거절한다), 끔이면 블록 없음·max_tokens 불변.
    정도(effort·budget)는 보내지 않는다(서버 기본값의 몫)."""

    @patch("agent_cli.providers.anthropic.requests.post")
    def test_anthropic_on_sends_adaptive_without_budget(self, mock_post, caps_thinking):
        mock_post.return_value = _mock_response(
            {
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
            }
        )
        provider = AnthropicProvider("https://api.anthropic.com/v1", "key")
        provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="claude-opus-5-5",
            capabilities=caps_thinking,
            settings=CallSettings(thinking=True),
        )
        body = mock_post.call_args.kwargs["json"]
        assert body["thinking"] == {"type": "adaptive"}
        assert body["max_tokens"] == 4096  # 예산을 더하지 않는다
        assert "budget_tokens" not in str(body)

    @patch("agent_cli.providers.anthropic.requests.post")
    def test_anthropic_default_is_off(self, mock_post, caps_thinking):
        """settings 없음 = CallSettings 기본 = 끔 → thinking 블록 없음."""
        mock_post.return_value = _mock_response(
            {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}
        )
        provider = AnthropicProvider("https://api.anthropic.com/v1", "key")
        provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="claude-opus-5-5",
            capabilities=caps_thinking,
        )
        body = mock_post.call_args.kwargs["json"]
        assert "thinking" not in body
        assert body["max_tokens"] == 4096

    @patch("agent_cli.providers.anthropic.requests.post")
    def test_anthropic_no_thinking_regression(self, mock_post, caps_no_thinking):
        mock_post.return_value = _mock_response(
            {
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
            }
        )
        provider = AnthropicProvider("https://api.anthropic.com/v1", "key")
        provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="claude-sonnet-4-20250514",
            capabilities=caps_no_thinking,
        )
        body = mock_post.call_args.kwargs["json"]
        assert "thinking" not in body
        assert body["max_tokens"] == 4096

    @patch("agent_cli.providers.anthropic.requests.post")
    def test_anthropic_off_sends_no_block(self, mock_post, caps_thinking):
        """끔은 블록 없음 — ``disabled`` 는 현행 모델 일부가 거절하므로 모델 기본에
        맡긴다."""
        mock_post.return_value = _mock_response(
            {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}
        )
        provider = AnthropicProvider("https://api.anthropic.com/v1", "key")
        provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="claude-opus-5-5",
            capabilities=caps_thinking,
            settings=CallSettings(thinking=False),
        )
        body = mock_post.call_args.kwargs["json"]
        assert "thinking" not in body
        assert body["max_tokens"] == 4096

    @patch("agent_cli.providers.anthropic.requests.post")
    def test_anthropic_switch_ignored_when_not_supported(
        self, mock_post, caps_no_thinking
    ):
        """v8.21.1: supports_thinking=False 면 켬 요청을 무시 — 사고 미지원 모델에
        thinking 블록을 보내 400 나는 것 방지."""
        mock_post.return_value = _mock_response(
            {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}
        )
        provider = AnthropicProvider("https://api.anthropic.com/v1", "key")
        provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="claude-opus-5-5",
            capabilities=caps_no_thinking,
            settings=CallSettings(thinking=True),
        )
        body = mock_post.call_args.kwargs["json"]
        assert "thinking" not in body
        assert body["max_tokens"] == 4096  # base max_output, 사고 미주입

    @patch("agent_cli.providers.openai.requests.post")
    def test_openai_on_sends_only_the_switch(self, mock_post, caps_thinking):
        mock_post.return_value = _mock_response(
            {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            }
        )
        provider = OpenAIProvider("https://api.openai.com/v1", "key")
        provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="o3-mini",
            capabilities=caps_thinking,
            settings=CallSettings(thinking=True),
        )
        body = mock_post.call_args.kwargs["json"]
        assert "reasoning_effort" not in body
        assert body["chat_template_kwargs"] == {"enable_thinking": True}

    @patch("agent_cli.providers.openai.requests.post")
    def test_openai_no_thinking_regression(self, mock_post, caps_no_thinking):
        mock_post.return_value = _mock_response(
            {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            }
        )
        provider = OpenAIProvider("https://api.openai.com/v1", "key")
        provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="gpt-4o",
            capabilities=caps_no_thinking,
        )
        body = mock_post.call_args.kwargs["json"]
        assert "reasoning_effort" not in body


class TestCreateProvider:
    def test_anthropic(self):
        p = create_provider("anthropic", "https://api.anthropic.com/v1", "key")
        assert isinstance(p, AnthropicProvider)

    def test_openai(self):
        p = create_provider("openai", "https://api.openai.com/v1", "key")
        assert isinstance(p, OpenAIProvider)

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            create_provider("gemini", "http://x", "")


class TestThinkingFieldCapture:
    """Each provider must surface its native reasoning channel through
    LLMResponse.thinking. Empty string when the response carries none —
    this is the graceful fallback path for non-reasoning models."""

    @patch("agent_cli.providers.anthropic.requests.post")
    def test_anthropic_captures_thinking_block(self, mock_post, caps_structured):
        # Anthropic extended thinking returns a dedicated content block
        mock_post.return_value = _mock_response(
            {
                "content": [
                    {"type": "thinking", "thinking": "Let me reason..."},
                    {"type": "text", "text": '{"action":"complete"}'},
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "stop_reason": "end_turn",
            }
        )
        provider = AnthropicProvider("https://api.anthropic.com/v1", "k")
        result = provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="claude-opus-4-1",
            capabilities=caps_structured,
        )
        assert result.thinking == "Let me reason..."
        assert result.content == '{"action":"complete"}'

    @patch("agent_cli.providers.anthropic.requests.post")
    def test_anthropic_stream_isolates_inline_think(self, mock_post, caps_structured):
        """스트리밍 경로도 인라인 <think> 격리 — OpenAI 스트림과 동형
        (리뷰 §4.2 수리). content 에 흘러든 태그는 thinking 으로 분리된다."""
        sse = [
            b'data: {"type":"message_start","message":{"usage":{"input_tokens":5}}}',
            b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"<think>inline r</think>"}}',
            b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Answer"}}',
            b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}',
        ]
        r = MagicMock()
        r.iter_lines.return_value = iter(sse)
        r.raise_for_status.return_value = None
        mock_post.return_value = r

        provider = AnthropicProvider("https://api.anthropic.com/v1", "k")
        result = provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="qwen-on-omlx",
            capabilities=caps_structured,
            on_chunk=lambda _c: None,
        )
        assert result.content == "Answer"
        assert result.thinking == "inline r"
        assert result.stop_reason == "stop"  # P0-1 정규화 유지


class TestThinkingSwitchGate:
    """공용 게이트 ``thinking_switch`` (v10.72.0 — 종전 ``resolve_thinking_policy``
    의 자리): 미지원 모델은 None(아무것도 싣지 않음), 지원 모델은 세션 값 그대로."""

    def _caps(self, supports):
        from agent_cli.providers.capabilities import ModelCapabilities

        return ModelCapabilities(
            context_window=32768, max_output_tokens=4096, supports_thinking=supports
        )

    def test_unsupported_returns_none(self):
        from agent_cli.providers.base import thinking_switch

        assert thinking_switch(self._caps(False), False) is None
        assert thinking_switch(self._caps(False), True) is None

    def test_supported_passes_the_session_value(self):
        from agent_cli.providers.base import thinking_switch

        assert thinking_switch(self._caps(True), True) is True
        assert thinking_switch(self._caps(True), False) is False

    def test_no_policy_or_effort_vocabulary_remains(self):
        """정책 객체·effort→budget 표는 지웠다 — 되살아나면 실패."""
        import agent_cli.providers.anthropic as anth
        from agent_cli.providers import base

        for name in ("ThinkingPolicy", "resolve_thinking_policy"):
            assert not hasattr(base, name), name
        for name in ("_EFFORT_TO_BUDGET", "_MIN_THINKING_BUDGET"):
            assert not hasattr(anth, name), name

    @patch("agent_cli.providers.anthropic.requests.post")
    def test_anthropic_no_thinking_block_returns_empty(
        self, mock_post, caps_structured
    ):
        mock_post.return_value = _mock_response(
            {
                "content": [{"type": "text", "text": '{"action":"complete"}'}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "stop_reason": "end_turn",
            }
        )
        provider = AnthropicProvider("https://api.anthropic.com/v1", "k")
        result = provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="claude-opus-4-1",
            capabilities=caps_structured,
        )
        assert result.thinking == ""

    @patch("agent_cli.providers.openai.requests.post")
    def test_openai_captures_reasoning_content(self, mock_post, caps_structured):
        # vLLM convention: reasoning_content sibling to content
        mock_post.return_value = _mock_response(
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"action":"complete"}',
                            "reasoning_content": "Reasoning here.",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        )
        provider = OpenAIProvider("http://localhost:8000/v1", "")
        result = provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="qwen3-served-via-vllm",
            capabilities=caps_structured,
        )
        assert result.thinking == "Reasoning here."
        assert result.content == '{"action":"complete"}'

    @patch("agent_cli.providers.openai.requests.post")
    def test_openai_no_reasoning_content_returns_empty(
        self, mock_post, caps_structured
    ):
        # Plain OpenAI Chat Completions does not expose reasoning here
        mock_post.return_value = _mock_response(
            {
                "choices": [
                    {
                        "message": {"content": '{"action":"complete"}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        )
        provider = OpenAIProvider("https://api.openai.com/v1", "k")
        result = provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="gpt-4o",
            capabilities=caps_structured,
        )
        assert result.thinking == ""


class TestInterruptibleLines:
    """`interruptible_lines` keeps a streaming read interruptible even during
    no-data gaps (TTFT, between-token stalls) by running the blocking
    `iter_lines()` in a reader thread and polling `interrupt_check` while the
    queue is empty."""

    class _FakeResp:
        """Minimal streaming response. `iter_lines` blocks on `gate` before the
        first line (simulating a TTFT stall) when one is provided."""

        def __init__(self, lines, gate=None, raise_after=None):
            self._lines = lines
            self._gate = gate
            self._raise_after = raise_after
            self.closed = False

        def iter_lines(self):
            if self._gate is not None:
                # Block until released (by close() or test) — the TTFT window.
                self._gate.wait(2.0)
            for i, ln in enumerate(self._lines):
                if self.closed:
                    return
                yield ln
                if self._raise_after is not None and i == self._raise_after:
                    raise ConnectionError("boom")

        def close(self):
            self.closed = True
            if self._gate is not None:
                self._gate.set()

    def test_no_check_is_passthrough(self):
        """Without interrupt_check: plain iteration, no thread."""
        r = self._FakeResp([b"a", b"b"])
        assert list(interruptible_lines(r, None)) == [b"a", b"b"]

    def test_yields_all_when_never_interrupted(self):
        r = self._FakeResp([b"a", b"b", b"c"])
        out = list(interruptible_lines(r, lambda: False, poll_interval=0.01))
        assert out == [b"a", b"b", b"c"]

    def test_breaks_during_ttft_stall_before_first_line(self):
        """The reader blocks before any line arrives; an interrupt during that
        stall is caught on a poll, the response is closed, and nothing is
        yielded — the case a per-chunk check could never reach."""
        gate = threading.Event()
        r = self._FakeResp([b"late"], gate=gate)
        calls = {"n": 0}

        def interrupt_check():
            # False on the first poll, True on the next — exercising the
            # empty-queue poll path (no line ever arrived).
            calls["n"] += 1
            return calls["n"] >= 2

        out = list(interruptible_lines(r, interrupt_check, poll_interval=0.01))
        assert out == []
        assert r.closed is True

    def test_interrupt_before_first_poll_yields_nothing(self):
        """Flag already set when entering: returns immediately, closes."""
        r = self._FakeResp([b"a", b"b"])
        out = list(interruptible_lines(r, lambda: True, poll_interval=0.01))
        assert out == []
        assert r.closed is True

    def test_propagates_stream_error(self):
        """A genuine error from iter_lines surfaces to the caller (not
        swallowed) — only our own close()-on-interrupt is silent."""
        r = self._FakeResp([b"a"], raise_after=0)
        with pytest.raises(ConnectionError):
            list(interruptible_lines(r, lambda: False, poll_interval=0.01))


class TestStripThinkBlocks:
    """<think> 류 인라인 추론 태그 제거 (5.10.0) — MiMo 등이 content 에
    긴 추론 블록을 태그로 흘리는 것을 provider 응답 조립 지점에서 격리.
    제거분은 버리지 않고 LLMResponse.thinking 으로 이동 (verbose 가시성)."""

    def test_strips_closed_block_and_moves_to_thinking(self):
        from agent_cli.providers.base import strip_think_blocks

        content, think = strip_think_blocks(
            "<think>아주 긴 추론...</think>\n## Thought\n간다\n\n## Action\n[]"
        )
        assert content.startswith("## Thought")
        assert "아주 긴 추론" in think and "<think>" not in content

    def test_strips_unclosed_block_to_eof(self):
        # max_tokens 를 think 안에서 소진 — 열림 태그 이후 전부가 추론.
        from agent_cli.providers.base import strip_think_blocks

        content, think = strip_think_blocks("<think>끝나지 않는 추론이 계속")
        assert content == ""
        assert "끝나지 않는" in think

    def test_multiple_tags_and_variants(self):
        from agent_cli.providers.base import strip_think_blocks

        content, think = strip_think_blocks(
            "<THINK>a</THINK>본문1<reasoning>b</reasoning>본문2"
        )
        assert content == "본문1본문2"
        assert "a" in think and "b" in think

    def test_plain_content_untouched(self):
        from agent_cli.providers.base import strip_think_blocks

        raw = '## Thought\nx\n\n## Action\n[{"action": "complete"}]'
        content, think = strip_think_blocks(raw)
        assert content == raw and think == ""

    def test_openai_parse_response_strips(self):
        from agent_cli.providers.openai import OpenAIProvider

        prov = OpenAIProvider(base_url="http://x/v1", api_key="k")
        data = {
            "choices": [
                {
                    "message": {"content": "<think>추론</think>답변"},
                    "finish_reason": "stop",
                }
            ],
        }
        resp = prov._parse_response(data)
        assert resp.content == "답변"
        assert "추론" in resp.thinking

    def test_openai_parse_appends_to_reasoning_content(self):
        # 서버 reasoning_content 와 인라인 태그가 공존해도 둘 다 thinking 으로.
        from agent_cli.providers.openai import OpenAIProvider

        prov = OpenAIProvider(base_url="http://x/v1", api_key="k")
        data = {
            "choices": [
                {
                    "message": {
                        "content": "<think>tag</think>답",
                        "reasoning_content": "필드",
                    },
                    "finish_reason": "stop",
                }
            ],
        }
        resp = prov._parse_response(data)
        assert resp.content == "답"
        assert "필드" in resp.thinking and "tag" in resp.thinking


class TestProviderRegistry:
    """프로바이더 self-register (v8.41.0 — 리뷰 §4.2): 추가 = 모듈 1개 +
    내장 목록 1줄. create_provider/_detect_runtime_capabilities 의 이름
    분기 소멸 — 등가성은 릴리스 하네스로 신·구 동작(반환 클래스·에러
    메시지·transport 인자) 일치 확인."""

    def test_builtins_registered(self):
        from agent_cli.providers import get_provider_class

        assert get_provider_class("openai") is OpenAIProvider
        assert get_provider_class("anthropic") is AnthropicProvider
        assert get_provider_class("nope") is None

    def test_create_provider_unknown_message_stable(self):
        from agent_cli.providers import create_provider

        with pytest.raises(ValueError) as exc:
            create_provider("gemini", "u", "k")
        # 종전 하드코딩 메시지와 동일 (정렬된 등록명 나열)
        assert "Unknown provider: gemini" in str(exc.value)
        assert "anthropic, openai" in str(exc.value)

    def test_register_collision_raises_same_class_tolerated(self):
        from agent_cli.providers import _PROVIDERS, register_provider

        register_provider("openai", OpenAIProvider)  # 같은 클래스 재등록 no-op
        assert _PROVIDERS["openai"] is OpenAIProvider
        with pytest.raises(ValueError):
            register_provider("openai", AnthropicProvider)

    def test_capability_transport_hook_owned_by_provider(self):
        from agent_cli.providers.capabilities import (
            _AnthropicTransport,
            _OpenAITransport,
        )

        t = OpenAIProvider.capability_transport("http://b/v1", "m1", "kk")
        assert isinstance(t, _OpenAITransport)
        assert (t.base, t.model, t.api_key) == ("http://b/v1", "m1", "kk")
        t2 = AnthropicProvider.capability_transport("http://b/v1", "m2")
        assert isinstance(t2, _AnthropicTransport)
        assert (t2.base, t2.model) == ("http://b/v1", "m2")

    def test_detect_runtime_uses_provider_hook(self):
        """감지 오케스트레이터가 프로바이더 클래스의 훅을 경유 — 커스텀
        프로바이더가 자기 transport 를 꽂을 수 있음을 고정."""
        from unittest.mock import patch as _patch

        from agent_cli.providers.capabilities import _detect_runtime_capabilities

        sentinel_transport = object()

        class _CustomProvider:
            @staticmethod
            def capability_transport(base_url, model, api_key=""):
                return sentinel_transport

        captured = {}

        def fake_detect(model, transport):
            captured["transport"] = transport

        with (
            _patch(
                "agent_cli.providers.get_provider_class",
                return_value=_CustomProvider,
            ),
            _patch(
                "agent_cli.providers.capabilities._detect_capabilities", fake_detect
            ),
        ):
            _detect_runtime_capabilities("custom", "u", "m")
        assert captured["transport"] is sentinel_transport

    def test_detect_runtime_unknown_provider_none(self):
        from agent_cli.providers.capabilities import _detect_runtime_capabilities

        assert _detect_runtime_capabilities("gemini", "u", "m") is None


class TestDegenerationWindow:
    """degeneration 조기종료 검사창 (v8.41.0 효율 — 리뷰 §4.2): 트리거
    청크에서 최근 _DEGEN_WINDOW tail 만 검사 — 러너웨이(조밀 반복)는 tail
    로 충분하고 누적-전체 재검사의 O(n²) 를 피한다. 턴-종료 후 전문
    라벨링(dispatch)은 별개로 종전 그대로."""

    def test_tail_text_takes_last_window(self):
        from agent_cli.providers.http import _tail_text

        assert _tail_text(["abc", "def", "ghi"], 4) == "fghi"
        assert _tail_text(["abc"], 100) == "abc"
        assert _tail_text([], 10) == ""

    def test_dense_runaway_still_early_stops(self):
        """실 러너웨이(조밀 반복)는 창 안에서 ≥2 히트 — 조기종료 유지."""
        from agent_cli.providers.http import run_sse_stream

        chunks = ['data: {"c": "## Thought\\n## Action\\n"}'] * 3
        r = MagicMock()
        r.iter_lines.return_value = iter([c.encode() for c in chunks])

        def mapper(data):
            from agent_cli.providers.http import StreamEvent

            return StreamEvent(text=data.get("c", ""))

        acc = run_sse_stream(
            r,
            lambda _t: None,
            map_payload=mapper,
            degeneration_check=lambda t: t.count("## Thought") >= 2,
            degeneration_trigger="#",
        )
        assert acc.stop_reason == "degenerate_runaway"

    def test_content_joined_across_chunks(self):
        """리스트 누적 → 종료 시 join — content 는 청크 연결과 바이트 동일."""
        from agent_cli.providers.http import StreamEvent, run_sse_stream

        chunks = ['data: {"c": "hello "}', 'data: {"c": "world"}', "data: [DONE]"]
        r = MagicMock()
        r.iter_lines.return_value = iter([c.encode() for c in chunks])
        acc = run_sse_stream(
            r,
            lambda _t: None,
            map_payload=lambda d: StreamEvent(text=d.get("c", "")),
        )
        assert acc.content == "hello world"


class TestUnclosedThinkStop:
    """v10.1.4 (회사 실측 NO_OUTPUT): 모델이 <think> 를 열고 안 닫은 채 호출을 내면
    프로바이더가 EOF 까지 사고로 삼켜 content 가 비었다. 방언의 정지점(첫 구조
    토큰)을 받으면 호출 직전에서 멈춘다."""

    RAW = (
        "<think>\nI should read the file first.\n<tool_call>\n<function=read_file>\n"
        "<parameter=path>a.py</parameter>\n</function>\n</tool_call>"
    )

    def test_openai_non_stream_keeps_call_after_unclosed_think(self):
        import re

        from agent_cli.dialects import get

        provider = OpenAIProvider("http://x/v1", "k")
        data = {
            "choices": [{"message": {"content": self.RAW}, "finish_reason": "stop"}]
        }
        swallowed = provider._parse_response(data)
        assert swallowed.content == ""  # 정지점 없이는 종전처럼 EOF 까지
        kept = provider._parse_response(data, thinking_stop=get("xml_fc").thinking_stop)
        assert kept.content.startswith("<tool_call>")
        assert kept.thinking == "I should read the file first."
        assert isinstance(get("xml_fc").thinking_stop, re.Pattern)

    def test_anthropic_non_stream_keeps_call_after_unclosed_think(self):
        from agent_cli.dialects import get

        provider = AnthropicProvider("https://api.anthropic.com/v1", "k")
        data = {
            "content": [{"type": "text", "text": self.RAW}],
            "stop_reason": "end_turn",
        }
        kept = provider._parse_response(data, thinking_stop=get("xml_fc").thinking_stop)
        assert kept.content.startswith("<tool_call>")
        assert kept.thinking == "I should read the file first."


class TestStreamedToolCalls:
    """v10.1.5: 서버 tool parser 가 호출을 ``delta.tool_calls`` 조각으로 보내면
    content 가 비어 NO_OUTPUT 이었다. 조각을 index 별로 모아 ``tool_calls`` 로
    돌려주면 루프(v10.1.4 `_render_server_tool_calls`)가 캐노니컬로 되살린다."""

    @patch("agent_cli.providers.openai.requests.post")
    def test_delta_tool_calls_are_accumulated(self, mock_post, caps_structured):
        sse = [
            b'data: {"choices":[{"delta":{"role":"assistant","content":""}}]}',
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"read_file","arguments":""}}]}}]}',
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"path\\": "}}]}}]}',
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"a.py\\"}"}}]}}]}',
            b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":5,"completion_tokens":33}}',
            b"data: [DONE]",
        ]
        r = MagicMock()
        r.iter_lines.return_value = iter(sse)
        r.raise_for_status.return_value = None
        mock_post.return_value = r
        provider = OpenAIProvider("http://x/v1", "k")
        result = provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="s",
            model="m",
            capabilities=caps_structured,
            on_chunk=lambda t: None,
        )
        assert result.content == ""
        assert result.tool_calls == [
            {"id": "c1", "name": "read_file", "input": {"path": "a.py"}}
        ]
        assert result.usage.output_tokens == 33


class TestStreamDump:
    """v10.1.6: ``AGENT_CLI_DUMP_STREAM`` — 서버가 보낸 SSE 줄을 해석 전 그대로 파일에."""

    @patch("agent_cli.providers.openai.requests.post")
    def test_raw_lines_are_dumped(
        self, mock_post, caps_structured, tmp_path, monkeypatch
    ):
        dump = tmp_path / "stream.log"
        monkeypatch.setenv("AGENT_CLI_DUMP_STREAM", str(dump))
        sse = [
            b'data: {"choices":[{"delta":{"reasoning_content":"hmm"}}]}',
            b'data: {"choices":[{"delta":{"content":"hi"}}]}',
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            b"data: [DONE]",
        ]
        r = MagicMock()
        r.iter_lines.return_value = iter(sse)
        r.raise_for_status.return_value = None
        mock_post.return_value = r
        provider = OpenAIProvider("http://x/v1", "k")
        result = provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="s",
            model="m",
            capabilities=caps_structured,
            on_chunk=lambda t: None,
        )
        assert result.content == "hi" and result.thinking == "hmm"
        text = dump.read_text()
        assert text.count("### stream ") == 1
        assert '"reasoning_content":"hmm"' in text and "data: [DONE]" in text


class TestNativeToolsInBody:
    """v10.2.0: CallSettings.tools → 요청 body `tools` + `tool_choice: auto`."""

    @patch("agent_cli.providers.openai.requests.post")
    def test_tools_are_sent_when_set(self, mock_post, caps_structured):
        mock_post.return_value = _mock_response(
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )
        provider = OpenAIProvider("http://x/v1", "k")
        tools = [
            {
                "type": "function",
                "function": {"name": "f", "parameters": {"type": "object"}},
            }
        ]
        provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="s",
            model="m",
            capabilities=caps_structured,
            settings=CallSettings(tools=tools),
        )
        body = mock_post.call_args.kwargs["json"]
        assert body["tools"] == tools and body["tool_choice"] == "auto"
        provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="s",
            model="m",
            capabilities=caps_structured,
            settings=CallSettings(),
        )
        assert "tools" not in mock_post.call_args.kwargs["json"]


# ── 이미지 (v10.59.0) — 공급자 경계에서만 조각으로 ──────────────────────


def _png_ref(tmp_path, name="a.png"):
    from PIL import Image

    from agent_cli import images

    p = tmp_path / name
    Image.new("RGB", (32, 16), (255, 0, 0)).save(p, format="PNG")
    return images.make_ref(p)


class TestImagesAtProviderBoundary:
    @patch("agent_cli.providers.openai.requests.post")
    def test_openai_expands_to_image_url_parts(self, mock_post, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        ref = _png_ref(tmp_path)
        mock_post.return_value = _mock_response(
            {"choices": [{"message": {"content": "red"}, "finish_reason": "stop"}]}
        )
        caps = ModelCapabilities(32768, 4096, False, supports_vision=True)
        OpenAIProvider("http://x/v1", "k").call(
            messages=[
                {"role": "user", "content": ref.placeholder, "images": [ref.to_dict()]},
                {"role": "assistant", "content": "ok"},
            ],
            system="s",
            model="m",
            capabilities=caps,
        )
        msgs = mock_post.call_args.kwargs["json"]["messages"]
        assert all("images" not in m for m in msgs)
        user = msgs[1]
        assert user["content"][0] == {"type": "text", "text": ref.placeholder}
        assert user["content"][1]["type"] == "image_url"
        assert user["content"][1]["image_url"]["url"].startswith(
            "data:image/png;base64,"
        )
        assert msgs[2]["content"] == "ok"

    @patch("agent_cli.providers.anthropic.requests.post")
    def test_anthropic_expands_to_image_blocks(self, mock_post, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        ref = _png_ref(tmp_path)
        mock_post.return_value = _mock_response(
            {
                "content": [{"type": "text", "text": "red"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "stop_reason": "end_turn",
            }
        )
        caps = ModelCapabilities(32768, 4096, False, supports_vision=True)
        AnthropicProvider("http://x/v1", "k").call(
            messages=[
                {"role": "user", "content": ref.placeholder, "images": [ref.to_dict()]}
            ],
            system="s",
            model="m",
            capabilities=caps,
        )
        msgs = mock_post.call_args.kwargs["json"]["messages"]
        assert "images" not in msgs[0]
        block = msgs[0]["content"][1]
        assert block["type"] == "image"
        assert block["source"]["type"] == "base64"
        assert block["source"]["media_type"] == "image/png"

    @patch("agent_cli.providers.openai.requests.post")
    def test_text_only_model_gets_the_placeholder_and_a_note(
        self, mock_post, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        ref = _png_ref(tmp_path)
        mock_post.return_value = _mock_response(
            {"choices": [{"message": {"content": "?"}, "finish_reason": "stop"}]}
        )
        caps = ModelCapabilities(32768, 4096, False, supports_vision=False)
        OpenAIProvider("http://x/v1", "k").call(
            messages=[
                {"role": "user", "content": ref.placeholder, "images": [ref.to_dict()]}
            ],
            system="s",
            model="m",
            capabilities=caps,
        )
        user = mock_post.call_args.kwargs["json"]["messages"][1]
        assert isinstance(user["content"], str)
        assert user["content"].startswith(ref.placeholder)
        assert "cannot view images" in user["content"]

    @patch("agent_cli.providers.openai.requests.post")
    def test_unknown_support_probes_once_then_remembers(
        self, mock_post, tmp_path, monkeypatch
    ):
        """등록값이 없으면(None) 첫 이미지에서 한 번 프로브 — 같은 프로세스에선
        다시 묻지 않는다."""
        from agent_cli.providers import capabilities as caps_mod

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(caps_mod, "_vision_probed", {})
        ref = _png_ref(tmp_path)
        probe = _mock_response(
            {"choices": [{"message": {"content": "green then blue"}}]}
        )
        chat = _mock_response(
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )
        mock_post.side_effect = [probe, chat, chat]
        caps = ModelCapabilities(32768, 4096, False)  # supports_vision=None
        provider = OpenAIProvider("http://x/v1", "k")
        msgs = [{"role": "user", "content": ref.placeholder, "images": [ref.to_dict()]}]
        provider.call(messages=msgs, system="s", model="m", capabilities=caps)
        provider.call(messages=msgs, system="s", model="m", capabilities=caps)
        assert mock_post.call_count == 3  # 프로브 1 + 호출 2
        sent = mock_post.call_args.kwargs["json"]["messages"][1]["content"]
        assert sent[1]["type"] == "image_url"

    @patch("agent_cli.providers.openai.requests.post")
    def test_no_images_means_no_probe(self, mock_post, monkeypatch):
        from agent_cli.providers import capabilities as caps_mod

        monkeypatch.setattr(caps_mod, "_vision_probed", {})
        mock_post.return_value = _mock_response(
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )
        OpenAIProvider("http://x/v1", "k").call(
            messages=[{"role": "user", "content": "hi"}],
            system="s",
            model="m",
            capabilities=ModelCapabilities(32768, 4096, False),
        )
        assert mock_post.call_count == 1


class TestSamplingInTheBody:
    """🎲 (v10.69.0): 세션 노브 > models.json 엔트리 > 미주입 — OpenAI·Anthropic 이
    같은 이름으로 싣고, 어느 층도 안 정한 키는 본문에 없다(종전과 같은 본문)."""

    def _ok(self, mock_post, kind):
        r = MagicMock()
        r.raise_for_status.return_value = None
        r.json.return_value = (
            {"choices": [{"message": {"content": "[]"}, "finish_reason": "stop"}]}
            if kind == "openai"
            else {
                "content": [{"type": "text", "text": "[]"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        )
        mock_post.return_value = r

    def test_resolve_sampling_precedence(self):
        from agent_cli.providers.base import resolve_sampling, sampling_source

        plain = ModelCapabilities(32768, 4096, False)
        entry = ModelCapabilities(32768, 4096, False, temperature=0.3, top_p=0.95)
        assert resolve_sampling(plain, None) == {}
        assert resolve_sampling(entry, None) == {"temperature": 0.3, "top_p": 0.95}
        assert resolve_sampling(entry, {"temperature": 0.2}) == {
            "temperature": 0.2,
            "top_p": 0.95,
        }
        assert resolve_sampling(plain, {"top_p": 0.5}) == {"top_p": 0.5}
        assert sampling_source(entry, {"temperature": 0.2}, "temperature") == "session"
        assert sampling_source(entry, {}, "top_p") == "models.json"
        assert sampling_source(plain, None, "top_p") == "server"

    @patch("agent_cli.providers.openai.requests.post")
    def test_openai_body(self, mock_post):
        self._ok(mock_post, "openai")
        provider = OpenAIProvider("http://x/v1", "k")
        entry = ModelCapabilities(32768, 4096, False, temperature=0.3)
        plain = ModelCapabilities(32768, 4096, False)
        call = lambda caps, **kw: provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="s",
            model="m",
            capabilities=caps,
            **kw,
        )
        call(plain)
        body = mock_post.call_args.kwargs["json"]
        assert "temperature" not in body and "top_p" not in body
        call(entry)
        assert mock_post.call_args.kwargs["json"]["temperature"] == 0.3
        call(entry, settings=CallSettings(sampling={"temperature": 0.2, "top_p": 0.9}))
        body = mock_post.call_args.kwargs["json"]
        assert body["temperature"] == 0.2 and body["top_p"] == 0.9

    @patch("agent_cli.providers.anthropic.requests.post")
    def test_anthropic_body(self, mock_post):
        self._ok(mock_post, "anthropic")
        provider = AnthropicProvider("http://x/v1", "k")
        caps = ModelCapabilities(32768, 4096, False, top_p=0.95)
        provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="s",
            model="m",
            capabilities=caps,
            settings=CallSettings(sampling={"temperature": 0.5}),
        )
        body = mock_post.call_args.kwargs["json"]
        assert body["temperature"] == 0.5 and body["top_p"] == 0.95
