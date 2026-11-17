"""읽기 전용 공개 조회 — `AgentRegistry.queued_for`·`taken_by`, `Ledger.open_requests`.

테스트가 레지스트리의 저장소(main 메일함·받은편지함 미러·처리 중 목록·private 메일함·장부의 요청 목록)를
직접 읽지 않고 이 조회로 묻는다. 고정하는 계약: 돌려주는 것은 불변 값(사본)이다 — 받은 쪽이 바꿀 수 없고,
뒤의 변화가 이미 받은 값에 비치지 않는다 · 두 저장 모양(main 메일·에이전트 항목)이 같은 어휘로 보인다.
"""

from __future__ import annotations

import dataclasses
import threading

import pytest

from agent_cli.loop.ledger import Ledger
from agent_cli.subagent.agent_mail import MailView
from tests.test_agents_live import make_registry, wait_until


def _gated_runner(gate: threading.Event, started: threading.Event):
    """런 하나를 ``gate`` 가 열릴 때까지 붙잡는 가짜 러너 — 시작하면 ``started`` 를 세운다."""
    from tests.test_agents_live import _FakeLoopResult

    def runner(kind, start, ctx):
        started.set()
        gate.wait(5)
        return _FakeLoopResult(output=f"done:{start.query}"), 0.01

    return runner


@pytest.fixture
def held(tmp_path):
    """런 하나가 붙잡힌 상주 하나 — ``(reg, key, gate)``."""
    gate, started = threading.Event(), threading.Event()
    reg = make_registry(tmp_path, runner=_gated_runner(gate, started))
    key, err = reg.spawn()
    assert not err
    assert wait_until(lambda: reg.get(key).state == "idle")
    assert reg.request(key, "first job") == ""
    assert started.wait(5)
    yield reg, key, gate
    gate.set()
    reg.shutdown_all()


class TestQueuedFor:
    def test_main_mail_shows_its_header_words(self, tmp_path):
        reg = make_registry(tmp_path)
        rid = reg.message_to_main("agt-x", "please look", expects_reply=True)
        nid = reg.message_to_main("agt-x", "fyi", notice=True)
        got = reg.queued_for("main")
        assert [m.id for m in got] == [rid, nid]
        assert got[0] == MailView(
            id=rid, src="agent:agt-x", text="please look", expects_reply=True
        )
        assert got[1].expects_reply is False and got[1].wakes is True

    def test_the_view_is_a_frozen_copy(self, tmp_path):
        reg = make_registry(tmp_path)
        reg.message_to_main("agt-x", "one", notice=True)
        got = reg.queued_for("main")
        assert isinstance(got, tuple)
        with pytest.raises(dataclasses.FrozenInstanceError):
            got[0].text = "changed"
        reg.message_to_main("agt-x", "two", notice=True)
        assert len(got) == 1  # 이미 받은 값은 뒤의 도착을 비추지 않는다
        reg.drain_replies()
        assert len(got) == 1 and reg.queued_for("main") == ()

    def test_agent_inbox_in_arrival_order(self, held):
        reg, key, _ = held
        me = f"agent:{key}"
        assert reg.request(key, "second", author="agent:other") == ""
        assert reg.request(key, "from a person", author="user:dj") == ""
        got = reg.queued_for(me)
        assert [(m.src, m.text) for m in got] == [
            ("agent:other", "second"),
            ("user:dj", "from a person"),
        ]
        # 에이전트의 요청은 `reply` 를 빚지우고, 사람의 말은 아니다
        assert [m.expects_reply for m in got] == [True, False]
        assert reg.queued_for(key) == got  # 맨 키도 같은 주소다

    def test_reply_and_notice_items_owe_nothing(self, held):
        reg, key, _ = held
        reg.request(
            key, "answer", author="main", expects_reply=False, kind="reply", re="r-9"
        )
        (m,) = reg.queued_for(f"agent:{key}")
        assert (m.src, m.re, m.expects_reply, m.wakes) == ("main", "r-9", False, True)

    def test_items_that_do_not_wake_or_persist(self, held):
        reg, key, _ = held
        reg.request(key, "quiet", expects_reply=False, kind="reply", opens_run=False)
        reg.request(key, "now only", expects_reply=False, kind="checkin", ledger="none")
        quiet, checkin = reg.queued_for(f"agent:{key}")
        assert (quiet.wakes, quiet.persists) == (False, True)
        assert (checkin.wakes, checkin.persists) == (True, False)

    def test_private_mailbox(self, tmp_path):
        reg = make_registry(tmp_path)
        reg.deliver(
            "harness:job-1",
            mail={"kind": "job", "output": "done"},
            text="done",
            author="harness",
            expects_reply=False,
        )
        (m,) = reg.queued_for("harness:job-1")
        assert (m.src, m.text) == ("job", "done") and m.id.startswith("n-")
        assert reg.take_mail("harness:job-1") and reg.queued_for("harness:job-1") == ()

    def test_unknown_address_is_empty(self, tmp_path):
        reg = make_registry(tmp_path)
        assert reg.queued_for("agent:nobody") == () == reg.taken_by("agent:nobody")


class TestTakenBy:
    def test_the_run_holds_what_it_took_until_it_ends(self, held):
        reg, key, gate = held
        me = f"agent:{key}"
        (m,) = reg.taken_by(me)
        assert (m.src, m.text, m.expects_reply) == ("main", "first job", True)
        assert reg.queued_for(me) == ()
        gate.set()
        assert wait_until(lambda: reg.taken_by(me) == ())

    def test_main_and_private_hold_nothing(self, tmp_path):
        reg = make_registry(tmp_path)
        reg.message_to_main("agt-x", "fyi", notice=True)
        assert reg.taken_by("main") == () == reg.taken_by("harness:job-1")


class TestLedgerOpenRequests:
    def test_copies_in_arrival_order(self):
        led = Ledger()
        led.add_request("u-1", "Bob", "first")
        led.add_request("n-2", "mail", "second")
        got = led.open_requests()
        assert got == (
            {"id": "u-1", "author": "Bob", "text": "first"},
            {"id": "n-2", "author": "mail", "text": "second"},
        )
        got[0]["author"] = "changed"
        assert led.open_requests()[0]["author"] == "Bob"  # 사본 — 장부는 그대로
        led.settle({"u-1"})
        assert [r["id"] for r in led.open_requests()] == ["n-2"]
