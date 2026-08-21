"""OpenAI-compatible API provider adapter with streaming support.

Covers: OpenAI, vLLM, LM Studio, mlx-lm, and any /v1/chat/completions endpoint.
"""

from __future__ import annotations

import json

import requests

from agent_cli.constants import (
    LLM_API_TIMEOUT,
    LLM_READ_TIMEOUT,
    LLM_STREAM_TIMEOUT,
    STREAM_MAX_RECONNECTS,
)
from agent_cli.providers.base import LLMResponse, TokenUsage, strip_think_blocks
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.providers.http import (
    StreamEvent,
    StreamIdleTimeout,
    make_stream_patient,
    post_with_retry,
    raise_for_status_with_body,
    run_sse_stream,
)


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

        # Thinking/reasoning effort for reasoning models (o1, o3, etc.)
        if capabilities.supports_thinking and capabilities.thinking_budget > 0:
            if capabilities.thinking_budget <= 1024:
                body["reasoning_effort"] = "low"
            elif capabilities.thinking_budget <= 8192:
                body["reasoning_effort"] = "medium"
            else:
                body["reasoning_effort"] = "high"

        # Per-session runtime overrides (web UI 사고/노력 컨트롤) — applied on top of
        # the capabilities-derived defaults so they win at request time.
        #  · reasoning_effort: "low"|"medium"|"high" 로 덮어쓰거나 "off"/None 이면 제거.
        #  · enable_thinking: True/False → body["chat_template_kwargs"](Qwen/MLX 스위치;
        #    테스트로 이 백엔드에서 유효 확인). None 이면 미전송(모델 기본값 유지).
        overrides = kwargs.get("request_overrides") or {}
        eff = overrides.get("reasoning_effort")
        if eff in ("low", "medium", "high"):
            body["reasoning_effort"] = eff
        elif eff == "off":
            body.pop("reasoning_effort", None)
        enable = overrides.get("enable_thinking")
        if enable is not None:
            ctk = dict(body.get("chat_template_kwargs") or {})
            ctk["enable_thinking"] = bool(enable)
            body["chat_template_kwargs"] = ctk

        if on_chunk:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
            # Stream timeout is (connect 30s, read 30s) — the short read bounds
            # the header wait + interrupt-during-header. After post() returns we
            # relax the socket to patient (LLM_READ_TIMEOUT) so body reads aren't
            # killed at 30s; the poll-loop idle detector handles body stalls and
            # raises StreamIdleTimeout after ~10min of silence, which we
            # reconnect + re-send (up to STREAM_MAX_RECONNECTS).
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
        """OpenAI-호환 SSE 스트림 — 골격(idle/파싱/누산/조기종료/interrupt)은
        ``http.run_sse_stream`` 공용, 여기는 이벤트 shape 해석과 usage 조립만
        (C6, v4.48.0). ``degeneration_check``/``interrupt_check`` 의미는 골격
        docstring 참조."""
        acc = run_sse_stream(
            r,
            on_chunk,
            map_payload=_map_openai_payload,
            degeneration_check=degeneration_check,
            interrupt_check=interrupt_check,
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
