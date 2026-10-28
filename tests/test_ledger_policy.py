"""장부 P3 (v10.56.0, docs/ledger/DESIGN.md §11) — 정책 하나 · 순서 하나 · id 하나.

- **계속형 붙잡기**: `complete` 결과는 언제나 배달된다. 열린 항목이 남으면 런은
  끝나지 않고 되돌린다 — 되묻기·독촉이 전부 런 공유 예산(`NAG_BUDGET` = 8)을 쓴다.
  예산이 끝나면 하니스가 닫는다(요청은 최종 답에 표시, 빚은 런 끝 폴백).
- **순서**: 행동으로 닫는 빚(`reply`·`answer`) 먼저, `complete` 의 `answers` 로 닫는
  요청·메일 마지막 — 꼬리와 되돌림 관찰이 같은 순서로 보여 준다.
- **`reply(id)` 하나**: main 도 회신 빚을 id 로 갚는다. `agent request` 는 빚을 갚지 않는다.
- **id 형식** `<접두>-<토큰>`: 접두가 닫는 법을 말한다(`u-`·`n-`·`r-`·`q-`).
"""

from __future__ import annotations

import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_cli import item_ids
from agent_cli.context.manager import ContextManager
from agent_cli.input_queue import InputQueue
from agent_cli.loop import AgentLoop
from agent_cli.loop.ledger import (
    NAG_BUDGET,
    RESULT_SHAPE_LINE,
    Ledger,
    wrong_closer_hint,
)
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.subagent.agents_live import AgentRegistry
from tests.loop_ports import make_ports

_REPLY = {"kind": "reply", "to": "agent:agt-a", "id": "r-4", "text": "검토해 줘"}
_QUESTION = {"kind": "answer", "to": "agt-b", "id": "q-7a3f", "text": "할인 상한은?"}


class _Port:
    def __init__(self, debts=()):
        self.list = list(debts)
        self.nonblocking = True

    def debts(self):
        return list(self.list)


@pytest.fixture
def finals(monkeypatch):
    seen: list[str] = []
    import agent_cli.loop.dispatch as d

    def _render(kind, content, *a, **k):
        if kind == "final":
            seen.append(content)

    monkeypatch.setattr(d, "render_step", _render)
    return seen


def _loop(tmp_path, *, requests=(), debts=(), ctx=False):
    q = [dict(it) for it in requests]
    loop = AgentLoop(
        query="",
        provider=MagicMock(),
        capabilities=ModelCapabilities(
            context_window=32768, max_output_tokens=4096, supports_thinking=False
        ),
        model="m",
        ctx=ContextManager(Path(tmp_path) / "s", max_context_tokens=30_000)
        if ctx
        else None,
        ports=make_ports(
            dequeue_user_message=lambda: q.pop(0) if q else None,
            questions=_Port(debts) if debts else None,
        ),
    )
    loop._inject_queued_messages()
    return loop


def _req(i, text, nick="dj"):
    return {"id": f"u-{i}", "nickname": nick, "text": text}


def _complete(loop, **ai):
    turn = types.SimpleNamespace(
        thought="", record={"role": "assistant", "content": "raw"}
    )
    op = types.SimpleNamespace(action="complete", action_input=dict(ai))
    res = loop._dispatch._op_complete("raw", turn, op, {})
    return getattr(res, "output", None), loop.messages[-1][
        "content"
    ] if loop.messages else ""


