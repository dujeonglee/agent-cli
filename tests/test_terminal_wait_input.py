"""`run` 의 제자리 대기를 깨울 사람 입력(`main._terminal_steer_input`) — main 앞에 줄 선 말도, 터미널에서 친 줄도
같은 판정(`surface.is_command`)으로: 명령이면 그 자리에서 라우팅하고 깨우지 않는다, 아니면 깨운다.

줄 선 말은 예약 발화·StaleGuard 의 Hint 처럼 터미널을 거치지 않고 온 것이다. 순서는 테스트가 쥔다 — 넣고 나서
대기 함수를 ``timeout=0`` 으로 부른다(표준입력은 대역)."""

from __future__ import annotations

import io

import pytest

import agent_cli.main as M
from agent_cli.main_input import MainInput


class _Tty(io.StringIO):
    def isatty(self):
        return True


def _setup(monkeypatch, *, tty: bool, typed: str = ""):
    q = MainInput()
    routed: list[str] = []

    class _Surface:
        def is_command(self, text):
            return text.startswith(("/", "@")) and not text.startswith("/tmp")

        def echo(self, item):  # run 의 표면은 말풍선이 없다
            pass

        def route(self, text, start=None):
            routed.append(text)
            return True

    stdin = _Tty(typed) if tty else io.StringIO(typed)
    monkeypatch.setattr(M.sys, "stdin", stdin)
    import select

    monkeypatch.setattr(
        select, "select", lambda r, w, x, t: (r if typed else [], [], [])
    )
    return q, routed, M._terminal_steer_input(q, _Surface())


@pytest.mark.parametrize("tty", [False, True])
def test_a_queued_command_is_routed_in_place_and_does_not_wake(monkeypatch, tty):
    q, routed, wait = _setup(monkeypatch, tty=tty)
    q.enqueue(None, "/stop agt-5", nickname="⏰ nightly")
    assert wait(0) is False
    assert routed == ["/stop agt-5"] and q.pending_count() == 0


@pytest.mark.parametrize("tty", [False, True])
def test_queued_words_wake_and_stay_queued(monkeypatch, tty):
    q, routed, wait = _setup(monkeypatch, tty=tty)
    q.enqueue(None, "/tmp/notes.txt — read this", nickname="⏰ nightly")
    assert wait(0) is True and routed == [] and q.pending_count() == 1


def test_a_typed_command_is_routed_in_place(monkeypatch):
    q, routed, wait = _setup(monkeypatch, tty=True, typed="@agents\n")
    assert wait(0) is False and routed == ["@agents"] and q.pending_count() == 0


def test_typed_words_are_queued_and_wake(monkeypatch):
    q, routed, wait = _setup(monkeypatch, tty=True, typed="skip the tests\n")
    assert wait(0) is True and routed == []
    assert [i["text"] for i in q.snapshot()] == ["skip the tests"]


def test_nothing_typed_and_nothing_queued_keeps_waiting(monkeypatch):
    q, routed, wait = _setup(monkeypatch, tty=True)
    assert wait(0) is False and routed == [] and q.pending_count() == 0
