"""Standardized return type for all tool functions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ToolResult:
    """Standardized return type for all tool functions."""

    success: bool
    output: str = ""
    error: str = ""
    artifact: str = ""  # artifact path (delegate subdir, etc.)
