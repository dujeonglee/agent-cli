"""Tests for provider adapters (mocked HTTP)."""

from unittest.mock import MagicMock, patch

import pytest

from agent_cli.providers import create_provider
from agent_cli.providers.anthropic import AnthropicProvider
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.compat import ModelCapabilities
from agent_cli.providers.ollama import OllamaProvider, REACT_JSON_SCHEMA
from agent_cli.providers.openai_compat import OpenAICompatProvider


@pytest.fixture
def caps_structured():
    return ModelCapabilities(
        context_window=32768,
        max_output_tokens=4096,
        supports_structured_output=True,
        supports_tool_calling=False,
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
        supports_tool_calling=False,
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


class TestOpenAICompatProvider:
    @patch("agent_cli.providers.openai_compat.requests.post")
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

        provider = OpenAICompatProvider("https://api.openai.com/v1", "test-key")
        result = provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="gpt-4o",
            capabilities=caps_structured,
        )

        assert isinstance(result, LLMResponse)
        body = mock_post.call_args.kwargs["json"]
        assert body["response_format"] == {"type": "json_object"}

    @patch("agent_cli.providers.openai_compat.requests.post")
    def test_without_structured_output(self, mock_post, caps_basic):
        mock_post.return_value = _mock_response(
            {
                "choices": [
                    {"message": {"content": "plain text"}, "finish_reason": "stop"}
                ],
            }
        )

        provider = OpenAICompatProvider("http://localhost:8080/v1", "")
        result = provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="local-model",
            capabilities=caps_basic,
        )

        body = mock_post.call_args.kwargs["json"]
        assert "response_format" not in body
        assert result.content == "plain text"


class TestOllamaProvider:
    @patch("agent_cli.providers.ollama.requests.post")
    def test_with_constrained_decoding(self, mock_post, caps_structured):
        mock_post.return_value = _mock_response(
            {
                "message": {"content": '{"thought": "hi"}'},
                "prompt_eval_count": 10,
                "eval_count": 5,
                "done_reason": "stop",
            }
        )

        provider = OllamaProvider("http://localhost:11434")
        result = provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="qwen3:32b",
            capabilities=caps_structured,
        )

        body = mock_post.call_args.kwargs["json"]
        assert body["format"] == REACT_JSON_SCHEMA
        assert result.usage.input_tokens == 10
        assert result.usage.output_tokens == 5

    @patch("agent_cli.providers.ollama.requests.post")
    def test_without_constrained_decoding(self, mock_post, caps_basic):
        mock_post.return_value = _mock_response(
            {
                "message": {"content": '{"thought": "hi"}'},
            }
        )

        provider = OllamaProvider("http://localhost:11434")
        provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="old-model",
            capabilities=caps_basic,
        )

        body = mock_post.call_args.kwargs["json"]
        assert body["format"] == "json"


@pytest.fixture
def caps_tool_calling():
    return ModelCapabilities(
        context_window=128000,
        max_output_tokens=4096,
        supports_structured_output=True,
        supports_tool_calling=True,
        supports_thinking=False,
        thinking_budget=0,
        supports_strict_schema=True,
    )


class TestAnthropicToolCalling:
    @patch("agent_cli.providers.anthropic.requests.post")
    def test_tool_use_extracted(self, mock_post, caps_tool_calling):
        mock_post.return_value = _mock_response(
            {
                "content": [
                    {"type": "text", "text": "I'll read the file."},
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "read_file",
                        "input": {"path": "a.py"},
                    },
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "stop_reason": "tool_use",
            }
        )

        provider = AnthropicProvider("https://api.anthropic.com/v1", "key")
        tools = [
            {
                "name": "read_file",
                "description": "Read file",
                "input_schema": {"type": "object"},
            }
        ]
        result = provider.call(
            messages=[{"role": "user", "content": "read a.py"}],
            system="sys",
            model="claude-sonnet-4-20250514",
            capabilities=caps_tool_calling,
            tools=tools,
        )

        assert result.tool_calls is not None
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0]["name"] == "read_file"
        assert result.tool_calls[0]["input"] == {"path": "a.py"}
        assert result.tool_calls[0]["id"] == "tu_1"
        assert result.content == "I'll read the file."

    @patch("agent_cli.providers.anthropic.requests.post")
    def test_tools_sent_in_request(self, mock_post, caps_tool_calling):
        mock_post.return_value = _mock_response(
            {
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
            }
        )

        provider = AnthropicProvider("https://api.anthropic.com/v1", "key")
        tools = [
            {
                "name": "shell",
                "description": "Run cmd",
                "input_schema": {"type": "object"},
            }
        ]
        provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="claude-sonnet-4-20250514",
            capabilities=caps_tool_calling,
            tools=tools,
        )

        body = mock_post.call_args.kwargs["json"]
        assert "tools" in body
        assert body["tools"] == tools

    @patch("agent_cli.providers.anthropic.requests.post")
    def test_no_tool_calling_regression(self, mock_post, caps_basic):
        """When supports_tool_calling=False, tool_calls should be None."""
        mock_post.return_value = _mock_response(
            {
                "content": [{"type": "text", "text": '{"thought": "t"}'}],
                "stop_reason": "end_turn",
            }
        )

        provider = AnthropicProvider("https://api.anthropic.com/v1", "key")
        result = provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="claude-sonnet-4-20250514",
            capabilities=caps_basic,
        )

        assert result.tool_calls is None
        body = mock_post.call_args.kwargs["json"]
        assert "tools" not in body


