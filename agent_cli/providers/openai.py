"""OpenAI-compatible API provider adapter with streaming support.

Covers: OpenAI, vLLM, LM Studio, mlx-lm, and any /v1/chat/completions endpoint.
"""

from __future__ import annotations

import json

import requests

from agent_cli.constants import LLM_API_TIMEOUT

from agent_cli.providers.base import LLMResponse, TokenUsage
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.providers.http import post_with_retry


class OpenAIProvider:
    """Adapter for OpenAI-compatible /v1/chat/completions API."""

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def call(
        self,
        messages: list[dict],
        system: str,
        model: str,
        capabilities: ModelCapabilities,
        **kwargs,
    ) -> LLMResponse:
        on_chunk = kwargs.get("on_chunk")
        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        msgs = [{"role": "system", "content": system}] + messages

        body: dict = {
            "model": model,
            "max_tokens": capabilities.max_output_tokens,
            "messages": msgs,
        }

        # JSON-object mode is requested by the wire plugin via ``json_mode``
        # (computed in ``WireFormat.provider_call_kwargs`` from the model's
        # capabilities — the single wire ⨯ capability decision point). The
        # provider does NOT inspect ``capabilities`` for this; it just
        # honors the wire's decision. Forcing JSON on a non-JSON wire
        # (prefix_md's markdown) makes the model degenerate (the ``[2025]``
        # / ``[1000, 1000]`` bug).
        if kwargs.get("json_mode"):
            body["response_format"] = {"type": "json_object"}

        # Thinking/reasoning effort for reasoning models (o1, o3, etc.)
        if capabilities.supports_thinking and capabilities.thinking_budget > 0:
            if capabilities.thinking_budget <= 1024:
                body["reasoning_effort"] = "low"
            elif capabilities.thinking_budget <= 8192:
                body["reasoning_effort"] = "medium"
            else:
                body["reasoning_effort"] = "high"

        if on_chunk:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
            r = post_with_retry(
                requests.post,
                url,
                headers=headers,
                json=body,
                timeout=LLM_API_TIMEOUT,
                stream=True,
            )
            r.raise_for_status()
            return self._handle_stream(r, on_chunk)

        r = post_with_retry(
            requests.post, url, headers=headers, json=body, timeout=LLM_API_TIMEOUT
        )
        r.raise_for_status()
        return self._parse_response(r.json())

    def _handle_stream(self, r, on_chunk) -> LLMResponse:
        """Process SSE streaming response."""
        import time

        content = ""
        thinking = ""
        usage = None
        stop_reason = None
        t0 = time.perf_counter_ns()
        t_first = 0

        for line in r.iter_lines():
            if not line:
                continue
            line_str = line.decode("utf-8") if isinstance(line, bytes) else line
            if not line_str.startswith("data: "):
                continue
            payload = line_str[6:]
            if payload == "[DONE]":
                break

            data = json.loads(payload)

            # Usage in final chunk (stream_options.include_usage)
            usage_data = data.get("usage")
            if usage_data:
                usage = TokenUsage(
                    input_tokens=usage_data.get("prompt_tokens", 0),
                    output_tokens=usage_data.get("completion_tokens", 0),
                )

            choices = data.get("choices", [])
            if not choices:
                continue

            delta = choices[0].get("delta", {})
            # `reasoning_content` is the vLLM convention for exposing
            # the model's reasoning channel through OpenAI-compatible
            # endpoints (qwen3 / DeepSeek-R1 served via vLLM, etc.).
            # OpenAI's hosted reasoning models do not expose it via
            # Chat Completions, so this stays empty there — graceful.
            thinking_chunk = delta.get("reasoning_content", "")
            if thinking_chunk:
                thinking += thinking_chunk
            chunk = delta.get("content", "")
            if chunk:
                if not t_first:
                    t_first = time.perf_counter_ns()
                content += chunk
                on_chunk(chunk)

            finish = choices[0].get("finish_reason")
            if finish:
                stop_reason = finish

        t_end = time.perf_counter_ns()
        ttft_ns = (t_first - t0) if t_first else 0
        decode_ns = (t_end - t_first) if t_first else 0

        # Enrich usage with client-measured timing
        if usage:
            usage.prompt_eval_ns = ttft_ns
            usage.eval_ns = decode_ns
            usage.ttft_ns = ttft_ns

        return LLMResponse(
            content=content,
            tool_calls=None,
            usage=usage,
            stop_reason=stop_reason,
            thinking=thinking,
        )

    def _parse_response(self, data: dict) -> LLMResponse:
        """Parse non-streaming response."""
        choice = data["choices"][0]
        message = choice["message"]
        content = message.get("content") or ""
        # vLLM convention for reasoning models served via OpenAI-compat.
        # Empty string when the server doesn't expose it.
        thinking = message.get("reasoning_content") or ""

        # Parse tool calls if present
        tool_calls = None
        raw_tool_calls = message.get("tool_calls")
        if raw_tool_calls:
            tool_calls = []
            for tc in raw_tool_calls:
                try:
                    tool_input = json.loads(tc["function"]["arguments"])
                except (json.JSONDecodeError, ValueError, KeyError):
                    tool_input = {}
                tool_calls.append(
                    {
                        "id": tc.get("id", ""),
                        "name": tc["function"]["name"],
                        "input": tool_input,
                    }
                )

        usage = None
        usage_data = data.get("usage")
        if usage_data:
            usage = TokenUsage(
                input_tokens=usage_data.get("prompt_tokens", 0),
                output_tokens=usage_data.get("completion_tokens", 0),
            )

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            stop_reason=choice.get("finish_reason"),
            thinking=thinking,
        )
