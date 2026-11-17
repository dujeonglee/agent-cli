"""회신·메시지의 결과는 사실을 말한다 (v10.145.2) — 모델이 읽는 글과 코드가 하는 일이 같아야 한다.

- 런이 어떻게 끝나든(`wait` 로 끝나도) 그 런에서 닫힌 줄은 버린다 — 다음 런의 같은 id `reply` 에 "in this run"
  이라고 말하지 않는다.
"""

from __future__ import annotations

import threading

from tests.test_agents_live import _FakeLoopResult, make_registry
from tests.test_engine_p2 import wait_until


def _two_idle_agents(tmp_path):
    """런이 문에서 막히는 두 상주 에이전트 — 빚은 테스트가 직접 적는다(`note_owes`)."""
    door = threading.Event()

    def runner(kind, start, ctx):
        door.wait()
        return _FakeLoopResult(output="ok"), 0.01

    reg = make_registry(tmp_path, runner=runner)
    a, _ = reg.spawn(name="a")
    b, _ = reg.spawn(name="b")
    assert wait_until(lambda: all(reg.get(k).state == "idle" for k in (a, b)))
    return reg, a, b, door


class TestClosedRowsGoAtEveryRunEnd:
    def test_reply_again_after_a_wait_ended_run_is_not_in_this_run(self, tmp_path):
        reg, a, b, door = _two_idle_agents(tmp_path)
        try:
            me, them = f"agent:{b}", f"agent:{a}"
            d1 = reg.note_owes(me, them, "first request")
            d2 = reg.note_owes(me, them, "second request")  # 열린 채 — id 길을 탄다
            port = reg.question_port(b)
            assert port.reply("answer", id=d1.id) == ""
            same_run = port.reply("again", id=d1.id)
            assert same_run.startswith(f"already replied to [{d1.id}]")
            assert "in this run" in same_run  # 같은 런 — 참
            # 런이 `wait` 로 끝난다 — 워커가 하는 두 단계
            reg.run_ended(me, "", waiting=True)
            reg._close_run(reg.get(b), 0)
            next_run = port.reply("again", id=d1.id)
            assert next_run == f'no unpaid request "{d1.id}" — you owe: ' + (
                f'[{d2.id}] {a}: "second request"' + "."
            )
            assert [d.id for d in reg.reply_debts(me)] == [d2.id]  # 잠든 빚은 남는다
        finally:
            door.set()
            reg.shutdown_all()
