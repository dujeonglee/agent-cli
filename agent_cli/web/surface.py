"""``agent-cli web`` 의 펌프 표면 (``pump.PumpSurface``, docs/pump/DESIGN.md).

run 과 다른 것만 여기 있다: 전송 버튼 상태(``worker_idle/busy``), 말풍선·깨우기
카드 에코, Stop 버튼의 중단 핸들(``server.set_stop_handle``), 웹 전용 슬래시
명령(``/help``·``/sh``·``/compact``)이 공용 디스패치보다 먼저, 런 예외는
화면에 알리고 계속(워커가 죽으면 큐만 쌓인다), 결과 파일은 없다(화면이 결과).
"""

from __future__ import annotations

import threading
from collections.abc import Callable


class WebSurface:
    graceful_interrupt = True  # Stop 버튼 = 턴 경계의 우아한 중단

    def __init__(self, *, renderer, server, ctx, dispatch: Callable):
        self._renderer = renderer
        self._server = server
        self._ctx = ctx
        self._dispatch = dispatch  # (text, stop_event) -> bool
        # 이 런의 중단 핸들 — 워커는 하나라 두 런이 겹치지 않는다. 종전엔
        # 반복마다 클로저가 잡았다(``noqa: B023``).
        self._stop: threading.Event | None = None

    def idle(self) -> None:
        # 프런트에 "다음 메시지 대기" — ``_latest_worker_state`` 로 가서
        # 새로 연 클라이언트도 스냅샷 재생으로 같은 전송 버튼 상태에 선다.
        self._renderer.worker_idle()

    def busy(self) -> None:
        # 다음 dequeue 까지 바쁨 — 그 사이의 prompt_user/confirm 대기도 포함.
        self._renderer.worker_busy()

    def echo(self, item: dict, *, wake: bool) -> None:
        hidx = getattr(self._ctx, "next_ordinal", None)
        text = item.get("text") or ""
        if wake:
            # 기계가 만든 깨우기 — 사람 발화가 아니다. 말풍선으로 그리면
            # 사용자가 저렇게 타이핑한 것처럼 보인다(사용자 제보).
            self._renderer.agent_wake(text, hidx=hidx)
            return
        nickname = item.get("nickname") or ""
        self._renderer.push_user_message(
            f"[{nickname}]: {text}", author=nickname, hidx=hidx
        )

    def bind_stop(self, event: threading.Event | None) -> None:
        self._stop = event
        self._server.set_stop_handle(event)

    def route(self, text: str) -> bool:
        from agent_cli.web.slash import handle_slash_command

        if handle_slash_command(text, self._renderer, ctx=self._ctx):
            return True
        return self._dispatch(text, self._stop)

    def run_ended(self, result) -> None:
        pass  # 화면이 결과다

    def run_failed(self, exc: Exception) -> bool:
        # 프런트가 보게 밀어 넣고 워커는 다음 메시지를 계속 받는다.
        self._renderer.error(f"Worker error: {exc}", 0)
        return True
