"""run 에이전트 — `agent` ``mode`` "run" (docs/agent-engine/DESIGN.md §2·§3, v10.141.0).

run 은 `spawn(ends_after_task=True)` + 과제 요청이다: 바로 돌아오고 결과는 회신으로 온다. 여기는
그 진입(`run_agent.start_run`·`launch_run`)의 단위 — 상한, 앞 거부 판정(올리기 전), 정지(부른 런에
묶인 `LinkedStop`), 훅(띄울 때·내려올 때), ``@agent``, 명단·주소, 숨은 부작용, 화면 줄. 루프를
통째로 도는 흐름(제자리 대기·회신·끼어들기·resume)은 tests/test_engine_p2.py.
"""

from __future__ import annotations

import io
import json
import threading
import time

import pytest
from rich.console import Console

from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.session_slots import get_agent_registry, set_agent_registry
from agent_cli.subagent import run_agent
from agent_cli.subagent.agents_live import AgentInstance, AgentRegistry, tool_agent
from agent_cli.subagent.delegation_checks import (
    depth_or_recursion_refusal,
    run_refusal,
)
from agent_cli.tools.result import ToolResult
from tests.binding_support import make_binding
from tests.loop_kinds import make_caller, runtime_of

CAPS = ModelCapabilities(
    context_window=32768, max_output_tokens=4096, supports_thinking=False
)


@pytest.fixture
def reg(tmp_path):
    prev = get_agent_registry()
    registry = AgentRegistry(tmp_path / "sess")
    set_agent_registry(registry)
    yield registry
    registry.shutdown_all()
    set_agent_registry(prev)


def _caller(tmp_path, **kw):
    from agent_cli.context.manager import ContextManager

    ctx = ContextManager(tmp_path / "sess", max_context_tokens=10_000)
    kw.setdefault("binding", make_binding(provider=object(), capabilities=CAPS))
    kw.setdefault("stop_event", threading.Event())
    return make_caller(ctx=ctx, **kw)


def _run(reg, caller, **args):
    return run_agent.start_run(
        reg, {"task": "go", **args}, caller=caller, runtime=runtime_of(caller)
    )


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


