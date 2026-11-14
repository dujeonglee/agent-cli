"""세션 슬롯 — 세션 조립(`runtime.build_*`)이 넣고 도구·엔진·웹 서버가 꺼내는 프로세스 전역 객체 다섯.

타입은 TYPE_CHECKING 으로만 안다(맨 아래 층, 모듈 수준 agent_cli import 0). 여기서 만들지 않는다.
없으면 None — 꺼내는 쪽이 None 을 처리한다. 필요할 때 만드는 싱글턴(snapshot·browser)과 렌더러는
여기 없다.

전역인 이유는 도구가 서브에이전트 루프에서도 불리기 때문이다 — 레지스트리를 손에서 손으로
릴레이하던 시절엔 경로가 늘 때마다 배선이 빠졌다(에이전트 레지스트리: 사용자 슬래시 경로 둘이
빠져 spawn 이 "main-session only" 로 거부되던 사고, v7.17.0). 에이전트 레지스트리의 권한 경계는
"미지정 vs 명시 None" 구분이 지킨다 — 서브에이전트 루프의 dispatch 는 포트의 agent_registry(=None)를
**명시** 전달하므로 슬롯을 조회하지 않고 run-only 가 유지된다.

훅 러너는 네 루프(main·상주·스킬·일회성)가 같은 인스턴스를 본다(v10.35.0). 셸 훅(`hooks.json`)은
종류의 `hooks_config` 경로로 돌며 여기와 무관하다 — 러너에 `shell_hooks_config` 를 주지 않아
두 번 돌지 않는다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_cli.hooks.runner import HookRunner
    from agent_cli.jobs import JobRegistry
    from agent_cli.monitor.registry import MonitorRegistry
    from agent_cli.schedule.registry import ScheduleRegistry
    from agent_cli.subagent.agents_live import AgentRegistry

_JOBS: JobRegistry | None = None
_MONITORS: MonitorRegistry | None = None
_SCHEDULES: ScheduleRegistry | None = None
_HOOKS: HookRunner | None = None
_AGENTS: AgentRegistry | None = None


def set_job_registry(registry: JobRegistry | None) -> None:
    """백그라운드 작업 레지스트리 — `runtime.build_job_registry` 가 넣는다."""
    global _JOBS
    _JOBS = registry


def get_job_registry() -> JobRegistry | None:
    return _JOBS


def set_monitor_registry(registry: MonitorRegistry | None) -> None:
    """monitor 레지스트리 — `runtime.build_monitor_registry` 가 넣는다."""
    global _MONITORS
    _MONITORS = registry


def get_monitor_registry() -> MonitorRegistry | None:
    return _MONITORS


def set_schedule_registry(registry: ScheduleRegistry | None) -> None:
    """예약 레지스트리 — `runtime.build_schedule_registry` 가 넣는다."""
    global _SCHEDULES
    _SCHEDULES = registry


def get_schedule_registry() -> ScheduleRegistry | None:
    return _SCHEDULES


def set_hook_runner(runner: HookRunner | None) -> None:
    """Python 훅 러너 — `runtime.build_hook_runner` 가 넣는다."""
    global _HOOKS
    _HOOKS = runner


def get_hook_runner() -> HookRunner | None:
    return _HOOKS


def set_agent_registry(registry: AgentRegistry | None) -> None:
    """main 세션의 에이전트 레지스트리 — `runtime.build_agent_registry` 가 넣는다."""
    global _AGENTS
    _AGENTS = registry


def get_agent_registry() -> AgentRegistry | None:
    return _AGENTS


def clear_session_slots() -> None:
    """다섯을 None 으로."""
    set_job_registry(None)
    set_monitor_registry(None)
    set_schedule_registry(None)
    set_hook_runner(None)
    set_agent_registry(None)
