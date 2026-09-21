"""프로세스 단위 monitor 레지스트리 — 도구가 닿는 경로 (v9.11.0).

`agents_live.set_main_registry` / `main_registry` 와 같은 모양이다. 도구는
서브에이전트 루프에서도 불리므로 모듈 전역이 단순하다.

종전엔 루프도 `LoopConfig.monitor_registry` 로 같은 객체를 받았다 — 턴 경계
에서 보고를 drain 해야 했기 때문이다. v9.14.0 부터 보고는 **설치한 주소로**
배달되므로(docs/wiring §3.2) 루프 쪽 경로가 통째로 사라졌고, 남은 것은 이
전역(도구가 등록할 때)과 `wire_monitor_delivery` 가 꽂는 배달 seam 둘뿐이다.
도구가 자기 주소를 아는 것은 `RunContext.owner` 다.
"""

from __future__ import annotations

from agent_cli.monitor.registry import MonitorRegistry

_MAIN: MonitorRegistry | None = None


def set_monitor_registry(registry: MonitorRegistry | None) -> None:
    global _MAIN
    _MAIN = registry


def get_monitor_registry() -> MonitorRegistry | None:
    return _MAIN
