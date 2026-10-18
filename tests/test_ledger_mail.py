"""장부 P2 (v10.55.0, docs/ledger/DESIGN.md) — main 앞 메일이 열린 항목이 된다.

규칙 하나: **따로 닫는 행동이 있는 빚이면 그 목록(Owed Replies), 없으면 Open
Requests.** 질문·회신 기대 메시지는 이미 빚이고 독촉은 새 항목이 아니다 — 나머지
일곱(회신·답·통지·모니터·작업·감사·사망)이 `[nK] (mail)` 로 올라 `complete` 의
`answers` 로 닫힌다. 깨우기 런은 시작 메시지 없이 열린다 — 그 런이 처리할 것은
장부가 말한다.
"""

from __future__ import annotations

import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_cli.context.manager import ContextManager
from agent_cli.loop import AgentLoop
from agent_cli.loop.core import mail_item
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.pump import RunRequest
from agent_cli.subagent.agents_live import AgentRegistry, MailWaker
from tests.loop_ports import make_ports

JOB = {
    "kind": "job",
    "success": True,
    "output": "🧵 job #1 finished rc=0 after 3m00s\nlog",
}


@pytest.fixture(autouse=True)
def _no_render(monkeypatch):
    import agent_cli.loop.dispatch as d

    monkeypatch.setattr(d, "render_step", lambda *a, **k: None)


def _loop(tmp_path, *, query="", items=(), mail=()):
    reg = AgentRegistry(tmp_path / "reg")
    for m in mail:
        reg._push_reply(dict(m))
    q = [dict(it) for it in items]
    ctx = ContextManager(Path(tmp_path) / "s", max_context_tokens=30_000)
    loop = AgentLoop(
        query=query,
        query_request_id="u-1" if query else "",
        query_author="dj" if query else None,
        provider=MagicMock(),
        capabilities=ModelCapabilities(
            context_window=32768, max_output_tokens=4096, supports_thinking=False
        ),
        model="m",
        ctx=ctx,
        ports=make_ports(
            agent_registry=reg,
            dequeue_user_message=lambda: q.pop(0) if q else None,
        ),
    )
    return loop, reg, ctx


def _complete(loop, **ai):
    turn = types.SimpleNamespace(
        thought="", record={"role": "assistant", "content": "raw"}
    )
    op = types.SimpleNamespace(action="complete", action_input=dict(ai))
    return loop._dispatch._op_complete("raw", turn, op, {})


class TestWhichMailBecomesAnItem:
    @pytest.mark.parametrize(
        "reply",
        [
            {"kind": "reply", "key": "agt-a"},
            {"kind": "answer", "key": "agt-a"},
            {"kind": "peer_message", "key": "agt-a", "notice": True},
            {"kind": "monitor"},
            {"kind": "job"},
            {"kind": "harness", "job": "auditor"},
            {"kind": "died", "key": "agt-a"},
        ],
    )
    def test_seven_kinds_are_items(self, reply):
        assert mail_item({**reply, "mail_id": 4}) == ("n-4", "mail")

    @pytest.mark.parametrize(
        "reply",
        [
            {"kind": "question", "key": "agt-a", "id": "q-1"},  # answer(id) 로 닫는 빚
            {"kind": "reminder"},  # 기존 빚을 다시 알릴 뿐
            {"kind": "peer_message", "key": "agt-a", "expects_reply": True},  # 회신 빚
        ],
    )
    def test_debts_and_reminders_stay_out(self, reply):
        assert mail_item({**reply, "mail_id": 4}) is None


class TestWakeRunHasNoStarter:
    def test_loop_query_is_empty_for_a_wake(self):
        import threading

        wake = RunRequest(
            text=MailWaker.WAKE_TEXT,
            author="🤝 agent",
            author_is_user=False,
            request_id="",
            stop_event=threading.Event(),
            wake=True,
        )
        assert wake.loop_query == ""
        user = RunRequest(
            text="hi",
            author="dj",
            author_is_user=True,
            request_id="3",
            stop_event=threading.Event(),
            wake=False,
        )
        assert user.loop_query == "hi"

    def test_first_input_is_the_mail_and_it_is_an_open_item(self, tmp_path):
        loop, _reg, ctx = _loop(tmp_path, mail=[JOB])
        loop._setup()
        assert [m for m in ctx.get_raw_messages() if m.get("role") == "user"] == []
        loop._deliver_agent_mail()
        users = [m for m in ctx.get_raw_messages() if m.get("role") == "user"]
        assert len(users) == 1 and users[0]["tool"] == "job"
        assert "New agent mail" not in str(ctx.get_raw_messages())
        (item,) = loop._state.ledger.requests
        assert item == {
            "id": "n-1",
            "author": "mail",
            "text": "🧵 job #1 finished rc=0 after 3m00s",
        }
        assert users[0]["request_id"] == "n-1"
        tail = loop._llm._build_session_state(10_000)
        assert '[n-1] (mail) "🧵 job #1 finished rc=0 after 3m00s"' in tail
        assert 'complete(result="...", answers=["n-1"])' in tail

    def test_nothing_delivered_means_no_llm_call(self, tmp_path):
        loop, _reg, _ctx = _loop(tmp_path)  # 메일 없음 — 다른 런이 이미 가져갔다
        loop._call_llm = MagicMock(side_effect=AssertionError("LLM called"))
        res = loop.run()
        assert res.success and res.output == ""