class TestResultIsAlwaysDelivered:
    def test_omitting_answers_delivers_then_asks_which_items_it_closes(
        self, tmp_path, finals
    ):
        lp = _loop(tmp_path, requests=[_req(1, "A 해줘")])
        out, obs = _complete(lp, result="A 했다")
        assert finals == ["A 했다"]  # 결과는 나갔다
        assert out is None  # 런은 계속
        assert "`answers` is missing" in obs and '[u-1] (dj) "A 해줘"' in obs
        assert lp._state.ledger.request_ids() == ["u-1"]  # 모름 = 아무것도 안 닫음

    def test_empty_answers_is_a_statement_not_an_omission(self, tmp_path, finals):
        lp = _loop(tmp_path, requests=[_req(1, "A 해줘")])
        out, obs = _complete(lp, result="못 했다", answers=[])
        assert finals == ["못 했다"] and out is None
        assert "1 item is still open" in obs and "missing" not in obs

    def test_a_run_with_only_debts_is_not_refused(self, tmp_path, finals):
        """종전엔 사용자 요청이 없는 런(깨우기·동료 항목)에서 빚이 남은 complete 를
        결과 미배달로 거부했다. 이제 결과는 배달되고 빚을 가리켜 이어 간다."""
        lp = _loop(tmp_path, debts=[_REPLY])
        out, obs = _complete(lp, result="끝")
        assert finals == ["끝"] and out is None
        assert '`reply` with id "r-4"' in obs and "refused" not in obs


class TestOneOrderEverywhere:
    def test_nag_lists_actions_first_and_complete_last(self, tmp_path, finals):
        lp = _loop(
            tmp_path,
            requests=[_req(1, "A"), _req(2, "B", "ann")],
            debts=[_REPLY, _QUESTION],
        )
        _out, obs = _complete(lp, result="A", answers=["u-1"])
        lines = obs.splitlines()
        assert lines.index("  1. by action") < lines.index("  2. by `complete` (last)")
        assert '[u-2] (ann) "B"' in obs and "[u-1]" not in obs  # 닫힌 건 빠진다
        assert 'then `complete`, listing in `answers` the ids it covers ("u-2")' in obs

    def test_tail_and_nag_use_the_same_lines(self, tmp_path, finals):
        lp = _loop(tmp_path, requests=[_req(1, "A")], debts=[_REPLY])
        block = lp._state.ledger.block()
        _out, obs = _complete(lp, result="x")
        for line in block.splitlines()[1:-1]:  # 머리·꼬리 줄 사이 = 항목 줄
            assert line in obs


class TestOneBudget:
    def test_requests_and_debts_share_the_budget(self, tmp_path, finals):
        lp = _loop(tmp_path, requests=[_req(1, "A")], debts=[_REPLY])
        for i in range(NAG_BUDGET):
            out, obs = _complete(lp, result=f"r{i}")
            assert out is None
            left = NAG_BUDGET - i - 1
            assert (
                (f"({left} more reminder" in obs) if left else ("Last reminder" in obs)
            )
        out, _obs = _complete(lp, result="final")
        assert out.startswith("final\n\n📋 `complete` came without `answers`")
        assert len(finals) == NAG_BUDGET + 1  # 매번 배달됐다

    def test_declared_miss_after_the_budget_says_not_closed(self, tmp_path, finals):
        lp = _loop(tmp_path, requests=[_req(1, "A")])
        lp._state.ledger.budget = 0
        out, _ = _complete(lp, result="x", answers=[])
        assert (
            out
            == 'x\n\n⏳ 1 open item(s) in this run were not closed:\n   [u-1] (dj) "A"'
        )

    def test_closing_everything_ends_cleanly(self, tmp_path, finals):
        lp = _loop(tmp_path, requests=[_req(1, "A"), _req(2, "B")])
        out, _ = _complete(lp, result="둘 다", answers=["u-1", "u-2"])
        assert out == "둘 다" and lp._state.ledger.budget == NAG_BUDGET


class TestIdsAreOneFormat:
    def test_sources_make_the_final_form(self, tmp_path):
        assert InputQueue().enqueue("c", "hi")["id"] == "u-1"
        reg = AgentRegistry(tmp_path)
        # 번호 공간 하나를 모든 접두가 함께 쓴다 (v10.77.0)
        assert reg.note_owes("main", "agent:agt-a", "t").id == "r-2"
        reg._push_reply({"kind": "job", "output": "🧵 job #1 finished"})
        assert reg.pending_mail()[0]["id"] == "n-3"

    @pytest.mark.parametrize(
        "item,closer",
        [
            ("u-1", "complete"),
            ("n-3", "complete"),
            ("r-12", "reply"),
            ("q-7a3f", "answer"),
            ("7", ""),
        ],
    )
    def test_the_prefix_names_the_closer(self, item, closer):
        assert item_ids.closer_of(item) == closer

    def test_wrong_tool_is_pointed_to_the_right_one(self):
        assert 'list "u-1" in `answers`' in wrong_closer_hint("u-1", "answer")
        assert '`reply` with id "r-2"' in wrong_closer_hint("r-2", "answer")
        assert '`answer` with id "q-1a"' in wrong_closer_hint("q-1a", "reply")
        assert wrong_closer_hint("r-2", "reply") == ""


