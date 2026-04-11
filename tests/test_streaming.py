"""Tests for streaming support across all providers."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from agent_cli.providers.base import LLMResponse
from agent_cli.providers.compat import ModelCapabilities

# Shared test capabilities
_CAPS = ModelCapabilities(
    context_window=4096,
    max_output_tokens=2048,
    supports_structured_output=False,
    supports_tool_calling=False,
    supports_thinking=False,
    thinking_budget=0,
    supports_strict_schema=False,
)


def _make_response(lines: list[bytes], status_code: int = 200):
    """Create a mock requests.Response with iter_lines."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.iter_lines.return_value = iter(lines)
    resp.raise_for_status.return_value = None
    return resp


# ── Ollama Streaming ────────────────────────────────


class TestOllamaStreaming:
    def _make_stream_lines(self, chunks: list[str], final_data: dict) -> list[bytes]:
        lines = []
        for chunk in chunks:
            lines.append(
                json.dumps({"message": {"content": chunk}, "done": False}).encode()
            )
        final = {**final_data, "done": True, "message": {"content": ""}}
        lines.append(json.dumps(final).encode())
        return lines

    def test_streaming_accumulates_content(self):
        from agent_cli.providers.ollama import OllamaProvider

        chunks = ["Hello", " ", "world"]
        lines = self._make_stream_lines(
            chunks, {"eval_count": 3, "prompt_eval_count": 10}
        )
        resp = _make_response(lines)

        collected = []

        with patch("agent_cli.providers.ollama.requests.post", return_value=resp):
            provider = OllamaProvider("http://localhost:11434")
            result = provider.call(
                messages=[{"role": "user", "content": "hi"}],
                system="sys",
                model="test",
                capabilities=_CAPS,
                on_chunk=lambda c: collected.append(c),
            )

        assert result.content == "Hello world"
        assert collected == ["Hello", " ", "world"]

    def test_streaming_preserves_usage(self):
        from agent_cli.providers.ollama import OllamaProvider

        lines = self._make_stream_lines(
            ["ok"],
            {
                "eval_count": 5,
                "prompt_eval_count": 20,
                "prompt_eval_duration": 100_000_000,
                "eval_duration": 50_000_000,
            },
        )
        resp = _make_response(lines)

        with patch("agent_cli.providers.ollama.requests.post", return_value=resp):
            provider = OllamaProvider("http://localhost:11434")
            result = provider.call(
                messages=[],
                system="",
                model="m",
                capabilities=_CAPS,
                on_chunk=lambda c: None,
            )

        assert result.usage is not None
        assert result.usage.input_tokens == 20
        assert result.usage.output_tokens == 5
        assert result.usage.prompt_eval_ns == 100_000_000
        assert result.usage.eval_ns == 50_000_000

    def test_no_on_chunk_uses_non_streaming(self):
        from agent_cli.providers.ollama import OllamaProvider

        resp = MagicMock()
        resp.json.return_value = {
            "message": {"content": "hello"},
            "done": True,
        }
        resp.raise_for_status.return_value = None

        with patch(
            "agent_cli.providers.ollama.requests.post", return_value=resp
        ) as mock_post:
            provider = OllamaProvider("http://localhost:11434")
            result = provider.call(
                messages=[], system="", model="m", capabilities=_CAPS
            )

        assert result.content == "hello"
        # Verify stream=False was passed
        call_body = mock_post.call_args[1]["json"]
        assert call_body["stream"] is False


# ── OpenAI Streaming ────────────────────────────────


class TestOpenAIStreaming:
    def _make_sse_lines(
        self, chunks: list[str], usage: dict | None = None
    ) -> list[bytes]:
        lines = []
        for i, chunk in enumerate(chunks):
            data = {"choices": [{"delta": {"content": chunk}, "finish_reason": None}]}
            lines.append(f"data: {json.dumps(data)}".encode())
        # Final chunk with finish_reason
        final = {"choices": [{"delta": {}, "finish_reason": "stop"}]}
        if usage:
            final["usage"] = usage
        lines.append(f"data: {json.dumps(final)}".encode())
        lines.append(b"data: [DONE]")
        return lines

    def test_streaming_accumulates_content(self):
        from agent_cli.providers.openai_compat import OpenAICompatProvider

        lines = self._make_sse_lines(
            ["Hi", " there"], {"prompt_tokens": 10, "completion_tokens": 2}
        )
        resp = _make_response(lines)
        collected = []

        with patch(
            "agent_cli.providers.openai_compat.requests.post", return_value=resp
        ):
            provider = OpenAICompatProvider("http://localhost:8000/v1", "key")
            result = provider.call(
                messages=[],
                system="",
                model="m",
                capabilities=_CAPS,
                on_chunk=lambda c: collected.append(c),
            )

        assert result.content == "Hi there"
        assert collected == ["Hi", " there"]
        assert result.usage.input_tokens == 10
        assert result.usage.output_tokens == 2

    def test_no_on_chunk_uses_non_streaming(self):
        from agent_cli.providers.openai_compat import OpenAICompatProvider

        resp = MagicMock()
        resp.json.return_value = {
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1},
        }
        resp.raise_for_status.return_value = None

        with patch(
            "agent_cli.providers.openai_compat.requests.post", return_value=resp
        ) as mock_post:
            provider = OpenAICompatProvider("http://localhost:8000/v1", "key")
            result = provider.call(
                messages=[], system="", model="m", capabilities=_CAPS
            )

        assert result.content == "hi"
        call_body = mock_post.call_args[1]["json"]
        assert "stream" not in call_body


