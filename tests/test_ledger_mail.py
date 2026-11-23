"""장부 P2 (v10.55.0, docs/ledger/DESIGN.md) — main 앞 메일이 열린 항목이 된다.

규칙 하나: **따로 닫는 행동이 있는 빚이면 그 목록(Owed Replies), 없으면 Open
Requests, 이미 열린 것의 재알림이면 장부 밖.** 어느 갈래인지는 만드는 쪽이 말한다
(v10.78.1, §14): 회신 기대는 `expects_reply`, 장부 밖은 `ledger: "none"`. 나머지
(회신·답·통지·모니터·작업·감사·사망)가 `[nK] (mail)` 로 올라 `complete` 의
`answers` 로 닫힌다. 깨우기 런은 시작 메시지 없이 열린다 — 그 런이 처리할 것은
장부가 말한다.
"""

from __future__ import annotations

import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_cli.context.manager import ContextManager
from agent_cli.loop.core import mail_item
from agent_cli.main_input import intake_item
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.runtime import MainLoop
from agent_cli.subagent.agents_live import AgentRegistry
from tests.loop_ports import make_ports, queue_inbox
from tests.loopkit import make_loop
from tests.mail_support import to_main

JOB = {
    "kind": "job",
    "success": True,
    "output": "🧵 job #1 finished rc=0 after 3m00s\nlog",
}


@pytest.fixture(autouse=True)
def _no_render(monkeypatch):
    import agent_cli.loop.dispatch as d

    monkeypatch.setattr(d, "render_step", lambda *a, **k: None)


def _loop(tmp_path, *, query="", items=(), mail=(), opening=None):
    reg = AgentRegistry(tmp_path / "reg")
    for m in mail:
        to_main(reg, dict(m))
    q = [dict(it) for it in items]
    ctx = ContextManager(Path(tmp_path) / "s", max_context_tokens=30_000)
    loop = make_loop(
        query=query,
        query_request_id="u-1" if query else "",
        query_author="dj" if query else None,
        opening=opening,
        provider=MagicMock(),
        capabilities=ModelCapabilities(
            context_window=32768, max_output_tokens=4096, supports_thinking=False
        ),
        model="m",
        ctx=ctx,
        ports=make_ports(
            agent_registry=reg,
            inbox=queue_inbox(lambda: q.pop(0) if q else None),
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
            {"kind": "reply", "key": "agt-a"},  # 요청 id 가 없는 옛 모양
            {"kind": "answer", "key": "agt-a"},
            {"kind": "monitor"},
            {"kind": "job"},
            {"kind": "harness", "job": "auditor"},
            {"kind": "died", "key": "agt-a"},
            {"kind": "checkin"},
        ],
    )
    def test_other_kinds_are_items(self, reply):
        assert mail_item({**reply, "item_id": "n-4"}) == ("n-4", "mail")

    @pytest.mark.parametrize(
        "reply",
        [
            {"kind": "peer_message", "key": "agt-a", "expects_reply": True},  # 회신 빚
            {"kind": "monitor", "ledger": "none"},  # 만드는 쪽이 장부 밖이라 했다
            {"kind": "reply", "key": "agt-a", "ledger": "none"},
            # v10.141.0 (사용자 결정): 회신·통지는 읽으면 끝 — 항목이 아니다
            {"kind": "reply", "key": "agt-a", "re": "r-2"},  # 런 끝 폴백 요약
            {"kind": "peer_message", "key": "agt-a", "re": "r-2"},  # 명시 `reply`
            {"kind": "peer_message", "key": "agt-a", "notice": True},  # 통지
        ],
    )
    def test_debts_replies_notices_and_declared_out_of_ledger_mail_stay_out(
        self, reply
    ):
        assert mail_item({**reply, "item_id": "n-4"}) is None

    @pytest.mark.parametrize("kind", ["question", "reminder"])
    def test_the_kind_name_alone_no_longer_decides(self, kind):
        """v10.78.1: 장부 밖은 ``ledger: "none"`` 로 선언한다 — 종전의 kind 문자열
        목록(``question``·``reminder``)은 만드는 곳이 없는 죽은 분기였다."""
        assert mail_item({"kind": kind, "item_id": "n-4"}) == ("n-4", "mail")

    def test_ledger_item_is_the_default_and_may_be_explicit(self):
        assert mail_item({"kind": "job", "item_id": "n-4"}) == ("n-4", "mail")
        assert mail_item({"kind": "job", "ledger": "item", "item_id": "n-4"}) == (
            "n-4",
            "mail",
        )