class TestMainRepliesById:
    def test_reply_settles_that_debt_and_agent_request_settles_nothing(
        self, tmp_path, monkeypatch
    ):
        from tests.test_agents_live import make_registry

        reg = make_registry(tmp_path)
        key, _ = reg.spawn()
        sent = []
        monkeypatch.setattr(
            reg, "request", lambda k, text, **kw: sent.append((k, text, kw)) or ""
        )
        d1 = reg.note_owes("main", f"agent:{key}", "첫 질문")
        d2 = reg.note_owes("main", f"agent:{key}", "둘째 질문")
        port = reg.question_port(None)
        assert port.reply("둘째 답", id=d2.id) == ""
        assert [d.id for d in reg.reply_debts("main")] == [d1.id]  # 그 빚만
        assert sent == [(key, "둘째 답", {"author": "main", "expects_reply": False})]

    def test_reply_needs_an_id(self, tmp_path):
        from tests.test_agents_live import make_registry

        reg = make_registry(tmp_path)
        reg.note_owes("main", "agent:agt-a", "질문")
        err = reg.question_port(None).reply("답")
        assert err.startswith("reply needs `id`") and "[r-1]" in err

    def test_nothing_owed_points_to_agent_request(self, tmp_path):
        from tests.test_agents_live import make_registry

        reg = make_registry(tmp_path)
        err = reg.question_port(None).reply("답", id="r-1")
        assert (
            err.startswith("nothing to reply to")
            and '`agent` with mode "request"' in err
        )


class TestLedgerUnit:
    def test_settle_never_closes_on_unknown(self):
        led = Ledger()
        led.add_request("u-1", "", "a")
        assert led.settle(None) == [{"id": "u-1", "author": "", "text": "a"}]
        assert led.settle(set()) == [{"id": "u-1", "author": "", "text": "a"}]
        assert led.settle({"u-1"}) == []

    def test_spend_stops_at_zero(self):
        led = Ledger()
        assert [led.spend() for _ in range(NAG_BUDGET + 1)] == [True] * NAG_BUDGET + [
            False
        ]


def test_multi_line_bodies_are_folded_to_one_item_line():
    """여러 줄 본문도 항목은 한 줄 — 닫는 도구가 그 줄 끝에 붙는다."""
    led = Ledger()
    led.bind_questions(
        _Port([{**_REPLY, "text": "AUDIT findings\n[1] unmet\n  — 다시"}])
    )
    led.add_request("u-1", "dj", "첫 줄\n둘째 줄")
    block = led.block()
    assert (
        '[r-4] (agt-a) reply owed: "AUDIT findings [1] unmet — 다시"  → `reply` with id "r-4"'
        in block
    )
    assert '[u-1] (dj) "첫 줄 둘째 줄"' in block


def test_result_shape_line_sits_under_the_complete_clause_only():
    """v10.75.0: result 의 모양(답 먼저 + Checked/Not checked)은 `complete` 절의
    닫는 줄 바로 아래 — 도구 설명의 같은 틀을 35B 가 0/8 로 안 따랐다."""
    led = Ledger()
    led.add_request("u-1", "dj", "테스트 통과해?")
    lines = led.block().splitlines()
    i = next(n for n, l in enumerate(lines) if "→ then `complete`" in l)
    assert lines[i + 1] == RESULT_SHAPE_LINE
    assert "`Checked:`" in RESULT_SHAPE_LINE and "`Not checked:`" in RESULT_SHAPE_LINE
    led2 = Ledger()
    led2.bind_questions(_Port([_REPLY]))
    assert RESULT_SHAPE_LINE not in led2.block()  # 행동으로 닫는 빚뿐이면 없음
