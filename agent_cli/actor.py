"""런을 여는 루프 하나 — main 과 에이전트가 같은 루프를 돈다 (`run_actor`).

액터는 주소 하나의 주인이다: 런을 열 것을 기다리고(`take`), 열고(`open`), 돌리고(`run_start`), 끝을 판정한다(`ended`).
main(`pump.MainActor`)과 에이전트 워커(`subagent.resident_loop.AgentActor`)가 다른 것은 그 메서드들뿐이다 —
꺼내는 법(창구의 `Take`), 수명(세션 내내 / 과제 하나), 표면·명령·런 끝 정리.

루프는 예외를 잡지 않는다 — 런의 예외 정책은 구현의 것이다(main 은 표면의 `run_failed`, 에이전트는 종류의
`crashed`). ``KeyboardInterrupt`` 는 run 의 호출자가 받는다.
"""

from __future__ import annotations

from typing import Any, Protocol

from agent_cli.main_input import CLOSED


class Actor(Protocol):
    #: 기다림의 시한(초) — 지나면 `should_stop` 을 다시 묻는다. None = 시한 없이(닫힘·도착이 깨운다)
    poll_secs: float | None

    def should_stop(self) -> bool:
        """시한이 지날 때마다 — 참이면 루프를 끝낸다(``poll_secs`` 가 None 이면 묻지 않는다)."""

    def idle(self) -> None:
        """런을 열 것을 기다리기 직전."""

    def take(self, timeout: float | None) -> Any:
        """런을 열 것 — 꺼낸 것, `CLOSED`(루프를 끝낸다), 또는 None(시한이 지났다)."""

    def open(self, got: Any) -> Any:
        """꺼낸 것으로 런의 시작(`Start`)을 짓는다 — None 이면 런을 열지 않은 차례(main 의 명령)."""

    def run_start(self, start: Any) -> Any:
        """그 시작으로 런을 돌린다 — 끝난 것(종류의 반환)."""

    def ended(self, start: Any, finished: Any) -> bool:
        """런이 끝났다 — 참이면 액터의 수명이 끝났다(루프를 끝낸다)."""


def run_actor(actor: Actor) -> None:
    """기다림 → 열기 → 런 → 끝 판정을 닫힐 때까지 되풀이한다."""
    while True:
        if actor.poll_secs is not None and actor.should_stop():
            return
        actor.idle()
        got = actor.take(actor.poll_secs)
        if got is CLOSED:
            return
        if got is None:
            continue  # 시한이 지났다 — 정지 조건을 다시 판정
        start = actor.open(got)
        if start is None:
            continue
        if actor.ended(start, actor.run_start(start)):
            return
