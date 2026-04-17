"""Skill loader — discovers and parses skill files from disk.

Uses ResourceLoader for file discovery + frontmatter parsing.
Skill-specific logic (hooks, Skill dataclass construction) stays here.

Search paths (project root takes priority):
  1. .agent-cli/skills/*.md  (project local, flat)
  2. .agent-cli/skills/<name>/SKILL.md  (project local, directory)
  3. ~/.agent-cli/skills/*.md (user global, flat)
  4. ~/.agent-cli/skills/<name>/SKILL.md (user global, directory)
  5. agent_cli/skills/builtin/* (package built-in)
"""

from __future__ import annotations

from pathlib import Path

from agent_cli.hooks import parse_hooks_config
from agent_cli.resource_loader import ResourceLoader
from agent_cli.skills.models import Skill

# Search order: project root first (priority), then user home, then built-in
_BUILTIN_DIR = Path(__file__).parent / "builtin"

_SEARCH_PATHS = [
    Path.cwd() / ".agent-cli" / "skills",
    Path.home() / ".agent-cli" / "skills",
    _BUILTIN_DIR,
]

_loader = ResourceLoader(_SEARCH_PATHS, pattern="*.md", dir_entry="SKILL.md")


def _reset_loader(search_paths: list[Path] | None = None) -> None:
    """Reset the loader with new search paths (for testing)."""
    global _loader
    paths = search_paths if search_paths is not None else _SEARCH_PATHS
    _loader = ResourceLoader(paths, pattern="*.md", dir_entry="SKILL.md")


def load_skills() -> dict[str, Skill]:
    """Load all skills from disk. Project-local skills override user-global.

    Always rescans disk so skills authored mid-session (e.g. via /create-skill)
    are visible without a restart. Cost is negligible for typical skill counts.
    """
    resources = _loader.load_all()
    skills: dict[str, Skill] = {}
    for name, res in resources.items():
        skill = _resource_to_skill(res)
        if skill:
            skills[name] = skill
    return skills


def _parse_skill_file(path: Path) -> Skill | None:
    """Parse a single skill file. Used by tests and executor."""
    res = ResourceLoader._parse_file(path)
    if res is None:
        return None
    return _resource_to_skill(res)


def _resource_to_skill(res) -> Skill | None:
    """Convert a Resource to a Skill dataclass.

    Skills require YAML frontmatter (at minimum name or description).
    Plain markdown files without frontmatter are skipped.
    """
    meta = res.meta
    if not meta:
        return None  # Skills require frontmatter

    return Skill(
        name=res.name,
        description=meta.get("description", ""),
        prompt_template=res.body,
        allowed_tools=meta.get("allowed-tools"),
        max_turns=int(meta.get("max-turns", 0)),
        argument_hint=meta.get("argument-hint", ""),
        model=meta.get("model"),
        context=meta.get("context"),
        hooks=_parse_hooks(meta.get("hooks")),
        disable_model_invocation=bool(meta.get("disable-model-invocation", False)),
        user_invocable=bool(meta.get("user-invocable", True)),
        source_path=res.source_path,
    )


def _parse_hooks(raw) -> dict | None:
    """Parse hooks from frontmatter into HookMatcher dicts."""
    if not raw or not isinstance(raw, dict):
        return None
    parsed = parse_hooks_config(raw)
    return parsed if parsed else None
