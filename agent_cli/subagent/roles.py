"""teammate 역할 정의 로더 (D11(b), docs/teammate/DESIGN.md §4.3).

delegate 의 agents/ 로더(tools/delegate/agents.py)와 **의도적으로 분리** —
teammate 전용 frontmatter(auto-spawn 등)가 자랄 자리를 처음부터 갖는다.
파일 파싱·탐색 자체는 :class:`~agent_cli.resource_loader.ResourceLoader`
공유라 포맷 복제는 없다. md 본문(role)이 teammate 서브루프의 system
prompt 로 로드되고, frontmatter 의 ``allowed-tools``/``model``/``hooks``
는 agent md 와 동일 키·동일 의미 (오버레이는 subagent/runner 소유).
"""

from __future__ import annotations

import re
from pathlib import Path

from agent_cli.resource_loader import ResourceLoader

_ROLE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")

# 패키지 내장 전문가 — subagent/ 아래라 패키지 루트까지 parent 2단.
_BUILTIN_TEAMMATES_DIR = Path(__file__).parent.parent / "teammates" / "builtin"

_TEAMMATE_SEARCH_PATHS = [
    Path.cwd() / ".agent-cli" / "teammates",
    Path.home() / ".agent-cli" / "teammates",
    _BUILTIN_TEAMMATES_DIR,
]

_teammate_loader = ResourceLoader(_TEAMMATE_SEARCH_PATHS)


def load_teammate_role(name: str) -> tuple[str | None, dict, str | None]:
    """teammate 역할 md 로드 — ``(role_prompt, config, error)``.

    delegate 의 ``_load_agent`` 와 동형 계약: 성공 ``(body, meta, None)``,
    실패 ``(None, {}, message)``.
    """
    if not _ROLE_NAME_PATTERN.match(name):
        return None, {}, f"Invalid teammate role '{name}': only [a-zA-Z0-9_-] allowed"

    resource = _teammate_loader.load_one(name)
    if resource is None:
        paths_str = ", ".join(str(p / f"{name}.md") for p in _TEAMMATE_SEARCH_PATHS)
        return None, {}, f"Teammate role '{name}' not found. Searched: {paths_str}"

    if not resource.body:
        return None, {}, f"Teammate role file '{name}.md' has no content"

    return resource.body, resource.meta, None


def available_roles(*, include_meta: bool = False) -> list:
    """스폰 가능한 역할 목록 — 시스템 프롬프트 광고·auto-spawn 스캔용.

    ``disable-model-invocation: true`` frontmatter 는 모델에게 광고하지
    않는다 (agents/skills 와 동일 규율 — auto-spawn 스캔은 meta 를 직접
    보므로 영향 없음: include_meta=True 는 전체 반환).
    반환: include_meta=False → ``[(name, description), ...]``,
    True → ``[(name, meta_dict), ...]``.
    """
    resources = _teammate_loader.load_all()
    if include_meta:
        return [(name, res.meta) for name, res in resources.items()]
    return [
        (name, res.meta.get("description", ""))
        for name, res in resources.items()
        if not res.meta.get("disable-model-invocation")
    ]
