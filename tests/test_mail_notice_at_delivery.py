"""main 앞 메일 통지의 "바쁨" 은 그 메일이 도착한 순간의 사실이다 (v10.145.x, FIX-IDLE).

`AgentRegistry._push_reply` 는 배달(``_cv`` 안) → 디스크 저장 → `on_reply` 콜백 순이다. 콜백이 지금의 한가함을 읽으면,
그 사이 펌프의 `mark_idle` 이 이미 든 메일을 보고 스스로 깨우기를 넣고 꺼내 런을 연 뒤라 "바쁨" 으로 읽혔다(CLI 대기 줄이
같은 메일의 관찰 앞에 한 줄 더 — 기록 S/g33/idle-hunt-full/run-16). 이제 배달 구간에서 읽어 넘긴다.

순서는 조건변수의 ``__exit__`` 에 건 훅으로 강제한다 — 메일이 main 상자에 든 구간을 나온 **직후**, 콜백 **전**에 펌프 쪽
동작을 끼운다. 시간을 재지 않는다.
"""

from __future__ import annotations

import threading

import pytest

from agent_cli.post.office import PostOffice
from agent_cli.runtime import wire_agent_mail
from agent_cli.subagent.agents_live import AgentRegistry
from tests.test_agents_live import _FakeLoopResult
from tests.test_engine_p2 import wait_until


class HookedCondition(threading.Condition):
    """락을 **완전히** 놓을 때마다(재진입 구간의 안쪽 출구는 빼고) ``after_exit`` 를 부른다 — 훅 안의 구간은 훅을
    다시 부르지 않는다."""

    def __init__(self):
        super().__init__()
        self.after_exit = None
        self._in_hook = threading.local()

    def __exit__(self, *exc):
        out = super().__exit__(*exc)
        hook = self.after_exit
        if self._is_owned():  # 바깥 구간이 아직 쥐고 있다 — 배달 구간 안이다
            return out
        if hook is not None and not getattr(self._in_hook, "on", False):
            self._in_hook.on = True
            try:
                hook()
            finally:
                self._in_hook.on = False
        return out


@pytest.fixture
def wired(tmp_path):
    """레지스트리(훅 건 조건변수) + `wire_agent_mail` 의 waker·통지 기록 + 문에서 멈추는 상주 하나."""
    cv = HookedCondition()
    door = threading.Event()

    def runner(kind, start, ctx):
        door.wait()
        return _FakeLoopResult(output="ok"), 0.01

    reg = AgentRegistry(
        tmp_path,
        runtime={"model": "m", "timeout": 5},
        runner=runner,
        post=PostOffice(cv=cv),
    )
    notices: list[bool] = []
    wakes: list[str] = []
    waker, _, _ = wire_agent_mail(
        reg,
        enqueue_wake=lambda conn, text: wakes.append(text),
        on_mail_notice=lambda reply, busy: notices.append(busy),
    )
    key, _ = reg.spawn(name="helper")
    assert wait_until(lambda: reg.get(key).state == "idle")
    try:
        yield reg, cv, waker, key, notices, wakes
    finally:
        cv.after_exit = None
        door.set()
        reg.shutdown_all()


def _once_mail_lands(reg, cv, action):
    """main 상자에 메일이 든 구간을 나온 직후 한 번 ``action``."""
    fired = []

    def hook():
        if not fired and reg.queued_for("main"):
            fired.append(True)
            action()

    cv.after_exit = hook
    return fired


def test_idle_at_delivery_wins_over_a_pump_that_wakes_before_the_callback(wired):
    reg, cv, waker, key, notices, _ = wired
    waker.idle.set()  # 메일이 든 순간 main 은 한가하다
    fired = _once_mail_lands(reg, cv, waker.idle.clear)  # 콜백 전에 펌프가 런을 연다
    reg.message_to_main(key, "all done", notice=True)
    assert fired and notices == [False]


def test_busy_at_delivery_stays_busy_even_if_main_idles_before_the_callback(wired):
    reg, cv, waker, key, notices, _ = wired
    waker.idle.clear()  # 메일이 든 순간 main 은 바쁘다
    fired = _once_mail_lands(reg, cv, waker.idle.set)  # 콜백 전에 런이 끝나 한가해진다
    reg.message_to_main(key, "all done", notice=True)
    assert fired and notices == [True]


def test_the_recorded_order_gives_one_wake_and_no_busy_line(wired):
    """기록의 순서 그대로 — 배달 → 펌프의 `mark_idle` 이 든 메일을 보고 무장 → 깨우기를 꺼내 런을 연다(``idle`` 끔) →
    콜백. 통지는 "한가했다"(CLI 대기 줄 없음), 깨우기는 하나(콜백의 `on_mail` 은 무장하지 않는다)."""
    reg, cv, waker, key, notices, wakes = wired
    waker.idle.set()

    def pump_takes_the_mail():
        waker.mark_idle()  # 바퀴 꼭대기 — 든 메일을 보고 스스로 무장
        assert (
            waker.handle_dequeued(waker.WAKE_TEXT) == "run"
        )  # 꺼낸 깨우기 — 배달할 것이 있다
        waker.idle.clear()  # 런이 열린다

    fired = _once_mail_lands(reg, cv, pump_takes_the_mail)
    reg.message_to_main(key, "all done", notice=True)
    assert fired and notices == [False]
    assert wakes == [waker.WAKE_TEXT]
