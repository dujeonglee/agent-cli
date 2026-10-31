"""`LoopKind` 기본 틀 (v10.87.0, docs/loop-kinds/DESIGN.md) — 템플릿 순서·엔진 계약·호출자.

종류 다섯의 각자 동작은 각자의 테스트(oneshot·skill·agents_live·harness·pump)가 본다.
여기는 공용 틀만: ``run()`` 이 ⑬ ctx → ⑭ 스코프 → 엔진 → ⑱ 정리 → ⑰ 결과 순서로 돌고,
거절·예외가 어디로 가는지, 엔진 호출 모양이 가짜 러너 계약과 같은지.
"""

from __future__ import annotations

import pytest

from agent_cli.loop.kind import Caller, LoopKind, LoopKindError, PrivateLoop, Start
from agent_cli.tools.result import ToolResult
from tests.loop_kinds import make_caller
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
