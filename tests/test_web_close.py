"""웹 방을 닫을 때 — 정리는 main 이 멈춘 뒤 (6d-2).

`main.web` 의 ``finally`` 는 main 상자를 닫고(`WebServer.shutdown` → `MainInput.close`) main 이 멈추기를 기다린 뒤
(`wait_for_main_to_stop`) 정리한다(`close_session`). 기다림의 상한은 `WEB_CLOSE_WAIT_S`, SIGTERM 이면 그 시한 안이다.
가짜 worker 는 받은 ``timeout`` 을 적는다 — 시간을 재지 않는다.
"""

from __future__ import annotations

import inspect
import time

import pytest

import agent_cli.main as M


class _Worker:
    def __init__(self, raises=None):
        self.timeouts: list[float] = []
        self._raises = raises

    def join(self, timeout=None):
        self.timeouts.append(timeout)
        if self._raises is not None:
            raise self._raises


def test_without_a_deadline_it_waits_up_to_the_cap():
    w = _Worker()
    M.wait_for_main_to_stop(w, None)
    assert w.timeouts == [M.WEB_CLOSE_WAIT_S]


def test_the_cap_is_two_seconds():
    """6d-2 전의 값 — 2초 안에 멈추지 않은 main 은 정리와 겹친다(그 호출의 기록이 사라지고 자식이 고아로 남는다)."""
    assert M.WEB_CLOSE_WAIT_S == 2.0


def test_a_passed_deadline_does_not_wait():
    w = _Worker()
    M.wait_for_main_to_stop(w, time.monotonic() - 1)
    assert w.timeouts == [0.0]


def test_a_second_ctrl_c_during_the_wait_escapes_the_close():
    """6d-2 전의 값 — 닫는 도중 Ctrl+C 를 한 번 더 누르면 `KeyboardInterrupt` 가 그대로 나가 정리·저장을 건너뛴다."""
    with pytest.raises(KeyboardInterrupt):
        M.wait_for_main_to_stop(_Worker(raises=KeyboardInterrupt()), None)


def test_the_web_host_waits_for_main_before_the_teardown():
    """`web` 의 ``finally`` — main 상자를 닫고, 기다리고, 그 뒤 정리한다. `web` 은 typer 데코레이터에 싸여 원문으로 본다."""
    src = inspect.getsource(M)
    body = src[src.index("def web(") :]
    close, wait, teardown = (
        body.index("server.shutdown()"),
        body.index("wait_for_main_to_stop(worker, deadline)"),
        body.index("close_session(spec, stores, owners, warn_stuck=False"),
    )
    assert close < wait < teardown
