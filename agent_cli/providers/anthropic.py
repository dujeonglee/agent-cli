"""Anthropic API provider adapter with streaming support."""

from __future__ import annotations

import json

import requests

from agent_cli.constants import LLM_API_TIMEOUT

from agent_cli.providers.base import LLMResponse, TokenUsage
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.providers.http import post_with_retry


class AnthropicProvider:
    """Adapter for the Anthropic Messages API (/v1/messages)."""

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
        url = f"{self.base_url}/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }
        # System is sent as a single content block with ``cache_control``
        # to enable Anthropic prompt caching. The whole system prompt is
        # the cache key — it must be byte-stable across calls for cache
        # hits (see system_prompt.py: Date excluded for this reason).
        # Non-Claude endpoints that don't recognize cache_control should
        # ignore the field; behavior on strict proxies is unverified.
        body: dict = {
            "model": model,
            "max_tokens": capabilities.max_output_tokens,
            "system": [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": messages,
        }

        # Thinking budget: enable extended thinking with budget
        if capabilities.supports_thinking and capabilities.thinking_budget > 0:
            body["thinking"] = {
                "type": "enabled",
                "budget_tokens": capabilities.thinking_budget,
            }
            # Anthropic deducts thinking from max_tokens
            body["max_tokens"] = (
                capabilities.thinking_budget + capabilities.max_output_tokens
            )

        if on_chunk:
            body["stream"] = True
            r = post_with_retry(
                requests.post,
                url,
                headers=headers,
                json=body,
                timeout=LLM_API_TIMEOUT,
                stream=True,
            )
            r.raise_for_status()
            return self._handle_stream(r, on_chunk, kwargs.get("interrupt_check"))

        r = post_with_retry(
            requests.post, url, headers=headers, json=body, timeout=LLM_API_TIMEOUT
        )
        r.raise_for_status()
        return self._parse_response(r.json())

    def _handle_stream(self, r, on_chunk, interrupt_check=None) -> LLMResponse:
        """Process Anthropic SSE streaming response.

        ``interrupt_check`` (optional): a zero-arg predicate polled once per
        text chunk. True means the user interrupted (Ctrl+C / web stop)
        mid-generation, so the stream is closed and the partial returned with
        ``stop_reason="interrupted"`` — the loop discards it (see the openai
        provider's ``_handle_stream`` for the rationale on closing here rather
        than from the signal handler / another thread)."""
        import time

        content = ""
        thinking = ""
        stop_reason = None
        input_tokens = 0
        output_tokens = 0
        cache_creation_input_tokens = 0
        cache_read_input_tokens = 0
        t0 = time.perf_counter_ns()
        t_first = 0

        for line in r.iter_lines():
            if not line:
                continue
            line_str = line.decode("utf-8") if isinstance(line, bytes) else line

            if line_str.startswith("data: "):
                payload = line_str[6:]
                try:
                    data = json.loads(payload)
                except json.JSONDecodeError:
                    continue

                event_type = data.get("type", "")

                if event_type == "message_start":
                    usage = data.get("message", {}).get("usage", {})
                    input_tokens = usage.get("input_tokens", 0)
                    cache_creation_input_tokens = usage.get(
                        "cache_creation_input_tokens", 0
                    )
                    cache_read_input_tokens = usage.get("cache_read_input_tokens", 0)

                elif event_type == "content_block_delta":
                    delta = data.get("delta", {})
                    delta_type = delta.get("type")
                    if delta_type == "text_delta":
                        chunk = delta.get("text", "")
                        if chunk:
                            if not t_first:
                                t_first = time.perf_counter_ns()
                            content += chunk
                            on_chunk(chunk)
                            if interrupt_check is not None and interrupt_check():
                                stop_reason = "interrupted"
                                r.close()
                                break
                    elif delta_type == "thinking_delta":
                        # Extended-thinking deltas: accumulate but do
                        # not stream to on_chunk — thinking is internal
                        # reasoning, not user-facing output.
                        thinking += delta.get("thinking", "")

                elif event_type == "message_delta":
                    stop_reason = data.get("delta", {}).get("stop_reason")
                    usage = data.get("usage", {})
                    output_tokens = usage.get("output_tokens", output_tokens)

        t_end = time.perf_counter_ns()
        ttft_ns = (t_first - t0) if t_first else 0
        decode_ns = (t_end - t_first) if t_first else 0

        usage = None
        if (
            input_tokens
            or output_tokens
            or cache_creation_input_tokens
            or cache_read_input_tokens
        ):
            usage = TokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                prompt_eval_ns=ttft_ns,
                eval_ns=decode_ns,
                ttft_ns=ttft_ns,
                cache_creation_input_tokens=cache_creation_input_tokens,
                cache_read_input_tokens=cache_read_input_tokens,
            )

        return LLMResponse(
            content=content,
            tool_calls=None,
            usage=usage,
            stop_reason=stop_reason,
            thinking=thinking,
        )

    def _parse_response(self, data: dict) -> LLMResponse:
        """Parse non-streaming response."""
        content = ""
        thinking = ""
        tool_calls = None
        for block in data.get("content", []):
            btype = block.get("type")
            if btype == "text":
                content = block["text"]
            elif btype == "thinking":
                # Extended-thinking block: capture for diagnostics and
                # for self-quoting on retry. Anthropic places reasoning
                # in a dedicated content block, not inside text.
                thinking = block.get("thinking", "")
            elif btype == "tool_use":
                if tool_calls is None:
                    tool_calls = []
                tool_calls.append(
                    {
                        "id": block["id"],
                        "name": block["name"],
                        "input": block["input"],
                    }
                )

        usage = None
        usage_data = data.get("usage")
        if usage_data:
            usage = TokenUsage(
                input_tokens=usage_data.get("input_tokens", 0),
                output_tokens=usage_data.get("output_tokens", 0),
                cache_creation_input_tokens=usage_data.get(
                    "cache_creation_input_tokens", 0
                ),
                cache_read_input_tokens=usage_data.get("cache_read_input_tokens", 0),
            )

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            stop_reason=data.get("stop_reason"),
            thinking=thinking,
        )
