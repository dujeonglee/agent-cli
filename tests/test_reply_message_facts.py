"""회신·메시지의 결과는 사실을 말한다 (v10.145.2) — 모델이 읽는 글과 코드가 하는 일이 같아야 한다.

- 런이 어떻게 끝나든(`wait` 로 끝나도) 그 런에서 닫힌 줄은 버린다 — 다음 런의 같은 id `reply` 에 "in this run"
  이라고 말하지 않는다.
- 요청자가 죽었거나 사라졌으면 `reply` 는 "not delivered" 를 말하고 실패로 기록된다 — 에이전트가 갚든 main 이
  갚든 같은 문장. 빚은 닫힌다(기다리는 이가 없다). 그 id 에 다시 `reply` 해도 같은 문장이다 — "이미 회신했다" 가
  아니다(닫힌 사유 ``closed_by == "unsent"`` 로 가른다).
- `message` 는 보냈을 때만 성공으로 기록된다 — 핸들러가 ``(receipt, err)`` 를 돌려준다. 관찰 글은 그대로.
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
        f'not delivered — agent "{key}", which sent request [{rid}], was already gone. '
        f"[{rid}] is closed; there is nothing more to do for it."
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

    def test_replying_again_by_id_repeats_the_fact(self, tmp_path):
        """빚이 남아 있을 때(id 길) — 가지 못한 회신에 다시 `reply` 해도 "already replied" 가 아니다."""
        reg, a, b, door = _two_idle_agents(tmp_path)
        try:
            me, them = f"agent:{b}", f"agent:{a}"
            d = reg.note_owes(me, them, "please decide X")
            other = reg.note_owes(me, "main", "keep this one open")
            assert reg.kill(a) == ""
            assert wait_until(lambda: reg.get(a).state == "dead")
            port = reg.question_port(b)
            assert port.reply("the answer", id=d.id) == _gone(a, d.id)
            assert port.reply("the answer, again", id=d.id) == _gone(a, d.id)
            assert [x.id for x in reg.reply_debts(me)] == [other.id]
        finally:
            door.set()
            reg.shutdown_all()

    def test_replying_again_with_nothing_left_owed_repeats_the_fact(self, tmp_path):
        """빚이 하나도 안 남았을 때 — 이 런을 연 요청자(a)가 죽어 가지 못한 회신 뒤 다시 `reply`."""
        reg, a, b, door = _two_idle_agents(tmp_path)
        try:
            me = f"agent:{b}"
            _receipt, err = reg.message_handler(f"agent:{a}")(b, "please decide X")
            assert err == ""
            assert wait_until(
                lambda: reg.get(b).state == "busy"
            )  # b 의 런 — a 의 요청으로
            (d,) = reg.reply_debts(me)
            assert reg.kill(a) == ""
            assert wait_until(lambda: reg.get(a).state == "dead")
            port = reg.question_port(b)
            assert port.reply("the answer", id=d.id) == _gone(a, d.id)
            assert reg.reply_debts(me) == []
            assert port.reply("the answer, again", id=d.id) == _gone(a, d.id)
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


def _run_one_turn(tmp_path, ops: str, ports):
    """대본 한 턴(`ops`) 뒤 complete — 그 턴의 관찰 레코드들을 돌려준다."""
    from agent_cli.context.manager import ContextManager
    from agent_cli.providers.base import LLMResponse
    from agent_cli.providers.capabilities import ModelCapabilities
    from tests.binding_support import make_binding
    from tests.loopkit import run_loop

    script = [ops, json.dumps({"action": "complete", "result": "done"})]

    class _P:
        def call(self, messages, system, model, capabilities, **kw):
            return LLMResponse(content=script.pop(0))

    ctx = ContextManager(tmp_path / "loop", max_context_tokens=20_000)
    run_loop(
        query="go",
        binding=make_binding(
            provider=_P(), capabilities=ModelCapabilities(32768, 4096, False)
        ),
        ports=ports,
        ctx=ctx,
    )
    return [m for m in ctx.get_raw_messages() if m.get("tool")]


class TestMessageIsRecordedAsWhatHappened:
    def _ports(self, reg, b):
        from tests.loop_ports import make_ports

        return make_ports(
            owner=f"agent:{b}", message_handler=reg.message_handler(f"agent:{b}")
        )

    def test_handler_says_sent_or_not(self, tmp_path):
        reg, a, b, door = _two_idle_agents(tmp_path)
        try:
            send = reg.message_handler(f"agent:{b}")
            receipt, err = send("main", "hello", True)
            assert err == "" and receipt.startswith('delivered as request "r-')
            unknown = (
                'unknown agent "nobody" — use a key or name from `## Live Agents` '
                '(or "main")'
            )
            assert send("nobody", "hi", True) == ("", unknown)
            assert reg.kill(a) == ""
            assert wait_until(lambda: reg.get(a).state == "dead")
            receipt, err = send(a, "hi", True)
            assert receipt == "" and err.startswith(f'agent "{a}" is dead')
        finally:
            door.set()
            reg.shutdown_all()

    def test_a_failed_message_is_recorded_as_failed(self, tmp_path):
        reg, _a, b, door = _two_idle_agents(tmp_path)
        try:
            ops = json.dumps({"action": "message", "to": "nobody", "text": "hi"})
            (obs,) = _run_one_turn(tmp_path, ops, self._ports(reg, b))
            assert obs["tool"] == "message" and obs["success"] is False
            assert obs["content"] == (
                'Observation: [message → nobody] unknown agent "nobody" — use a key or '
                'name from `## Live Agents` (or "main")'
            )
        finally:
            door.set()
            reg.shutdown_all()

    def test_a_sent_message_is_recorded_as_sent(self, tmp_path):
        reg, a, b, door = _two_idle_agents(tmp_path)
        try:
            ops = json.dumps(
                {"action": "message", "to": a, "text": "fyi", "expects_reply": False}
            )
            (obs,) = _run_one_turn(tmp_path, ops, self._ports(reg, b))
            assert obs["success"] is True
            assert obs["content"].startswith(
                f'Observation: [message → {a}] delivered as notice "n-'
            )
        finally:
            door.set()
            reg.shutdown_all()

    def test_in_a_batch_only_the_failed_one_is_failed(self, tmp_path):
        reg, a, b, door = _two_idle_agents(tmp_path)
        try:
            ops = json.dumps(
                [
                    {
                        "action": "message",
                        "to": a,
                        "text": "fyi",
                        "expects_reply": False,
                    },
                    {"action": "message", "to": "nobody", "text": "hi"},
                ]
            )
            (obs,) = _run_one_turn(tmp_path, ops, self._ports(reg, b))
            assert obs["success"] is False
            assert "[1/2] message — OK" in obs["content"]
            assert "[2/2] message — FAILED" in obs["content"]
        finally:
            door.set()
            reg.shutdown_all()


_NOT_BLOCKED = (
    "\nYou are not blocked. Continue with whatever does not depend "
    "on the answer; it will arrive as a new message. If nothing else "
    "can proceed, `complete` — you will be resumed when it arrives."
)


class TestResidentAskIsRecordedAsWhatHappened:
    """상주 에이전트의 막지 않는 ``ask`` — 질문이 전부 올라갔을 때만 성공.

    빈 질문은 실제 루프로 닿는다: ``_extract_questions`` 는 ``""`` 만 거르고
    공백뿐인 ``"   "`` 은 통과시킨다 — 레지스트리가 strip 한 뒤 "empty question"
    으로 거부한다. 그래서 가짜 포트 없이 실제 ``question_port`` 로 몬다.
    관찰 글은 바뀌지 않는다 — 아래 기대 글은 고치기 전의 출력과 같다.
    """

    def _ports(self, reg, b):
        from tests.loop_ports import make_ports

        return make_ports(owner=f"agent:{b}", questions=reg.question_port(b))

    def _qid(self, reg, text):
        (q,) = [q for q in reg.open_human_questions() if q.text == text]
        return q.id

    def test_all_questions_sent_is_recorded_as_success(self, tmp_path):
        reg, _a, b, door = _two_idle_agents(tmp_path)
        try:
            ops = json.dumps({"action": "ask", "questions": ["first?", "second?"]})
            (obs,) = _run_one_turn(tmp_path, ops, self._ports(reg, b))
            assert obs["tool"] == "ask" and obs["success"] is True
            q1, q2 = self._qid(reg, "first?"), self._qid(reg, "second?")
            assert obs["content"] == (
                f'Observation: question "{q1}" sent — "first?"\n'
                f'question "{q2}" sent — "second?"' + _NOT_BLOCKED
            )
        finally:
            door.set()
            reg.shutdown_all()

    def test_one_refused_question_is_recorded_as_failed(self, tmp_path):
        reg, _a, b, door = _two_idle_agents(tmp_path)
        try:
            ops = json.dumps({"action": "ask", "questions": ["first?", "   "]})
            (obs,) = _run_one_turn(tmp_path, ops, self._ports(reg, b))
            assert obs["tool"] == "ask" and obs["success"] is False
            q1 = self._qid(reg, "first?")
            assert obs["content"] == (
                f'Observation: question "{q1}" sent — "first?"\n'
                "could not ask: empty question" + _NOT_BLOCKED
            )
        finally:
            door.set()
            reg.shutdown_all()

    def test_in_a_batch_the_refused_ask_is_failed(self, tmp_path):
        reg, _a, b, door = _two_idle_agents(tmp_path)
        try:
            ops = json.dumps(
                [
                    {"action": "ask", "questions": ["first?"]},
                    {"action": "ask", "questions": ["   "]},
                ]
            )
            (obs,) = _run_one_turn(tmp_path, ops, self._ports(reg, b))
            assert obs["success"] is False
            assert "[1/2] ask — OK" in obs["content"]
            assert "[2/2] ask — FAILED" in obs["content"]
        finally:
            door.set()
            reg.shutdown_all()
