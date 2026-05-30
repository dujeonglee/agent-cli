"""LLM provider protocol and response types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from agent_cli.providers.capabilities import ModelCapabilities


@dataclass
class TokenUsage:
    input_tokens: int
    output_tokens: int
    # Durations in nanoseconds.
    # OpenAI/Anthropic: client-measured via streaming.
    prompt_eval_ns: int = 0  # prefill / time-to-first-token
    eval_ns: int = 0  # decode / first-to-last token
    ttft_ns: int = 0  # client-measured TTFT (all providers, streaming only)
    # Anthropic prompt cache. Non-zero only when cache_control is set on
    # request blocks. ``input_tokens`` excludes both cache fields, so the
    # billable input total is input_tokens + cache_creation + cache_read.
    cache_creation_input_tokens: int = 0  # tokens written to cache (25% premium)
    cache_read_input_tokens: int = 0  # tokens served from cache (10% cost)


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[dict] | None = None
    usage: TokenUsage | None = None
    stop_reason: str | None = None
    # Reasoning content surfaced via a separate API field (e.g. Anthropic
    # thinking blocks, OpenAI reasoning). Empty string when the provider
    # doesn't expose it or the model didn't produce any.
    thinking: str = ""


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