class TestTheMailThatOpensARun:
    def test_a_mail_run_has_no_starter_message_and_carries_its_mail(self):
        import threading

        opened = {"mail": dict(JOB)}
        wake = MainLoop.start(opened, threading.Event())
        assert wake.query == "" and wake.author_is_user is False
        assert wake.opening == opened
        user = MainLoop.start(
            {"message": {"id": "u-3", "text": "hi", "nickname": "dj"}},
            threading.Event(),
        )
        assert (user.query, user.author, user.request_id) == ("hi", "dj", "u-3")
        assert user.opening is None

    def test_first_input_is_the_mail_and_it_is_an_open_item(self, tmp_path):
        """런을 연 메일은 시작(`_setup`)에서 들어간다 — 첫 턴 경계를 기다리지 않는다. 열리자마자 멈춰도 기록에 남는다."""
        reg = AgentRegistry(tmp_path / "opener")
        to_main(reg, dict(JOB))
        opened = intake_item(reg._post.next_run("main", timeout=0))
        loop, _reg, ctx = _loop(tmp_path, opening=opened)
        loop._setup()
        users = [m for m in ctx.get_raw_messages() if m.get("role") == "user"]
        assert len(users) == 1 and users[0]["tool"] == "job"
        assert "New agent mail" not in str(ctx.get_raw_messages())
        (item,) = loop._state.ledger.open_requests()
        assert item == {
            "id": "n-1",
            "author": "mail",
            "text": "🧵 job #1 finished rc=0 after 3m00s",
        }
        assert users[0]["request_id"] == "n-1"
        tail = loop._llm._build_session_state(10_000)
        assert '[n-1] (mail) "🧵 job #1 finished rc=0 after 3m00s"' in tail
        assert 'then `complete`, listing in `answers` the ids it covers ("n-1")' in tail

    def test_mail_that_arrives_later_is_read_at_the_turn_boundary(self, tmp_path):
        loop, _reg, ctx = _loop(tmp_path, mail=[JOB])
        loop._setup()
        assert [m for m in ctx.get_raw_messages() if m.get("role") == "user"] == []
        loop._intake()
        users = [m for m in ctx.get_raw_messages() if m.get("role") == "user"]
        assert len(users) == 1 and users[0]["tool"] == "job"


class TestClosingAMailItem:
    def test_answers_close_it(self, tmp_path):
        loop, _reg, _ctx = _loop(tmp_path, mail=[JOB])
        loop._setup()
        loop._intake()
        res = _complete(loop, result="잡 통과", answers=["n-1"])
        assert res.success and res.output == "잡 통과"
        assert loop._state.ledger.open_requests() == ()

    def test_omitting_it_is_nagged_within_the_budget_then_reported(self, tmp_path):
        """v10.56.0 (P3): 생략은 결과를 배달한 뒤 예산 안에서 되돌린다 — 예산이
        끝나면 미신고 통지로 닫는다."""
        from agent_cli.loop.ledger import NAG_BUDGET

        loop, _reg, _ctx = _loop(tmp_path, mail=[JOB])
        loop._setup()
        loop._intake()
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
        loop._intake()
        res = _complete(loop, result="할 일 없음 — 감사 통과", answers=["n-1"])
        assert res.output == "할 일 없음 — 감사 통과"


class TestBusyRunMixesWithUserRequests:
    def test_mail_joins_the_users_open_requests(self, tmp_path):
        loop, reg, _ctx = _loop(tmp_path, query="README 정리해 줘")
        loop._setup()
        to_main(reg, dict(JOB))  # 런 도중 도착
        loop._intake()
        assert loop._state.ledger.request_ids() == ["u-1", "n-1"]
        tail = loop._llm._build_session_state(10_000)
        assert (
            "## Open Items" in tail
            and 'listing in `answers` the ids it covers ("u-1", "n-1")' in tail
        )
        assert '[u-1] (dj) "README 정리해 줘"' in tail and "[n-1] (mail)" in tail
        res = _complete(loop, result="둘 다", answers=["u-1", "n-1"])
        assert res.output == "둘 다"


class TestOpenMailIsPinned:
    def test_record_is_pinned_while_open_and_released_when_closed(self, tmp_path):
        loop, _reg, ctx = _loop(tmp_path, mail=[JOB])
        loop._setup()
        loop._intake()
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
            to_main(reg, dict(m))
        ctx = ContextManager(Path(tmp_path) / "s", max_context_tokens=30_000)
        loop = make_loop(
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
        loop._intake()
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
        assert loop._state.ledger.open_requests() == ()  # 빚이지 열린 요청이 아니다
        rec = next(m for m in ctx._cache if m.get("tool") == "agent")
        debt_id = rec["request_id"]
        assert debt_id in loop._state.ledger.pin_ids()
        idx = ctx._cache.index(rec)
        assert idx in ctx._pinned_indices()
