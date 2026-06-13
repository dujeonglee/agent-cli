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

from agent_cli.providers.base import LLMResponse, TokenUsage
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.providers.http import (
    StreamIdleTimeout,
    raise_for_status_with_body,
    interruptible_lines,
    make_stream_patient,
    post_with_retry,
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

        # JSON-object mode is requested by the wire plugin via ``json_mode``
        # (computed in ``WireFormat.provider_call_kwargs`` from the model's
        # capabilities — the single wire ⨯ capability decision point). The
        # provider does NOT inspect ``capabilities`` for this; it just
        # honors the wire's decision. Forcing JSON on a non-JSON wire
        # (md_array's markdown) makes the model degenerate (the ``[2025]``
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
        """Process SSE streaming response.

        ``degeneration_check`` (optional): a predicate on the accumulated
        text. When it returns True the stream is closed early — the model has
        started looping the wire shape (format runaway) and the rest is just
        repetition, so generating to max_tokens would waste tokens/latency.
        The truncated text is still parsed/recorded downstream.

        ``interrupt_check`` (optional): a zero-arg predicate for user interrupt
        (Ctrl+C / web stop). The line read goes through ``interruptible_lines``,
        which polls this during no-data gaps — including the TTFT window before
        the first token — so the interrupt isn't stuck behind a blocking read.
        When it fires the stream is closed and the partial returned with
        ``stop_reason="interrupted"``; unlike the degeneration partial, the
        loop DISCARDS this text (the user is redirecting) — not parsed/recorded.
        """
        import time

        content = ""
        thinking = ""
        usage = None
        stop_reason = None
        t0 = time.perf_counter_ns()
        t_first = 0

        # interruptible_lines runs the blocking read in a reader thread and
        # polls interrupt_check during no-data gaps (TTFT, stalls), so a user
        # interrupt breaks even before the first token. Per-line is the SSE
        # equivalent of per-chunk, so no separate in-loop interrupt check is
        # needed; the interrupt is detected by re-checking interrupt_check()
        # after the loop (the partial is discarded by the loop, not parsed).
        #
        # Idle handling: every STREAM_IDLE_THRESHOLD seconds with no token
        # renders a "still waiting" notice (on_idle); after STREAM_IDLE_MAX_TICKS
        # of silence interruptible_lines raises StreamIdleTimeout for call() to
        # reconnect. The counter resets when a token arrives.
        from agent_cli.constants import STREAM_IDLE_MAX_TICKS, STREAM_IDLE_THRESHOLD

        def _on_idle(tick: int, seconds: float) -> None:
            from agent_cli.render import render_status

            render_status(
                "running",
                f"응답 대기 중 — 토큰 없음 {int(seconds)}s "
                f"({tick}/{STREAM_IDLE_MAX_TICKS}, {STREAM_IDLE_MAX_TICKS * STREAM_IDLE_THRESHOLD // 60}분 후 재연결)",
            )

        for line in interruptible_lines(
            r,
            interrupt_check,
            idle_threshold=STREAM_IDLE_THRESHOLD,
            max_idle_ticks=STREAM_IDLE_MAX_TICKS,
            on_idle=_on_idle,
        ):
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
                # Early-stop format runaway. Gate on '#' so the predicate
                # (regex) only runs when a new header could have arrived,
                # keeping this O(headers) not O(chunks).
                if (
                    degeneration_check is not None
                    and "#" in chunk
                    and degeneration_check(content)
                ):
                    stop_reason = "degenerate_runaway"
                    r.close()
                    break

            finish = choices[0].get("finish_reason")
            if finish:
                stop_reason = finish

        # The reader thread stopped early because the user interrupted; the
        # flag is still set, so label the (discarded) partial accordingly.
        if interrupt_check is not None and interrupt_check():
            stop_reason = "interrupted"

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
