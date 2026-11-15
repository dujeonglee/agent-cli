"""run 에이전트 — `agent` ``mode`` "run" 이 레지스트리에 올리는 에이전트 (docs/agent-engine/DESIGN.md).

실제 `AgentRegistry` 와 가짜 provider/엔진으로 본다: 레지스트리에 오르고 회신 뒤 내려옴,
상한 계산, 병렬, 깊이·순환(올리기 전), 정지(호출자의 Event — 상주의 평생 stop 과 무관),
resume 이 되살리지 않음, 훅 인자, ``@agent``, 모델 대면 명단에서 빠짐, 조회 함수의 숨은
부작용 없음, CLI 에이전트 줄, 웹 채널 페이로드. 바이트 등가는
`tests/equivalence/test_run_result_parity.py` 가 본다.
"""

from __future__ import annotations

import io
import json
import threading

import pytest
from rich.console import Console

from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.session_slots import get_agent_registry, set_agent_registry
from agent_cli.subagent import run_agent
from agent_cli.subagent.agents_live import AgentInstance, AgentRegistry
from agent_cli.subagent.delegation_checks import (
    depth_or_recursion_refusal,
    run_refusal,
)
from agent_cli.subagent.run_result import RunReply, write_result_md_and_tool_result
from agent_cli.tools.result import ToolResult
from tests.binding_support import make_binding
from tests.loop_kinds import make_caller

CAPS = ModelCapabilities(
    context_window=32768, max_output_tokens=4096, supports_thinking=False
)


@pytest.fixture
def reg(tmp_path):
    prev = get_agent_registry()
    registry = AgentRegistry(tmp_path / "sess")
    set_agent_registry(registry)
    yield registry
    set_agent_registry(prev)


def _caller(tmp_path, **kw):
    from agent_cli.context.manager import ContextManager

    ctx = ContextManager(tmp_path / "sess", max_context_tokens=10_000)
    kw.setdefault("binding", make_binding(provider=object(), capabilities=CAPS))
    kw.setdefault("stop_event", threading.Event())
    return make_caller(ctx=ctx, **kw)


def _engine(monkeypatch, fn):
    monkeypatch.setattr("agent_cli.loop.run.run_loop", fn)


def _resident(registry, key="agt-res00001", *, state="idle", profile="code-writer"):
    """명단에만 오른 상주 개체(스레드 없음) — 상한·명단 판정용."""
    tm = AgentInstance(
        key,
        profile_name=profile,
        role_prompt="",
        allowed_tools=None,
        model="m",
        hooks_config=None,
        context_mode="none",
        home_dir=registry.session_dir / "agents" / key,
    )
    tm.state = state
    registry._agents[key] = tm
    return tm


# ── 레지스트리에 오르고, 회신 뒤 내려온다 ──────────────────────────


