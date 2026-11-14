"""SIGTERM 으로 끝나도 정상 정리 경로를 탄다 (v10.134.1).

보드는 방을 끌 때 SIGTERM 을 보내고(삭제 경로는 8초 뒤 SIGKILL) — 종전엔 처리기가 없어
`teardown_session` 이 돌지 않고 죽었다: 백그라운드 잡(`start_new_session`, 부모가 죽어도
산다)·모니터·상주 워커·MCP 자식이 남고 세션 저장도 건너뛰었다.

처리기는 신호 문맥에서 표시만 한다 — 정리는 메인 스레드가 지금의 정상 정리 경로로 한다:

- run: 처리기가 `Terminated` 를 던져 메인 스레드(펌프·런)를 푼다 — 두 번째 Ctrl-C 가
  `KeyboardInterrupt` 로 푸는 것과 같은 자리. `BaseException` 이라 `except Exception`·
  `except KeyboardInterrupt` 가 삼키지 않고, `run` 의 ``finally`` 가 정리한다.
- web: uvicorn 이 SIGTERM 을 잡아 서버를 내린다 — 처리기는 받았다고 적고(``should_exit``),
  서버가 돌아오면 ``finally`` 가 정리한다.

정리가 끝나면 `die` 가 기본 동작으로 되돌려 자기에게 SIGTERM 을 다시 보낸다 — 부모가 보는
종료 상태는 종전과 같다(신호 15, 셸에선 143). 정리 중 두 번째 SIGTERM 은 기본 동작(즉시 죽음).
"""

from __future__ import annotations

import os
import signal
import time

#: SIGTERM 경로의 정리 대기 총예산(초) — 보드는 SIGTERM 8초 뒤 SIGKILL 한다. 나머지
#: 2초는 서버 종료·MCP 해제·세션 저장 몫이다. 정상 종료·Ctrl-C 경로는 이 값을 쓰지 않는다.
TEARDOWN_BUDGET_S = 6.0


class Terminated(BaseException):
    """SIGTERM 이 run 의 메인 스레드를 풀 때 던지는 것."""


class SigTerm:
    """한 세션의 SIGTERM 표시 — 받은 시각과 정리 기한."""

    def __init__(self) -> None:
        self.at: float | None = None

    @property
    def received(self) -> bool:
        return self.at is not None

    def deadline(self) -> float | None:
        """정리 대기의 기한(``time.monotonic`` 기준) — 받지 않았으면 None(대기 그대로)."""
        return None if self.at is None else self.at + TEARDOWN_BUDGET_S

    def _mark(self) -> None:
        if self.at is None:
            self.at = time.monotonic()

    def install_unwind(self):
        """run — 받으면 `Terminated` 를 던진다. 돌려주는 것은 이전 처리기(`restore` 에)."""

        def _handler(signum, frame):
            self._mark()
            signal.signal(signal.SIGTERM, signal.SIG_DFL)  # 정리 중 두 번째는 즉시 죽음
            raise Terminated()

        return signal.signal(signal.SIGTERM, _handler)

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
