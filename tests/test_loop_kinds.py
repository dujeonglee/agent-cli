"""`LoopKind` 기본 틀 (v10.87.0, docs/loop-kinds/DESIGN.md) — 템플릿 순서·엔진 계약·호출자.

종류 다섯의 각자 동작은 각자의 테스트(oneshot·skill·agents_live·harness·pump)가 본다.
여기는 공용 틀만: ``run()`` 이 ⑬ ctx → ⑭ 스코프 → 엔진 → ⑱ 정리 → ⑰ 결과 순서로 돌고,
거절·예외가 어디로 가는지, 엔진 호출 모양이 가짜 러너 계약과 같은지.
"""

from __future__ import annotations

import pytest

from agent_cli.loop.kind import Caller, LoopKind, LoopKindError, PrivateLoop, Start
from agent_cli.tools.result import ToolResult
from tests.loop_kinds import make_caller, make_kind
from tests.loop_ports import make_ports


class _Trace(LoopKind):
    """순서를 기록하는 종류."""

    def __init__(self, *, reject=False, boom=False, engine=None):
        self.owner = "main"
        self.ports = make_ports()
        self.log: list[str] = []
        self._reject, self._boom = reject, boom
        self.engine = engine

    def start(self):
        return Start(query="q")

    def make_ctx(self, start):
        self.log.append("ctx")
        if self._reject:
            raise LoopKindError("no ctx")
        return "CTX"

    def open_scope(self, start):
        self.log.append("open")
        return "H"

    def close_scope(self, handle, finished, secs):
        self.log.append(f"close:{handle}:{finished!r}")

    def finish(self, result, secs, released):
        self.log.append(f"finish:{released}")
        if self._boom:
            raise RuntimeError("boom")
        return ("done", result)

    def release(self):
        self.log.append("release")
        return "R"

    def rejected(self, reason):
        self.log.append(f"rejected:{reason}")
        return ("rejected", reason)

    def crashed(self, exc, secs):
        self.log.append(f"crashed:{exc}")
        return ("crashed", str(exc))


def _engine(query, ctx, **kw):
    return ToolResult(True, output=f"{query}@{ctx}"), 0.01


class TestTemplateOrder:
    def test_happy_path_runs_in_order_and_closes_with_the_finished_value(self):
        k = _Trace(engine=_engine)
        out = k.run()
        assert out[0] == "done" and out[1].output == "q@CTX"
        assert k.log == ["open", "ctx", "release", "finish:R", f"close:H:{out!r}"]

    def test_rejected_ctx_skips_engine_and_release_but_closes_the_scope(self):
        k = _Trace(reject=True, engine=_engine)
        out = k.run()
        assert out == ("rejected", "no ctx")
        assert k.log == ["open", "ctx", "rejected:no ctx", f"close:H:{out!r}"]

    def test_engine_exception_still_releases_then_goes_to_crashed(self):
        def bad(query, ctx, **kw):
            raise ValueError("engine died")

        k = _Trace(engine=bad)
        out = k.run()
        assert out == ("crashed", "engine died")
        assert k.log == [
            "open",
            "ctx",
            "release",
            "crashed:engine died",
            f"close:H:{out!r}",
        ]

    def test_finish_exception_goes_to_crashed(self):
        k = _Trace(boom=True, engine=_engine)
        assert k.run() == ("crashed", "boom")

    def test_defaults_reraise(self):
        class Bare(LoopKind):
            owner = "main"

            def __init__(self):
                self.ports = make_ports()

            def make_ctx(self, start):
                raise LoopKindError("x")

        with pytest.raises(LoopKindError):
            Bare().run(Start(query="q"))


class TestEngineContract:
    def test_default_engine_is_run_loop_read_at_call_time(self, monkeypatch):
        """`patch("agent_cli.loop.run_loop")` 하나가 다섯 종류 전부에 닿는다."""
        seen = {}

        def fake(**kw):
            seen.update(kw)
            return ToolResult(True, output="ok")

        monkeypatch.setattr("agent_cli.loop.run_loop", fake)
        k = _Trace()
        k.depth, k.max_depth, k.tools, k.agent_name = 1, 3, ["shell"], "explorer"
        stop = object()
        k.stop_event = stop
        k.run()
        assert seen["query"] == "q" and seen["ctx"] == "CTX"
        assert seen["depth"] == 1 and seen["max_depth"] == 3
        assert seen["active_tools"] == ["shell"] and seen["agent_name"] == "explorer"
        assert seen["stop_event"] is stop and seen["verbose"] is False
        assert seen["ports"] is k.ports

    def test_start_stop_event_wins_over_the_kinds(self):
        k = _Trace()
        k.stop_event = "kind"
        assert k.loop_kwargs(Start(query="q", stop_event="run"))["stop_event"] == "run"
        assert k.loop_kwargs(Start(query="q"))["stop_event"] == "kind"

    def test_loop_kwargs_cover_every_run_loop_parameter_but_query_and_ctx(self):
        import inspect

        from agent_cli.loop import run_loop

        params = set(inspect.signature(run_loop).parameters)
        given = set(_Trace().loop_kwargs(Start(query="q"))) | {"query", "ctx"}
        # 바인딩 없이 부르는 쪽의 부품(provider/capabilities/model/…/dialect)만 남는다
        leftovers = params - given
        assert leftovers <= {
            "peer_agents_section",  # 맨 종류만 — 종류는 `prompt_profile` 로 말한다
            "provider",
            "capabilities",
            "model",
            "provider_name",
            "base_url",
            "api_key",
            "dialect",
        }, leftovers


