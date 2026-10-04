"""프로세스 단위 schedule 레지스트리 — 도구와 web API 가 닿는 경로.

`monitor/runtime.py` 와 같은 모양이다. 도구는 서브에이전트 루프에서도 불리므로
모듈 전역이 단순하다.
"""

from __future__ import annotations

from agent_cli.schedule.registry import ScheduleRegistry

_MAIN: ScheduleRegistry | None = None


def set_schedule_registry(registry: ScheduleRegistry | None) -> None:
    global _MAIN
    _MAIN = registry


def get_schedule_registry() -> ScheduleRegistry | None:
    return _MAIN
