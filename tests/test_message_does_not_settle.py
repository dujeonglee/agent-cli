"""`message` 는 빚을 갚지 않는다 — 갚는 것은 `reply` 하나 (v10.145.1, 사용자 결정).

모델이 읽는 계약은 `message` = 요청·통지, "To answer a request you received, use `reply`." 이다. 종전(v9.21.0)엔
`message` 의 받는 쪽이 내 요청자면 내가 그에게 진 가장 오래된 빚을 조용히 닫아, 되물은 뒤의 `reply` 가 거절되고
요청자는 답을 못 받았다(`S/g33/probe` 재현). 워커는 문(``door``)에서 멈춘다 — 배달된 항목이 런으로 소비되며
상자가 바뀌는 것과 단언이 경주하지 않게.
"""

from __future__ import annotations

import re
import threading

import pytest

from tests.test_agents_live import _FakeLoopResult, make_registry
from tests.test_engine_p2 import (
    Provider,
    call,
    complete_all,
    contents,
    owed_id,
    session,  # noqa: F401 — 픽스처
    wait_until,
)


@pytest.fixture
def ab(tmp_path):
    """a 가 b 에게 요청 하나(b 가 빚진다) — b 가 되묻거나 통지하는 쪽."""
    door = threading.Event()

    def runner(kind, start, ctx):
        door.wait()
        return _FakeLoopResult(output="ok"), 0.01

    reg = make_registry(tmp_path, runner=runner)
    a, _ = reg.spawn(name="a")
    b, _ = reg.spawn(name="b")
    assert wait_until(lambda: all(reg.get(k).state == "idle" for k in (a, b)))
    debt = reg.note_owes(f"agent:{b}", f"agent:{a}", "please decide X")
    try:
        yield reg, a, b, debt
    finally:
        door.set()
        reg.shutdown_all()


def test_asking_back_leaves_the_request_open_and_reply_answers_it(ab):
    """되물은 `message`(회신 기대) 뒤에도 빚은 열려 있고, `reply(id)` 가 갚아 요청자에게 ``re [id]`` 로 간다."""
    reg, a, b, debt = ab
    me, them = f"agent:{b}", f"agent:{a}"
    receipt, err = reg.message_handler(me)(a, "which branch do you mean?", True)
    assert err == "" and "delivered" in receipt
    assert [d.id for d in reg.reply_debts(me)] == [debt.id]
    assert [r["id"] for r in reg.awaited_replies(them)] == [debt.id]
    assert reg.question_port(b).reply("X is decided: use main", id=debt.id) == ""
    assert reg.reply_debts(me) == [] and reg.awaited_replies(them) == []
    arrived = [*reg.queued_for(them), *reg.taken_by(them)]
    assert [m.re for m in arrived if m.text == "X is decided: use main"] == [debt.id]


def test_a_notice_to_the_requester_does_not_settle_either(ab):
    reg, a, b, debt = ab
    receipt, err = reg.message_handler(f"agent:{b}")(a, "fyi: started", False)
    assert err == "" and "delivered" in receipt
    assert [d.id for d in reg.reply_debts(f"agent:{b}")] == [debt.id]


def test_complete_after_asking_back_names_the_debt(session):  # noqa: F811
    """되묻고 `complete` 하면 되묻기 문장(`_nag_open_items`)이 그 빚을 말한다 — 기존 장치 그대로. 그다음
    `reply` 가 갚고, main 은 그 요청의 회신을 ``re [그 id]`` 로 받는다."""
    seen: dict = {}

    def read_the_nag_then_reply(msgs):
        # 되묻기 관찰 — main 의 회신이 같은 턴 경계에 흡수되면 그 뒤에 오므로 마지막 글이 아닐 수 있다
        (nag,) = [
            str(m.get("content") or "")
            for m in msgs
            if "your result is recorded, but" in str(m.get("content") or "")
        ]
        seen["nag"] = nag
        seen["id"] = re.search(r"\[(r-\d+)\] \(main\) reply owed", nag).group(1)
        return call("reply", id=seen["id"], text="decided: main")

    p = Provider(
        {
            "MAIN": [
                call("agent", mode="run", task="TASK-Q decide the branch"),
                call("wait"),
                lambda m: call("reply", id=owed_id(m), text="use main"),
                call("wait"),
                complete_all("done"),
            ],
            "TASK-Q": [
                call("message", to="main", text="which branch?"),
                call("complete", result="went with main"),
                read_the_nag_then_reply,
                call("complete", result="ok"),
            ],
        }
    )
    s = session(p)
    assert s["run"]().success
    assert "Observation: your result is recorded, but" in seen["nag"]
    assert f"[{seen['id']}] (main) reply owed" in seen["nag"]
    assert f'`reply` with `id` "{seen["id"]}"' in seen["nag"]
    assert f"re [{seen['id']}]" in contents(s["ctx"])
    assert "decided: main" in contents(s["ctx"])
