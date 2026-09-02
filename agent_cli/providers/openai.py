"""OpenAI-compatible API provider adapter with streaming support.

Covers: OpenAI, vLLM, LM Studio, mlx-lm, and any /v1/chat/completions endpoint.
"""

from __future__ import annotations

import json

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


class OpenAIProvider:
    """Adapter for OpenAI-compatible /v1/chat/completions API."""

    @staticmethod
    def capability_transport(base_url: str, model: str, api_key: str = ""):
        """capability 프로브 transport — 프로바이더 클래스 소유 (v8.41.0
        self-register: 종전 capabilities 쪽 provider-이름 분기 흡수)."""
        from agent_cli.providers.capabilities import _OpenAITransport

        return _OpenAITransport(base_url, model, api_key)

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

        # Thinking/reasoning effort — 해석은 공용 ``resolve_thinking_policy``
        # (supports_thinking=False 면 None → 일절 미주입, v8.21.1 게이트).
        # 여기는 정책을 OpenAI 방언으로 번역만 한다:
        #  · enabled → reasoning_effort (기본 medium, 오버라이드 low/medium/high).
        #  · disabled → reasoning_effort **미주입** — 종전엔 enable_thinking=False
        #    여도 effort 가 잔존해 엄격 백엔드에 상충 신호를 보냈다(리뷰 §4.2).
        #  · chat_template_kwargs.enable_thinking (Qwen/MLX 스위치)은 명시
        #    enable_thinking 오버라이드가 있을 때만 방출 — 방출 표면 불변,
        #    값은 정책의 enabled (eff="off"+enable=True 조합도 프로바이더 간 동형).
        # 세션-런타임 노브 (v8.55.0, base.CallSettings): thinking 오버라이드·
        # 스트림 무진전 한도·요청-시 클램프 max_tokens 가 한 컨테이너로 온다.
        settings = kwargs.get("settings") or CallSettings()
        if settings.max_output_tokens is not None:
            body["max_tokens"] = settings.max_output_tokens
        policy = resolve_thinking_policy(capabilities, settings.thinking)
        if policy is not None:
            if policy.enabled:
                body["reasoning_effort"] = policy.effort
            if policy.enable_override is not None:
                ctk = dict(body.get("chat_template_kwargs") or {})
                ctk["enable_thinking"] = policy.enabled
                body["chat_template_kwargs"] = ctk

        if on_chunk:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
            # 스트리밍 POST + idle 재연결 — 골격은 http.stream_with_reconnect
            # 공용 (v8.41.0; timeout/patient-socket/재전송 의미는 그 docstring).
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
        """OpenAI-호환 SSE 스트림 — 골격(idle/파싱/누산/조기종료/interrupt)은
        ``http.run_sse_stream`` 공용, 여기는 이벤트 shape 해석과 usage 조립만
        (C6, v4.48.0). ``degeneration_check``/``interrupt_check`` 의미는 골격
        docstring 참조."""
        acc = run_sse_stream(
            r,
            on_chunk,
            map_payload=_map_openai_payload,
            degeneration_check=degeneration_check,
            degeneration_trigger=degeneration_trigger,
            interrupt_check=interrupt_check,
            idle_timeout_s=idle_timeout_s,
            on_thinking=on_thinking,
        )
        usage = None
        if acc.usage_fields:
            usage = TokenUsage(
                input_tokens=acc.usage_fields.get("input_tokens", 0),
                output_tokens=acc.usage_fields.get("output_tokens", 0),
                prompt_eval_ns=acc.ttft_ns,
                eval_ns=acc.decode_ns,
                ttft_ns=acc.ttft_ns,
            )
        content, inline_think = strip_think_blocks(acc.content)
        thinking = "\n\n".join(x for x in (acc.thinking, inline_think) if x)
        return LLMResponse(
            content=content,
            tool_calls=None,
            usage=usage,
            stop_reason=acc.stop_reason,
            thinking=thinking,
        )

    def _parse_response(self, data: dict) -> LLMResponse:
        """Parse non-streaming response."""
        choice = data["choices"][0]
        message = choice["message"]
        # 인라인 <think> 류 태그 격리 (MiMo 등 — 5.10.0) + vLLM 관례
        # reasoning_content 필드. 둘 다 thinking 으로 합류.
        content, inline_think = strip_think_blocks(message.get("content") or "")
        thinking = "\n\n".join(
            x for x in (message.get("reasoning_content") or "", inline_think) if x
        )

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


def _map_openai_payload(data: dict) -> StreamEvent | None:
    """OpenAI-호환 chunk 하나 → 정규화 StreamEvent (provider 고유 부분 전부).

    - 최종 chunk 의 ``usage``(stream_options.include_usage) → usage_fields
    - ``choices[0].delta.content`` → text
    - ``delta.reasoning_content`` → thinking (vLLM 관례 — qwen3/R1 계열;
      OpenAI 호스티드는 미노출이라 자연 무시)
    - ``finish_reason`` → stop_reason
    """
    ev = StreamEvent()
    usage_data = data.get("usage")
    if usage_data:
        ev.usage_fields = {
            "input_tokens": usage_data.get("prompt_tokens", 0),
            "output_tokens": usage_data.get("completion_tokens", 0),
        }
    choices = data.get("choices", [])
    if choices:
        delta = choices[0].get("delta", {})
        ev.thinking = delta.get("reasoning_content", "") or ""
        ev.text = delta.get("content", "") or ""
        finish = choices[0].get("finish_reason")
        if finish:
            ev.stop_reason = finish
    if not (ev.text or ev.thinking or ev.stop_reason or ev.usage_fields):
        return None
    return ev


# self-register (v8.41.0) — 프로바이더 추가 = 모듈 1개 + 내장 목록 1줄.
from agent_cli.providers import register_provider as _register_provider

_register_provider("openai", OpenAIProvider)