class TestLifecycle:
    def test_run_is_a_registry_agent_that_comes_down_after_its_reply(
        self, tmp_path, reg, monkeypatch
    ):
        during = {}

        def engine(kind, start, ctx):
            tm = kind.tm
            during.update(
                key=tm.key,
                state=tm.state,
                listed=reg.get(tm.key) is tm,
                thread=threading.current_thread().name,
                alive=reg.alive_count(),
            )
            return ToolResult(True, output="answer")

        _engine(monkeypatch, engine)
        out = run_agent.run_agents({"tasks": [{"task": "go"}]}, _caller(tmp_path))
        assert out.success and "answer" in out.output
        tm = reg.get(during["key"])
        assert during["listed"] and during["state"] == "busy"
        assert during["thread"] == f"agent-{tm.key}"  # 자기 스레드 — 상주와 같다
        assert during["alive"] == 1  # 도는 동안 상한에 든다
        assert tm.ends_after_task and tm.state == "dead" and tm.handled == 1
        assert tm.ctx is None  # 내려온 run 은 대화를 쥐지 않는다
        assert not tm.worker.is_alive()
        assert reg.alive_count() == 0  # 내려와 자리를 비웠다
        saved = json.loads((reg.session_dir / "agents.json").read_text())
        entry = next(e for e in saved["agents"] if e["key"] == tm.key)
        assert entry["ends_after_task"] is True and entry["state"] == "dead"
        assert entry["home"] == out.artifact.rstrip("/")

    def test_home_is_the_run_dir_and_owner_is_private(self, tmp_path, reg, monkeypatch):
        seen = {}

        def engine(kind, start, ctx):
            seen.update(
                owner=kind.owner, ctx_dir=ctx.session_dir, home=kind.tm.home_dir
            )
            return ToolResult(True, output="ok")

        _engine(monkeypatch, engine)
        out = run_agent.run_agents({"tasks": [{"task": "go"}]}, _caller(tmp_path))
        assert seen["owner"] == f"run:{seen['home'].name}"
        assert seen["ctx_dir"] == seen["home"]
        assert out.artifact == f"{seen['home'].name}/"
        assert (seen["home"] / "result.md").exists()

    def test_parallel_runs_are_agents_each(self, tmp_path, reg, monkeypatch):
        gate = threading.Barrier(2, timeout=5)

        def engine(kind, start, ctx):
            gate.wait()  # 둘이 동시에 돈다
            return ToolResult(True, output=f"did {start.query}")

        _engine(monkeypatch, engine)
        out = run_agent.run_agents(
            {"tasks": [{"task": "a"}, {"task": "b"}]}, _caller(tmp_path)
        )
        assert out.success
        assert "[Task 1] a" in out.output and "[Task 2] b" in out.output
        runs = [t for t in reg._agents.values() if t.ends_after_task]
        assert len(runs) == 2 and all(t.state == "dead" for t in runs)

    def test_engine_crash_reraises_for_a_single_run(self, tmp_path, reg, monkeypatch):
        def engine(kind, start, ctx):
            raise RuntimeError("boom")

        _engine(monkeypatch, engine)
        with pytest.raises(RuntimeError, match="boom"):
            run_agent.run_agents({"tasks": [{"task": "go"}]}, _caller(tmp_path))
        (tm,) = [t for t in reg._agents.values() if t.ends_after_task]
        assert tm.state == "dead"

    def test_engine_crash_in_a_batch_is_the_old_thread_message(
        self, tmp_path, reg, monkeypatch
    ):
        def engine(kind, start, ctx):
            if start.query == "bad":
                raise RuntimeError("boom")
            return ToolResult(True, output="ok")

        _engine(monkeypatch, engine)
        out = run_agent.run_agents(
            {"tasks": [{"task": "ok"}, {"task": "bad"}]}, _caller(tmp_path)
        )
        assert not out.success
        assert "ERROR: Thread timed out or crashed" in out.error

    def test_no_registry_is_refused(self, tmp_path, monkeypatch):
        prev = get_agent_registry()
        set_agent_registry(None)
        try:
            out = run_agent.run_agents({"tasks": [{"task": "go"}]}, _caller(tmp_path))
        finally:
            set_agent_registry(prev)
        assert out.error == run_agent.NO_REGISTRY_FOR_RUN


# ── 상한 ────────────────────────────────────────────────────────


class TestCap:
    def test_single_run_over_the_cap_gets_the_spawn_sentence(self, tmp_path, reg):
        reg.set_max_agents(1)
        _resident(reg)
        out = run_agent.run_agents({"tasks": [{"task": "go"}]}, _caller(tmp_path))
        assert out.error == f"Delegation rejected: {reg.agent_limit_refusal()}"
        assert not [t for t in reg._agents.values() if t.ends_after_task]

    def test_spawn_uses_the_same_sentence(self, reg):
        reg.set_max_agents(1)
        _resident(reg)
        _key, err = reg.spawn(profile="", name="x")
        assert err == reg.agent_limit_refusal()

    def test_batch_over_the_cap_fails_only_that_task(self, tmp_path, reg, monkeypatch):
        release = threading.Event()

        def engine(kind, start, ctx):
            release.wait(5)
            return ToolResult(True, output="ok")

        _engine(monkeypatch, engine)
        reg.set_max_agents(2)
        _resident(reg)
        done = {}

        def call():
            done["out"] = run_agent.run_agents(
                {"tasks": [{"task": "first"}, {"task": "second"}]}, _caller(tmp_path)
            )

        t = threading.Thread(target=call)
        t.start()
        t.join(0.5)
        release.set()
        t.join(5)
        out = done["out"]
        assert "[Task 1] first\nSTATUS: success" in out.error
        assert (
            f"[Task 2] second\nERROR: Delegation rejected: {reg.agent_limit_refusal()}"
            in (out.error)
        )


