"""Anthropic API provider adapter with streaming support."""

from __future__ import annotations


import requests

from agent_cli.constants import (
    LLM_API_TIMEOUT,
    LLM_READ_TIMEOUT,
    LLM_STREAM_TIMEOUT,
    STREAM_MAX_RECONNECTS,
)

from agent_cli.providers.base import LLMResponse, TokenUsage
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.providers.http import (
    StreamEvent,
    StreamIdleTimeout,
    make_stream_patient,
    post_with_retry,
    raise_for_status_with_body,
    run_sse_stream,
)


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
            # openai 동형 idle-재연결 래퍼 (C6): 골격이 침묵 시
            # StreamIdleTimeout 을 올리면 재연결+재전송 — 이전엔 anthropic
            # 만 이 래퍼가 없어 무한 대기했다.
            for attempt in range(STREAM_MAX_RECONNECTS + 1):
                r = post_with_retry(
                    requests.post,
                    url,
                    headers=headers,
                    json=body,
                    timeout=LLM_STREAM_TIMEOUT,
                    stream=True,
                )
                raise_for_status_with_body(r)
                make_stream_patient(r, LLM_READ_TIMEOUT)
                try:
                    return self._handle_stream(
                        r,
                        on_chunk,
                        kwargs.get("degeneration_check"),
                        kwargs.get("interrupt_check"),
                    )
                except StreamIdleTimeout:
                    if attempt >= STREAM_MAX_RECONNECTS:
                        raise
                    from agent_cli.render import render_status

                    render_status(
                        "running",
                        "스트림 무응답 — 재연결 후 재전송 "
                        f"({attempt + 1}/{STREAM_MAX_RECONNECTS})",
                    )

        r = post_with_retry(
            requests.post, url, headers=headers, json=body, timeout=LLM_API_TIMEOUT
        )
        raise_for_status_with_body(r)
        return self._parse_response(r.json())

    def _handle_stream(
        self, r, on_chunk, degeneration_check=None, interrupt_check=None
    ) -> LLMResponse:
        """Anthropic SSE 스트림 — 골격은 ``http.run_sse_stream`` 공용 (C6,
        v4.48.0). 이로써 idle notice/StreamIdleTimeout·JSONDecodeError 관용이
        openai 와 **동일 적용**(이전엔 양쪽에 한 조각씩만 있던 비대칭).
        여기는 이벤트 shape 해석과 캐시-토큰 포함 usage 조립만."""
        acc = run_sse_stream(
            r,
            on_chunk,
            map_payload=_map_anthropic_payload,
            degeneration_check=degeneration_check,
            interrupt_check=interrupt_check,
        )
        f = acc.usage_fields
        usage = None
        if any(
            f.get(k)
            for k in (
                "input_tokens",
                "output_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
            )
        ):
            usage = TokenUsage(
                input_tokens=f.get("input_tokens", 0),
                output_tokens=f.get("output_tokens", 0),
                prompt_eval_ns=acc.ttft_ns,
                eval_ns=acc.decode_ns,
                ttft_ns=acc.ttft_ns,
                cache_creation_input_tokens=f.get("cache_creation_input_tokens", 0),
                cache_read_input_tokens=f.get("cache_read_input_tokens", 0),
            )
        return LLMResponse(
            content=acc.content,
            tool_calls=None,
            usage=usage,
            stop_reason=acc.stop_reason,
            thinking=acc.thinking,
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


def _map_anthropic_payload(data: dict) -> StreamEvent | None:
    """Anthropic 이벤트 하나 → 정규화 StreamEvent (provider 고유 부분 전부).

    - message_start → input + 캐시 2종 usage_fields
    - content_block_delta.text_delta → text / .thinking_delta → thinking
      (thinking 은 on_chunk 로 스트리밍하지 않음 — 골격이 text 만 스트림)
    - message_delta → stop_reason + output_tokens
    """
    event_type = data.get("type", "")
    if event_type == "message_start":
        usage = data.get("message", {}).get("usage", {})
        return StreamEvent(
            usage_fields={
                "input_tokens": usage.get("input_tokens", 0),
                "cache_creation_input_tokens": usage.get(
                    "cache_creation_input_tokens", 0
                ),
                "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
            }
        )
    if event_type == "content_block_delta":
        delta = data.get("delta", {})
        if delta.get("type") == "text_delta":
            return StreamEvent(text=delta.get("text", "") or "")
        if delta.get("type") == "thinking_delta":
            return StreamEvent(thinking=delta.get("thinking", "") or "")
        return None
    if event_type == "message_delta":
        usage = data.get("usage", {})
        ev = StreamEvent(stop_reason=data.get("delta", {}).get("stop_reason"))
        if usage.get("output_tokens"):
            ev.usage_fields = {"output_tokens": usage["output_tokens"]}
        return ev
    return None
