"""Anthropic API provider adapter with streaming support."""

from __future__ import annotations

import requests

from agent_cli.constants import (
    LLM_API_TIMEOUT,
)
from agent_cli.providers.base import (
    CallSettings,
    LLMResponse,
    TokenUsage,
    resolve_thinking_policy,
    strip_think_blocks,
)
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.providers.http import (
    StreamEvent,
    post_with_retry,
    raise_for_status_with_body,
    run_sse_stream,
    stream_with_reconnect,
)

# reasoning_effort → Anthropic thinking budget_tokens 번역표. Anthropic 은
# OpenAI 식 low/medium/high enum 이 없어 budget_tokens 가 유일한 사고 레버라,
# web UI 런타임 effort 를 budget 으로 옮긴다. high=32768 은 Anthropic 이 "이
# 이상은 배치 처리 권장" 하는 자연 상한 — 256K 컨텍스트에서 budget+max_output
# 여유 충분. v8.21.0: 정적 thinking_budget 필드 제거 후 medium 이 **기본값** —
# supports_thinking 모델이 effort 오버라이드 없이 사고할 때 이 budget 을 쓴다.
_EFFORT_TO_BUDGET = {"low": 4096, "medium": 16384, "high": 32768}

# Anthropic thinking budget_tokens 하한 (API 제약).
_MIN_THINKING_BUDGET = 1024

# P0-1: stop_reason 정규화 — LLMResponse.stop_reason 은 **루프 어휘**(OpenAI
# finish_reason 계열: "length"=출력 절단, "stop"=정상 종료)로 통일한다. 루프의
# 출력-절단 가드(loop/core.py — ``stop_reason == "length"``)가 Anthropic 원어
# ("max_tokens")를 몰라 절단된 write_file/shell 이 그대로 디스패치되던 버그의
# 수리. 미지의 값(합성 "interrupted"/"degenerate_runaway" 포함)은 통과.
_STOP_REASON_MAP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
}


def _normalize_stop_reason(reason: str | None) -> str | None:
    return _STOP_REASON_MAP.get(reason, reason)


class AnthropicProvider:
    """Adapter for the Anthropic Messages API (/v1/messages)."""

    @staticmethod
    def capability_transport(base_url: str, model: str, api_key: str = ""):
        """capability 프로브 transport — 프로바이더 클래스 소유 (v8.41.0)."""
        from agent_cli.providers.capabilities import _AnthropicTransport

        return _AnthropicTransport(base_url, model, api_key)

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

        # Thinking budget — 해석은 공용 ``resolve_thinking_policy``
        # (supports_thinking=False 면 None → 기본값도 오버라이드도 일절 미적용,
        # v8.21.1 게이트). 여기는 정책을 Anthropic 방언으로 번역만 한다:
        # effort → budget_tokens (Anthropic 은 effort enum 이 없어 budget 이
        # 유일 레버), disabled → thinking 블록 미주입.
        # 세션-런타임 노브 (v8.55.0, base.CallSettings): thinking 오버라이드·
        # 스트림 무진전 한도·요청-시 클램프 max_tokens 가 한 컨테이너로 온다.
        settings = kwargs.get("settings") or CallSettings()
        if settings.max_output_tokens is not None:
            body["max_tokens"] = settings.max_output_tokens
        max_out = body["max_tokens"]
        policy = resolve_thinking_policy(capabilities, settings.thinking)
        if policy is not None and policy.enabled:
            budget = _EFFORT_TO_BUDGET[policy.effort]
            budget = max(budget, _MIN_THINKING_BUDGET)
            body["thinking"] = {"type": "enabled", "budget_tokens": budget}
            # Anthropic deducts thinking from max_tokens
            body["max_tokens"] = budget + max_out

        if on_chunk:
            body["stream"] = True
            # 스트리밍 POST + idle 재연결 — 골격은 http.stream_with_reconnect
            # 공용 (v8.41.0; 종전 openai 와 28행 동형 복붙의 단일화).
            return stream_with_reconnect(
                url,
                headers=headers,
                body=body,
                handle_stream=lambda r: self._handle_stream(
                    r,
                    on_chunk,
                    kwargs.get("degeneration_check"),
                    kwargs.get("interrupt_check"),
                    degeneration_trigger=kwargs.get("degeneration_trigger", "#"),
                    idle_timeout_s=settings.stream_idle_timeout_s,
                    on_thinking=kwargs.get("on_thinking"),
                ),
            )

        r = post_with_retry(
            requests.post, url, headers=headers, json=body, timeout=LLM_API_TIMEOUT
        )
        raise_for_status_with_body(r)
        return self._parse_response(r.json())

    def _handle_stream(
        self,
        r,
        on_chunk,
        degeneration_check=None,
        interrupt_check=None,
        degeneration_trigger="#",
        idle_timeout_s=None,
        on_thinking=None,
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
            degeneration_trigger=degeneration_trigger,
            interrupt_check=interrupt_check,
            idle_timeout_s=idle_timeout_s,
            on_thinking=on_thinking,
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
        # 인라인 <think> 격리 — OpenAI 경로와 동형 (T1 잔여, 리뷰 §4.2).
        # 실 Anthropic 모델은 사고를 thinking 블록으로 내지만, Anthropic-호환
        # 로컬 서버(omlx 등)가 태그를 content 에 흘리면 격리해 thinking 으로.
        content, inline_think = strip_think_blocks(acc.content)
        thinking = "\n\n".join(x for x in (acc.thinking, inline_think) if x)
        return LLMResponse(
            content=content,
            tool_calls=None,
            usage=usage,
            stop_reason=_normalize_stop_reason(acc.stop_reason),
            thinking=thinking,
        )

    def _parse_response(self, data: dict) -> LLMResponse:
        """Parse non-streaming response.

        text/thinking 블록은 **누산**한다 — 다중 블록 응답에서 마지막 블록만
        잔존하던 종전 동작(리뷰 §4.2)을 스트리밍 경로(델타 연결)와 동형화.
        인라인 <think> 격리도 OpenAI 경로와 동형 적용."""
        text_parts: list[str] = []
        think_parts: list[str] = []
        tool_calls = None
        for block in data.get("content", []):
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block["text"])
            elif btype == "thinking":
                # Extended-thinking block: capture for diagnostics and
                # for self-quoting on retry. Anthropic places reasoning
                # in a dedicated content block, not inside text.
                think_parts.append(block.get("thinking", ""))
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

        content, inline_think = strip_think_blocks("".join(text_parts))
        thinking = "\n\n".join(x for x in ("".join(think_parts), inline_think) if x)
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            stop_reason=_normalize_stop_reason(data.get("stop_reason")),
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


# self-register (v8.41.0) — 프로바이더 추가 = 모듈 1개 + 내장 목록 1줄.
from agent_cli.providers import register_provider as _register_provider

_register_provider("anthropic", AnthropicProvider)