def _wait(pred, timeout=5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end and not pred():
        time.sleep(0.02)
    return pred()


class TestLaunch:
    def test_run_returns_at_once_with_the_request_id(self, tmp_path, reg, monkeypatch):
        gate = threading.Event()

        def engine(kind, start, ctx):
            gate.wait(5)
            return ToolResult(True, output="ok")

        _engine(monkeypatch, engine)
        out = _run(reg, _caller(tmp_path), profile="")
        assert out.success
        m = __import__("re").match(
            r'started agent "(agt-[0-9a-f]{8})" on request "(r-\d+)"', out.output
        )
        assert m, out.output
        tm = reg.get(m.group(1))
        assert tm.ends_after_task and tm.task_id == m.group(2)
        assert (
            tm.home_dir == reg.session_dir / "agents" / tm.key
        )  # 다른 에이전트와 같은 집
        assert [r["id"] for r in reg.awaited_replies("main")] == [m.group(2)]
        gate.set()
        assert _wait(lambda: tm.state == "dead")
        assert reg.awaited_replies("main") == []  # 회신(폴백)이 나갔다

    def test_owner_is_the_agent_key(self, tmp_path, reg, monkeypatch):
        seen = {}

        def engine(kind, start, ctx):
            seen["owner"] = kind.ports.owner
            seen["kind"] = type(kind).__name__
            return ToolResult(True, output="ok")

        _engine(monkeypatch, engine)
        _run(reg, _caller(tmp_path))
        assert _wait(lambda: "owner" in seen)
        assert seen["owner"].startswith("agent:agt-") and seen["kind"] == "ResidentLoop"


class TestCap:
    def test_run_over_the_cap_gets_the_spawn_sentence(self, tmp_path, reg):
        reg.set_max_agents(1)
        _resident(reg)
        out = _run(reg, _caller(tmp_path))
        assert out.error == f"Delegation rejected: {reg.agent_limit_refusal()}"
        assert not [t for t in reg._agents.values() if t.ends_after_task]

    def test_spawn_uses_the_same_sentence(self, reg):
        reg.set_max_agents(1)
        _resident(reg)
        _key, err = reg.spawn(profile="", name="x")
        assert err == reg.agent_limit_refusal()


class TestChecks:
    def test_order_empty_provider_recursion_depth(self):
        b = make_binding(provider=object(), capabilities=CAPS)
        kw = {"agent_name": "a", "agent_stack": ["a"], "depth": 9, "max_depth": 2}
        assert (
            run_refusal(task=" ", binding=None, **kw)
            == "Delegation rejected: empty task"
        )
        assert (
            run_refusal(task="t", binding=None, **kw)
            == "Delegation rejected: missing provider/capabilities"
        )
        assert run_refusal(task="t", binding=b, **kw).startswith(
            "Recursive agent call blocked"
        )
        assert run_refusal(
            task="t", binding=b, agent_name="a", agent_stack=[], depth=2, max_depth=2
        ).startswith("Maximum call depth reached")

    def test_refused_runs_never_reach_the_registry(self, tmp_path, reg):
        for caller_kw, args in (
            ({"depth": 2}, {}),
            ({"agent_stack": ["general"]}, {"profile": "general"}),
            ({}, {"task": "  "}),
        ):
            out = _run(reg, _caller(tmp_path, **caller_kw), **args)
            assert not out.success
        assert reg._agents == {}

    def test_spawn_checks_with_the_callers_depth_and_stack(self, reg):
        _key, err = reg.spawn(profile="", runtime={"depth": 2, "max_depth": 2})
        assert err == depth_or_recursion_refusal("", [], 2, 2)
        _key, err = reg.spawn(profile="general", agent_stack=["general"])
        assert err.startswith("Recursive agent call blocked")

    def test_agent_spawned_by_an_agent_is_one_deeper(self, tmp_path, reg, monkeypatch):
        seen = []
        _engine(
            monkeypatch,
            lambda kind, start, ctx: (
                seen.append(kind.depth),
                ToolResult(True, output="o"),
            )[1],
        )
        caller = _caller(tmp_path, depth=1, max_depth=3, owner="agent:agt-p")
        _run(reg, caller)
        assert _wait(lambda: seen) and seen == [2]


class TestStop:
    def test_run_stops_with_the_callers_stop_but_not_the_other_way(self, tmp_path, reg):
        caller = _caller(tmp_path)
        resident = _resident(reg)
        tm_stop = run_agent.LinkedStop(caller.stop_event)
        assert not tm_stop.is_set()
        tm_stop.set()  # run 의 kill 은 부른 런을 멈추지 않는다
        assert not caller.stop_event.is_set()
        other = run_agent.LinkedStop(caller.stop_event)
        caller.stop_event.set()  # 방 ■ — 부른 런이 멈추면 run 도
        assert other.is_set()
        assert not resident.stop_event.is_set()  # 상주의 평생 stop 은 무관

    def test_linked_stop_without_a_parent_is_a_plain_event(self):
        ev = run_agent.LinkedStop(None)
        assert not ev.is_set()
        ev.set()
        assert ev.is_set()

    def test_run_agent_comes_down_when_the_caller_stops(
        self, tmp_path, reg, monkeypatch
    ):
        caller = _caller(tmp_path)

        def engine(kind, start, ctx):
            caller.stop_event.set()  # 방 ■ 이 런 도중에
            return ToolResult(False, error="Interrupted by user")

        _engine(monkeypatch, engine)
        _run(reg, caller)
        (tm,) = reg._agents.values()
        assert _wait(lambda: tm.state == "dead") and tm.stop_event.is_set()
        assert (
            reg.awaited_replies("main") == []
        )  # 버린 빚 — 요청자는 더 기다리지 않는다


class TestHooks:
    def test_agent_start_at_launch_and_end_when_it_comes_down(
        self, tmp_path, reg, monkeypatch
    ):
        fired = []

        class Hooks:
            def fire(self, event, **kw):
                fired.append((event, kw))

        gate = threading.Event()

        def engine(kind, start, ctx):
            gate.wait(5)
            return ToolResult(True, output="hook-out")

        _engine(monkeypatch, engine)
        caller = _caller(tmp_path, hook_runner=Hooks(), turn=4)
        _run(reg, caller, profile="")
        assert [e for e, _ in fired] == ["OnAgentStart"]  # 띄울 때 — 아직 안 끝났다
        start = fired[0][1]
        assert start["tool_name"] == "agent" and start["tool_input"]["task"] == "go"
        assert start["turn"] == 4
        gate.set()
        assert _wait(lambda: len(fired) == 2)
        event, end = fired[1]
        assert event == "OnAgentEnd" and end["delegate_result"].output == "hook-out"


class TestAtAgent:
    def test_at_agent_waits_for_the_reply_and_keeps_it_off_mains_mail(
        self, tmp_path, reg, monkeypatch
    ):
        from agent_cli.context.manager import ContextManager
        from agent_cli.main import _dispatch_agent

        _engine(monkeypatch, lambda kind, start, ctx: ToolResult(True, output="hello"))
        ctx = ContextManager(tmp_path / "sess", max_context_tokens=10_000)
        answer, ok = _dispatch_agent(
            "@general look around",
            make_binding(provider=object(), capabilities=CAPS),
            ctx=ctx,
            agent_registry=reg,
        )
        assert ok and answer == "hello"  # 폴백 라벨은 답이 아니다
        assert (
            not reg.has_pending_replies()
        )  # 명령의 답 — main 의 다음 턴 메일이 아니다
        (tm,) = reg._agents.values()
        assert tm.profile_name == "general" and tm.context_mode == "fork"
        assert _wait(lambda: tm.state == "dead")

    def test_at_agent_without_a_registry_says_so(self, tmp_path):
        from agent_cli.main import _dispatch_agent
        from agent_cli.subagent.agents_live import NO_REGISTRY

        answer, ok = _dispatch_agent(
            "@general look", make_binding(provider=object(), capabilities=CAPS)
        )
        assert not ok and answer == NO_REGISTRY


class TestListedAndAddressable:
    def _with_run(self, reg, *, state="busy", profile="code-reviewer"):
        tm = _resident(reg, "agt-run00009", state=state, profile=profile)
        tm.ends_after_task = True
        return tm

    def test_live_run_agent_is_in_the_roster_status_and_addressing(self, reg):
        from agent_cli.prompts.system_prompt import build_live_agents_section

        self._with_run(reg)
        tail = build_live_agents_section(reg, include_state=True)
        assert "`agt-run00009` (code-reviewer · run) [busy]" in tail
        assert "[code-reviewer · run] busy" in reg.format_status("agt-run00009")
        assert reg.resolve_key("agt-run00009") == "agt-run00009"
        assert reg.request("agt-run00009", "hi", author="user:ana") == ""

    def test_finished_run_leaves_the_model_roster_but_stays_on_screen(self, reg):
        from agent_cli.prompts.system_prompt import build_live_agents_section

        self._with_run(reg, state="dead")
        assert "agt-run00009" not in build_live_agents_section(reg, include_state=True)
        (row,) = reg.roster_snapshot()
        assert row["ends_after_task"] is True and row["state"] == "dead"
        assert "resumable" not in reg.format_status("agt-run00009")

    def test_runs_never_block_a_spawn_of_that_profile(self, reg):
        self._with_run(reg, state="dead", profile="")
        assert reg._duplicate_label("", "") == ""
        tm = self._with_run(reg, state="busy", profile="code-reviewer")
        tm.instance_name = "x"
        assert reg._duplicate_label("code-reviewer", "x") == ""

    def test_dead_run_is_not_a_same_profile_hint_on_spawn(self, reg, monkeypatch):
        from agent_cli.subagent.agents_live import _agent_spawn

        self._with_run(reg, state="dead", profile="code-writer")
        monkeypatch.setattr(reg, "spawn", lambda **kw: ("agt-new00001", ""))
        out = _agent_spawn(
            reg, {"profile": "code-writer"}, caller=make_caller(), runtime={}
        )
        assert "agt-run00009" not in out.output

    def test_parallel_same_profile_runs_are_allowed(self, tmp_path, reg, monkeypatch):
        gate = threading.Event()
        _engine(
            monkeypatch,
            lambda kind, start, ctx: (gate.wait(5), ToolResult(True, output="o"))[1],
        )
        caller = _caller(tmp_path)
        a = _run(reg, caller, profile="general")
        b = _run(reg, caller, profile="general")
        gate.set()
        assert a.success and b.success


class TestNoHiddenSideEffects:
    def test_lookups_do_not_save_or_notify(self, reg, monkeypatch):
        tm = _resident(reg)
        run_tm = _resident(reg, "agt-run00002")
        run_tm.ends_after_task = True
        writes = []
        monkeypatch.setattr(reg, "_save_state", lambda: writes.append("save"))
        monkeypatch.setattr(reg, "_notify_roster", lambda: writes.append("roster"))
        reg.roster_snapshot()
        reg.format_status()
        reg.format_status(tm.key)
        reg.resolve_key(tm.key)
        reg.resolve_key(run_tm.key)
        reg.agent_limit_refusal()
        reg.pending_for("main")
        reg.awaited_replies("main")
        reg._task_delivered(run_tm)
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

    def test_tool_agent_refreshes_the_registry_runtime_only_for_main(self, reg):
        tool_agent(
            {"mode": "status"}, registry=reg, caller=make_caller(), runtime={"x": 1}
        )
        assert reg.runtime == {"x": 1}
        tool_agent(
            {"mode": "status"},
            registry=reg,
            caller=make_caller(depth=1, owner="agent:a"),
            runtime={"x": 2},
        )
        assert reg.runtime == {"x": 1}  # 에이전트의 호출은 main 의 기본값을 덮지 않는다


class TestCliAgentLines:
    def _r(self):
        from agent_cli.render.minimal import MinimalRenderer

        out = io.StringIO()
        return MinimalRenderer(Console(file=out, width=200, force_terminal=False)), out

    def test_one_line_per_tool_step_and_nothing_else(self):
        r, out = self._r()
        r.begin_agent_work(
            key="agt-3", item="r-1", profile="explorer", message="find\nall"
        )
        r.thought("thinking hard", 1)
        r.action("shell", json.dumps({"command": "rg -n parse src/"}), 1)
        r.observation("lots of output", 1, tool_name="shell", success=True)
        r.action("read_file", json.dumps({"path": "src/a.py"}), 2)
        r.observation("ERROR: nope", 2, tool_name="read_file", success=False)
        r.final("the answer", 3)
        r.end_agent_work(key="agt-3", item="r-1", success=True, duration_s=2.0)
        assert out.getvalue().splitlines() == [
            "  agt-3 │ ▶ explorer · find",
            "  agt-3 │ shell  rg -n parse src/  ✓",
            "  agt-3 │ read_file  src/a.py  ✗",
            "  agt-3 │ ✓ done (2.0s)",
        ]

    def test_main_message_and_wait_lines(self):
        """§6: main 의 `message` 는 ``✉ message → agt-3 (notice) …``, 제자리 대기는 ⏳ 한 줄."""
        r, out = self._r()
        r.action(
            "message",
            json.dumps(
                {
                    "to": "agt-3",
                    "text": "Scope change: ignore tests/",
                    "expects_reply": False,
                }
            ),
            1,
        )
        r.wait_in_place(True, [{"id": "r-21", "from": "agent:agt-3", "text": "t"}])
        r.wait_in_place(False, [])
        lines = out.getvalue().splitlines()
        assert "  ✉ message → agt-3 (notice) Scope change: ignore tests/" in lines
        assert any(
            "⏳ waiting for 1 reply (r-21) — type to steer or give new work" in ln
            for ln in lines
        )
        assert len([ln for ln in lines if "⏳" in ln]) == 1  # 깰 때는 찍지 않는다

    def test_reply_notice_line(self, monkeypatch):
        from agent_cli.main import _agent_mail_notice

        seen = []

        class R:
            def agent_mail_hint(self, *, key, kind, text):
                seen.append(text)

        monkeypatch.setattr("agent_cli.render.get_renderer", lambda: R())
        _agent_mail_notice(
            {
                "kind": "peer_message",
                "key": "agt-3",
                "output": "7 callers under src/",
                "re": "r-21",
            },
            True,
        )
        assert seen == ["↩ reply r-21 ← agt-3  7 callers under src/"]


class TestWebRunScope:
    def test_task_run_payload_names_channel_dir_and_caller(self):
        from agent_cli.render.web import WebRenderer

        w = WebRenderer()
        w.begin_agent_work(
            key="agt-1",
            item="r-4",
            profile="explorer",
            message="go",
            caller_task="agt-9#u-1",
        )
        start = next(dict(d) for e, d in w.hub.events if e == "scope_start")
        assert start["channel"] == "agt-1" and start["ctx_dir"] == "agents/agt-1"
        assert start["run"] is True and start["caller_task"] == "agt-9#u-1"
        assert w.current_task_id() == "agt-1#r-4"
        w.end_agent_work(key="agt-1", item="r-4", success=True, duration_s=0.0)
        assert w.current_task_id() == ""

    def test_other_runs_have_no_caller_card(self):
        from agent_cli.render.web import WebRenderer

        w = WebRenderer()
        w.begin_agent_work(key="agt-2", item="u-1", profile="", message="hi")
        start = next(dict(d) for e, d in w.hub.events if e == "scope_start")
        assert start["ctx_dir"] == "agents/agt-2" and start["channel"] == "agt-2"
        assert "run" not in start and "caller_task" not in start

    def test_wait_in_place_sticky(self):
        from agent_cli.render.web import WebRenderer

        w = WebRenderer()
        w.wait_in_place(True, [{"id": "r-21", "from": "agent:agt-3", "text": "t"}])
        assert w.hub.sticky_payload("waiting_in_place") == {
            "waiting": True,
            "awaited": [{"id": "r-21", "from": "agt-3"}],
        }
        w.wait_in_place(False, [])
        assert w.hub.sticky_payload("waiting_in_place") is None
