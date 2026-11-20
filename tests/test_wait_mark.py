"""창구의 기다림 표시 — 받아들여진 `wait` 가 세우고, 기다림이 끝나는 모든 길에서 내린다 (5a).

5a 에서는 아무 판정도 이 표시를 읽지 않는다 — 여기서만 읽는다. 기다리는 **도중**의 값은 기다림 루프가 부르는 것
(잡 레지스트리 가짜의 ``running`` — 제자리 대기의 "아직 기다릴 것" 이 묻는다) 안에서 읽어 기록한다. 시간으로 단정하지
않는다.
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_cli.dialects import get as get_dialect
from agent_cli.loop.kind import PrivateLoop
from agent_cli.post import envelope as E
from agent_cli.post.office import Take
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.session_slots import (
    set_agent_registry,
    set_job_registry,
    set_monitor_registry,
    set_schedule_registry,
)
from agent_cli.subagent.resident_loop import ResidentLoop
from tests.loop_ports import make_ports, queue_inbox
from tests.loopkit import make_loop
from tests.test_agents_live import make_registry

A = "agent:a"
H = "harness:t"


@pytest.fixture
def reg(tmp_path):
    set_job_registry(None)
    set_monitor_registry(None)
    set_schedule_registry(None)
    r = make_registry(tmp_path)
    set_agent_registry(r)
    yield r
    set_agent_registry(None)
    set_job_registry(None)
    set_schedule_registry(None)


@pytest.fixture
def schedule_on():
    """켜진 예약 하나 — main·에이전트의 `wait` 를 받아들일 근거(런을 끝내는 대기)."""
    set_schedule_registry(SimpleNamespace(has_active_work=lambda: True))


def _provider(*replies: dict):
    p = MagicMock()
    p.call.side_effect = [LLMResponse(content=json.dumps(r)) for r in replies]
    return p


def _loop(ports, provider, **attrs):
    return make_loop(
        ports=ports,
        query="Q",
        query_request_id="",
        provider=provider,
        capabilities=ModelCapabilities(
            context_window=32768, max_output_tokens=4096, supports_thinking=False
        ),
        model="m",
        dialect=get_dialect("json_fc"),
        **attrs,
    )


WAIT = {"action": "wait"}
DONE = {"action": "complete", "result": "done"}


class _Jobs:
    """주인 ``H`` 의 잡 하나가 도는 대역. 체크인이 걸리면(``log_size``) 그다음부터의 ``running`` 은 제자리 대기의
    "아직 기다릴 것" 질문이다 — 그때마다 표시를 기록하고 ``then`` 을 한 번 한다."""

    def __init__(self, reg, then):
        self.reg = reg
        self.then = then
        self.armed = False
        self.seen: list[bool] = []
        self.job = SimpleNamespace(id=1, command="sleep 30")

    def running(self, owner=None):
        if not self.armed:
            return [self.job]
        self.seen.append(self.reg._post.is_waiting(H))
        return self.then(self)

    def log_size(self, job):
        self.armed = True
        return 0


def _nothing_left(jobs):
    return []


class TestInPlace:
    """제자리 대기(private 주인) — 깨는 길마다 표시가 돌아올 때 내려간다."""

    def _run(self, reg, then, **attrs):
        jobs = _Jobs(reg, then)
        set_job_registry(jobs)
        loop = _loop(PrivateLoop.make_ports(H), _provider(WAIT, DONE), **attrs)
        return jobs, loop

    def test_nothing_left(self, reg):
        jobs, loop = self._run(reg, _nothing_left)
        assert loop.run().output == "done"
        assert jobs.seen and all(jobs.seen)
        assert reg._post.is_waiting(H) is False

    def test_woken_by_mail(self, reg):
        def mail_arrives(jobs):
            if len(jobs.seen) == 1:
                reg.deliver(
                    H,
                    mail={"kind": "job", "output": "done"},
                    text="done",
                    author="main",
                    expects_reply=False,
                )
            return [jobs.job]

        jobs, loop = self._run(reg, mail_arrives)
        assert loop.run().output == "done"
        assert jobs.seen == [True]
        assert reg._post.is_waiting(H) is False

    def test_stopped(self, reg):
        ev = threading.Event()

        def stop(jobs):
            ev.set()
            return [jobs.job]

        jobs, loop = self._run(reg, stop, stop_event=ev)
        loop.run()
        assert jobs.seen == [True]
        assert reg._post.is_waiting(H) is False

    def test_error_while_waiting(self, reg):
        def boom(jobs):
            raise RuntimeError("registry broke")

        jobs, loop = self._run(reg, boom)
        with pytest.raises(RuntimeError):
            loop.run()
        assert jobs.seen == [True]
        assert reg._post.is_waiting(H) is False


class TestRefused:
    def test_a_refused_wait_does_not_mark(self, reg):
        """깨울 것이 없다 — 거절되고 런이 잇는다. 표시는 서지 않는다."""
        loop = _loop(
            make_ports(inbox=queue_inbox(lambda: None), agent_registry=reg),
            _provider(WAIT, DONE),
        )
        assert loop.run().output == "done"
        assert "wait refused — nothing would wake you" in str(loop._state.messages)
        assert reg._post.is_waiting("main") is False

    def test_a_wait_refused_for_arrived_mail_does_not_mark(self, reg, schedule_on):
        """모델 호출 도중 통지가 왔다 — 깨울 것(예약)이 있어도 도착분이 있으면 거절된다."""
        p = MagicMock()

        def call(*_a, **_k):
            if p.call.call_count == 1:
                reg._post.deliver(E.notice("n-1", "job", "main", "done"))
                return LLMResponse(content=json.dumps(WAIT))
            return LLMResponse(content=json.dumps({**DONE, "answers": ["n-1"]}))

        p.call.side_effect = call
        loop = _loop(make_ports(inbox=queue_inbox(lambda: None), agent_registry=reg), p)
        assert loop.run().output == "done"
        assert "1 notice arrived while you were thinking" in str(loop._state.messages)
        assert reg._post.is_waiting("main") is False


def _resident_ports():
    return ResidentLoop.make_ports(
        key="a",
        message_handler=None,
        questions=None,
        inbox=list,
        peer_roster=None,
        agent_registry=None,
    )


class TestEndOfRun:
    """런을 끝낸 대기(상주·main) — 받아들인 `wait` 가 그 런을 끝내고, 다음 런이 열리거나 상자가 닫힐 때 내린다."""

    def _waited(self, ports):
        loop = _loop(ports, _provider(WAIT))
        res = loop.run()
        assert res.waiting is True  # 받아들여진 `wait` 는 그 런을 끝냈다
        assert loop.turn == 1
        return res

    def test_resident_until_a_waking_envelope_opens_a_run(self, reg, schedule_on):
        self._waited(_resident_ports())
        po = reg._post
        assert po.is_waiting(A) is True
        assert (
            po.next_run(A, Take.ARRIVAL, timeout=0) is None
        )  # 열 것이 없다 — 아직 기다린다
        po.deliver(E.reply("n-1", "agent:b", A, "x", re="r-0", wakes=False))
        assert po.next_run(A, Take.ARRIVAL, timeout=0) is None
        assert po.is_waiting(A) is True  # 깨우지 않는 봉투로는 런이 열리지 않는다
        po.deliver(E.request("r-1", "main", A, "do"))
        assert [e.id for e in po.next_run(A, Take.ARRIVAL, timeout=0)] == ["r-1"]
        assert po.is_waiting(A) is False

    def test_resident_box_closed(self, reg, schedule_on):
        self._waited(_resident_ports())
        reg._post.close(A)  # ✕·세션 종료
        assert reg._post.is_waiting(A) is False

    def test_resident_owner_gone(self, reg, schedule_on):
        self._waited(_resident_ports())
        reg.drop_mailbox(A)  # 워커의 끝(`release_owner`)
        assert reg._post.is_waiting(A) is False

    def test_main_until_words_open_a_run(self, reg, schedule_on):
        self._waited(make_ports(inbox=queue_inbox(lambda: None), agent_registry=reg))
        po = reg._post
        assert po.is_waiting("main") is True
        assert (
            po.next_run("main", Take.WORDS_FIRST, timeout=0) is None
        )  # 펌프의 시한 만료
        assert po.is_waiting("main") is True
        po.deliver(E.words("u-1", "user:dj", "main", "hi"))
        assert [e.id for e in po.next_run("main", Take.WORDS_FIRST, timeout=0)] == [
            "u-1"
        ]
        assert po.is_waiting("main") is False

    def test_main_until_waking_mail_opens_a_run(self, reg, schedule_on):
        self._waited(make_ports(inbox=queue_inbox(lambda: None), agent_registry=reg))
        po = reg._post
        po.deliver(E.notice("n-1", "job", "main", "done"))
        assert po.next_run("main", Take.WORDS_FIRST, timeout=0) == ()
        assert po.is_waiting("main") is False

    def test_main_box_closed(self, reg, schedule_on):
        self._waited(make_ports(inbox=queue_inbox(lambda: None), agent_registry=reg))
        reg._post.close("main")
        assert reg._post.is_waiting("main") is False


def test_no_registry_marks_nothing():
    from agent_cli.wake import Mailbox

    mb = Mailbox(None, H, count_words=True)
    mb.mark_waiting()
    mb.clear_waiting()  # 레지스트리 없는 세션 — 아무것도 하지 않는다
