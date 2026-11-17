"""갚음은 호출자가 말한다 (v10.145.0) — `request`·`message_to_main` 의 ``settles``.

봉투 모양이 같아도 뜻이 다르다: `agent` 도구의 위임은 `message` 의 요청과 같은 봉투지만 빚을 갚지 않는다(v10.56.0
결정 — `test_agent_questions.py::TestCompleteIsLocal::test_reply_settles_the_debt_and_agent_request_does_not`).
그래서 그 배달이 닫는 줄은 호출자가 ``settles`` 로 말하고(줄 id, `message` 는 `SETTLES_OLDEST`), 말하지 않은 배달은 닫지 않는다. ``settles`` 는 배달
지시라 항목·저장 파일에 실리지 않는다.

워커는 문(``door``)에서 멈춘다 — 배달된 항목이 런으로 소비되며 상자가 바뀌는 것과 단언이 경주하지 않게.
"""

from __future__ import annotations

import json
import threading

import pytest

from agent_cli.post.request_book import RowError
from agent_cli.subagent.agents_live import SETTLES_OLDEST
from tests.test_agents_live import _FakeLoopResult, make_registry
from tests.test_engine_p2 import wait_until


@pytest.fixture
def two(tmp_path):
    """에이전트 a·b, b 가 a 에게 회신 둘을 빚진다(d1 이 오래된 것)."""
    door = threading.Event()

    def runner(kind, start, ctx):
        door.wait()
        return _FakeLoopResult(output="ok"), 0.01

    reg = make_registry(tmp_path, runner=runner)
    a, _ = reg.spawn(name="a")
    b, _ = reg.spawn(name="b")
    assert wait_until(lambda: all(reg.get(k).state == "idle" for k in (a, b)))
    me, them = f"agent:{b}", f"agent:{a}"
    d1 = reg.note_owes(me, them, "first")
    d2 = reg.note_owes(me, them, "second")
    try:
        yield reg, a, b, me, d1, d2
    finally:
        door.set()
        reg.shutdown_all()


def _owed(reg, me):
    return [d.id for d in reg.reply_debts(me)]


def test_the_same_envelope_without_settles_closes_nothing(two):
    """위임의 모양(요청)·회신의 모양 — 둘 다 ``settles`` 없이는 빚을 닫지 않는다."""
    reg, a, _b, me, d1, d2 = two
    assert reg.request(a, "delegated work", author=me) == ""
    assert (
        reg.request(
            a, "an answer", author=me, expects_reply=False, kind="reply", re=d2.id
        )
        == ""
    )
    assert _owed(reg, me) == [d1.id, d2.id]


def test_settles_closes_that_row_only(two):
    """회신 모양 + ``settles`` → 그 줄만(`pay`). 오래된 빚은 그대로."""
    reg, a, _b, me, d1, d2 = two
    err = reg.request(
        a,
        "the answer",
        author=me,
        expects_reply=False,
        kind="reply",
        re=d2.id,
        settles=d2.id,
    )
    assert err == ""
    assert _owed(reg, me) == [d1.id]
    assert reg.debt_by_id(me, d2.id).settled


def test_a_message_settles_the_oldest_debt_the_post_office_finds(two):
    """`message` 는 줄을 지목하지 않는다 — `SETTLES_OLDEST` 면 우편이 락 안에서 받는 쪽에 진 가장 오래된 빚을 닫고
    (먼저 닫힌 줄이 있으면 그다음 것), 빚이 없으면 배달만 한다. 같은 봉투에 지시가 없으면 닫지 않는다."""
    reg, a, _b, me, d1, d2 = two
    them = f"agent:{a}"
    assert reg.request(a, "fyi", author=me, kind="notice", expects_reply=False) == ""
    assert _owed(reg, me) == [d1.id, d2.id]  # 지시 없음 — 닫지 않는다
    assert reg.request(a, "move one", author=me, settles=SETTLES_OLDEST) == ""
    assert _owed(reg, me) == [d2.id]  # 가장 오래된 d1
    assert reg.request(a, "move two", author=me, settles=SETTLES_OLDEST) == ""
    assert _owed(reg, me) == []
    before = len(reg.queued_for(them)) + len(reg.taken_by(them))
    assert reg.request(a, "move three", author=me, settles=SETTLES_OLDEST) == ""
    assert (
        len(reg.queued_for(them)) + len(reg.taken_by(them)) == before + 1
    )  # 빚 없음 — 배달만


def test_settles_is_not_written_into_the_item_or_the_state_file(two, tmp_path):
    reg, a, _b, me, d1, _d2 = two
    reg.request(
        a, "ans", author=me, expects_reply=False, kind="reply", re=d1.id, settles=d1.id
    )
    state = (tmp_path / "agents.json").read_text(encoding="utf-8")
    assert "settles" not in state and json.loads(state)  # 파일은 읽히고 지시는 없다


def test_unknown_and_already_closed_ids_keep_todays_wording(two):
    """없는 rid·이미 닫힌 rid — 거절 문구는 지금 그대로(`QuestionPort.reply`)."""
    reg, a, b, me, _d1, d2 = two
    port = reg.question_port(b)
    assert port.reply("answer two", id=d2.id) == ""
    again = port.reply("answer two again", id=d2.id)
    assert again.startswith(f"already replied to [{d2.id}] ({d2.to}) in this run")
    unknown = port.reply("hm", id="r-999")
    assert unknown.startswith('no unpaid request "r-999" — you owe: ')
    with pytest.raises(RowError):  # 갚는 자리가 없는 줄을 대면 배달 전에 거절된다
        reg.request(a, "x", author=me, kind="reply", re="r-999", settles="r-999")
