"""Skill data model — Claude Code compatible format."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Skill:
    name: str
    description: str
    prompt_template: str
    active_tools: list[str] | None = None  # None = all tools
    max_iter: int = 0  # 0 = use default
    argument_hint: str = ""
    source_path: str = ""