class TestOpenAIToolCalling:
    @patch("agent_cli.providers.openai_compat.requests.post")
    def test_tool_calls_extracted(self, mock_post, caps_tool_calling):
        mock_post.return_value = _mock_response(
            {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "shell",
                                        "arguments": '{"command": "ls"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        )

        provider = OpenAICompatProvider("https://api.openai.com/v1", "key")
        tools = [{"type": "function", "function": {"name": "shell", "parameters": {}}}]
        result = provider.call(
            messages=[{"role": "user", "content": "ls"}],
            system="sys",
            model="gpt-4o",
            capabilities=caps_tool_calling,
            tools=tools,
        )

        assert result.tool_calls is not None
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0]["name"] == "shell"
        assert result.tool_calls[0]["input"] == {"command": "ls"}

    @patch("agent_cli.providers.openai_compat.requests.post")
    def test_tools_sent_no_response_format(self, mock_post, caps_tool_calling):
        """When tool calling is used, response_format should NOT be set."""
        mock_post.return_value = _mock_response(
            {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            }
        )

        provider = OpenAICompatProvider("https://api.openai.com/v1", "key")
        tools = [{"type": "function", "function": {"name": "shell", "parameters": {}}}]
        provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="gpt-4o",
            capabilities=caps_tool_calling,
            tools=tools,
        )

        body = mock_post.call_args.kwargs["json"]
        assert "tools" in body
        assert "response_format" not in body

    @patch("agent_cli.providers.openai_compat.requests.post")
    def test_no_tool_calling_regression(self, mock_post, caps_basic):
        """When supports_tool_calling=False, should use response_format instead."""
        mock_post.return_value = _mock_response(
            {
                "choices": [
                    {"message": {"content": '{"thought":"t"}'}, "finish_reason": "stop"}
                ],
            }
        )

        provider = OpenAICompatProvider("http://localhost:8080/v1", "")
        result = provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="local",
            capabilities=caps_basic,
        )

        assert result.tool_calls is None
        body = mock_post.call_args.kwargs["json"]
        assert "tools" not in body


@pytest.fixture
def caps_thinking():
    return ModelCapabilities(
        context_window=32768,
        max_output_tokens=4096,
        supports_structured_output=True,
        supports_tool_calling=False,
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
        supports_tool_calling=False,
        supports_thinking=False,
        thinking_budget=0,
        supports_strict_schema=False,
    )


class TestThinkingBudget:
    @patch("agent_cli.providers.ollama.requests.post")
    def test_ollama_num_predict(self, mock_post, caps_thinking):
        mock_post.return_value = _mock_response(
            {
                "message": {"content": '{"thought": "hi"}'},
            }
        )
        provider = OllamaProvider("http://localhost:11434")
        provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="qwen3:32b",
            capabilities=caps_thinking,
        )
        body = mock_post.call_args.kwargs["json"]
        assert body["options"]["num_predict"] == 4096 + 4096

    @patch("agent_cli.providers.ollama.requests.post")
    def test_ollama_no_thinking_no_options(self, mock_post, caps_no_thinking):
        mock_post.return_value = _mock_response(
            {
                "message": {"content": '{"thought": "hi"}'},
            }
        )
        provider = OllamaProvider("http://localhost:11434")
        provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="llama3.1:8b",
            capabilities=caps_no_thinking,
        )
        body = mock_post.call_args.kwargs["json"]
        assert "options" not in body

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

    @patch("agent_cli.providers.openai_compat.requests.post")
    def test_openai_reasoning_effort(self, mock_post, caps_thinking):
        mock_post.return_value = _mock_response(
            {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            }
        )
        provider = OpenAICompatProvider("https://api.openai.com/v1", "key")
        provider.call(
            messages=[{"role": "user", "content": "hi"}],
            system="sys",
            model="o3-mini",
            capabilities=caps_thinking,
        )
        body = mock_post.call_args.kwargs["json"]
        assert body["reasoning_effort"] == "medium"

    @patch("agent_cli.providers.openai_compat.requests.post")
    def test_openai_no_thinking_regression(self, mock_post, caps_no_thinking):
        mock_post.return_value = _mock_response(
            {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            }
        )
        provider = OpenAICompatProvider("https://api.openai.com/v1", "key")
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
        assert isinstance(p, OpenAICompatProvider)

    def test_ollama(self):
        p = create_provider("ollama", "http://localhost:11434", "")
        assert isinstance(p, OllamaProvider)

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            create_provider("gemini", "http://x", "")