class TestCaller:
    def test_from_loop_reads_the_running_loops_config(self):
        from agent_cli.loop import LoopConfig, LoopState

        cfg = LoopConfig(owner="agent:k", depth=1, max_depth=4, tools_list=["shell"])
        state = LoopState()
        c = Caller.from_loop(cfg, state, ctx="CTX")
        assert c.owner == "agent:k" and c.depth == 1 and c.max_depth == 4
        assert c.tools == ["shell"] and c.ctx == "CTX"
        assert c.stop_event is state.stop_event

    def test_main_session_is_depth_zero_with_every_tool(self):
        c = Caller.main_session(binding=None, ctx=None, session=None)
        assert c.owner == "main" and c.depth == 0 and c.tools is None

    def test_make_caller_is_a_main_caller(self):
        c = make_caller(depth=2)
        assert c.owner == "main" and c.depth == 2


class TestPrivateLoop:
    def test_private_ports_absorb_their_own_mailbox_and_release_the_owner(
        self, monkeypatch
    ):
        from agent_cli.subagent.agents_live import AgentRegistry, set_main_registry

        reg = AgentRegistry(None)
        set_main_registry(reg)
        try:
            k = PrivateLoop("run:t9")
            assert k.ports.owner == "run:t9" and k.ports.wakeable()
            reg.deliver(
                "run:t9",
                mail={"kind": "job", "output": "job #1 finished", "success": True},
                text="job #1 finished",
                author="harness",
                expects_reply=False,
            )
            assert [m["record"]["tool"] for m in k.ports.absorb_inbox()] == ["job"]
            reg.deliver(
                "run:t9",
                mail={"kind": "job", "output": "late", "success": True},
                text="late",
                author="harness",
                expects_reply=False,
            )
            k.release()
            assert reg.take_mail("run:t9") == []  # 메일함이 버려졌다
        finally:
            set_main_registry(None)


# ── PR 2 (v10.88.0): 엔진이 종류에게 묻는다 ─────────────────────────────────


class TestKindsStateTheirPolicy:
    """다섯 종류의 ⑯ wait 정책·⑮ 흡수·⑨ 윤곽 — 명시된 값. 맨 종류(`make_kind`)는 포트·깊이에서
    같은 답을 유도한다(종전 엔진의 플래그 추측과 같은 규칙, 한 곳에)."""

    def _kinds(self, tmp_path):
        from agent_cli.context.manager import ContextManager
        from agent_cli.harness.jobs import HarnessLoop
        from agent_cli.runtime import MainLoop
        from agent_cli.skills.executor import SkillLoop
        from agent_cli.skills.models import Skill
        from agent_cli.subagent.oneshot import OneShotLoop
        from tests.binding_support import make_binding

        reg = object()
        main = MainLoop(
            binding=make_binding(model="m"),
            ctx=None,
            session=None,
            agent_registry=None,
            mcp_manager=None,
            dequeue_user_message=lambda: None,
            route_message=lambda t: False,
            max_turns=0,
            max_depth=2,
            verbose=False,
            record_turns=True,
            hooks_config=None,
            graceful_interrupt=False,
        )
        caller = make_caller(binding=make_binding(model="m"), agent_registry=reg)
        skill = SkillLoop(
            Skill(name="s", description="", prompt_template="x"), "", caller
        )
        one = OneShotLoop({"task": "t"}, caller)
        harness = HarnessLoop(
            "auditor",
            "1-2",
            "t",
            "body",
            {},
            runtime={"binding": make_binding(model="m")},
            session_ctx=ContextManager(tmp_path / "s", max_context_tokens=1000),
            stop_event=None,
        )
        return main, skill, one, harness, reg

    def test_wait_policy(self, tmp_path):
        from agent_cli.loop.kind import WAIT_BLOCK, WAIT_END_RUN, WAIT_REFUSE

        main, skill, one, harness, _ = self._kinds(tmp_path)
        assert main.wait_policy() == WAIT_END_RUN
        assert skill.wait_policy() == WAIT_REFUSE  # 호출자의 턴 안 인라인
        assert one.wait_policy() == WAIT_BLOCK and harness.wait_policy() == WAIT_BLOCK
        # 맨 종류: 깨울 길 없음 → 거부, private 주소 → 제자리, 그 외 → 런 종료
        assert make_kind().wait_policy() == WAIT_REFUSE
        assert (
            make_kind(ports=make_ports(dequeue_user_message=lambda: None)).wait_policy()
            == WAIT_END_RUN
        )
        assert (
            make_kind(ports=make_ports(owner="run:x", absorb_inbox=list)).wait_policy()
            == WAIT_BLOCK
        )

    def test_skill_does_not_absorb_the_callers_mail(self, tmp_path):
        """D1: main 아래 스킬은 main 레지스트리의 회신을 흡수하지 않는다 — 스킬엔 `reply` 가 없고
        빚이 main 이름으로 적혔다. 종전엔 `agent_registry` 만 보고 흡수했다."""

        class _Reg:
            def drain_replies(self):
                return [{"key": "agt-1", "output": "late reply"}]

        caller = make_caller(agent_registry=_Reg())
        from agent_cli.skills.executor import SkillLoop
        from agent_cli.skills.models import Skill

        skill = SkillLoop(
            Skill(name="s", description="", prompt_template="x"), "", caller
        )
        assert skill.intake() == []
        # 같은 포트의 맨 종류·main 은 받는다
        bare = make_kind(ports=make_ports(agent_registry=_Reg()))
        assert [i["mail"]["output"] for i in bare.intake()] == ["late reply"]

    def test_prompt_profile(self, tmp_path):
        main, skill, one, harness, reg = self._kinds(tmp_path)
        p = main.prompt_profile()
        assert p.role == "" and p.ask_blocking and not p.reports_to_caller
        s = skill.prompt_profile()
        assert (
            s.agent_registry is reg and s.reports_to_caller
        )  # main 의 일 — `agent` 전부
        o = one.prompt_profile()
        assert o.agent_registry is None and o.ask_blocking and not o.peer_channel
        assert harness.prompt_profile().role == "body"
        assert skill.renders() is False and one.renders() and main.renders()
        assert main.tracks_run_authors and not skill.tracks_run_authors