# ── 깊이·순환 — 올리기 전에 ─────────────────────────────────────


class TestChecks:
    def test_order_empty_provider_recursion_depth(self):
        b = make_binding(provider=object(), capabilities=CAPS)
        assert (
            run_refusal(
                task=" ",
                binding=None,
                agent_name="a",
                agent_stack=["a"],
                depth=9,
                max_depth=2,
            )
            == "Delegation rejected: empty task"
        )
        assert (
            run_refusal(
                task="t",
                binding=None,
                agent_name="a",
                agent_stack=["a"],
                depth=9,
                max_depth=2,
            )
            == "Delegation rejected: missing provider/capabilities"
        )
        assert run_refusal(
            task="t", binding=b, agent_name="a", agent_stack=["a"], depth=9, max_depth=2
        ).startswith("Recursive agent call blocked")
        assert run_refusal(
            task="t", binding=b, agent_name="a", agent_stack=[], depth=2, max_depth=2
        ).startswith("Maximum call depth reached")
        assert (
            run_refusal(
                task="t",
                binding=b,
                agent_name="a",
                agent_stack=[],
                depth=1,
                max_depth=2,
            )
            == ""
        )

    def test_refused_runs_never_reach_the_registry(self, tmp_path, reg):
        for caller_kw, spec in (
            ({"depth": 2}, {"task": "go"}),
            ({"agent_stack": ["general"]}, {"task": "go", "agent": "general"}),
            ({}, {"task": "  "}),
        ):
            out = run_agent.run_agents(
                {"tasks": [spec]}, _caller(tmp_path, **caller_kw)
            )
            assert not out.success
        assert reg._agents == {}

    def test_spawn_calls_the_same_depth_check(self, reg):
        reg.runtime = {"depth": 2, "max_depth": 2}
        _key, err = reg.spawn(profile="", name="x")
        assert err == depth_or_recursion_refusal("", [], 2, 2)
        assert err.startswith("Maximum call depth reached")


# ── 정지 ────────────────────────────────────────────────────────


class TestStop:
    def test_run_follows_the_callers_stop_event(self, tmp_path, reg, monkeypatch):
        caller = _caller(tmp_path)
        resident = _resident(reg)
        seen = {}

        def engine(kind, start, ctx):
            seen["same"] = kind.stop_event is caller.stop_event
            caller.stop_event.set()  # 방 ■
            return ToolResult(False, error="Interrupted by user")

        _engine(monkeypatch, engine)
        out = run_agent.run_agents({"tasks": [{"task": "go"}]}, caller)
        assert seen["same"] and not out.success
        run_tm = next(t for t in reg._agents.values() if t.ends_after_task)
        # 상주의 평생 stop 과 run 개체의 평생 stop 은 건드리지 않는다
        assert not resident.stop_event.is_set()
        assert not run_tm.stop_event.is_set()

    def test_no_caller_event_gets_one_shared_by_the_batch(
        self, tmp_path, reg, monkeypatch
    ):
        events = []
        lock = threading.Lock()

        def engine(kind, start, ctx):
            with lock:
                events.append(kind.stop_event)
            return ToolResult(True, output="ok")

        _engine(monkeypatch, engine)
        caller = _caller(tmp_path, stop_event=None)
        run_agent.run_agents({"tasks": [{"task": "a"}, {"task": "b"}]}, caller)
        assert events[0] is not None and events[0] is events[1]

    def test_interrupted_wait_stops_the_runs_and_reraises(self):
        stop = threading.Event()

        class _Worker:
            def join(self):
                raise KeyboardInterrupt

        started = [run_agent._Started(tm=type("T", (), {"worker": _Worker()})())]
        with pytest.raises(KeyboardInterrupt):
            run_agent._wait_for_replies(started, stop)
        assert stop.is_set()


