"""Ollama API provider adapter with constrained decoding and streaming support."""

from __future__ import annotations

import json
import sys

import requests

from agent_cli.constants import LLM_API_TIMEOUT

from agent_cli.providers.base import LLMResponse, TokenUsage
from agent_cli.providers.compat import ModelCapabilities
from agent_cli.providers.http import post_with_retry

# JSON Schema for ReAct format used with Ollama's constrained decoding.
# "thought" is free-form string; action/action_input are schema-enforced.
REACT_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "thought": {"type": "string"},
        "action": {"type": "string"},
        "action_input": {},
    },
    "required": ["thought"],
}


class OllamaProvider:
    """Adapter for Ollama's /api/chat endpoint."""

    def __init__(self, base_url: str, api_key: str = ""):
        self.base_url = base_url.rstrip("/")

    def call(
        self,
        messages: list[dict],
        system: str,
        model: str,
        capabilities: ModelCapabilities,
        **kwargs,
    ) -> LLMResponse:
        on_chunk = kwargs.get("on_chunk")
        url = f"{self.base_url}/api/chat"
        msgs = [{"role": "system", "content": system}] + messages

        body: dict = {
            "model": model,
            "stream": bool(on_chunk),
            "messages": msgs,
        }

        # format control: skip_json_format=True disables all JSON forcing
        # (needed for plan generation where free-form text is expected)
        skip_json = kwargs.get("skip_json_format", False)
        if not skip_json:
            if capabilities.supports_structured_output:
                body["format"] = REACT_JSON_SCHEMA
            else:
                body["format"] = "json"

        # Thinking budget: allocate enough output tokens for thinking + response
        if capabilities.supports_thinking and capabilities.thinking_budget > 0:
            body.setdefault("options", {})
            body["options"]["num_predict"] = (
                capabilities.thinking_budget + capabilities.max_output_tokens
            )

        try:
            r = post_with_retry(
                requests.post,
                url,
                json=body,
                timeout=LLM_API_TIMEOUT,
                stream=bool(on_chunk),
            )
            r.raise_for_status()
        except requests.exceptions.HTTPError as e:
            # Only one recovery path lives here: Ollama < 0.5.0 rejects
            # JSON Schema in the `format` field with 400. Anything else —
            # 401 (no API key), 403 (cloud model subscription required),
            # 429, 5xx — must bubble up so the user sees the real error
            # instead of an empty-response retry loop. Earlier versions
            # caught but never re-raised non-400 HTTPErrors; the 403
            # response body would then be streamed as if it were normal
            # content and produce "" on every turn.
            is_json_schema_400 = (
                capabilities.supports_structured_output
                and e.response is not None
                and e.response.status_code == 400
            )
            if not is_json_schema_400:
                raise
            print(
                "[warn] Ollama rejected JSON Schema format. "
                "Falling back to basic JSON mode. "
                "Upgrade Ollama to 0.5.0+ for constrained decoding.",
                file=sys.stderr,
            )
            body["format"] = "json"
            body["stream"] = False
            r = post_with_retry(requests.post, url, json=body, timeout=LLM_API_TIMEOUT)
            r.raise_for_status()
            on_chunk = None  # fell back to non-streaming

        if on_chunk:
            return self._handle_stream(r, on_chunk)

        data = r.json()
        return self._parse_response(data)

    def _handle_stream(self, r, on_chunk) -> LLMResponse:
        """Process streaming NDJSON response."""
        import time

        content = ""
        final_data = {}
        t0 = time.perf_counter_ns()
        t_first = 0
        for line in r.iter_lines():
            if not line:
                continue
            data = json.loads(line)
            # Ollama keeps HTTP 200 but can emit {"error": "..."} lines
            # mid-stream (e.g., mlx runner failure, cache corruption).
            # raise_for_status() is already past; the only signal is the
            # top-level `error` key, which normal chunks never carry.
            # Surfacing it avoids silently collapsing to empty content
            # and looping on "Invalid JSON" retries.
            if "error" in data:
                raise RuntimeError(f"Ollama streaming error: {data['error']}")
            chunk = data.get("message", {}).get("content", "")
            if chunk:
                if not t_first:
                    t_first = time.perf_counter_ns()
                content += chunk
                on_chunk(chunk)
            if data.get("done"):
                final_data = data
                break

        ttft_ns = (t_first - t0) if t_first else 0

        usage = None
        if "eval_count" in final_data or "prompt_eval_count" in final_data:
            usage = TokenUsage(
                input_tokens=final_data.get("prompt_eval_count", 0),
                output_tokens=final_data.get("eval_count", 0),
                prompt_eval_ns=final_data.get("prompt_eval_duration", 0),
                eval_ns=final_data.get("eval_duration", 0),
                ttft_ns=ttft_ns,
            )

        return LLMResponse(
            content=content,
            tool_calls=None,
            usage=usage,
            stop_reason=final_data.get("done_reason"),
        )

    def _parse_response(self, data: dict) -> LLMResponse:
        """Parse non-streaming response."""
        # Mirror of the streaming guard: Ollama sometimes returns HTTP
        # 200 with a body shaped {"error": "..."} (no message, no done).
        # raise_for_status() won't catch that; surface it explicitly so
        # the loop can render the failure instead of seeing empty content.
        if "error" in data:
            raise RuntimeError(f"Ollama error: {data['error']}")
        content = data.get("message", {}).get("content", "")

        usage = None
        if "eval_count" in data or "prompt_eval_count" in data:
            usage = TokenUsage(
                input_tokens=data.get("prompt_eval_count", 0),
                output_tokens=data.get("eval_count", 0),
                prompt_eval_ns=data.get("prompt_eval_duration", 0),
                eval_ns=data.get("eval_duration", 0),
            )

        return LLMResponse(
            content=content,
            tool_calls=None,
            usage=usage,
            stop_reason=data.get("done_reason"),
        )