class TestEngineAsksTheKind:
    def test_no_kind_sniffing_left_in_the_engine(self):
        """엔진(`loop/*.py`)에 종류 추측이 남지 않았다 — 종전 여덟 자리의 표현을 소스에서 금지."""
        import pathlib

        banned = [
            "cfg.wakeable",
            "is_private_owner(self.cfg.owner)",
            "dequeue_user_message is None",
            "dequeue_user_message is not None",
            "message_handler is not None",
            ".peer_roster",
            "not self.skill_name",
            "render=not self",
            "questions.nonblocking",
            "self.cfg.depth > 0",
        ]
        hits = []
        for f in pathlib.Path("agent_cli/loop").glob("*.py"):
            if f.name in ("kind.py", "ports.py"):  # 종류와 포트 자신은 자기 상태를 본다
                continue
            src = f.read_text()
            for b in banned:
                if b in src:
                    hits.append(f"{f.name}: {b}")
        assert hits == [], hits

    def test_intake_dispatches_each_item_shape(self, tmp_path):
        """종류가 돌려준 항목을 엔진이 모양대로 넣는다 — message 는 조종 메시지, record 는 관찰,
        user_request 는 장부에."""
        from types import SimpleNamespace

        from agent_cli.loop.core import AgentLoop
        from agent_cli.loop.ledger import Ledger

        class _Kind:
            tracks_run_authors = False

            def intake(self):
                return [
                    {"message": {"text": "steer", "nickname": "dj", "id": "u-1"}},
                    {
                        "record": {
                            "role": "user",
                            "tool": "job",
                            "content": "job #1 done",
                        }
                    },
                    {"user_request": {"id": "u-2", "author": "user:dj", "text": "do"}},
                ]

        loop = AgentLoop.__new__(AgentLoop)
        loop._state = SimpleNamespace(ledger=Ledger(), turn=0)
        loop._config = SimpleNamespace(kind=_Kind(), agent_registry=None)
        loop.ctx = None
        loop.messages = []
        loop.task_log = []
        loop.run_authors = []
        loop.route_message = None
        loop.turn = 0
        loop._intake()
        assert [m["content"] for m in loop.messages] == [
            "[dj]: steer",
            "job #1 done",
            "[user:dj]: do",
        ]
        assert sorted(loop._state.ledger.request_ids()) == ["u-1", "u-2"]

    def test_loop_config_without_a_kind_gets_a_bare_one(self):
        from agent_cli.loop import LoopConfig
        from agent_cli.loop.kind import WAIT_REFUSE, LoopKind

        cfg = LoopConfig(owner="agent:q", depth=1, tools_list=["shell"])
        assert isinstance(cfg.kind, LoopKind) and cfg.kind.owner == "agent:q"
        assert (
            cfg.kind.ports.owner == "agent:q" and cfg.kind.wait_policy() == WAIT_REFUSE
        )
        assert cfg.kind.prompt_profile().reports_to_caller
