"""SIGTERM 으로 끝나도 정상 정리 경로를 탄다 (v10.134.1).

보드는 방을 끌 때 SIGTERM 을 보내고(삭제 경로는 8초 뒤 SIGKILL) — 종전엔 처리기가 없어
`teardown_session` 이 돌지 않고 죽었다: 백그라운드 잡(`start_new_session`, 부모가 죽어도
산다)·모니터·상주 워커·MCP 자식이 남고 세션 저장도 건너뛰었다.

처리기는 신호 문맥에서 표시만 한다 — 정리는 메인 스레드가 지금의 정상 정리 경로로 한다:

- run (6d-3): 처리기가 main 상자를 닫는다(`MainInput.close` — 웹의 닫기와 같은 길). main 이 멈추면 펌프가 돌아오고
  ``finally`` 가 정리한다. 안전망: main 이 시한 안(`CLOSE_WAIT_S`, `TEARDOWN_BUDGET_S` 안)에 멈추지 않으면 메인
  스레드에 SIGTERM 을 다시 보내고, 두 번째 SIGTERM 은 `Terminated` 로 메인 스레드를 푼다 — `BaseException` 이라
  `except Exception`·`except KeyboardInterrupt` 가 삼키지 않는다.
- web: uvicorn 이 SIGTERM 을 잡아 서버를 내린다 — 처리기는 받았다고 적고(``should_exit``),
  서버가 돌아오면 ``finally`` 가 정리한다.

정리가 끝나면 `die` 가 기본 동작으로 되돌려 자기에게 SIGTERM 을 다시 보낸다 — 부모가 보는
종료 상태는 종전과 같다(신호 15, 셸에선 143). 정리 중(펌프가 돌아온 뒤) 두 번째 SIGTERM 은 기본 동작(즉시 죽음).
"""

from __future__ import annotations

import os
import signal
import threading
import time

#: SIGTERM 경로의 정리 대기 총예산(초) — 보드는 SIGTERM 8초 뒤 SIGKILL 한다. 나머지
#: 2초는 서버 종료·MCP 해제·세션 저장 몫이다. 정상 종료·Ctrl-C 경로는 이 값을 쓰지 않는다.
TEARDOWN_BUDGET_S = 6.0

#: 세션을 닫을 때 main 이 멈추기를 기다리는 상한(초) — SIGTERM 으로 들어왔으면 그 시한(`TEARDOWN_BUDGET_S`) 안에서다.
#: 전경 셸의 정지(정지를 보는 0.2초 + TERM 뒤 3초 + KILL 뒤 3초 — `tools.shell._stop_group`)와 그 결과 기록을 덮는다.
#: 응답 머리를 기다리는 모델 호출처럼 정지를 보지 않는 대기는 이 상한에서 끊긴다
CLOSE_WAIT_S = 7.0


class Terminated(BaseException):
    """SIGTERM 이 run 의 메인 스레드를 풀 때 던지는 것."""


class SigTerm:
    """한 세션의 SIGTERM 표시 — 받은 시각과 정리 기한."""

    def __init__(self) -> None:
        self.at: float | None = None
        #: run 의 안전망 타이머(`install_close`) — 걸지 않았으면 None
        self._net: threading.Timer | None = None

    @property
    def received(self) -> bool:
        return self.at is not None

    def deadline(self) -> float | None:
        """정리 대기의 기한(``time.monotonic`` 기준) — 받지 않았으면 None(대기 그대로)."""
        return None if self.at is None else self.at + TEARDOWN_BUDGET_S

    def _mark(self) -> None:
        if self.at is None:
            self.at = time.monotonic()

    def install_close(self, close, *, timer=threading.Timer):
        """run — 첫 SIGTERM 은 ``close()``(main 상자를 닫는다)를 부르고 안전망을 건다: main 이 시한 안
        (`wait_left(deadline, CLOSE_WAIT_S)`)에 멈추지 않으면 메인 스레드에 SIGTERM 을 다시 보낸다(`pthread_kill` —
        막힌 시스템 호출을 깨운다. 없는 플랫폼은 안전망 없이 닫기만). 두 번째 SIGTERM(안전망의 것이든 밖의 것이든)은
        `Terminated` 를 던져 메인 스레드를 푼다. ``timer`` 는 안전망을 거는 법(테스트가 주입한다). 돌려주는 것은 이전
        처리기(`restore` 에). 펌프가 돌아오면 `cancel_safety_net`."""
        main_ident = threading.main_thread().ident

        def _handler(signum, frame):
            if self.received:
                raise Terminated()
            self._mark()
            close()
            if hasattr(signal, "pthread_kill"):
                self._net = timer(
                    wait_left(self.deadline(), CLOSE_WAIT_S),
                    signal.pthread_kill,
                    (main_ident, signal.SIGTERM),
                )
                self._net.daemon = True
                self._net.start()

        return signal.signal(signal.SIGTERM, _handler)

    def cancel_safety_net(self) -> None:
        """펌프가 돌아왔다 — 걸어 둔 안전망을 거둔다(정리 도중 울리지 않게). 걸지 않았으면 아무것도 안 한다."""
        if self._net is not None:
            self._net.cancel()

    def install_for_server(self, server):
        """web — uvicorn 이 신호를 잡아 서버를 내린다(버전마다 잡는 방식이 달라 ``handle_exit``
        를 감싸 받은 것을 적는다). 서버가 돌기 전·뒤에 오면 이 처리기가 받아 서버를 내린다
        (0.29+ 는 서버를 내린 뒤 잡은 신호를 이전 처리기로 다시 보낸다 — 그것도 여기 온다)."""

        def _handler(signum, frame):
            self._mark()
            server.should_exit = True

        prev = signal.signal(signal.SIGTERM, _handler)
        orig = server.handle_exit

        def _handle_exit(sig, frame):
            if sig == signal.SIGTERM:
                self._mark()
            orig(sig, frame)

        server.handle_exit = _handle_exit
        return prev

    @staticmethod
    def restore(prev) -> None:
        signal.signal(signal.SIGTERM, prev if prev is not None else signal.SIG_DFL)

    @staticmethod
    def default() -> None:
        """정리에 들어가며 — 두 번째 SIGTERM 은 기본 동작."""
        signal.signal(signal.SIGTERM, signal.SIG_DFL)

    @staticmethod
    def die() -> None:
        """정리가 끝난 뒤 — 기본 동작으로 자기에게 SIGTERM. 부모는 신호 15 로 본다."""
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGTERM)
        raise SystemExit(128 + signal.SIGTERM)  # 신호가 막혀 있던 드문 경우의 대비


def wait_left(deadline: float | None, cap: float) -> float:
    """기한이 있으면 남은 시간과 ``cap`` 중 작은 것(0 이상), 없으면 ``cap``."""
    if deadline is None:
        return cap
    return max(0.0, min(cap, deadline - time.monotonic()))
