"""`agent-cli run` 의 Ctrl+C — 한 번은 main 의 런만, 2초 안에 두 번은 전부(main + 모든 에이전트) (v10.142.0).

웹의 정지 버튼과 같은 범위다(docs/agent-engine/DESIGN.md §2.3): Stop = main 만, Stop everything = 전부. main 의
런은 그 런의 stop Event 로 멈춘다(웹 ``/api/stop`` 과 같은 길 — 기록은 ``[interrupt]``), 에이전트는 레지스트리의
"모두 정지"(`AgentRegistry.stop_all_runs`)로. 멈출 것이 없으면(main 이 한가하고 일하는 에이전트도 없음) main 상자를
닫는다(6d-3 — 웹의 닫기·SIGTERM 과 같은 길: 펌프가 돌아오고 run 이 정리한 뒤 130). main 이 이미 멈추는 중인데 창 밖에서
또 누르거나 2초 안 두 번인데 멈출 것이 없으면 ``KeyboardInterrupt`` — 도구 하나가 멈춤 검사에 닿지 못할 때의
안전망이다(종전 두 번째 Ctrl+C 의 강제 종료). 확인(`c-N`)을 기다리는 호출은
멈춤 검사에 닿는다 — 한 번은 main 의 확인을, 두 번은 에이전트의 확인도 깨운다(6d).

시계는 주입한다 — 두 번 누름의 창을 테스트가 시각 없이 정한다.
"""

from __future__ import annotations

import time
from collections.abc import Callable

#: 두 번 누름의 창(초).
DOUBLE_PRESS_S = 2.0

MAIN = "main"
ALL = "all"


class CtrlCPresses:
    """누름 하나 → 범위(``"main"`` 또는 ``"all"``). 직전 누름에서 창 안이면 ``"all"`` 이고, 그 누름은 다음
    두 번 누름의 짝이 되지 않는다(세 번째는 다시 ``"main"`` 부터)."""

    def __init__(
        self,
        *,
        window_s: float = DOUBLE_PRESS_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window = window_s
        self._clock = clock
        self._last: float | None = None

    def press(self) -> str:
        now = self._clock()
        double = self._last is not None and now - self._last <= self._window
        self._last = None if double else now
        return ALL if double else MAIN


STOPPED_MAIN = "■ main stopped (press Ctrl+C again within 2s to stop all agents)"
MAIN_IDLE = "main is idle — press Ctrl+C again within 2s to stop all agents"
STOPPING_ALL = "■ stopping everything — main and {n} working agent{s}"


def stop_for_ctrl_c(
    scope: str,
    *,
    main_stop,
    registry,
    say: Callable[[str], None],
    close: Callable[[], None],
):
    """누름 하나의 범위대로 멈춘다 — 한 번인데 멈출 것이 없으면 ``close()``(main 상자를 닫는다), 그 밖에 멈출 것이
    없으면 ``KeyboardInterrupt``(안전망).

    ``main_stop`` 은 지금 main 런의 stop Event(한가하면 None), ``registry`` 는 에이전트 레지스트리(없으면
    None), ``say`` 는 사람에게 한 줄, ``close`` 는 run 을 끝내는 닫기. 에이전트 각자의 결과 줄(``■ agt-3 │ stopped by user — …``)은 레지스트리의
    정지가 따로 찍는다. "전부"(두 번)는 main 을 먼저 멈추고, 그 정지 회신들은 main 을 깨우지 않는다(사용자 결정 — 다음 입력 런에)."""
    working = bool(registry is not None and registry.working_agents())
    main_running = main_stop is not None and not main_stop.is_set()
    if scope == ALL and (working or main_running):
        if main_stop is not None:
            main_stop.set()
        n = (
            len(registry.stop_all_runs(wakes_requesters=False))
            if registry is not None
            else 0
        )
        say(STOPPING_ALL.format(n=n, s="" if n == 1 else "s"))
        return
    if main_running:
        main_stop.set()
        say(STOPPED_MAIN)
        return
    if main_stop is None and working:
        say(MAIN_IDLE)
        return
    if scope == MAIN and main_stop is None:
        close()
        return
    raise KeyboardInterrupt
