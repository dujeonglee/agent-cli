"""Tests for provider adapters (mocked HTTP)."""

from unittest.mock import MagicMock, patch

import pytest

from agent_cli.providers import create_provider
from agent_cli.providers.anthropic import AnthropicProvider
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.providers.openai import OpenAIProvider


@pytest.fixture
def caps_structured():
    return ModelCapabilities(
        context_window=32768,
        max_output_tokens=4096,
        supports_structured_output=True,
        supports_thinking=False,
        thinking_budget=0,
        supports_strict_schema=False,
    )


@pytest.fixture
def caps_basic():
    return ModelCapabilities(
        context_window=4096,
        max_output_tokens=2048,
        supports_structured_output=False,
        supports_thinking=False,
        thinking_budget=0,
        supports_strict_schema=False,
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
        assert result.stop_reason == "end_turn"

        call_kwargs = mock_post.call_args
        assert "x-api-key" in call_kwargs.kwargs["headers"]
        assert call_kwargs.kwargs["json"]["max_tokens"] == 4096

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
        # input_tokens stays separate — billable total is the sum
        assert result.usage.input_tokens == 5

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


class TestOpenAIProvider:
    @patch("agent_cli.providers.openai.requests.post")
    def test_with_structured_output(self, mock_post, caps_structured):
        mock_post.return_value = _mock_response(
            {
                "choices": [
                    {
                        "message": {"content": '{"thought": "ok"}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        )

        provider = OpenAIProvider("https://api.openai.com/v1", "test-key")
        result = provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="gpt-4o",
            capabilities=caps_structured,
            json_mode=True,  # wire plugin decided JSON mode from capabilities
        )

        assert isinstance(result, LLMResponse)
        body = mock_post.call_args.kwargs["json"]
        assert body["response_format"] == {"type": "json_object"}

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
    def test_no_json_mode_omits_response_format(self, mock_post, caps_structured):
        """When the wire plugin decides ``json_mode=False`` (prefix_md's
        markdown shape), the provider must NOT force the server's JSON-object
        mode even though the model *supports* structured output — the
        provider honors the wire's decision and never re-derives it from
        capabilities. Forcing JSON against a markdown prompt makes omlx/mlx
        degenerate (the ``[2025]`` / ``[1000, 1000]`` bug)."""
        mock_post.return_value = _mock_response(
            {
                "choices": [
                    {
                        "message": {"content": "## Thought\nok"},
                        "finish_reason": "stop",
                    }
                ],
            }
        )

        provider = OpenAIProvider("http://localhost:8080/v1", "")
        provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="local-model",
            capabilities=caps_structured,  # supports_structured_output=True
            json_mode=False,  # wire (markdown shape) said no JSON mode
        )

        body = mock_post.call_args.kwargs["json"]
        assert "response_format" not in body  # honored despite structured support

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
        supports_structured_output=True,
        supports_thinking=True,
        thinking_budget=4096,
        supports_strict_schema=False,
    )


@pytest.fixture
def caps_no_thinking():
    return ModelCapabilities(
        context_window=32768,
        max_output_tokens=4096,
        supports_structured_output=True,
        supports_thinking=False,
        thinking_budget=0,
        supports_strict_schema=False,
    )


class TestThinkingBudget:
    @patch("agent_cli.providers.anthropic.requests.post")
    def test_anthropic_thinking_param(self, mock_post, caps_thinking):
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
        assert body["thinking"] == {"type": "enabled", "budget_tokens": 4096}
        assert body["max_tokens"] == 4096 + 4096

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
