"""일회성 위임(agent mode=run)의 단건·병렬 경로 계약 (v10.48.0 C2).

단건은 호출 스레드에서 인라인으로 돌고(CLI 가 실시간·들여쓰기로 보여 준다),
병렬은 워커 스레드에서 돈다(CLI 가 모았다가 끝에 보여 준다) — **실행 자리**는
의도된 차이라 그대로 둔다. 같아야 하는 것은 카드 수명(열기 → 런 →
닫기·소요·오류)과 `OneShotLoop` 가 받는 과제·호출자다. 이 테스트는 두 경로가 렌더러에
내는 사건과 그 인자를 기록해 고정한다 — C2 이전 구현에서 먼저 통과시킨
특성 테스트다.
"""

from __future__ import annotations

import threading

import pytest

from agent_cli.subagent import oneshot
from agent_cli.tools.result import ToolResult
from tests.loop_kinds import make_caller


class _Renderer:
    def __init__(self):
        self.events: list[tuple] = []
        self._lock = threading.Lock()

    def current_scope(self):
        return "outer-scope"

    def begin_scope(self, **kw):
        with self._lock:
            self.events.append(("begin", kw))

    def end_scope(self, **kw):
        with self._lock:
            self.events.append(("end", kw))

    # 인라인 단건의 CLI 들여쓰기
    def push_depth(self):
        with self._lock:
            self.events.append(("push", {}))

    def pop_depth(self):
        with self._lock:
            self.events.append(("pop", {}))


@pytest.fixture
def rig(monkeypatch):
    from agent_cli import render

    r = _Renderer()
    monkeypatch.setattr(render, "_renderer", r)
    monkeypatch.setattr(render, "get_renderer", lambda: r)
    calls: list[dict] = []
    threads: list[str] = []

    from agent_cli.subagent.oneshot import OneShotLoop
    from tests.loop_kinds import fake_oneshot_run

    def fake_run(kind):
        c = kind.caller
        calls.append(
            {
                "task": kind.task,
                "context_mode": kind.context_mode,
                "allowed_tools": kind.requested_tools,
                "agent_name": kind.agent_name,
                "instructions": kind.instructions,
                "run_dir_name": kind.run_dir_name,
                "stop_event": kind.stop_event,
                "parent_ctx": c.ctx,
                "binding": c.binding,
                "depth": c.depth,
                "max_depth": c.max_depth,
                "max_turns": c.max_turns,
                "session": c.session,
                "skill_stack": c.skill_stack,
                "agent_stack": c.agent_stack,
                "hooks_config": c.hooks_config,
                "compaction_enabled": c.compaction_enabled,
            }
        )
        threads.append(threading.current_thread().name)
        if "boom" in kind.task:
            return ToolResult(False, error="it broke")
        return ToolResult(True, output=f"STATUS: success\nRESULT:\ndid {kind.task}")

    monkeypatch.setattr(OneShotLoop, "run", fake_oneshot_run(fake_run))
    return r, calls, threads


def _delegate(tasks, **kw):
    return oneshot.tool_delegate(
        {"tasks": tasks},
        make_caller(
            ctx=None,
            binding="B",
            depth=1,
            max_depth=3,
            max_turns=7,
            session="S",
            skill_stack=["sk"],
            agent_stack=["ag"],
            hooks_config={"h": 1},
            compaction_enabled=False,
        ),
    )


_COMMON = {
    "parent_ctx": None,
    "binding": "B",
    "depth": 1,
    "max_depth": 3,
    "max_turns": 7,
    "session": "S",
    "skill_stack": ["sk"],
    "agent_stack": ["ag"],
    "hooks_config": {"h": 1},
    "compaction_enabled": False,
}


def _strip(kw):
    """매번 바뀌는 값(uuid task_id, 시각, 소요, run dir)을 모양으로 바꾼다."""
    out = dict(kw)
    if "task_id" in out:
        out["task_id"] = out["task_id"].rsplit("-", 1)[0]
    if "ctx_dir" in out:
        out["ctx_dir"] = bool(out["ctx_dir"])
    if "ts" in out:
        out["ts"] = isinstance(out["ts"], float)
    if "duration_s" in out:
        out["duration_s"] = isinstance(out["duration_s"], float)
    return out


