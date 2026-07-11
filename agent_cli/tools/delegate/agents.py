"""In-process subagent delegation tool.

Supports context modes: none (independent), fork (copy conversation history).
Uses tasks array API: single item = sync, multiple items = parallel (threading).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from agent_cli.resource_loader import ResourceLoader

if TYPE_CHECKING:
    # Runtime-imported inside the dispatch path to avoid a
    # registry → delegate → context.manager → tools.registry →
    # tools import cycle (registry now imports DelegateTool at module load).
    pass

# ── Agent file loading ──────────────────────────

_AGENT_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")

# NOTE(C2 패키지化): 이 파일은 tools/delegate/ 아래로 한 단계 깊어짐 —
# 패키지 루트(agent_cli/)까지 parent 3단.
_BUILTIN_AGENTS_DIR = Path(__file__).parent.parent.parent / "agents" / "builtin"

_AGENT_SEARCH_PATHS = [
    Path.cwd() / ".agent-cli" / "agents",
    Path.home() / ".agent-cli" / "agents",
    _BUILTIN_AGENTS_DIR,
]

_FRONTMATTER_PATTERN = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n(.*)",
    re.S,
)

_agent_loader = ResourceLoader(_AGENT_SEARCH_PATHS)


def _validate_agent_name(name: str) -> bool:
    """Validate agent name: alphanumeric, hyphens, underscores only."""
    return bool(_AGENT_NAME_PATTERN.match(name))


def _load_agent(name: str) -> tuple[str | None, dict, str | None]:
    """Load agent definition file.

    Returns:
        (role_prompt, config_dict, error_message)
        - Success: (body, {allowed-tools, model, ...}, None)
        - Failure: (None, {}, error_message)
    """
    if not _validate_agent_name(name):
        return None, {}, f"Invalid agent name '{name}': only [a-zA-Z0-9_-] allowed"

    resource = _agent_loader.load_one(name)
    if resource is None:
        paths_str = ", ".join(str(p / f"{name}.md") for p in _AGENT_SEARCH_PATHS)
        return None, {}, f"Agent '{name}' not found. Searched: {paths_str}"

    if not resource.body:
        return None, {}, f"Agent file '{name}.md' has no content"

    return resource.body, resource.meta, None
