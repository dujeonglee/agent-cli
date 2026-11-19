"""창구는 바뀐 것이 있을 때만 깨운다 (v10.145.0 고침).

빈손의 꺼내기(그때 `start_run`, 지금 `next_run`)가 `notify_all` 을 부르면, 같은 조건변수에서 기다리는 다른 한가한
워커가 깨어 다시 빈손의 꺼내기로 깨우고 — 한가한 에이전트 둘이면 서로를 영원히 깨운다(바쁜 회전, CI 수트 270→1282초). 조건변수를
감싼 스파이로 `notify_all` 호출 수를 센다(시간을 재지 않는다).
"""

from __future__ import annotations

import threading

import pytest

from agent_cli.post import envelope as E
from agent_cli.post.office import PostOffice, Take

A = "agent:a"


class CountingCondition(threading.Condition):
    """`notify_all` 호출 수를 세는 조건변수."""

    def __init__(self):
        super().__init__()
        self.notified = 0

    def notify_all(self):
        self.notified += 1
        super().notify_all()


@pytest.fixture
def office():
    o = PostOffice(cv=CountingCondition())
    yield o
    o.check_consistency()


def _notifies(office, call) -> int:
    before = office.cv.notified
    call()
    return office.cv.notified - before


#: 아무것도 바꾸지 않는 호출 — 빈 상자·없는 줄.
NOTHING_CHANGES = {
    "next_run": lambda o: o.next_run(A, Take.ARRIVAL, timeout=0),
    "absorb": lambda o: o.absorb(A),
    "absorb_human_first": lambda o: o.absorb(A, human_first=True),
    "take": lambda o: o.take(A),
    "take_reply": lambda o: o.take_reply(A, "r-9"),
    "complete": lambda o: o.complete(A, ["u-9"], run_id="u-1"),
    "end_run_done": lambda o: o.end_run(A, run_id="r-1", outcome="done"),
    "end_run_stopped": lambda o: o.end_run(A, run_id="r-1", outcome="stopped"),
    "end_run_wait": lambda o: o.end_run(A, run_id="r-1", outcome="wait"),
    "end_run_session_end": lambda o: o.end_run(A, run_id="r-1", outcome="session_end"),
    "remove_queued": lambda o: o.remove_queued(A),
    "forget_questions_of": lambda o: o.forget_questions_of(A),
}


@pytest.mark.parametrize("name", sorted(NOTHING_CHANGES))
def test_a_call_that_changes_nothing_wakes_no_one(office, name):
    assert _notifies(office, lambda: NOTHING_CHANGES[name](office)) == 0


def test_a_call_that_changes_something_wakes(office):
    """같은 연산이라도 바뀐 것이 있으면 깨운다 — 상자·줄이 바뀐 호출마다 한 번 이상."""
    assert (
        _notifies(office, lambda: office.deliver(E.request("r-1", "main", A, "a"))) >= 1
    )
    office.deliver(E.request("r-2", "main", A, "b"))
    assert _notifies(office, lambda: office.next_run(A, Take.ARRIVAL, timeout=0)) >= 1
    assert _notifies(office, lambda: office.absorb(A)) >= 1
    assert (
        _notifies(office, lambda: office.end_run(A, run_id="r-1", outcome="done")) >= 1
    )
    office.deliver(E.system("n-3", "job", "harness:j", "done", opens_row=False))
    assert _notifies(office, lambda: office.take("harness:j")) >= 1
    office.deliver(E.reply("n-4", A, "main", "ans", re="r-7"))
    assert _notifies(office, lambda: office.take_reply("main", "r-7")) >= 1
    office.deliver(E.human("u-5", "user:dj", A, "hi"))
    office.next_run(A, Take.ARRIVAL, timeout=0)
    assert _notifies(office, lambda: office.complete(A, ["u-5"], run_id="u-5")) >= 1
    assert (
        _notifies(office, lambda: office.end_run(A, run_id="u-5", outcome="wait")) >= 1
    )
    office.deliver(E.notice("n-6", "main", A, "x"))
    assert _notifies(office, lambda: office.remove_queued(A)) >= 1
    office.ask("q-7", A, "user:dj", "?")
    assert _notifies(office, lambda: office.forget_questions_of(A)) >= 1