# ── resume ──────────────────────────────────────────────────────


class TestResume:
    def test_restore_never_revives_a_run_agent(self, tmp_path):
        sdir = tmp_path / "sess"
        sdir.mkdir()
        (sdir / "agents.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "agents": [
                        {
                            "key": "agt-run00001",
                            "profile": "code-analyst",
                            # 끝나기 전에 프로세스가 죽었다 — 그래도 되살리지 않는다
                            "state": "idle",
                            "ends_after_task": True,
                            "home": "run_code-analyst_abc123_20261115T000000000",
                        }
                    ],
                    "pending": [],
                    "questions": [],
                }
            ),
            encoding="utf-8",
        )
        registry = AgentRegistry(sdir)
        assert registry.restore() == 0
        tm = registry.get("agt-run00001")
        assert tm.ends_after_task and tm.state == "dead" and tm.worker is None
        assert tm.home_dir == sdir / "run_code-analyst_abc123_20261115T000000000"
        # 화면 로스터에는 남고(회색 채널), 모델 대면 명단에는 없다
        assert [s["key"] for s in registry.roster_snapshot()] == ["agt-run00001"]
        assert (
            registry.format_status() == 'no live agents. Spawn one with `mode` "spawn".'
        )
        assert (
            registry.resume_teammate("agt-run00001") == 'unknown agent "agt-run00001"'
        )

    def test_tombstone_home_cannot_leave_the_session(self, tmp_path):
        registry = AgentRegistry(tmp_path)
        tm = registry._run_agent_tombstone({"key": "agt-x", "home": "../../etc"})
        assert tm.home_dir == tmp_path / "etc"


# ── 모델 대면 명단에서 빠진다 (P1 바이트 불변) ──────────────────────


class TestHiddenFromTheModel:
    def _with_run(self, reg, *, state="busy", profile="code-reviewer"):
        tm = _resident(reg, "agt-run00009", state=state, profile=profile)
        tm.ends_after_task = True
        return tm

    def test_live_agents_section_status_and_addressing(self, reg):
        from agent_cli.prompts.system_prompt import build_live_agents_section

        _resident(reg)
        before_tail = build_live_agents_section(reg, include_state=True)
        before_status = reg.format_status()
        self._with_run(reg)
        assert build_live_agents_section(reg, include_state=True) == before_tail
        assert reg.format_status() == before_status
        assert reg.format_status("agt-run00009") == 'unknown agent "agt-run00009"'
        assert reg.resolve_key("agt-run00009") is None
        assert reg.request("agt-run00009", "hi").startswith(
            'unknown agent "agt-run00009"'
        )
        assert reg.pending_for("main") == []  # current_author 기본값이 main 이어도

    def test_a_finished_run_does_not_block_a_spawn_of_that_profile(self, reg):
        self._with_run(reg, state="dead", profile="")
        assert reg._duplicate_label("", "") == ""
        tm = self._with_run(reg, state="dead", profile="code-reviewer")
        tm.instance_name = "x"
        assert reg._duplicate_label("code-reviewer", "x") == ""

    def test_dead_run_is_not_a_same_profile_hint_on_spawn(self, reg, monkeypatch):
        from agent_cli.subagent.agents_live import _agent_spawn

        self._with_run(reg, state="dead", profile="code-writer")
        monkeypatch.setattr(reg, "spawn", lambda **kw: ("agt-new00001", ""))
        out = _agent_spawn(reg, {"profile": "code-writer"}, parent_ctx=None, runtime={})
        assert "agt-run00009" not in out.output

    def test_ui_roster_includes_run_agents_with_their_flag(self, reg):
        self._with_run(reg)
        (row,) = reg.roster_snapshot()
        assert row["ends_after_task"] is True