# ── Anthropic Streaming ─────────────────────────────


class TestAnthropicStreaming:
    def _make_sse_lines(
        self, chunks: list[str], input_tokens: int = 10, output_tokens: int = 5
    ) -> list[bytes]:
        lines = []
        # message_start
        lines.append(b"event: message_start")
        start = {
            "type": "message_start",
            "message": {"usage": {"input_tokens": input_tokens}},
        }
        lines.append(f"data: {json.dumps(start)}".encode())
        # content chunks
        for chunk in chunks:
            lines.append(b"event: content_block_delta")
            delta = {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": chunk},
            }
            lines.append(f"data: {json.dumps(delta)}".encode())
        # message_delta (stop + output usage)
        lines.append(b"event: message_delta")
        md = {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": output_tokens},
        }
        lines.append(f"data: {json.dumps(md)}".encode())
        return lines

    def test_streaming_accumulates_content(self):
        from agent_cli.providers.anthropic import AnthropicProvider

        lines = self._make_sse_lines(["Hey", "!"], input_tokens=8, output_tokens=2)
        resp = _make_response(lines)
        collected = []

        with patch("agent_cli.providers.anthropic.requests.post", return_value=resp):
            provider = AnthropicProvider("https://api.anthropic.com/v1", "key")
            result = provider.call(
                messages=[{"role": "user", "content": "hi"}],
                system="sys",
                model="m",
                capabilities=_CAPS,
                on_chunk=lambda c: collected.append(c),
            )

        assert result.content == "Hey!"
        assert collected == ["Hey", "!"]
        assert result.usage.input_tokens == 8
        assert result.usage.output_tokens == 2
        assert result.stop_reason == "end_turn"

    def test_no_on_chunk_uses_non_streaming(self):
        from agent_cli.providers.anthropic import AnthropicProvider

        resp = MagicMock()
        resp.json.return_value = {
            "content": [{"type": "text", "text": "hello"}],
            "usage": {"input_tokens": 5, "output_tokens": 1},
            "stop_reason": "end_turn",
        }
        resp.raise_for_status.return_value = None

        with patch("agent_cli.providers.anthropic.requests.post", return_value=resp):
            provider = AnthropicProvider("https://api.anthropic.com/v1", "key")
            result = provider.call(
                messages=[{"role": "user", "content": "hi"}],
                system="sys",
                model="m",
                capabilities=_CAPS,
            )

        assert result.content == "hello"


# ── Loop Streaming Wiring ───────────────────────────


class TestLoopStreamingWiring:
    def test_suppress_output_no_streaming(self):
        """When suppress_output=True, on_chunk should NOT be passed."""
        from agent_cli.loop import AgentLoop

        provider = MagicMock()
        provider.call.return_value = LLMResponse(
            content='{"thought":"hi","action":"complete","action_input":{"result":"done"}}'
        )

        loop = AgentLoop(
            query="test",
            provider=provider,
            capabilities=_CAPS,
            model="test",
            suppress_output=True,
        )
        loop._setup()
        loop.turn = 1
        loop._call_llm()

        # Verify on_chunk was NOT passed
        call_kwargs = provider.call.call_args[1]
        assert "on_chunk" not in call_kwargs

    def test_normal_output_has_streaming(self):
        """When suppress_output=False, on_chunk should be passed."""
        from agent_cli.loop import AgentLoop

        provider = MagicMock()
        provider.call.return_value = LLMResponse(content='{"thought":"hi"}')

        loop = AgentLoop(
            query="test",
            provider=provider,
            capabilities=_CAPS,
            model="test",
            suppress_output=False,
        )
        loop._setup()
        loop.turn = 1
        loop._call_llm()

        call_kwargs = provider.call.call_args[1]
        assert "on_chunk" in call_kwargs
        assert callable(call_kwargs["on_chunk"])
