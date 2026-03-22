"""Skill loader — discovers and parses skill files from disk.

Search paths (project root takes priority):
  1. .agent-cli/skills/*.md  (project local)
  2. ~/.agent-cli/skills/*.md (user global)
"""

from __future__ import annotations

import re
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

from agent_cli.skills.models import Skill

# Search order: project root first (priority), then user home
_SEARCH_PATHS = [
    Path.cwd() / ".agent-cli" / "skills",
    Path.home() / ".agent-cli" / "skills",
]

_FRONTMATTER_PATTERN = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n(.*)",
    re.S,
)


_cached_skills: dict[str, Skill] | None = None


def load_skills(use_cache: bool = True) -> dict[str, Skill]:
    """Load all skills from disk. Project-local skills override user-global.

    Results are cached after first load. Pass use_cache=False to force reload.
    """
    global _cached_skills
    if use_cache and _cached_skills is not None:
        return _cached_skills

    skills: dict[str, Skill] = {}

    # Load in reverse order so project-local overrides user-global
    for search_dir in reversed(_SEARCH_PATHS):
        if not search_dir.is_dir():
            continue
        for md_file in sorted(search_dir.glob("*.md")):
            skill = _parse_skill_file(md_file)
            if skill:
                skills[skill.name] = skill

    _cached_skills = skills
    return skills


def _parse_skill_file(path: Path) -> Skill | None:
    """Parse a skill markdown file with YAML frontmatter."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        import sys

        print(f"[warn] Cannot read skill file {path}: {e}", file=sys.stderr)
        return None

    match = _FRONTMATTER_PATTERN.match(text)
    if not match:
        return None

    frontmatter_text = match.group(1)
    body = match.group(2).strip()

    if yaml is not None:
        try:
            meta = yaml.safe_load(frontmatter_text)
        except Exception:
            meta = _parse_simple_frontmatter(frontmatter_text)
    else:
        meta = _parse_simple_frontmatter(frontmatter_text)

    if not isinstance(meta, dict):
        return None

    name = meta.get("name", "")
    if not name:
        # Use filename as fallback
        name = path.stem

    return Skill(
        name=name,
        description=meta.get("description", ""),
        prompt_template=body,
        allowed_tools=meta.get("allowed-tools"),
        max_iter=int(meta.get("max-iter", 0)),
        argument_hint=meta.get("argument-hint", ""),
        source_path=str(path),
    )


def _parse_simple_frontmatter(text: str) -> dict:
    """Fallback frontmatter parser when PyYAML is not available."""
    result = {}
    for line in text.strip().split("\n"):
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            # Handle list values: [a, b, c]
            if value.startswith("[") and value.endswith("]"):
                value = [v.strip().strip("'\"") for v in value[1:-1].split(",")]
            elif value.isdigit():
                value = int(value)
            result[key] = value
    return result
