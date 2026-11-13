"""수트가 저장소 루트에 파일을 남기지 않는지 보는 가드의 순수 부분.

``conftest.py`` 의 autouse 픽스처가 테스트 앞뒤로 :func:`snapshot` 을 찍고
:func:`new_entries` 로 새로 생긴 항목을 가려 그 테스트를 실패시킨다. 저장소에
쓰는 테스트는 서로의 상태를 보고(순서 의존), 개발자의 진짜 ``.agent-cli/``
(세션·스냅샷 저장소)를 건드린다 — 그래서 새는 테스트는 임시 디렉터리에서 돈다.
"""

from __future__ import annotations

import os
from pathlib import Path

# 이름이 이것이면 어디서든 뺀다 — 캐시뿐이다.
IGNORED_NAMES = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache"})
# 저장소가 추적하는 자리(그 아래도 통째로 뺀다).
IGNORED_PATHS = frozenset({".agent-cli/skills"})
# 루트 말고 두 단계까지 더 들여다보는 디렉터리.
DEEP_DIRS = (".agent-cli",)
DEEP_LEVELS = 2


def _listdir(path: Path) -> list[str]:
    try:
        return os.listdir(path)
    except (FileNotFoundError, NotADirectoryError):
        return []


def snapshot(root: Path) -> frozenset[str]:
    """``root`` 바로 아래 항목과 ``.agent-cli/`` 아래 두 단계까지의 상대 경로."""
    seen: set[str] = set(_listdir(root))
    for deep in DEEP_DIRS:
        frontier = [deep]
        for _ in range(DEEP_LEVELS):
            nxt: list[str] = []
            for rel in frontier:
                if rel in IGNORED_PATHS:
                    continue
                for name in _listdir(root / rel):
                    child = f"{rel}/{name}"
                    seen.add(child)
                    nxt.append(child)
            frontier = nxt
    return frozenset(seen)


def _ignored(rel: str) -> bool:
    parts = rel.split("/")
    if any(p in IGNORED_NAMES for p in parts):
        return True
    return any(rel == p or rel.startswith(p + "/") for p in IGNORED_PATHS)


def new_entries(before: frozenset[str], after: frozenset[str]) -> list[str]:
    """``after`` 에만 있는 항목(캐시·추적 자리 제외), 정렬해서."""
    return sorted(rel for rel in after - before if not _ignored(rel))
