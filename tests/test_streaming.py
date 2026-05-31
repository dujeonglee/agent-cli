"""Tests for streaming support across all providers."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities

# Shared test capabilities
_CAPS = ModelCapabilities(
    context_window=4096,
    max_output_tokens=2048,
    supports_structured_output=False,
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
        from agent_cli.providers.openai import OpenAIProvider

        lines = self._make_sse_lines(
            ["Hi", " there"], {"prompt_tokens": 10, "completion_tokens": 2}
        )
        resp = _make_response(lines)
        collected = []

        with patch("agent_cli.providers.openai.requests.post", return_value=resp):
            provider = OpenAIProvider("http://localhost:8000/v1", "key")
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
        from agent_cli.providers.openai import OpenAIProvider

        resp = MagicMock()
        resp.json.return_value = {
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1},
        }
        resp.raise_for_status.return_value = None

        with patch(
            "agent_cli.providers.openai.requests.post", return_value=resp
        ) as mock_post:
            provider = OpenAIProvider("http://localhost:8000/v1", "key")
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

    def test_streaming_parses_cache_usage(self):
        """Cache fields in message_start.usage flow through to TokenUsage."""
        from agent_cli.providers.anthropic import AnthropicProvider

        lines = [
            b"event: message_start",
            b'data: {"type":"message_start","message":{"usage":'
            b'{"input_tokens":3,"cache_creation_input_tokens":120,'
            b'"cache_read_input_tokens":80}}}',
            b"event: content_block_delta",
            b'data: {"type":"content_block_delta",'
            b'"delta":{"type":"text_delta","text":"ok"}}',
            b"event: message_delta",
            b'data: {"type":"message_delta",'
            b'"delta":{"stop_reason":"end_turn"},'
            b'"usage":{"output_tokens":1}}',
        ]
        resp = _make_response(lines)
        with patch("agent_cli.providers.anthropic.requests.post", return_value=resp):
            provider = AnthropicProvider("https://api.anthropic.com/v1", "key")
            result = provider.call(
                messages=[{"role": "user", "content": "hi"}],
                system="sys",
                model="m",
                capabilities=_CAPS,
                on_chunk=lambda c: None,
            )

        assert result.usage.cache_creation_input_tokens == 120
        assert result.usage.cache_read_input_tokens == 80
        assert result.usage.input_tokens == 3


# ── Loop Streaming Wiring ───────────────────────────


class TestLoopStreamingWiring:
    def test_on_chunk_always_passed(self):
        """on_chunk is always passed (streaming is default behavior now)."""
        from agent_cli.loop import AgentLoop

        provider = MagicMock()
        provider.call.return_value = LLMResponse(content='{"thought":"hi"}')

        loop = AgentLoop(
            query="test",
            provider=provider,
            capabilities=_CAPS,
            model="test",
        )
        loop._setup()
        loop.turn = 1
        loop._call_llm()

        call_kwargs = provider.call.call_args[1]
        assert "on_chunk" in call_kwargs
        assert callable(call_kwargs["on_chunk"])


# ── TTFT Measurement ────────────────────────────────


class TestTTFTMeasurement:
    def test_openai_ttft_measured(self):
        """OpenAI streaming measures TTFT and decode time client-side."""
        from agent_cli.providers.openai import OpenAIProvider

        lines = []
        data = {"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]}
        lines.append(f"data: {json.dumps(data)}".encode())
        final = {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1},
        }
        lines.append(f"data: {json.dumps(final)}".encode())
        lines.append(b"data: [DONE]")
        resp = _make_response(lines)

        with patch("agent_cli.providers.openai.requests.post", return_value=resp):
            provider = OpenAIProvider("http://localhost:8000/v1", "key")
            result = provider.call(
                messages=[],
                system="",
                model="m",
                capabilities=_CAPS,
                on_chunk=lambda c: None,
            )

        assert result.usage is not None
        assert result.usage.ttft_ns > 0
        assert result.usage.prompt_eval_ns > 0  # client-measured
        assert result.usage.eval_ns >= 0

    def test_anthropic_ttft_measured(self):
        """Anthropic streaming measures TTFT and decode time client-side."""
        from agent_cli.providers.anthropic import AnthropicProvider

        lines = []
        lines.append(b"event: message_start")
        start = {
            "type": "message_start",
            "message": {"usage": {"input_tokens": 5}},
        }
        lines.append(f"data: {json.dumps(start)}".encode())
        lines.append(b"event: content_block_delta")
        delta = {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "ok"},
        }
        lines.append(f"data: {json.dumps(delta)}".encode())
        lines.append(b"event: message_delta")
        md = {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 1},
        }
        lines.append(f"data: {json.dumps(md)}".encode())
        resp = _make_response(lines)

        with patch("agent_cli.providers.anthropic.requests.post", return_value=resp):
            provider = AnthropicProvider("https://api.anthropic.com/v1", "key")
            result = provider.call(
                messages=[{"role": "user", "content": "hi"}],
                system="sys",
                model="m",
                capabilities=_CAPS,
                on_chunk=lambda c: None,
            )

        assert result.usage is not None
        assert result.usage.ttft_ns > 0
        assert result.usage.prompt_eval_ns > 0
        assert result.usage.eval_ns >= 0

    def test_non_streaming_no_ttft(self):
        """Non-streaming calls should have ttft_ns=0."""
        from agent_cli.providers.openai import OpenAIProvider

        resp = MagicMock()
        resp.json.return_value = {
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1},
        }
        resp.raise_for_status.return_value = None

        with patch("agent_cli.providers.openai.requests.post", return_value=resp):
            provider = OpenAIProvider("http://localhost:8000/v1", "key")
            result = provider.call(
                messages=[], system="", model="m", capabilities=_CAPS
            )

        assert result.usage is not None
        assert result.usage.ttft_ns == 0

    def _usage(self, **kw):
        from agent_cli.providers.base import TokenUsage

        defaults = dict(
            input_tokens=100,
            output_tokens=50,
            prompt_eval_ns=200_000_000,
            eval_ns=100_000_000,
            ttft_ns=200_000_000,
        )
        defaults.update(kw)
        return TokenUsage(**defaults)

    def test_token_stats_shows_ttft_speed_and_context(self):
        """_format_token_stats renders ttft, speed, and context occupancy."""
        from agent_cli.loop import _build_token_stats
        from agent_cli.render.minimal import _format_token_stats

        stats = _build_token_stats(self._usage(), context_window=4096, total_out=50)
        msg = _format_token_stats(stats)
        assert "ttft: 200ms" in msg
        assert "tok/s" in msg
        assert "ctx:" in msg  # context occupancy shown

    def test_token_stats_context_percent(self):
        from agent_cli.loop import _build_token_stats
        from agent_cli.render.minimal import _format_token_stats

        stats = _build_token_stats(
            self._usage(input_tokens=2048, output_tokens=10),
            context_window=4096,
            total_out=10,
        )
        assert "(50%)" in _format_token_stats(stats)

    def test_token_stats_cumulative_output(self):
        from agent_cli.loop import _build_token_stats
        from agent_cli.render.minimal import _format_token_stats

        stats = _build_token_stats(self._usage(), context_window=4096, total_out=1800)
        assert "Σout: 1.8K" in _format_token_stats(stats)

    def _minimal_render_to_string(self, verbose):
        from io import StringIO
        from rich.console import Console
        from agent_cli.loop import _build_token_stats
        from agent_cli.render.minimal import MinimalRenderer

        buf = StringIO()
        r = MinimalRenderer(Console(file=buf, force_terminal=False))
        stats = _build_token_stats(self._usage(), context_window=4096, total_out=50)
        r.token_usage(stats, turn=1, verbose=verbose)
        return buf.getvalue()

    def test_token_usage_non_verbose_hints_raw_access(self):
        """Non-verbose line tells users raw responses need --verbose."""
        assert "--verbose" in self._minimal_render_to_string(verbose=False)

    def test_token_usage_verbose_omits_hint(self):
        """Verbose mode shows the raw response panel, so no hint here."""
        assert "--verbose" not in self._minimal_render_to_string(verbose=True)
