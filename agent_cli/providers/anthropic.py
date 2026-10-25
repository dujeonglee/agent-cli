"""Anthropic API provider adapter with streaming support."""

from __future__ import annotations

import requests

from agent_cli import images as _images
from agent_cli.constants import (
    LLM_API_TIMEOUT,
)
from agent_cli.providers.base import (
    CallSettings,
    LLMResponse,
    TokenUsage,
    resolve_sampling,
    strip_think_blocks,
    thinking_switch,
)
from agent_cli.providers.capabilities import ModelCapabilities, vision_support
from agent_cli.providers.http import (
    StreamEvent,
    post_with_retry,
    raise_for_status_with_body,
    run_sse_stream,
    stream_with_reconnect,
)

# 사고 켬의 Anthropic 모양 (v10.72.0). 현행 Claude 모델(4.6 이후)은 ``budget_tokens``
# 를 거절(400)하고 정도는 서버 기본값(``output_config.effort``)이 정한다 — 하니스는
# 켬/끔만 말한다. Anthropic-호환 로컬 서버(omlx)도 같은 ``type`` 어휘를 받는다.
_THINKING_ON = {"type": "adaptive"}

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
        thinking_stop = kwargs.get("thinking_stop")
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
        # 이미지 (v10.59.0): ``images`` 참조를 여기서만 ``image`` 블록으로 펼친다
        # — openai.py 와 같은 자리·같은 규칙(못 보면 글로 남긴다).
        enabled = any(
            isinstance(m.get("images"), list) and m["images"] for m in messages
        ) and vision_support(
            capabilities, self.capability_transport, self.base_url, model, self.api_key
        )
        messages = _images.expand(
            messages, enabled=enabled, part=_images.anthropic_part
        )
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

        # 사고 스위치 (v10.72.0): 지원 모델이고 세션이 켰을 때만 ``thinking`` 블록
        # (``_THINKING_ON``), 끔이면 블록 없음(모델 기본 — 끔을 말하는 ``disabled`` 는
        # 현행 모델 일부가 거절한다). 정도는 보내지 않는다(위 ``_THINKING_ON`` 주석).
        # 세션-런타임 노브 (v8.55.0, base.CallSettings): 사고 스위치·스트림 무진전
        # 한도·요청-시 클램프 max_tokens 가 한 컨테이너로 온다.
        settings = kwargs.get("settings") or CallSettings()
        if settings.max_output_tokens is not None:
            body["max_tokens"] = settings.max_output_tokens
        if thinking_switch(capabilities, settings.thinking):
            body["thinking"] = dict(_THINKING_ON)
        # 🎲 샘플링 (v10.69.0): OpenAI 와 같은 이름·같은 규칙(세션 > 엔트리 > 미주입).
        # Anthropic 의 범위(temperature 0~1, 확장 사고 중 temperature 고정)는 서버가
        # 거절로 말한다 — 하니스가 미리 좁히지 않는다.
        body.update(resolve_sampling(capabilities, settings.sampling))

        if on_chunk:
            body["stream"] = True
            # 스트리밍 POST + idle 재연결 — 골격은 http.stream_with_reconnect
            # 공용 (v8.41.0; 종전 openai 와 28행 동형 복붙의 단일화).
            return stream_with_reconnect(
                url,
                headers=headers,
                body=body,
                handle_stream=lambda r, attempt, attempts: self._handle_stream(
                    r,
                    on_chunk,
                    kwargs.get("degeneration_check"),
                    kwargs.get("interrupt_check"),
                    thinking_stop=thinking_stop,
                    degeneration_trigger=kwargs.get("degeneration_trigger", "#"),
                    idle_timeout_s=settings.stream_idle_timeout_s,
                    on_thinking=kwargs.get("on_thinking"),
                    attempt=attempt,
                    attempts=attempts,
                ),
                max_attempts=settings.stream_max_attempts,
            )

        r = post_with_retry(
            requests.post, url, headers=headers, json=body, timeout=LLM_API_TIMEOUT
        )
        raise_for_status_with_body(r)
        return self._parse_response(r.json(), thinking_stop=thinking_stop)

    def _handle_stream(
        self,
        r,
        on_chunk,
        degeneration_check=None,
        interrupt_check=None,
        degeneration_trigger="#",
        idle_timeout_s=None,
        on_thinking=None,
        attempt: int = 1,
        attempts: int = 1,
        thinking_stop=None,
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
            attempt=attempt,
            attempts=attempts,
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
        content, inline_think = strip_think_blocks(acc.content, stop=thinking_stop)
        thinking = "\n\n".join(x for x in (acc.thinking, inline_think) if x)
        return LLMResponse(
            content=content,
            tool_calls=None,
            usage=usage,
            stop_reason=_normalize_stop_reason(acc.stop_reason),
            stop_detail=acc.stop_detail,
            thinking=thinking,
        )

    def _parse_response(self, data: dict, *, thinking_stop=None) -> LLMResponse:
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

        content, inline_think = strip_think_blocks(
            "".join(text_parts), stop=thinking_stop
        )
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
