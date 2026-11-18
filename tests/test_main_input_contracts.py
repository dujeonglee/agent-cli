"""main 앞 사람의 입력 — 오라클과 기존 단위 테스트가 고정하지 않던 계약.

- 사람의 입력은 ``agents.json`` 에 저장되지 않는다(재시작하면 대기 중이던 말은 사라진다) — 같은 main 상자에 서도
  미배달 메일만 남는다.
- 웹 서버가 펌프를 닫아도 이미 줄 선 사람의 입력은 먼저 꺼내져 런을 연다.
- 한가한 main 을 메일이 깨워도 항목 번호를 더 쓰지 않는다 — 메일 한 통이 번호 하나다(번호 공간은 모든 접두가 함께
  쓴다). v10.146.6 까지는 깨우기 항목이 번호 하나를 더 썼다.
"""

from __future__ import annotations

import json
import threading

from agent_cli import item_ids
from agent_cli.main_input import MainInput
from agent_cli.pump import ForeverPolicy
from agent_cli.runtime import wire_agent_mail
from agent_cli.subagent.agents_live import AgentRegistry
from tests.mail_support import absorbed_mail
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
        q = MainInput()
        reg = AgentRegistry(tmp_path, runtime={"model": "m", "timeout": 5}, post=q.post)
        q.enqueue("c1", "MAIN please look", nickname="dj")
        _job_mail(reg)  # 저장을 부른다 — main 앞 미배달분 미러
        doc = json.loads((tmp_path / "agents.json").read_text(encoding="utf-8"))
        assert [m.get("kind") for m in doc["pending"]] == ["job"]
        assert "MAIN please look" not in json.dumps(doc, ensure_ascii=False)


class TestClosingThePump:
    def test_queued_words_still_open_runs_before_the_pump_ends(self):
        q = MainInput()
        q.enqueue("c1", "first", nickname="dj")
        q.enqueue("c1", "second", nickname="dj")
        q.close()
        ran: list[str] = []
        pump = make_pump(
            main_input=q,
            run_main=lambda req: ran.append(req.query),
            policy=ForeverPolicy(),
            surface=FakeSurface(),
        )
        pump.run()
        assert ran == ["first", "second"]


class TestNumbersAroundAMailRun:
    def test_a_mail_that_wakes_an_idle_main_uses_only_its_own_number(self, tmp_path):
        """사람 입력 ``a`` → (한가한 main 앞에 메일 한 통) → 사람 입력 ``b``. 그 사이 번호는 메일 하나뿐 — ``b`` 는
        ``a`` 보다 2 크다."""
        q = MainInput()
        reg = AgentRegistry(tmp_path, runtime={"model": "m", "timeout": 5}, post=q.post)
        wire_agent_mail(reg)
        opened: list[str] = []
        run_done = threading.Event()

        def run_main(req):
            opened.append(req.query)
            if not req.author_is_user:
                absorbed_mail(reg)  # 메일 런의 첫 턴 경계가 하는 일
            run_done.set()

        pump = make_pump(
            main_input=q, registry=reg, run_main=run_main, policy=ForeverPolicy()
        )
        a = q.enqueue("c1", "a", nickname="dj")
        ended = run_pump_in_thread(pump)
        try:
            assert wait_until(lambda: opened == ["a"])
            assert wait_until(q.idle)
            run_done.clear()
            _job_mail(reg)
            assert wait_until(run_done.is_set)
            b = q.enqueue("c1", "b", nickname="dj")
            assert item_ids.number_of(b["id"]) - item_ids.number_of(a["id"]) == 2
        finally:
            q.close()
            assert ended.wait(10)


class TestTheMailThatOpensARunSurvivesARestart:
    """한가한 main 을 깨운 메일은 그 런의 첫 턴 경계가 흡수할 때까지 ``agents.json`` 에 남는다 — 그 사이 다른 저장이
    끼어도. 그 사이에 프로세스가 죽으면 재시작이 그 메일을 다시 배달한다. 흡수한 뒤에는 대화 기록(history)에 들어
    있으므로 저장 파일에서 빠진다."""

    @staticmethod
    def _pending(tmp_path):
        doc = json.loads((tmp_path / "agents.json").read_text(encoding="utf-8"))
        return [m.get("kind") for m in doc["pending"]]

    def test_until_the_first_turn_boundary_takes_it(self, tmp_path):
        from agent_cli.main_input import OPEN_MAIL

        q = MainInput()
        reg = AgentRegistry(tmp_path, runtime={"model": "m", "timeout": 5}, post=q.post)
        _job_mail(reg)
        assert q.wait_to_open(0) is OPEN_MAIL  # 메일 런이 열린다
        assert self._pending(tmp_path) == ["job"]
        reg._save_state()  # 그 사이 다른 저장(에이전트의 상태 변화 등)
        assert self._pending(tmp_path) == ["job"]
        assert [m.get("kind") for m in absorbed_mail(reg)] == ["job"]  # 첫 턴 경계
        assert self._pending(tmp_path) == []
