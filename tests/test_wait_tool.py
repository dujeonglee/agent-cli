"""`wait` 도구 (v10.65.0, docs/wait/DESIGN.md) — 닫지 않고 끝낸다, 깨울 것이 있을 때만.

고정하는 계약: 자리(complete 바로 앞)·터미널(문법도) · 깨울 것이 있으면 런이
``waiting`` 결과로 끝나고 열린 요청이 다음 런의 장부로 넘어간다 · 없으면 거부
관찰 + 런 계속 · 기다리려고 쓴 complete 되돌림에 `wait` 안내 · 안내 문구들이
전부 `wait` 를 가리킨다(`complete` 로 기다리라는 문장 0).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from agent_cli.context.manager import _OBS_COMPLETE_NUDGE, ContextManager
from agent_cli.dialects import get as get_dialect
from agent_cli.dialects.grammar import terminal_tools
from agent_cli.jobs import JobRegistry, set_job_registry
from agent_cli.loop import AgentLoop
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.tools.registry import _BUILTIN_ORDER, TOOLS
from agent_cli.tools.virtual import WaitTool
from tests.loop_ports import TEST_PORTS, make_ports

#: main 모양의 포트 — 입력 큐가 있어 깨울 수 있다(빈 큐).
WAKEABLE = make_ports(dequeue_user_message=lambda: None)


@pytest.fixture
def jobs(tmp_path):
    r = JobRegistry(tmp_path)
    r.deliver = lambda *a, **k: None
    set_job_registry(r)
    yield r
    r.stop_all()
    set_job_registry(None)


def _loop(provider, *, ctx=None, query="Q", rid="u-1"):
    return AgentLoop(
        ports=WAKEABLE,
        query=query,
        query_request_id=rid,
        provider=provider,
        capabilities=ModelCapabilities(
            context_window=32768, max_output_tokens=4096, supports_thinking=False
        ),
        model="m",
        dialect=get_dialect("json_fc"),
        ctx=ctx,
    )


def _provider(*replies: dict):
    p = MagicMock()
    p.call.side_effect = [LLMResponse(content=json.dumps(r)) for r in replies]
    return p


class TestRegistration:
    def test_right_before_complete_and_terminal(self):
        order = list(_BUILTIN_ORDER)
        assert order[-2:] == ["wait", "complete"]
        assert isinstance(TOOLS["wait"], WaitTool) and TOOLS["wait"].terminal
        assert "wait" in terminal_tools(["wait", "complete", "shell"])

    def test_complete_points_at_wait(self):
        assert "call `wait`" in TOOLS["complete"].description


class TestAdvertisedOnlyWhereAWakeExists:
    """스킬·일회성 루프엔 깨울 길이 없다 — 거기서 wait 로 끝낸 결과는 호출자에게
    "기다리는 중" 으로 돌아가 버린다."""

    def _tools(self, ports):
        loop = _loop(MagicMock())
        loop = AgentLoop(
            ports=ports,
            query="Q",
            provider=MagicMock(),
            capabilities=loop.capabilities,
            model="m",
            dialect=get_dialect("json_fc"),
        )
        return list(loop.tools_list)

    def test_unwired_ports_hide_wait(self):
        assert "wait" not in self._tools(TEST_PORTS)
        assert not TEST_PORTS.wakeable()

    def test_main_queue_and_resident_inbox_show_wait(self):
        assert "wait" in self._tools(WAKEABLE)
        assert "wait" in self._tools(make_ports(absorb_inbox=list))


class TestWaitEndsTheRun:
    def test_waiting_on_a_job_ends_with_items_parked(self, jobs, tmp_path):
        jobs.start("sleep 30", owner="main")
        ctx = ContextManager(tmp_path / "s", max_context_tokens=10000)
        loop = _loop(
            _provider({"action": "wait", "reason": "install running"}), ctx=ctx
        )
        res = loop.run()
        assert res.success and res.waiting
        assert "⏸ waiting on job #1" in res.output and "install running" in res.output
        assert loop.turn == 1
        # 열린 요청은 닫히지 않고 다음 런으로
        assert [r["id"] for r in ctx.take_parked_requests()] == ["u-1"]
        # 기록: 보통의 op + 관찰 (새 카드 종류 없음)
        recs = [
            json.loads(line)
            for line in (tmp_path / "s" / "history.jsonl").read_text().splitlines()
        ]
        obs = [r for r in recs if r.get("tool") == "wait"]
        assert obs and obs[-1]["success"] is True
        assert "u-1" in obs[-1]["content"] and "job #1" in obs[-1]["content"]

    def test_next_run_on_the_same_ctx_restores_the_items(self, jobs, tmp_path):
        jobs.start("sleep 30", owner="main")
        ctx = ContextManager(tmp_path / "s", max_context_tokens=10000)
        _loop(_provider({"action": "wait"}), ctx=ctx).run()
        seen = {}

        def _answer(*_a, **_k):
            seen["ids"] = list(loop2._state.ledger.request_ids())
            return LLMResponse(
                content=json.dumps(
                    {"action": "complete", "result": "done", "answers": seen["ids"]}
                )
            )

        p = MagicMock()
        p.call.side_effect = _answer
        # 깨우기 런: 통지가 시작 메시지, 요청 id 는 없다 — 장부는 넘겨받은 것만
        loop2 = _loop(p, ctx=ctx, query="job #1 finished", rid="")
        res = loop2.run()
        assert res.success and seen["ids"] == ["u-1"]
        assert ctx.take_parked_requests() == []

    def test_nothing_pending_is_refused_and_the_run_goes_on(self, tmp_path):
        set_job_registry(None)
        loop = _loop(
            _provider(
                {"action": "wait"},
                {"action": "complete", "result": "ok", "answers": ["u-1"]},
            )
        )
        res = loop.run()
        assert res.success and not res.waiting and res.output == "ok"
        obs = [
            m["content"]
            for m in loop.messages
            if m["role"] == "user" and "wait refused" in str(m.get("content"))
        ]
        assert obs and "nothing would wake you" in obs[0]

    def test_agent_activity_counts_only_for_its_requester(self, tmp_path):
        """세션 전체 `has_active_work` 로 보면 상주 에이전트 자신이 busy 라 늘 참이다
        — 요청자별로 센다: 그 주소에게 빚진 회신, 그 주소의 항목."""
        from tests.test_agents_live import make_registry

        reg = make_registry(tmp_path)
        reg.begin_run("agent:a")
        reg.note_owes("agent:a", "main", "do X")
        assert reg.pending_for("main") == ["agent:a owes you a reply"]
        assert reg.pending_for("agent:a") == []  # 자기 빚은 자기를 깨우지 않는다
        assert reg.pending_for("agent:b") == []

    def test_agent_scoped_jobs_do_not_wake_main(self, jobs):
        jobs.start("sleep 30", owner="agent:x")
        from agent_cli.runtime import wake_sources

        assert wake_sources("main") == []
        assert wake_sources("agent:x") == ["job #1 (`sleep 30`)"]


class TestAlreadyArrived:
    """기다리던 것이 wait 보다 먼저 끝난 경우 (v10.66.2) — 통지가 메일함에 와 있다.
    벤치 실측: once 모니터가 즉시 발화·은퇴한 뒤의 wait 가 "nothing pending" 으로
    거부됐다. 거부는 맞지만(런을 끝내면 바로 다시 깨울 뿐) 문구가 사실과 달랐다."""

    def _registry(self, tmp_path):
        from agent_cli.subagent.agents_live import set_main_registry
        from tests.test_agents_live import make_registry

        reg = make_registry(tmp_path)
        set_main_registry(reg)
        return reg

    def _monitor_mail(self, reg):
        assert (
            reg.deliver(
                "main",
                mail={"kind": "monitor", "success": True, "output": "🔔 hit"},
                text="🔔 hit",
                author="main",
                expects_reply=False,
            )
            == ""
        )

    def _mid_turn(self, reg, n, first):
        """모델 호출 **도중** 에 통지 n 건이 도착한다 — 런 시작·턴 경계에 와 있던
        메일은 모델이 보기 전에 배달되므로(`_deliver_agent_mail`), 틈은 호출
        중에만 생긴다(벤치: 모니터가 그 사이에 발화). 둘째 호출은 장부의 열린
        항목(요청 `u-1` + 배달된 통지 `n-…`)을 전부 닫는 complete."""
        p = MagicMock()
        holder: dict = {}

        def _call(*_a, **_k):
            if p.call.call_count == 1:
                for _ in range(n):
                    self._monitor_mail(reg)
                return LLMResponse(content=json.dumps(first))
            ids = list(holder["loop"]._state.ledger.request_ids())
            return LLMResponse(
                content=json.dumps(
                    {"action": "complete", "result": "ok", "answers": ids}
                )
            )

        p.call.side_effect = _call
        p.holder = holder
        return p

    def _loop(self, reg, provider):
        loop = provider.holder["loop"] = AgentLoop(
            ports=make_ports(dequeue_user_message=lambda: None, agent_registry=reg),
            query="Q",
            query_request_id="u-1",
            provider=provider,
            capabilities=ModelCapabilities(
                context_window=32768, max_output_tokens=4096, supports_thinking=False
            ),
            model="m",
            dialect=get_dialect("json_fc"),
        )
        return loop

    def test_job_and_monitor_mail_count_as_arrived_not_as_wake_sources(self, tmp_path):
        from agent_cli.runtime import arrived_mail, wake_sources

        reg = self._registry(tmp_path)
        assert arrived_mail("main") == 0
        self._monitor_mail(reg)
        assert reg.arrived_for("main") == 1 and arrived_mail("main") == 1
        # 종전의 "a reply to you not yet delivered" 는 없다 — 도착분은 깨울 것이 아니다
        assert wake_sources("main") == []
        assert reg.arrived_for("agent:nobody") == 0

    def test_resident_inbox_items_count_for_that_agent(self, tmp_path):
        """받은편지함 미러 `inbox_items` 로 센다 — `SimpleQueue` 엔 `queue` 속성이
        없어 종전 `pending_for` 의 "has your request queued" 가지는 늘 비었다."""
        from types import SimpleNamespace

        reg = self._registry(tmp_path)
        reg._agents["k"] = SimpleNamespace(
            key="k", state="idle", inbox_items=[{"author": "main", "text": "x"}]
        )
        assert reg.arrived_for("agent:k") == 1
        assert reg.arrived_for("main") == 0
        assert reg.pending_for("main") == ["agent:k has your request queued"]

    def test_wait_is_refused_and_the_mail_lands_in_the_next_turn(self, jobs, tmp_path):
        reg = self._registry(tmp_path)
        # 돌고 있는 작업이 **있어도** 도착분이 먼저다 — 읽고 나서 다시 wait
        jobs.start("sleep 30", owner="main")
        loop = self._loop(
            reg,
            self._mid_turn(reg, 1, {"action": "wait", "reason": "monitor"}),
        )
        res = loop.run()
        assert res.success and not res.waiting and res.output == "ok"
        assert loop.turn == 2  # 거부 턴 + 닫는 턴 — 런 경계 없이 이어졌다
        users = [str(m.get("content")) for m in loop.messages if m["role"] == "user"]
        i = next(i for i, c in enumerate(users) if "wait refused" in c)
        assert "1 notice arrived while you were thinking" in users[i]
        assert "nothing you started is pending" not in users[i]
        # 그 통지는 거부 **뒤**, 다음 턴 경계에 배달됐다
        assert any("🔔 hit" in c for c in users[i + 1 :])
        assert reg.arrived_for("main") == 0

    def test_two_notices_pluralize(self, tmp_path):
        reg = self._registry(tmp_path)
        loop = self._loop(
            reg,
            self._mid_turn(reg, 2, {"action": "wait"}),
        )
        assert loop.run().success
        assert any(
            "2 notices arrived while you were thinking" in str(m.get("content"))
            for m in loop.messages
            if m["role"] == "user"
        )


class TestResidentRunEndingInWait:
    def test_no_fallback_reply_and_debt_stays_open(self, tmp_path):
        """상주 런이 wait 로 끝나면 폴백(런 요약) 배달도 질문 닫기도 없다 — 깨운
        뒤 갚는다. 종전 코드라면 요청자가 "⏸ waiting" 을 답으로 받았다."""
        from types import SimpleNamespace

        from tests.test_agents_live import make_registry, wait_until

        def runner(query, ctx, **kwargs):
            return SimpleNamespace(
                success=True, output="⏸ waiting on job #1", waiting=True
            ), 0.01

        reg = make_registry(tmp_path, runner=runner)
        key, _ = reg.spawn()
        assert wait_until(lambda: reg.get(key).state == "idle")
        assert reg.request(key, "do X") == ""
        assert wait_until(
            lambda: reg.get(key).state == "idle" and reg.get(key).handled >= 1
        )
        assert not reg.has_pending_replies()  # 폴백이 main 으로 가지 않았다
        debts = reg.reply_debts(f"agent:{key}")
        assert [d.to for d in debts] == ["main"]  # 빚은 그대로
        assert reg.pending_for("main") == [f"agent:{key} owes you a reply"]


class TestCompleteNag:
    def test_complete_without_answers_while_a_job_runs_points_at_wait(self, jobs):
        jobs.start("sleep 30", owner="main")
        loop = _loop(
            _provider(
                {"action": "complete", "result": "waiting for the install"},
                {"action": "wait"},
            )
        )
        res = loop.run()
        assert res.waiting
        nag = [
            m["content"]
            for m in loop.messages
            if m["role"] == "user" and "call `wait` instead" in str(m.get("content"))
        ]
        assert nag, "되돌림이 wait 를 가리키지 않았다"

    def test_no_wait_hint_when_nothing_is_pending(self):
        set_job_registry(None)
        loop = _loop(
            _provider(
                {"action": "complete", "result": "r"},
                {"action": "complete", "result": "r", "answers": ["u-1"]},
            )
        )
        loop.run()
        assert not any(
            "call `wait` instead" in str(m.get("content"))
            for m in loop.messages
            if m["role"] == "user"
        )


class TestWording:
    """기다릴 땐 `complete` 라고 말하는 문장이 하나도 남지 않는다."""

    def test_guideline_jobs_tail_and_nudge(self, tmp_path):
        from agent_cli.prompts.system_prompt import TASK_GUIDELINES

        assert "call `wait`" in TASK_GUIDELINES
        assert "do not use `complete` to wait" in TASK_GUIDELINES
        r = JobRegistry(tmp_path)
        r.deliver = lambda *a, **k: None
        try:
            r.start("sleep 30", owner="main")
            tail = r.render_tail("main")
        finally:
            r.stop_all()
        # 작업 블록 머리는 상태만 (v10.68.0) — wait 안내는 규칙(Guidelines)과
        # `## Open Items` 꼬리말 두 곳, 세 번째는 없다
        assert "do not poll" in tail and "wait" not in tail and "complete" not in tail
        assert "call `wait`" in _OBS_COMPLETE_NUDGE
        assert "address every outstanding request" not in TASK_GUIDELINES

    def test_open_items_tail_points_at_wait_only_where_wait_exists(self, jobs):
        """꼬리의 `## Open Items` 끝에 "못 닫으면 wait" 한 줄 (v10.67.1) — wait 가
        광고되는 루프(main 큐·상주 받은편지함)에만. 스킬·일회성 루프의 꼬리엔 없다."""
        from agent_cli.loop.ledger import Ledger

        led = Ledger()
        led.add_request("u-1", "DJ", "do X")
        assert "Call `wait`" in led.block(can_wait=True)
        assert "never `complete` to wait" in led.block(can_wait=True)
        assert "wait" not in led.block()
        # 실제 조립: wakeable 포트의 루프 꼬리에는 있고, 미연결 포트의 루프엔 없다
        for ports, expect in ((WAKEABLE, True), (TEST_PORTS, False)):
            loop = AgentLoop(
                ports=ports,
                query="Q",
                query_request_id="u-1",
                provider=MagicMock(),
                capabilities=ModelCapabilities(
                    context_window=32768,
                    max_output_tokens=4096,
                    supports_thinking=False,
                ),
                model="m",
                dialect=get_dialect("json_fc"),
            )
            loop._state.ledger.add_request("u-1", "DJ", "do X")
            tail = loop._llm._build_session_state(10000)
            assert ("Call `wait` — the items stay open" in tail) is expect, tail

    def test_no_complete_and_wait_phrase_in_package(self):
        import re
        from pathlib import Path

        bad = re.compile(
            r"complete.{0,12}and wait|complete-and-wait|`complete` saying you are waiting"
        )
        hits = []
        for p in Path("agent_cli").rglob("*.py"):
            for i, line in enumerate(p.read_text().splitlines(), 1):
                if bad.search(line) and not line.lstrip().startswith("#"):
                    hits.append(f"{p}:{i}")
        assert not hits, hits