class TestClosingAMailItem:
    def test_answers_close_it(self, tmp_path):
        loop, _reg, _ctx = _loop(tmp_path, mail=[JOB])
        loop._setup()
        loop._deliver_agent_mail()
        res = _complete(loop, result="잡 통과", answers=["n-1"])
        assert res.success and res.output == "잡 통과"
        assert loop._state.ledger.requests == []

    def test_omitting_it_is_nagged_within_the_budget_then_reported(self, tmp_path):
        """v10.56.0 (P3): 생략은 결과를 배달한 뒤 예산 안에서 되돌린다 — 예산이
        끝나면 미신고 통지로 닫는다."""
        from agent_cli.loop.ledger import NAG_BUDGET

        loop, _reg, _ctx = _loop(tmp_path, mail=[JOB])
        loop._setup()
        loop._deliver_agent_mail()
        for _ in range(NAG_BUDGET):
            assert not hasattr(_complete(loop, result="끝"), "output")  # 되돌림
        res = _complete(loop, result="끝")
        assert res.output.startswith("끝\n\n📋 `complete` came without `answers`")
        assert '[n-1] (mail) "🧵 job #1 finished' in res.output

    def test_nothing_to_do_is_closed_by_listing_it(self, tmp_path):
        loop, _reg, _ctx = _loop(
            tmp_path,
            mail=[
                {
                    "kind": "harness",
                    "job": "auditor",
                    "output": "AUDIT run seq 1..4: pass",
                }
            ],
        )
        loop._setup()
        loop._deliver_agent_mail()
        res = _complete(loop, result="할 일 없음 — 감사 통과", answers=["n-1"])
        assert res.output == "할 일 없음 — 감사 통과"


class TestBusyRunMixesWithUserRequests:
    def test_mail_joins_the_users_open_requests(self, tmp_path):
        loop, reg, _ctx = _loop(tmp_path, query="README 정리해 줘")
        loop._setup()
        reg._push_reply(dict(JOB))  # 런 도중 도착
        loop._deliver_agent_mail()
        assert loop._state.ledger.request_ids() == ["u-1", "n-1"]
        tail = loop._llm._build_session_state(10_000)
        assert "## Open Items" in tail and 'answers=["u-1", "n-1"]' in tail
        assert '[u-1] (dj) "README 정리해 줘"' in tail and "[n-1] (mail)" in tail
        res = _complete(loop, result="둘 다", answers=["u-1", "n-1"])
        assert res.output == "둘 다"


class TestOpenMailIsPinned:
    def test_record_is_pinned_while_open_and_released_when_closed(self, tmp_path):
        loop, _reg, ctx = _loop(tmp_path, mail=[JOB])
        loop._setup()
        loop._deliver_agent_mail()
        idx = next(i for i, m in enumerate(ctx._cache) if m.get("request_id") == "n-1")
        assert idx in ctx._pinned_indices()
        _complete(loop, result="ok", answers=["n-1"])
        assert idx not in ctx._pinned_indices()


class TestOpenDebtsArePinnedToo:
    """Owed Replies 의 원문도 열려 있는 동안 핀 (P2) — 종전엔 사용자 요청만."""

    def _main_loop(self, tmp_path, mail):
        from agent_cli.runtime import _main_questions

        reg = AgentRegistry(tmp_path / "reg")
        for m in mail:
            reg._push_reply(dict(m))
        ctx = ContextManager(Path(tmp_path) / "s", max_context_tokens=30_000)
        loop = AgentLoop(
            query="",
            provider=MagicMock(),
            capabilities=ModelCapabilities(
                context_window=32768, max_output_tokens=4096, supports_thinking=False
            ),
            model="m",
            ctx=ctx,
            ports=make_ports(agent_registry=reg, questions=_main_questions(reg)),
        )
        loop._setup()
        loop._deliver_agent_mail()
        return loop, reg, ctx

    def test_a_message_that_expects_a_reply(self, tmp_path):
        loop, _reg, ctx = self._main_loop(
            tmp_path,
            [
                {
                    "kind": "peer_message",
                    "key": "agt-a",
                    "expects_reply": True,
                    "output": "검토해 줘",
                }
            ],
        )
        assert loop._state.ledger.requests == []  # 빚이지 열린 요청이 아니다
        rec = next(m for m in ctx._cache if m.get("tool") == "agent")
        debt_id = rec["request_id"]
        assert debt_id in loop._state.ledger.pin_ids()
        idx = ctx._cache.index(rec)
        assert idx in ctx._pinned_indices()
