"""Skill loader — discovers and parses skill files from disk.

Search paths (project root takes priority):
  1. .agent-cli/skills/*.md  (project local, flat)
  2. .agent-cli/skills/<name>/SKILL.md  (project local, directory)
  3. ~/.agent-cli/skills/*.md (user global, flat)
  4. ~/.agent-cli/skills/<name>/SKILL.md (user global, directory)
"""

from __future__ import annotations

import re
from pathlib import Path

try:
    import yaml
except ImportError:
    raise ImportError(
        "PyYAML is required for skill loading. Install it with: pip install pyyaml"
    )

from agent_cli.hooks import parse_hooks_config
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
        # Collect all skill files: flat *.md + directory */SKILL.md
        skill_files: list[Path] = sorted(search_dir.glob("*.md"))
        for subdir in sorted(search_dir.iterdir()):
            if subdir.is_dir():
                skill_md = subdir / "SKILL.md"
                if skill_md.is_file():
                    skill_files.append(skill_md)

        # Track sources within this search_dir for duplicate detection
        seen: dict[str, Path] = {}
        for md_file in skill_files:
            skill = _parse_skill_file(md_file)
            if skill:
                if skill.name in seen:
                    raise ValueError(
                        f"Duplicate skill '{skill.name}' found in:\n"
                        f"  - {seen[skill.name]}\n"
                        f"  - {md_file}\n"
                        f"Remove one or rename to resolve the conflict."
                    )
                seen[skill.name] = md_file
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

    try:
        meta = yaml.safe_load(frontmatter_text)
    except Exception:
        return None

    if not isinstance(meta, dict):
        return None

    name = meta.get("name", "")
    if not name:
        # Use parent directory name for SKILL.md, else filename
        if path.name == "SKILL.md":
            name = path.parent.name
        else:
            name = path.stem

    return Skill(
        name=name,
        description=meta.get("description", ""),
        prompt_template=body,
        allowed_tools=meta.get("allowed-tools"),
        max_iter=int(meta.get("max-iter", 0)),
        argument_hint=meta.get("argument-hint", ""),
        model=meta.get("model"),
        context=meta.get("context"),
        hooks=_parse_hooks(meta.get("hooks")),
        disable_model_invocation=bool(meta.get("disable-model-invocation", False)),
        user_invocable=bool(meta.get("user-invocable", True)),
        source_path=str(path),
    )


def _parse_hooks(raw) -> dict | None:
    """Parse hooks from frontmatter into HookMatcher dicts."""
    if not raw or not isinstance(raw, dict):
        return None
    parsed = parse_hooks_config(raw)
    return parsed if parsed else None