# ── 숨은 부작용 — 조회 함수는 저장·로스터·ref 를 쓰지 않는다 ──────────


class TestNoHiddenSideEffects:
    def test_lookups_do_not_save_or_notify(self, reg, monkeypatch):
        tm = _resident(reg)
        run_tm = _resident(reg, "agt-run00002")
        run_tm.ends_after_task = True
        writes = []
        monkeypatch.setattr(reg, "_save_state", lambda: writes.append("save"))
        monkeypatch.setattr(reg, "_notify_roster", lambda: writes.append("roster"))
        reg._residents()
        reg.roster_snapshot()
        reg.format_status()
        reg.format_status(tm.key)
        reg.resolve_key(tm.key)
        reg.resolve_key(run_tm.key)
        reg.agent_limit_refusal()
        reg.pending_for("main")
        assert writes == []
        assert not tm.stop_event.is_set() and tm.state == "idle"

    def test_checks_and_current_task_id_are_pure(self):
        from agent_cli.render.base import Renderer
        from agent_cli.render.web import WebRenderer

        stack = ["a"]
        depth_or_recursion_refusal("a", stack, 0, 2)
        assert stack == ["a"]
        assert Renderer.current_task_id(object()) == ""
        w = WebRenderer()
        n = len(w.hub.events)
        assert w.current_task_id() == ""
        assert len(w.hub.events) == n

    def test_adapter_writes_only_result_md(self, tmp_path):
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(tmp_path / "ctx", max_context_tokens=1000)
        run_dir = tmp_path / "run_task_x"
        out = write_result_md_and_tool_result(
            RunReply(result=ToolResult(True, output="hi"), ctx=ctx, run_dir=run_dir)
        )
        assert out.output.startswith("STATUS: success\nRESULT:\nhi")
        assert out.artifact == "run_task_x/"
        assert [p.name for p in run_dir.iterdir()] == ["result.md"]
        assert write_result_md_and_tool_result(RunReply(refusal="nope")) == ToolResult(
            False, error="nope"
        )


# ── 훅 — run 에만, 지금과 같은 인자·시점 ───────────────────────────


class TestHooks:
    def test_agent_start_end_wrap_the_run(self, tmp_path, reg, monkeypatch):
        from agent_cli.loop.state import LoopState
        from agent_cli.loop.tool_bridge import ToolBridge
        from tests.loopkit import make_config

        fired = []

        class Hooks:
            def fire(self, event, **kw):
                fired.append((event, kw, len([t for t in reg._agents.values()])))

        _engine(monkeypatch, lambda kind, start, ctx: ToolResult(True, output="ok"))
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(tmp_path / "sess", max_context_tokens=10_000)
        cfg = make_config(
            binding=make_binding(provider=object(), capabilities=CAPS),
            hook_runner=Hooks(),
        )
        bridge = ToolBridge(cfg, LoopState(), ctx, None)
        args = {"mode": "run", "task": "go"}
        result = bridge._invoke_agent(args, dict(args))
        assert [e for e, _, _ in fired] == ["OnAgentStart", "OnAgentEnd"]
        start, end = fired[0][1], fired[1][1]
        assert start["tool_name"] == "agent" and start["tool_input"] == args
        assert end["delegate_result"] is result
        assert fired[0][2] == 0 and fired[1][2] == 1  # 시작 전 → 끝난 뒤


# ── @agent 명령 ─────────────────────────────────────────────────


