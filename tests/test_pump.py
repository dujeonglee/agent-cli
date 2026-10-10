"""세션 펌프 (v10.33.0, docs/pump/DESIGN.md) — run 과 web 이 같은 루프.

여기서 고정하는 것은 **루프의 계약**이다: 아이템 하나의 처리 순서, 깨우기
판정, 라우팅, 예외 정책, 수명 정책. 표면은 대역(``tests.pump_support``)이고
run 표면(``_ConsoleSurface``)은 아래 따로 본다.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

from agent_cli.input_queue import InputQueue
from agent_cli.pump import ForeverPolicy, PumpDeps, QuietPolicy, RunRequest, SessionPump
from agent_cli.tools.result import ToolResult
from tests.pump_support import (
    FakeRegistry,
    FakeSurface,
    FakeWaker,
    make_pump,
    run_pump_in_thread,
)


class _WakeWaker(FakeWaker):
    """``WAKE`` 텍스트를 깨우기로 판정 — ``verdicts`` 에서 하나씩 꺼내 준다."""

    def __init__(self, verdicts):
        super().__init__()
        self.verdicts = list(verdicts)
        self.handled: list[str] = []
        self.run_ends = 0

    def handle_dequeued(self, text):
        self.handled.append(text)
        if text != self.WAKE_TEXT:
            return None
        return self.verdicts.pop(0)

    def on_run_end(self):
        self.run_ends += 1


class TestOneItem:
    def test_order_of_surface_calls_around_a_run(self):
        q = InputQueue()
        q.enqueue(None, "hi", nickname="dj")
        surface = FakeSurface()
        reg = FakeRegistry()
        seen = []

        def run_main(req):
            seen.append(req)
            return ToolResult(True, output="ans")

        make_pump(queue=q, run_main=run_main, surface=surface, registry=reg).run()
        kinds = [c[0] for c in surface.calls]
        assert kinds == [
            "idle",
            "busy",
            "echo",
            "bind_stop",
            "route",
            "run_ended",
            "bind_stop",
        ], kinds  # 정지 판정이 루프 머리라 두 번째 idle 은 없다
        assert surface.calls[1:4] == [
            ("busy",),
            ("echo", "hi", False),
            ("bind_stop", True),
        ]
        assert surface.calls[-1] == ("bind_stop", False)  # 런 뒤 해제
        req = seen[0]
        assert req.text == "hi" and req.author == "dj" and req.author_is_user
        assert req.request_id and req.wake is False
        assert isinstance(req.stop_event, threading.Event)
        assert surface.stop_events[0] is req.stop_event  # 표면에 건 핸들 = 런의 핸들
        assert reg.authors == [["dj"]]

    def test_quiet_policy_ends_after_the_queue_drains(self):
        q = InputQueue()
        q.enqueue(None, "hi")
        done = run_pump_in_thread(make_pump(queue=q, run_main=lambda r: None))
        assert done.wait(2.0)

    def test_shutdown_ends_the_pump_even_under_forever_policy(self):
        q = InputQueue()
        pump = SessionPump(
            PumpDeps(
                queue=q,
                waker=FakeWaker(),
                agent_registry=FakeRegistry(),
                run_main=lambda r: None,
                surface=FakeSurface(),
                policy=ForeverPolicy(),
            )
        )
        done = run_pump_in_thread(pump)
        assert not done.wait(0.2), "영원 정책인데 혼자 끝났다"
        q.shutdown()
        assert done.wait(2.0)

    def test_routed_command_opens_no_run_but_still_closes_the_turn(self):
        q = InputQueue()
        q.enqueue(None, "/skill x")
        surface = FakeSurface(routed={"/skill x"})
        waker = _WakeWaker([])
        run_main = MagicMock()
        make_pump(queue=q, run_main=run_main, surface=surface, waker=waker).run()
        run_main.assert_not_called()
        assert ("route", "/skill x") in surface.calls
        assert ("bind_stop", False) in surface.calls  # finally 가 돌았다
        assert waker.run_ends == 1


class TestWake:
    def test_wake_run_has_no_author_and_is_not_a_user(self):
        q = InputQueue()
        q.enqueue(None, "WAKE", system=True)
        waker = _WakeWaker(["run"])
        surface = FakeSurface()
        reg = FakeRegistry()
        seen = []
        make_pump(
            queue=q,
            run_main=lambda r: seen.append(r),
            surface=surface,
            waker=waker,
            registry=reg,
        ).run()
        from agent_cli.pump import WAKE_AUTHOR

        assert seen[0].wake is True and seen[0].author == WAKE_AUTHOR
        assert seen[0].author_is_user is False  # 레코드가 깨우기 카드로 재생된다
        assert ("echo", "WAKE", True) in surface.calls
        assert reg.authors == [[]]
        # 깨우기 텍스트는 라우팅하지 않는다 (사람 명령이 아니다)
        assert ("route", "WAKE") not in surface.calls

    def test_stale_wake_is_skipped_without_a_run(self):
        q = InputQueue()
        q.enqueue(None, "WAKE", system=True)
        waker = _WakeWaker(["skip"])
        surface = FakeSurface()
        run_main = MagicMock()
        make_pump(queue=q, run_main=run_main, surface=surface, waker=waker).run()
        run_main.assert_not_called()
        assert ("busy",) not in surface.calls

    def test_route_consumes_an_injected_wake_through_the_waker(self):
        """런 도중 큐에서 꺼낸 깨우기 — 텍스트를 컨텍스트에 넣지 않고 waker 의
        무장을 푼다. 종전 web 은 사용자 메시지로 주입해 ``_armed`` 가 영영
        남았다(이후 깨우기 전부 드롭)."""
        waker = _WakeWaker(["run"])
        surface = FakeSurface()
        pump = make_pump(
            queue=InputQueue(), run_main=lambda r: None, surface=surface, waker=waker
        )
        assert pump.route("WAKE") is True
        assert waker.handled == ["WAKE"]
        assert surface.calls == []  # 표면 라우팅까지 가지 않는다

    def test_route_passes_human_text_to_the_surface(self):
        surface = FakeSurface(routed={"/x"})
        pump = make_pump(queue=InputQueue(), run_main=lambda r: None, surface=surface)
        assert pump.route("/x") is True
        assert pump.route("plain") is False
        assert [c for c in surface.calls if c[0] == "route"] == [
            ("route", "/x"),
            ("route", "plain"),
        ]


class TestFailurePolicy:
    def test_run_failed_false_propagates(self):
        q = InputQueue()
        q.enqueue(None, "hi")
        surface = FakeSurface(swallow_errors=False)

        def boom(req):
            raise RuntimeError("x")

        pump = make_pump(queue=q, run_main=boom, surface=surface)
        try:
            pump.run()
        except RuntimeError:
            pass
        else:
            raise AssertionError("run 표면은 예외를 전파해야 한다")
        assert ("bind_stop", False) in surface.calls  # finally 는 돌았다

    def test_run_failed_true_continues_with_the_next_item(self):
        q = InputQueue()
        q.enqueue(None, "bad")
        q.enqueue(None, "good")
        surface = FakeSurface(swallow_errors=True)
        seen = []

        def run_main(req):
            if req.text == "bad":
                raise RuntimeError("x")
            seen.append(req.text)
            return ToolResult(True, output="ok")

        make_pump(queue=q, run_main=run_main, surface=surface).run()
        assert seen == ["good"]
        assert any(c[0] == "run_failed" for c in surface.calls)


class TestQuietPolicy:
    def _policy(
        self, *, registry_busy=False, monitors=None, schedules=None, waits=None
    ):
        q = InputQueue()
        reg = MagicMock(has_active_work=MagicMock(return_value=registry_busy))
        return (
            QuietPolicy(
                queue=q,
                agent_registry=reg,
                monitors=monitors,
                schedules=schedules,
                on_schedule_wait=(lambda: waits.append(1))
                if waits is not None
                else None,
            ),
            q,
        )

    def test_stops_when_everything_is_quiet(self):
        policy, _ = self._policy()
        assert policy.should_stop()

    def test_each_live_term_holds(self):
        live = MagicMock(has_active_work=MagicMock(return_value=True))
        assert not self._policy(registry_busy=True)[0].should_stop()
        assert not self._policy(monitors=live)[0].should_stop()
        assert not self._policy(schedules=live)[0].should_stop()
        policy, q = self._policy()
        q.enqueue(None, "x")
        assert not policy.should_stop()

    def test_schedule_wait_is_announced_once_and_resets(self):
        sched = MagicMock(has_active_work=MagicMock(return_value=True))
        waits: list[int] = []
        policy, _ = self._policy(schedules=sched, waits=waits)
        assert not policy.should_stop() and not policy.should_stop()
        assert waits == [1], "기다리기 시작할 때 한 번만 알린다"
        sched.has_active_work.return_value = False
        assert policy.should_stop()
        sched.has_active_work.return_value = True
        assert not policy.should_stop()
        assert waits == [1, 1], "예약이 사라졌다 다시 생기면 다시 알린다"

    def test_poll_secs_is_bounded_and_forever_has_none(self):
        assert self._policy()[0].poll_secs == 0.5
        assert ForeverPolicy().poll_secs is None


class TestConsoleSurface:
    def _surface(self, tmp_path, dispatch=None):
        from agent_cli.main import _ConsoleSurface

        out = tmp_path / "r.txt"
        calls = []

        def _dispatch(text, stop):
            calls.append((text, stop))
            return text.startswith(("/", "@"))

        s = _ConsoleSurface(result_file=str(out), dispatch=dispatch or _dispatch)
        return s, out, calls

    def test_sh_is_not_routed_but_commands_are_with_the_bound_stop(self, tmp_path):
        s, _, calls = self._surface(tmp_path)
        ev = threading.Event()
        s.bind_stop(ev)
        assert s.route("/sh ls") is False and calls == []
        assert s.route("/skill a") is True and calls == [("/skill a", ev)]
        assert s.route("@agent t") is True
        assert s.route("plain words") is False  # 디스패치가 False 를 돌려준다

    def test_run_ended_writes_only_a_success(self, tmp_path):
        s, out, _ = self._surface(tmp_path)
        s.run_ended(ToolResult(False, error="boom"))
        assert not out.exists()
        s.run_ended(ToolResult(True, output="ans"))
        assert out.read_text(encoding="utf-8") == "ans"

    def test_errors_propagate_and_ctrl_c_is_the_interrupt(self, tmp_path):
        s, _, _ = self._surface(tmp_path)
        assert s.run_failed(RuntimeError("x")) is False
        assert s.graceful_interrupt is False

    def test_agent_result_file_follows_the_ok_flag(self, tmp_path):
        """``@agent task`` 의 답은 run 의 전용 분기 대신 콘솔 디스패치 어댑터가
        기록한다 — 실패 텍스트는 성공 답변처럼 기록하지 않는다(5.6.0 계약)."""
        from agent_cli.main import _ConsoleDispatchOutput

        out = tmp_path / "a.txt"
        o = _ConsoleDispatchOutput(result_file=str(out))
        o.agent_result("failed body", ok=False)
        assert not out.exists()
        o.agent_result(None, ok=True)
        assert not out.exists()
        o.agent_result("the answer", ok=True)
        assert out.read_text(encoding="utf-8") == "the answer"


class TestRunRequest:
    def test_is_frozen_and_keyword_only(self):
        import dataclasses

        import pytest

        req = RunRequest(
            text="t",
            author=None,
            author_is_user=True,
            request_id="",
            stop_event=threading.Event(),
            wake=False,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            req.text = "u"  # type: ignore[misc]
