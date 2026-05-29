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
    # mlx-lm / omlx (verified against a live omlx server, 2026-05-30):
    #   "Prompt too long: 360012 tokens exceeds max context window of
    #    262144 tokens"
    # The generic ``context window`` rule below already matches this, but
    # an explicit pattern keeps detection robust if omlx rewords the
    # phrasing (and documents the real shape for the next reader).
    r"prompt too long",
    r"exceeds max context window",
    # Generic
    r"too many tokens",
    r"input.*too long",
    r"context window",
]

_COMPILED_PATTERNS = [re.compile(p, re.I) for p in OVERFLOW_PATTERNS]


def is_context_overflow(error_message: str) -> bool:
    """Check if an error message indicates context overflow."""
    return any(p.search(error_message) for p in _COMPILED_PATTERNS)


# Amount-extraction patterns, tried in order. Each captures two integers:
# the *actual* prompt size and the model's *limit*, in whatever order the
# provider phrases them (the group indices encode which is which). These
# let the recovery layer reconcile the (often wrong) local estimate with
# the server's authoritative count and compute exactly how much to shed.
#
# Each tuple: (compiled_regex, actual_group, limit_group).
_AMOUNT_PATTERNS = [
    # mlx-lm / omlx: "<actual> tokens exceeds max context window of <limit> tokens"
    (
        re.compile(r"(\d+)\s+tokens?\s+exceeds\s+max context window of\s+(\d+)", re.I),
        1,
        2,
    ),
    # Anthropic: "prompt is too long: <actual> tokens > <limit> maximum"
    (re.compile(r"(\d+)\s+tokens?\s*>\s*(\d+)\s+maximum", re.I), 1, 2),
    # OpenAI: "maximum context length is <limit> tokens. However, your
    #          messages resulted in <actual> tokens"
    (
        re.compile(
            r"maximum context length is\s+(\d+)\s+tokens.*?resulted in\s+(\d+)",
            re.I | re.S,
        ),
        2,
        1,
    ),
]


def parse_overflow_amounts(error_message: str) -> tuple[int | None, int | None]:
    """Extract ``(actual_tokens, limit_tokens)`` from an overflow error.

    Returns the server-reported actual prompt size and the model's
    context limit when the message carries them, else ``None`` for any
    field that could not be parsed. Provider phrasings differ in order
    (omlx/Anthropic put the actual first, OpenAI puts the limit first),
    so each pattern records which capture group is which.

    The recovery layer uses ``actual_tokens`` to override its local
    char-based estimate (which under-counts CJK badly) and
    ``limit_tokens`` to set a precise shrink target, turning a blind
    "drop oldest until it works" loop into a calculated one.
    """
    if not error_message:
        return (None, None)
    for regex, actual_grp, limit_grp in _AMOUNT_PATTERNS:
        m = regex.search(error_message)
        if m:
            try:
                return (int(m.group(actual_grp)), int(m.group(limit_grp)))
            except (ValueError, IndexError):
                continue
    return (None, None)


def check_preemptive_overflow(
    messages: list[dict],
    capabilities: ModelCapabilities,
    reserve_tokens: int = OVERFLOW_RESERVE_TOKENS,
) -> bool:
    """Check if messages will likely exceed context window before calling LLM."""
    estimated = estimate_tokens_from_messages(messages)
    return estimated > (capabilities.context_window - reserve_tokens)
