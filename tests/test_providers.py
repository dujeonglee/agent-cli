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
        # wire format's is_degenerate predicate, which is provider-independent.
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

    def test_wire_formats_declare_trigger(self):
        from agent_cli.wire_formats import get as get_wire

        assert get_wire("json_fc").degeneration_trigger == "#"
        assert get_wire("xml_fc").degeneration_trigger == "<"

    def test_llm_caller_passes_wire_trigger(self):
        # llm.py 가 wire 의 트리거를 provider.call 로 전달하는 배선 고정.
        import inspect

        from agent_cli.loop import llm as llm_mod

        src = inspect.getsource(llm_mod)
        assert "degeneration_trigger" in src
        assert "getattr(\n                    self.cfg.wire_format" in src

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
    def test_request_overrides_apply_to_body(self, mock_post, caps_thinking):
        """세션 thinking 오버라이드(web UI) → 요청 body 반영: enable_thinking 은
        chat_template_kwargs(Qwen/MLX 스위치), reasoning_effort 는 그대로 필드로.
        공용 정책(resolve_thinking_policy): 사고 off 면 reasoning_effort 도
        **미전송** — 종전엔 enable_thinking=False 여도 effort 가 잔존해 엄격
        백엔드에 상충 신호를 보냈다(리뷰 §4.2 수리)."""
        r = MagicMock()
        r.raise_for_status.return_value = None
        r.json.return_value = {
            "choices": [{"message": {"content": "[]"}, "finish_reason": "stop"}]
        }
        mock_post.return_value = r
        provider = OpenAIProvider("https://api.openai.com/v1", "k")

        # 사고 off(enable=False) → 스위치 off + effort 미전송 (잔존 effort 수리)
        provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="s",
            model="m",
            capabilities=caps_thinking,
            settings=CallSettings(
                thinking={"enable_thinking": False, "reasoning_effort": "high"}
            ),
        )
        body = mock_post.call_args.kwargs["json"]
        assert body["chat_template_kwargs"] == {"enable_thinking": False}
        assert "reasoning_effort" not in body

        # 사고 on(enable=True) + effort → 둘 다 전송
        provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="s",
            model="m",
            capabilities=caps_thinking,
            settings=CallSettings(
                thinking={"enable_thinking": True, "reasoning_effort": "high"}
            ),
        )
        body = mock_post.call_args.kwargs["json"]
        assert body["chat_template_kwargs"] == {"enable_thinking": True}
        assert body["reasoning_effort"] == "high"

        # "off" → reasoning_effort 제거, enable_thinking None → 미전송
        provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="s",
            model="m",
            capabilities=caps_thinking,
            settings=CallSettings(thinking={"reasoning_effort": "off"}),
        )
        body = mock_post.call_args.kwargs["json"]
        assert "reasoning_effort" not in body
        assert "chat_template_kwargs" not in body

        # eff="off" + enable=True 상충 조합 — "off" 가 이긴다 (Anthropic 과
        # 동형: 공용 정책의 통일 규칙). 명시 오버라이드가 있으니 스위치는
        # 방출하되 값은 정책의 enabled(False).
        provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="s",
            model="m",
            capabilities=caps_thinking,
            settings=CallSettings(
                thinking={"enable_thinking": True, "reasoning_effort": "off"}
            ),
        )
        body = mock_post.call_args.kwargs["json"]
        assert "reasoning_effort" not in body
        assert body["chat_template_kwargs"] == {"enable_thinking": False}

    @patch("agent_cli.providers.openai.requests.post")
    def test_overrides_ignored_when_not_supported(self, mock_post, caps_structured):
        """v8.21.1: supports_thinking=False 면 런타임 오버라이드를 무시 —
        비추론 모델에 reasoning_effort/chat_template_kwargs 를 보내 400 나는 것 방지."""
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
            settings=CallSettings(
                thinking={"enable_thinking": True, "reasoning_effort": "high"}
            ),
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


