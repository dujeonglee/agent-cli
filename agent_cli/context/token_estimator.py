"""Token estimation using chars/4 heuristic (matches pi-mono)."""

from __future__ import annotations


def estimate_tokens(text: str | None) -> int:
    """Estimate token count from text length. ~4 chars per token."""
    if not text:
        return 0
    return len(text) // 4
