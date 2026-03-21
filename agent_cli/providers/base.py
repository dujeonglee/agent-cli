"""LLM provider protocol and response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from agent_cli.providers.compat import ModelCapabilities


@dataclass
class TokenUsage:
    input_tokens: int
    output_tokens: int


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[dict] | None = None
    usage: TokenUsage | None = None
    stop_reason: str | None = None


@runtime_checkable
class LLMProvider(Protocol):
    """Protocol that all provider adapters must satisfy."""

    def call(
        self,
        messages: list[dict],
        system: str,
        model: str,
        capabilities: ModelCapabilities,
        **kwargs,
    ) -> LLMResponse: ...
