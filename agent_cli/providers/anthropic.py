"""Anthropic API provider adapter with native tool calling support."""
from __future__ import annotations

import requests

from agent_cli.constants import LLM_API_TIMEOUT

from agent_cli.providers.base import LLMResponse, TokenUsage
from agent_cli.providers.compat import ModelCapabilities


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
        url = f"{self.base_url}/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }
        body: dict = {
            "model": model,
            "max_tokens": capabilities.max_output_tokens,
            "system": system,
            "messages": messages,
        }

        # Native tool calling: pass tool definitions to API
        tools = kwargs.get("tools")
        if capabilities.supports_tool_calling and tools:
            body["tools"] = tools

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

        r = requests.post(url, headers=headers, json=body, timeout=LLM_API_TIMEOUT)
        r.raise_for_status()
        data = r.json()

        # Parse response content blocks
        content = ""
        tool_calls = None
        for block in data.get("content", []):
            if block.get("type") == "text":
                content = block["text"]
            elif block.get("type") == "tool_use":
                if tool_calls is None:
                    tool_calls = []
                tool_calls.append({
                    "id": block["id"],
                    "name": block["name"],
                    "input": block["input"],  # already a dict
                })

        usage = None
        usage_data = data.get("usage")
        if usage_data:
            usage = TokenUsage(
                input_tokens=usage_data.get("input_tokens", 0),
                output_tokens=usage_data.get("output_tokens", 0),
            )

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            stop_reason=data.get("stop_reason"),
        )
