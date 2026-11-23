"""main 앞 메일 통지의 "바쁨" 은 그 메일이 도착한 순간의 사실이다 (v10.145.x, FIX-IDLE).

`AgentRegistry._push_reply` 는 배달(``_cv`` 안) → 디스크 저장 → `on_reply` 콜백 순이다. 콜백이 지금의 한가함을 읽으면,
그 사이 펌프가 이미 든 메일로 런을 연 뒤라 "바쁨" 으로 읽혔다(CLI 대기 줄이 같은 메일의 관찰 앞에 한 줄 더 —
기록 S/g33/idle-hunt-full/run-16). 이제 배달 구간에서 창구의 한가함(`PostOffice.idle`)을 읽어 넘긴다.

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
    """레지스트리(훅 건 조건변수) + `wire_agent_mail` 의 통지 기록 + 문에서 멈추는 상주 하나."""
    cv = HookedCondition()
    door = threading.Event()

    def runner(kind, start, ctx):
        door.wait()
        return _FakeLoopResult(output="ok"), 0.01

    post = PostOffice(cv=cv)
    reg = AgentRegistry(
        tmp_path,
        runtime={"model": "m", "timeout": 5},
        runner=runner,
        post=post,
    )
    notices: list[bool] = []
    wire_agent_mail(reg, on_mail_notice=lambda reply, busy: notices.append(busy))
    key, _ = reg.spawn(name="helper")
    assert wait_until(lambda: reg.get(key).state == "idle")
    try:
        yield reg, cv, post, key, notices
    finally:
        cv.after_exit = None
        door.set()
        reg.shutdown_all()


def _main_goes_idle(post) -> None:
    """펌프가 런을 열 것을 기다리기 시작했다 — 빈손으로 돌아와도 한가함은 남는다."""
    assert post.next_run("main", timeout=0) is None


def _main_opens_a_run(post) -> None:
    """펌프가 든 메일로 런을 연다 — 한가함이 내려간다."""

    assert post.next_run("main", timeout=0) is not None  # 메일이 연 런


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
    reg, cv, post, key, notices = wired
    _main_goes_idle(post)  # 메일이 든 순간 main 은 한가하다
    fired = _once_mail_lands(
        reg, cv, lambda: _main_opens_a_run(post)
    )  # 콜백 전에 펌프가 런을 연다
    reg.message_to_main(key, "all done", notice=True)
    assert fired and notices == [False]


def test_busy_at_delivery_stays_busy_when_the_pump_reaches_the_mail_before_the_callback(
    wired,
):
    """메일이 든 순간 main 은 바쁘다 — 그 뒤 펌프가 기다림에 들면 같은 락 구간에서 그 메일로 곧바로 런을 연다(한가함이
    서지 않는다). 통지는 배달 순간의 "바쁨" 이다."""
    reg, cv, post, key, notices = wired
    assert not post.idle("main")
    fired = _once_mail_lands(reg, cv, lambda: _main_opens_a_run(post))
    reg.message_to_main(key, "all done", notice=True)
    assert fired and notices == [True] and not post.idle("main")
