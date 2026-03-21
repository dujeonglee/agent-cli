"""Token estimation using chars/4 heuristic (matches pi-mono)."""

from __future__ import annotations


def estimate_tokens(text: str | None) -> int:
    """Estimate token count from text length. ~4 chars per token."""
    if not text:
        return 0
    return len(text) // 4


def estimate_tokens_from_messages(messages: list[dict]) -> int:
    """Estimate total tokens across a message list."""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            # Anthropic content blocks: estimate from serialized form
            content = str(content)
        total += estimate_tokens(content or "")
        total += 4  # role + formatting overhead per message
    return total
