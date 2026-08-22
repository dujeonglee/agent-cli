"""에이전트 프로파일 로더 — 단일 카탈로그 (agent 통합 PR-1, 5.0.0).

delegate 의 agents/ 로더(subagent/oneshot/agents.py)와 teammate 의
teammates/ 로더(subagent/roles.py)를 병합한 것. **검색 경로는 agents/
하나** — 같은 프로파일이 일회성(run)과 상주(spawn) 양쪽에 쓰인다.
teammates/ 경로는 하드컷 (하위호환 전면 포기 — 설계 §4).

frontmatter (병합 명세): ``description``(발견 표면 — 카탈로그 광고),
``allowed-tools``, ``model``, ``hooks``, ``disable-model-invocation``
(광고 제외 — 사용자 @명령으로만), ``auto-spawn``(상주 전용 — run 무시).
"""

from __future__ import annotations

import re
from pathlib import Path

from agent_cli.resource_loader import ResourceLoader

_PROFILE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")

# subagent/ 아래라 패키지 루트까지 parent 2단.
_BUILTIN_PROFILES_DIR = Path(__file__).parent.parent / "agents" / "builtin"

from agent_cli.paths import scoped_paths

_PROFILE_SEARCH_PATHS = [
    *scoped_paths("agents"),
    _BUILTIN_PROFILES_DIR,
]

# 테스트가 monkeypatch 로 교체하는 모듈 전역 — conftest autouse 가
# 스냅샷/복원한다 (구 agents._agent_loader 규율 승계).
_profile_loader = ResourceLoader(_PROFILE_SEARCH_PATHS)


def load_profile(name: str) -> tuple[str | None, dict, str | None]:
    """프로파일 md 로드 — ``(body, config, error)``.

    성공 ``(본문, frontmatter dict, None)``, 실패 ``(None, {}, 메시지)``.
    구 ``_load_agent``/``load_teammate_role`` 의 동형 계약.
    """
    if not _PROFILE_NAME_PATTERN.match(name):
        return None, {}, f"Invalid profile name '{name}': only [a-zA-Z0-9_-] allowed"

    resource = _profile_loader.load_one(name)
    if resource is None:
        paths_str = ", ".join(str(p / f"{name}.md") for p in _PROFILE_SEARCH_PATHS)
        return None, {}, f"Profile '{name}' not found. Searched: {paths_str}"

    if not resource.body:
        return None, {}, f"Profile file '{name}.md' has no content"

    return resource.body, resource.meta, None


def available_profiles(*, include_meta: bool = False) -> list:
    """카탈로그 — 시스템 프롬프트 광고·@목록·auto-spawn 스캔용.

    include_meta=False → ``[(name, description), ...]``
    (``disable-model-invocation: true`` 제외 — 광고 규율),
    True → ``[(name, meta_dict), ...]`` (전체 — auto-spawn 은 meta 직접 판단).
    """
    resources = _profile_loader.load_all()
    if include_meta:
        return [(name, res.meta) for name, res in resources.items()]
    return [
        (name, res.meta.get("description", ""))
        for name, res in resources.items()
        if not res.meta.get("disable-model-invocation")
    ]
