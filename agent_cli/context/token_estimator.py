"""Token estimation using chars/4 heuristic (matches pi-mono)."""

from __future__ import annotations


def estimate_tokens(text: str | None) -> int:
    """Estimate token count from text length. ~4 chars per token."""
    if not text:
        return 0
    return len(text) // 4


def utf8_len(text: str | None) -> int:
    """UTF-8 byte length — the unit of the measured tokens-per-byte ratio."""
    return len(text.encode("utf-8", "replace")) if text else 0