class TestThinkingBudget:
    @patch("agent_cli.providers.anthropic.requests.post")
    def test_anthropic_thinking_param(self, mock_post, caps_thinking):
        """v8.21.0: supports_thinking 단독 게이트 — 오버라이드 없으면 기본
        medium(16384) budget 으로 사고 활성."""
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
            capabilities=caps_thinking,
        )
        body = mock_post.call_args.kwargs["json"]
        assert body["thinking"] == {"type": "enabled", "budget_tokens": 16384}
        assert body["max_tokens"] == 16384 + 4096

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
    def test_anthropic_effort_override_maps_to_budget(self, mock_post, caps_thinking):
        """런타임 reasoning_effort high/medium → Anthropic budget_tokens 로 번역."""
        mock_post.return_value = _mock_response(
            {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}
        )
        provider = AnthropicProvider("https://api.anthropic.com/v1", "key")
        for eff, expect in (("high", 32768), ("medium", 16384), ("low", 4096)):
            provider.call(
                messages=[{"role": "user", "content": "hi"}],
                system="sys",
                model="claude-sonnet-4-20250514",
                capabilities=caps_thinking,
                settings=CallSettings(thinking={"reasoning_effort": eff}),
            )
            body = mock_post.call_args.kwargs["json"]
            assert body["thinking"] == {"type": "enabled", "budget_tokens": expect}
            assert body["max_tokens"] == expect + 4096  # budget + max_output

    @patch("agent_cli.providers.anthropic.requests.post")
    def test_anthropic_effort_off_disables_thinking(self, mock_post, caps_thinking):
        """reasoning_effort 'off' → 사고 비활성, max_tokens 원복."""
        mock_post.return_value = _mock_response(
            {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}
        )
        provider = AnthropicProvider("https://api.anthropic.com/v1", "key")
        provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="claude-sonnet-4-20250514",
            capabilities=caps_thinking,
            settings=CallSettings(thinking={"reasoning_effort": "off"}),
        )
        body = mock_post.call_args.kwargs["json"]
        assert "thinking" not in body
        assert body["max_tokens"] == 4096  # base max_output, no budget added

    @patch("agent_cli.providers.anthropic.requests.post")
    def test_anthropic_enable_false_disables_thinking(self, mock_post, caps_thinking):
        """enable_thinking False → capabilities 가 thinking 지원이어도 비활성."""
        mock_post.return_value = _mock_response(
            {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}
        )
        provider = AnthropicProvider("https://api.anthropic.com/v1", "key")
        provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="claude-sonnet-4-20250514",
            capabilities=caps_thinking,
            settings=CallSettings(thinking={"enable_thinking": False}),
        )
        body = mock_post.call_args.kwargs["json"]
        assert "thinking" not in body
        assert body["max_tokens"] == 4096

    @patch("agent_cli.providers.anthropic.requests.post")
    def test_anthropic_off_wins_over_enable_true(self, mock_post, caps_thinking):
        """eff="off" + enable=True 상충 조합 — 공용 정책의 통일 규칙: "off" 가
        이긴다 (OpenAI 와 동형 — 종전 Anthropic 의미를 공용 규칙으로 채택)."""
        mock_post.return_value = _mock_response(
            {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}
        )
        provider = AnthropicProvider("https://api.anthropic.com/v1", "key")
        provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="claude-sonnet-4-20250514",
            capabilities=caps_thinking,
            settings=CallSettings(
                thinking={"enable_thinking": True, "reasoning_effort": "off"}
            ),
        )
        body = mock_post.call_args.kwargs["json"]
        assert "thinking" not in body
        assert body["max_tokens"] == 4096

    @patch("agent_cli.providers.anthropic.requests.post")
    def test_anthropic_overrides_ignored_when_not_supported(
        self, mock_post, caps_no_thinking
    ):
        """v8.21.1: supports_thinking=False 면 enable_thinking True·effort 오버라이드를
        모두 무시 — 사고 미지원 모델에 thinking 블록을 보내 400 나는 것 방지."""
        mock_post.return_value = _mock_response(
            {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}
        )
        provider = AnthropicProvider("https://api.anthropic.com/v1", "key")
        for ov in (
            {"enable_thinking": True},
            {"reasoning_effort": "high"},
            {"enable_thinking": True, "reasoning_effort": "high"},
        ):
            provider.call(
                messages=[{"role": "user", "content": "hi"}],
                system="sys",
                model="claude-sonnet-4-20250514",
                capabilities=caps_no_thinking,
                request_overrides=ov,
            )
            body = mock_post.call_args.kwargs["json"]
            assert "thinking" not in body
            assert body["max_tokens"] == 4096  # base max_output, 사고 미주입

    @patch("agent_cli.providers.openai.requests.post")
    def test_openai_reasoning_effort(self, mock_post, caps_thinking):
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
        )
        body = mock_post.call_args.kwargs["json"]
        assert body["reasoning_effort"] == "medium"

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


class TestResolveThinkingPolicy:
    """공용 정책 함수(resolve_thinking_policy) — request_overrides 해석의
    단일 소스(T1 잔여, 리뷰 §4.2). 프로바이더는 이 결과를 방언으로 번역만."""

    def _caps(self, supports):
        from agent_cli.providers.capabilities import ModelCapabilities

        return ModelCapabilities(
            context_window=32768, max_output_tokens=4096, supports_thinking=supports
        )

    def test_unsupported_returns_none(self):
        from agent_cli.providers.base import resolve_thinking_policy

        assert resolve_thinking_policy(self._caps(False), None) is None
        assert (
            resolve_thinking_policy(self._caps(False), {"enable_thinking": True})
            is None
        )

    def test_default_enabled_medium(self):
        from agent_cli.providers.base import resolve_thinking_policy

        p = resolve_thinking_policy(self._caps(True), None)
        assert p.enabled is True
        assert p.effort == "medium"
        assert p.enable_override is None

    def test_effort_override(self):
        from agent_cli.providers.base import resolve_thinking_policy

        for eff in ("low", "medium", "high"):
            p = resolve_thinking_policy(self._caps(True), {"reasoning_effort": eff})
            assert p.enabled is True and p.effort == eff

    def test_off_wins_over_explicit_enable(self):
        from agent_cli.providers.base import resolve_thinking_policy

        p = resolve_thinking_policy(
            self._caps(True), {"enable_thinking": True, "reasoning_effort": "off"}
        )
        assert p.enabled is False
        assert p.enable_override is True  # 원본 오버라이드는 보존 (방언 방출용)

    def test_enable_false_disables(self):
        from agent_cli.providers.base import resolve_thinking_policy

        p = resolve_thinking_policy(
            self._caps(True), {"enable_thinking": False, "reasoning_effort": "high"}
        )
        assert p.enabled is False
        assert p.enable_override is False

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
