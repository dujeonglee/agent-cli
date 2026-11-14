"""Skill loader — discovers and parses skill files from disk.

Uses ResourceLoader for file discovery + frontmatter parsing.
Skill-specific logic (hooks, Skill dataclass construction) stays here.

Search paths (``_SEARCH_PATHS``; an earlier path overrides a later one by name):
  1. <cwd>/.agent-cli/skills/*.md and <name>/SKILL.md  (project)
  2. agent_cli/skills/builtin/*.md and <name>/SKILL.md  (package built-in)

``~/.agent-cli/skills`` is not searched.
"""

from __future__ import annotations

from pathlib import Path

from agent_cli.hooks import parse_hooks_config
from agent_cli.resource_loader import ResourceLoader
from agent_cli.skills.models import Skill

_BUILTIN_DIR = Path(__file__).parent / "builtin"

from agent_cli.paths import project_dir

# 프로젝트 > 내장 (docs/config-scopes) — 스킬은 프로젝트 자산이라 ~/.agent-cli/skills 는 보지 않는다.
_SEARCH_PATHS = [
    project_dir() / "skills",
    _BUILTIN_DIR,
]

_loader = ResourceLoader(_SEARCH_PATHS, pattern="*.md", dir_entry="SKILL.md")


def _reset_loader(search_paths: list[Path] | None = None) -> None:
    """Reset the loader with new search paths (for testing)."""
    global _loader
    paths = search_paths if search_paths is not None else _SEARCH_PATHS
    _loader = ResourceLoader(paths, pattern="*.md", dir_entry="SKILL.md")


def load_skills() -> dict[str, Skill]:
    """Load all skills from disk. Project skills override built-in ones of the same name.

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


def model_skills() -> dict[str, Skill]:
    """모델이 `run_skill` 로 부를 수 있는 스킬 — ``disable-model-invocation`` 이 아닌 것.
    목록 절·`run_skill` 광고·"not found" 안내가 같은 집합을 본다."""
    return {n: s for n, s in load_skills().items() if not s.disable_model_invocation}


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
