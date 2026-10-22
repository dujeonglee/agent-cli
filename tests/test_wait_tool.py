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
from tests.loop_ports import TEST_PORTS


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
        ports=TEST_PORTS,
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

    def test_agent_scoped_jobs_do_not_wake_main(self, jobs):
        jobs.start("sleep 30", owner="agent:x")
        from agent_cli.runtime import wake_sources

        assert wake_sources("main") == []
        assert wake_sources("agent:x") == ["job #1 (`sleep 30`)"]


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
        assert "Call `wait`" in tail and "complete" not in tail
        assert "call `wait`" in _OBS_COMPLETE_NUDGE

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
