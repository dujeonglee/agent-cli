"""OpenAI-compatible API provider adapter with native tool calling support.

Covers: OpenAI, vLLM, LM Studio, mlx-lm, and any /v1/chat/completions endpoint.
"""

from __future__ import annotations

import json

import requests

from agent_cli.constants import LLM_API_TIMEOUT

from agent_cli.providers.base import LLMResponse, TokenUsage
from agent_cli.providers.compat import ModelCapabilities


class OpenAICompatProvider:
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
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        msgs = [{"role": "system", "content": system}] + messages

        body: dict = {
            "model": model,
            "max_tokens": capabilities.max_output_tokens,
            "messages": msgs,
        }

        # Native tool calling: pass tool definitions to API
        tools = kwargs.get("tools")
        if capabilities.supports_tool_calling and tools:
            body["tools"] = tools
            # Don't use response_format with tool calling (mutually exclusive)
        elif capabilities.supports_structured_output:
            body["response_format"] = {"type": "json_object"}

        # Thinking/reasoning effort for reasoning models (o1, o3, etc.)
        if capabilities.supports_thinking and capabilities.thinking_budget > 0:
            if capabilities.thinking_budget <= 1024:
                body["reasoning_effort"] = "low"
            elif capabilities.thinking_budget <= 8192:
                body["reasoning_effort"] = "medium"
            else:
                body["reasoning_effort"] = "high"

        r = requests.post(url, headers=headers, json=body, timeout=LLM_API_TIMEOUT)
        r.raise_for_status()
        data = r.json()

        choice = data["choices"][0]
        content = choice["message"].get("content") or ""

        # Parse native tool calls if present
        tool_calls = None
        raw_tool_calls = choice["message"].get("tool_calls")
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
        )