class TestSingle:
    def test_runs_inline_with_depth_and_one_card(self, rig):
        r, calls, threads = rig
        res = _delegate(
            [{"task": "read it", "agent": "explorer", "instructions": "be brief"}]
        )
        assert res.success and res.output == "STATUS: success\nRESULT:\ndid read it"
        assert threads == [threading.current_thread().name]  # 인라인
        assert [e[0] for e in r.events] == ["begin", "push", "pop", "end"]
        assert _strip(r.events[0][1]) == {
            "task_id": "delegate-single",
            "kind": "run",
            "label": "agent:explorer",
            "agent": "explorer",
            "index": 0,
            "ctx_dir": True,
        }
        assert _strip(r.events[3][1]) == {
            "task_id": "delegate-single",
            "kind": "run",
            "success": True,
            "duration_s": True,
            "error": "",
        }
        assert r.events[0][1]["task_id"] == r.events[3][1]["task_id"]
        kw = dict(calls[0])
        assert bool(kw.pop("run_dir_name")) and kw.pop("stop_event", "x") is None
        assert kw == {
            "task": "read it",
            "context_mode": "none",
            "allowed_tools": None,
            "agent_name": "explorer",
            "instructions": "be brief",
            **_COMMON,
        }

    def test_failure_closes_the_card_with_the_error(self, rig):
        r, _calls, _t = rig
        res = _delegate([{"task": "boom"}])
        assert not res.success and res.error == "it broke"
        end = r.events[-1][1]
        assert end["success"] is False and end["error"] == "it broke"
        assert r.events[0][1]["label"] == "agent"  # 프로필 없는 단건

    def test_string_task_is_normalized(self, rig):
        _r, calls, _t = rig
        _delegate(["just text"])
        assert calls[0]["task"] == "just text"


class TestParallel:
    def test_workers_get_lineage_shared_ts_and_their_own_cards(self, rig):
        r, calls, threads = rig
        res = _delegate([{"task": "one", "agent": "a"}, {"task": "boom"}])
        assert not res.success
        assert res.error.startswith("STATUS: error\n[Task 1] one\nSTATUS: success")
        assert "[Task 2] boom\nERROR: it broke" in res.error
        assert "[Parallel execution: 2 tasks, 1 succeeded, 1 failed]" in res.error
        assert all(t != threading.current_thread().name for t in threads)
        begins = sorted(
            (e[1] for e in r.events if e[0] == "begin"), key=lambda k: k["index"]
        )
        ends = [e[1] for e in r.events if e[0] == "end"]
        assert [e[0] for e in r.events].count("push") == 0  # 병렬은 들여쓰기 없음
        assert [_strip(b) for b in begins] == [
            {
                "task_id": "delegate-0",
                "kind": "run",
                "label": "one",
                "agent": "a",
                "index": 0,
                "parent": "outer-scope",
                "ts": True,
                "ctx_dir": True,
            },
            {
                "task_id": "delegate-1",
                "kind": "run",
                "label": "boom",
                "agent": "",
                "index": 1,
                "parent": "outer-scope",
                "ts": True,
                "ctx_dir": True,
            },
        ]
        assert begins[0]["ts"] == begins[1]["ts"]  # 한 묶음 = 한 시작 시각
        by_id = {e["task_id"]: e for e in ends}
        assert by_id[begins[1]["task_id"]]["error"] == "it broke"
        assert by_id[begins[0]["task_id"]]["success"] is True
        for kw in calls:
            kw = dict(kw)
            assert bool(kw.pop("run_dir_name"))
            assert isinstance(kw.pop("stop_event"), threading.Event)
            assert {k: kw[k] for k in _COMMON} == _COMMON
        assert sorted(c["task"] for c in calls) == ["boom", "one"]

    def test_empty_tasks_rejected(self, rig):
        res = _delegate([])
        assert not res.success and "empty tasks" in res.error
