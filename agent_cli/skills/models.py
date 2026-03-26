"""Skill data model — Claude Code compatible format."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Skill:
    name: str
    description: str
    prompt_template: str
    allowed_tools: list[str] | None = None  # None = all tools
    max_iter: int = 0  # 0 = use default
    argument_hint: str = ""
    model: str | None = None  # None = use caller's model
    context: str | None = None  # "fork" = independent context
    hooks: dict | None = None  # parsed hook matchers per event
    source_path: str = ""