class TestAtAgent:
    def test_at_agent_runs_a_registry_run_agent(self, tmp_path, reg, monkeypatch):
        from agent_cli.context.manager import ContextManager
        from agent_cli.main import _dispatch_agent

        _engine(monkeypatch, lambda kind, start, ctx: ToolResult(True, output="hello"))
        ctx = ContextManager(tmp_path / "sess", max_context_tokens=10_000)
        answer, ok = _dispatch_agent(
            "@general look around",
            make_binding(provider=object(), capabilities=CAPS),
            ctx=ctx,
        )
        assert ok and "hello" in answer
        (tm,) = [t for t in reg._agents.values() if t.ends_after_task]
        assert tm.profile_name == "general" and tm.context_mode == "fork"
        assert tm.state == "dead"


# ── 화면 ────────────────────────────────────────────────────────


class TestCliAgentLines:
    def _r(self):
        from agent_cli.render.minimal import MinimalRenderer

        out = io.StringIO()
        return MinimalRenderer(Console(file=out, width=200, force_terminal=False)), out

    def test_one_line_per_tool_step_and_nothing_else(self):
        r, out = self._r()
        r.begin_agent_work(
            key="agt-3", item="run", profile="explorer", message="find\nall"
        )
        r.thought("thinking hard", 1)
        r.action("shell", json.dumps({"command": "rg -n parse src/"}), 1)
        r.observation("lots of output", 1, tool_name="shell", success=True)
        r.action("read_file", json.dumps({"path": "src/a.py"}), 2)
        r.observation("ERROR: nope", 2, tool_name="read_file", success=False)
        r.final("the answer", 3)
        r.end_agent_work(key="agt-3", item="run", success=True, duration_s=2.0)
        lines = out.getvalue().splitlines()
        assert lines == [
            "  agt-3 │ ▶ explorer · find",
            "  agt-3 │ shell  rg -n parse src/  ✓",
            "  agt-3 │ read_file  src/a.py  ✗",
            "  agt-3 │ ✓ done (2.0s)",
        ]

    def test_agents_interleave_in_arrival_order_and_main_is_unaffected(self):
        r, out = self._r()
        step = threading.Event()
        go = threading.Event()

        def agent(key, cmd, wait_for, signal):
            r.begin_agent_work(key=key, item="run", profile="", message="t")
            if wait_for is not None:
                wait_for.wait(5)
            r.action("shell", json.dumps({"command": cmd}), 1)
            r.observation("x", 1, tool_name="shell", success=True)
            if signal is not None:
                signal.set()
            r.end_agent_work(key=key, item="run", success=True, duration_s=0.0)

        a = threading.Thread(target=agent, args=("agt-a", "one", None, step))
        b = threading.Thread(target=agent, args=("agt-b", "two", step, go))
        a.start()
        b.start()
        a.join(5)
        b.join(5)
        r.observation(
            "main obs", 1, tool_name="read_file", success=True
        )  # main 은 평소대로
        text = out.getvalue()
        assert text.index("agt-a │ shell  one") < text.index("agt-b │ shell  two")
        assert "✓ read_file  success" in text


class TestWebRunScope:
    def test_payload_names_channel_dir_and_caller(self):
        from agent_cli.render.web import WebRenderer

        w = WebRenderer()
        w.begin_agent_work(
            key="agt-1",
            item="run",
            profile="explorer",
            message="go",
            ctx_dir="run_explorer_x",
            caller_task="agt-9#u-1",
        )
        start = next(dict(d) for e, d in w.hub.events if e == "scope_start")
        assert start["channel"] == "agt-1" and start["ctx_dir"] == "run_explorer_x"
        assert start["run"] is True and start["caller_task"] == "agt-9#u-1"
        assert w.current_task_id() == "agt-1#run"
        w.end_agent_work(key="agt-1", item="run", success=True, duration_s=0.0)
        assert w.current_task_id() == ""

    def test_resident_payload_is_unchanged_but_names_its_channel(self):
        from agent_cli.render.web import WebRenderer

        w = WebRenderer()
        w.begin_agent_work(key="agt-2", item="u-1", profile="", message="hi")
        start = next(dict(d) for e, d in w.hub.events if e == "scope_start")
        assert start["ctx_dir"] == "agents/agt-2" and start["channel"] == "agt-2"
        assert "run" not in start and "caller_task" not in start
