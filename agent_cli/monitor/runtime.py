"""프로세스 단위 monitor 레지스트리 — 도구가 닿는 경로 (v9.11.0).

`agents_live.set_main_registry` / `main_registry` 와 같은 모양이다. 루프는
`LoopConfig.monitor_registry` 필드로 받지만(턴 경계 drain 에 필요), 도구는
서브에이전트 루프에서도 불릴 수 있어 모듈 전역이 단순하다 — 두 경로가 **같은
객체**를 가리키도록 조립부(`runtime.build_monitor_registry`)가 하나를 만들어
양쪽에 준다.
"""

from __future__ import annotations

from agent_cli.monitor.registry import MonitorRegistry

_MAIN: MonitorRegistry | None = None


def set_monitor_registry(registry: MonitorRegistry | None) -> None:
    global _MAIN
    _MAIN = registry


def get_monitor_registry() -> MonitorRegistry | None:
    return _MAIN
