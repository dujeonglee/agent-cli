"""회신·메시지의 결과는 사실을 말한다 (v10.145.2) — 모델이 읽는 글과 코드가 하는 일이 같아야 한다.

- 런이 어떻게 끝나든(`wait` 로 끝나도) 그 런에서 닫힌 줄은 버린다 — 다음 런의 같은 id `reply` 에 "in this run"
  이라고 말하지 않는다.
- 요청자가 죽었거나 사라졌으면 `reply` 는 "not delivered" 를 말하고 실패로 기록된다 — 에이전트가 갚든 main 이
  갚든 같은 문장. 빚은 닫힌다(기다리는 이가 없다).
"""

from __future__ import annotations

import json
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


def _gone(key: str, rid: str) -> str:
    return (
        f'not delivered — agent "{key}" that sent request [{rid}] is gone. [{rid}] is '
        "closed; there is nothing more to do for it."
    )


class TestReplyToAGoneRequester:
    def test_agent_replies_to_a_dead_requester(self, tmp_path):
        reg, a, b, door = _two_idle_agents(tmp_path)
        try:
            me, them = f"agent:{b}", f"agent:{a}"
            d = reg.note_owes(me, them, "please decide X")
            assert reg.kill(a) == ""
            assert wait_until(lambda: reg.get(a).state == "dead")
            assert reg.question_port(b).reply("the answer", id=d.id) == _gone(a, d.id)
            assert reg.reply_debts(me) == []  # 닫혔다 — 다시 갚게 하지 않는다
            assert reg.queued_for(them) == ()  # 아무것도 가지 않았다
        finally:
            door.set()
            reg.shutdown_all()

    def test_agent_replies_to_a_requester_the_registry_does_not_know(self, tmp_path):
        reg, _a, b, door = _two_idle_agents(tmp_path)
        try:
            me = f"agent:{b}"
            d = reg.note_owes(me, "agent:agt-ghost00", "from nowhere")
            assert reg.question_port(b).reply("hi", id=d.id) == _gone(
                "agt-ghost00", d.id
            )
            assert reg.reply_debts(me) == []
        finally:
            door.set()
            reg.shutdown_all()

    def test_main_replies_to_a_dead_agent_with_the_same_sentence(self, tmp_path):
        reg, a, _b, door = _two_idle_agents(tmp_path)
        try:
            d = reg.note_owes("main", f"agent:{a}", "main owes a")
            assert reg.kill(a) == ""
            assert wait_until(lambda: reg.get(a).state == "dead")
            assert reg.question_port(None).reply("late", id=d.id) == _gone(a, d.id)
            assert reg.reply_debts("main") == []
        finally:
            door.set()
            reg.shutdown_all()

    def test_the_loop_records_it_as_failed(self, tmp_path):
        """루프의 관찰: `[reply] not delivered — …`, 레코드의 success 는 False."""
        from agent_cli.context.manager import ContextManager
        from agent_cli.providers.base import LLMResponse
        from agent_cli.providers.capabilities import ModelCapabilities
        from tests.binding_support import make_binding
        from tests.loop_ports import make_ports
        from tests.loopkit import run_loop

        reg, a, b, door = _two_idle_agents(tmp_path)
        try:
            me, them = f"agent:{b}", f"agent:{a}"
            d = reg.note_owes(me, them, "please decide X")
            assert reg.kill(a) == ""
            assert wait_until(lambda: reg.get(a).state == "dead")
            script = [
                json.dumps({"action": "reply", "id": d.id, "text": "the answer"}),
                json.dumps({"action": "complete", "result": "done"}),
            ]

            class _P:
                def call(self, messages, system, model, capabilities, **kw):
                    return LLMResponse(content=script.pop(0))

            ctx = ContextManager(tmp_path / "loop", max_context_tokens=20_000)
            run_loop(
                query="go",
                binding=make_binding(
                    provider=_P(),
                    capabilities=ModelCapabilities(32768, 4096, False),
                ),
                ports=make_ports(owner=me, questions=reg.question_port(b)),
                ctx=ctx,
            )
            (obs,) = [m for m in ctx.get_raw_messages() if m.get("tool") == "reply"]
            assert obs["success"] is False
            assert obs["content"] == "Observation: [reply] " + _gone(a, d.id)
        finally:
            door.set()
            reg.shutdown_all()
