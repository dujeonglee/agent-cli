"""main 앞 사람의 입력 — 오라클과 기존 단위 테스트가 고정하지 않던 계약 (v10.146.6 의 동작 그대로).

- 사람의 입력은 ``agents.json`` 에 저장되지 않는다(재시작하면 대기 중이던 말은 사라진다) — 미배달 메일만 남는다.
- 웹 서버가 펌프를 닫아도 이미 줄 선 사람의 입력은 먼저 꺼내져 런을 연다.
- 한가한 main 을 메일이 깨우면 그 깨움이 항목 번호 하나를 쓴다(번호 공간은 모든 접두가 함께 쓴다).
"""

from __future__ import annotations

import json
import threading

from agent_cli import item_ids
from agent_cli.input_queue import InputQueue
from agent_cli.pump import ForeverPolicy
from agent_cli.runtime import wire_agent_mail
from agent_cli.subagent.agents_live import AgentRegistry
from tests.pump_support import FakeSurface, make_pump, run_pump_in_thread
from tests.test_engine_p2 import wait_until


def _job_mail(reg) -> None:
    reg.deliver(
        "main",
        mail={"kind": "job", "success": True, "output": "🧵 job #1 finished rc=0"},
        text="",
        author="",
        expects_reply=False,
    )


class TestWordsAreNotSaved:
    def test_only_the_mail_reaches_agents_json(self, tmp_path):
        reg = AgentRegistry(tmp_path, runtime={"model": "m", "timeout": 5})
        q = InputQueue()
        q.enqueue("c1", "MAIN please look", nickname="dj")
        _job_mail(reg)  # 저장을 부른다 — main 앞 미배달분 미러
        doc = json.loads((tmp_path / "agents.json").read_text(encoding="utf-8"))
        assert [m.get("kind") for m in doc["pending"]] == ["job"]
        assert "MAIN please look" not in json.dumps(doc, ensure_ascii=False)


class TestClosingThePump:
    def test_queued_words_still_open_runs_before_the_pump_ends(self):
        q = InputQueue()
        q.enqueue("c1", "first", nickname="dj")
        q.enqueue("c1", "second", nickname="dj")
        q.shutdown()
        ran: list[str] = []
        pump = make_pump(
            queue=q,
            run_main=lambda req: ran.append(req.text),
            policy=ForeverPolicy(),
            surface=FakeSurface(),
        )
        pump.run()
        assert ran == ["first", "second"]


class TestNumbersAroundAMailRun:
    def test_a_mail_that_wakes_an_idle_main_uses_one_more_number(self, tmp_path):
        """사람 입력 ``a`` → (한가한 main 앞에 메일 한 통) → 사람 입력 ``b``. 그 사이 번호는 메일 하나와 메일이
        main 을 깨운 것 하나 — ``b`` 는 ``a`` 보다 3 크다."""
        reg = AgentRegistry(tmp_path, runtime={"model": "m", "timeout": 5})
        q = InputQueue()
        waker, _, _ = wire_agent_mail(
            reg, enqueue_wake=lambda conn, text: q.enqueue(conn, text, system=True)
        )
        opened: list[str] = []
        run_done = threading.Event()

        def run_main(req):
            opened.append(req.text)
            if req.wake:
                reg.drain_replies()  # 메일 런의 첫 턴 경계가 하는 일
            run_done.set()

        pump = make_pump(
            queue=q,
            waker=waker,
            registry=reg,
            run_main=run_main,
            policy=ForeverPolicy(),
        )
        a = q.enqueue("c1", "a", nickname="dj")
        ended = run_pump_in_thread(pump)
        try:
            assert wait_until(lambda: opened == ["a"])
            assert wait_until(lambda: waker.idle.is_set())
            run_done.clear()
            _job_mail(reg)
            assert wait_until(run_done.is_set)
            b = q.enqueue("c1", "b", nickname="dj")
            assert item_ids.number_of(b["id"]) - item_ids.number_of(a["id"]) == 3
        finally:
            q.shutdown()
            assert ended.wait(10)
