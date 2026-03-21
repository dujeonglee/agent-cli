"""Context overflow detection across providers."""
from __future__ import annotations

import re

from agent_cli.constants import OVERFLOW_RESERVE_TOKENS
from agent_cli.context.token_estimator import estimate_tokens_from_messages
from agent_cli.providers.compat import ModelCapabilities

# Provider-specific overflow error patterns (pi-mono reference)
OVERFLOW_PATTERNS = [
    # Anthropic
    r"prompt is too long",
    r"maximum context length",
    # OpenAI
    r"maximum context length.*exceeded",
    r"This model's maximum context length is",
    r"reduce the length of the messages",
    # Ollama / llama.cpp
    r"context length exceeded",
    r"token limit",
    r"exceeds the model's context",
    # Generic
    r"too many tokens",
    r"input.*too long",
    r"context window",
]

_COMPILED_PATTERNS = [re.compile(p, re.I) for p in OVERFLOW_PATTERNS]


def is_context_overflow(error_message: str) -> bool:
    """Check if an error message indicates context overflow."""
    return any(p.search(error_message) for p in _COMPILED_PATTERNS)


def check_preemptive_overflow(
    messages: list[dict],
    capabilities: ModelCapabilities,
    reserve_tokens: int = OVERFLOW_RESERVE_TOKENS,
) -> bool:
    """Check if messages will likely exceed context window before calling LLM."""
    estimated = estimate_tokens_from_messages(messages)
    return estimated > (capabilities.context_window - reserve_tokens)
