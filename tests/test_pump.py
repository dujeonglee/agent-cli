"""세션 펌프 (v10.33.0, docs/pump/DESIGN.md) — run 과 web 이 같은 루프.

여기서 고정하는 것은 **루프의 계약**이다: 아이템 하나의 처리 순서, 메일 런,
라우팅, 예외 정책, 수명 정책. 표면은 대역(``tests.pump_support``)이고
run 표면(``_ConsoleSurface``)은 아래 따로 본다.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from agent_cli.loop.kind import Start
from agent_cli.main_input import MainInput
from agent_cli.post import envelope as post_envelope
from agent_cli.pump import ForeverPolicy, QuietPolicy
from agent_cli.runtime import MainLoop
from agent_cli.tools.result import ToolResult
from tests.pump_support import (
    FakeRegistry,
    FakeSurface,
    make_pump,
    run_pump_in_thread,
)


def _opened(item: dict) -> Start:
    """펌프가 그 사람의 말로 연 차례의 시작 — 표면의 `route` 가 argv 질의였는지를 읽는다."""
    return MainLoop.start({"nickname": None, **item}, threading.Event())


def _mail(mi: MainInput, *, wakes: bool = True) -> None:
    """main 상자에 메일 한 통 — 작업 통지(깨움) 또는 깨우지 않는 회신."""
    env = (
        post_envelope.notice("n-900", "job", "main", "done")
        if wakes
        else post_envelope.reply("n-900", "agent:a", "main", "x", re="r-1", wakes=False)
    )
    mi.post.deliver(env)


class TestOneItem:
    def test_the_run_end_reaches_the_registry_with_its_waiting_flag(self):
        """v10.77.0: main 런의 끝도 레지스트리의 `run_ended` 한 곳으로 — `wait` 로
        끝났는지는 펌프가 판정하지 않고 넘긴다(상주와 같은 판정 자리)."""
        q = MainInput()
        q.enqueue(None, "a", nickname="dj")
        q.enqueue(None, "b", nickname="dj")
        reg = FakeRegistry()
        outs = iter(
            [ToolResult(True, output="x", waiting=True), ToolResult(True, output="y")]
        )

        make_pump(main_input=q, run_main=lambda req: next(outs), registry=reg).run()
        assert reg.ended == [("main", "x", True), ("main", "y", False)]

    def test_order_of_surface_calls_around_a_run(self):
        q = MainInput()
        q.enqueue(None, "hi", nickname="dj")
        surface = FakeSurface()
        reg = FakeRegistry()
        seen = []

        def run_main(req):
            seen.append(req)
            return ToolResult(True, output="ans")

        make_pump(main_input=q, run_main=run_main, surface=surface, registry=reg).run()
        kinds = [c[0] for c in surface.calls]
        assert kinds == [
            "idle",
            "busy",
            "echo",
            "bind_stop",
            "route",
            "run_starting",
            "run_ended",
            "bind_stop",
        ], kinds  # 정지 판정이 루프 머리라 두 번째 idle 은 없다
        assert (
            "run_starting",
            "hi",
        ) in surface.calls  # 런 직전, 그 요청으로 (v10.130.6)
        assert surface.calls[1:4] == [
            ("busy",),
            ("echo", "hi"),
            ("bind_stop", True),
        ]
        assert surface.calls[-1] == ("bind_stop", False)  # 런 뒤 해제
        start = seen[0]
        assert start.query == "hi" and start.author == "dj" and start.author_is_user
        assert start.request_id
        assert isinstance(start.stop_event, threading.Event)
        assert surface.stop_events[0] is start.stop_event  # 표면에 건 핸들 = 런의 핸들
        assert reg.authors == [["dj"]]

    def test_quiet_policy_ends_after_the_queue_drains(self):
        q = MainInput()
        q.enqueue(None, "hi")
        done = run_pump_in_thread(make_pump(main_input=q, run_main=lambda r: None))
        assert done.wait(2.0)

    def test_shutdown_ends_the_pump_even_under_forever_policy(self):
        q = MainInput()
        pump = make_pump(main_input=q, run_main=lambda r: None, policy=ForeverPolicy())
        done = run_pump_in_thread(pump)
        assert not done.wait(0.2), "영원 정책인데 혼자 끝났다"
        q.close()
        assert done.wait(2.0)

    def test_routed_command_opens_no_run_but_still_closes_the_turn(self):
        q = MainInput()
        q.enqueue(None, "/skill x")
        surface = FakeSurface(routed={"/skill x"})
        run_main = MagicMock()
        make_pump(main_input=q, run_main=run_main, surface=surface).run()
        run_main.assert_not_called()
        assert ("route", "/skill x") in surface.calls
        assert ("bind_stop", False) in surface.calls  # finally 가 돌았다


class TestMailRun:
    def test_a_waking_mail_opens_a_run_with_no_author_that_is_not_a_user(self):
        mi = MainInput()
        _mail(mi)
        surface = FakeSurface()
        reg = FakeRegistry()
        seen = []

        def run_main(req):
            seen.append(req)
            mi.post.absorb("main", human_first=True)  # 그 런의 첫 턴 경계가 하는 일
            mi.close()

        make_pump(
            main_input=mi,
            run_main=run_main,
            surface=surface,
            registry=reg,
            policy=ForeverPolicy(),
        ).run()
        assert seen[0].author is None and seen[0].author_is_user is False
        assert seen[0].request_id == "" and seen[0].query == ""  # 시작 메시지 없음
        assert not [c for c in surface.calls if c[0] == "echo"]  # 말풍선 없음
        assert reg.authors == [[]]
        # 메일 런의 글은 라우팅하지 않는다 (사람 명령이 아니다)
        assert not [c for c in surface.calls if c[0] == "route"]

    def test_words_open_before_the_mail_that_came_first(self):
        mi = MainInput()
        _mail(mi)
        mi.enqueue(None, "hi", nickname="dj")
        seen = []

        def run_main(req):
            seen.append(not req.author_is_user)
            if not req.author_is_user:
                mi.close()

        make_pump(main_input=mi, run_main=run_main, policy=ForeverPolicy()).run()
        assert seen == [False, True]

    def test_a_mail_that_does_not_wake_opens_no_run(self):
        mi = MainInput()
        _mail(mi, wakes=False)
        mi.close()
        surface = FakeSurface()
        run_main = MagicMock()
        make_pump(
            main_input=mi, run_main=run_main, surface=surface, policy=ForeverPolicy()
        ).run()
        run_main.assert_not_called()
        assert ("busy",) not in surface.calls

    def test_route_passes_human_text_to_the_surface(self):
        surface = FakeSurface(routed={"/x"})
        pump = make_pump(
            main_input=MainInput(), run_main=lambda r: None, surface=surface
        )
        assert pump.route("/x") is True
        assert pump.route("plain") is False
        assert [c for c in surface.calls if c[0] == "route"] == [
            ("route", "/x"),
            ("route", "plain"),
        ]


class TestFailurePolicy:
    def test_run_failed_false_propagates(self):
        q = MainInput()
        q.enqueue(None, "hi")
        surface = FakeSurface(swallow_errors=False)

        def boom(req):
            raise RuntimeError("x")

        pump = make_pump(main_input=q, run_main=boom, surface=surface)
        try:
            pump.run()
        except RuntimeError:
            pass
        else:
            raise AssertionError("run 표면은 예외를 전파해야 한다")
        assert ("bind_stop", False) in surface.calls  # finally 는 돌았다

    def test_run_starting_is_inside_the_runs_try(self):
        """v10.130.6: 런 시작 알림은 ``run_main`` 바로 앞, 같은 try 안 — 표면의 예외도
        런 예외처럼 ``run_failed`` 로 가고, 런은 열리지 않는다(종전 web 의 비전
        알림이 ``_run_main`` 안에서 돌던 자리와 같다). 예외로 끝난 차례도 끝 정리를 한다(v10.156.0 — 실패한
        런처럼, `run_failed` 뒤에)."""
        q = MainInput()
        q.enqueue(None, "hi")
        surface = FakeSurface(swallow_errors=True, fail_on_start=True)
        run_main = MagicMock()
        make_pump(main_input=q, run_main=run_main, surface=surface).run()
        run_main.assert_not_called()
        kinds = [c[0] for c in surface.calls]
        assert (
            kinds.index("run_starting")
            < kinds.index("run_failed")
            < kinds.index("run_ended")
        )
        assert surface.calls[-1] == ("bind_stop", False)

    def test_a_route_that_raises_goes_to_run_failed_and_the_pump_goes_on(self):
        """명령 라우팅의 예외도 런의 예외처럼 ``run_failed`` 로 — web 은 화면에 알리고 다음 말을 받는다(워커가 죽으면
        대기열만 쌓인다), 런은 열리지 않는다."""
        q = MainInput()
        q.enqueue(None, "/bad x")
        q.enqueue(None, "good")
        surface = FakeSurface(swallow_errors=True, fail_on_route={"/bad x"})
        seen = []
        make_pump(
            main_input=q, run_main=lambda s: seen.append(s.query), surface=surface
        ).run()
        assert seen == ["good"]
        kinds = [c[0] for c in surface.calls]
        assert kinds.index("route") < kinds.index("run_failed")
        assert "run_starting" in kinds[kinds.index("run_failed") :]
        assert surface.calls.count(("bind_stop", False)) == 2  # 두 차례 다 해제

    def test_a_route_that_raises_propagates_on_the_run_surface(self):
        q = MainInput()
        q.enqueue(None, "/bad x")
        surface = FakeSurface(swallow_errors=False, fail_on_route={"/bad x"})
        with pytest.raises(RuntimeError, match="route failed"):
            make_pump(main_input=q, run_main=MagicMock(), surface=surface).run()
        assert [c[0] for c in surface.calls].count("run_failed") == 1
        assert surface.calls[-1] == ("bind_stop", False)

    def test_routed_command_does_not_announce_a_run(self):
        q = MainInput()
        q.enqueue(None, "/skill x")
        surface = FakeSurface(routed={"/skill x"})
        make_pump(main_input=q, run_main=MagicMock(), surface=surface).run()
        assert "run_starting" not in [c[0] for c in surface.calls]

    def test_run_failed_true_continues_with_the_next_item(self):
        q = MainInput()
        q.enqueue(None, "bad")
        q.enqueue(None, "good")
        surface = FakeSurface(swallow_errors=True)
        seen = []

        def run_main(req):
            if req.query == "bad":
                raise RuntimeError("x")
            seen.append(req.query)
            return ToolResult(True, output="ok")

        make_pump(main_input=q, run_main=run_main, surface=surface).run()
        assert seen == ["good"]
        assert any(c[0] == "run_failed" for c in surface.calls)


class TestQuietPolicy:
    def _policy(
        self, *, registry_busy=False, monitors=None, schedules=None, waits=None
    ):
        q = MainInput()
        reg = MagicMock(has_agent_work=MagicMock(return_value=registry_busy))
        return (
            QuietPolicy(
                main_input=q,
                agent_registry=reg,
                monitors=monitors,
                schedules=schedules,
                on_wait=(lambda kind: waits.append(kind))
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
        assert waits == ["schedules"], "기다리기 시작할 때 한 번만 알린다"
        sched.has_active_work.return_value = False
        assert policy.should_stop()
        sched.has_active_work.return_value = True
        assert not policy.should_stop()
        assert waits == ["schedules", "schedules"], (
            "예약이 사라졌다 다시 생기면 다시 알린다"
        )

    def test_poll_secs_is_bounded_and_forever_has_none(self):
        assert self._policy()[0].poll_secs == 0.5
        assert ForeverPolicy().poll_secs is None


class TestConsoleSurface:
    def _surface(self, tmp_path, dispatch=None):
        from agent_cli.main import _ConsoleSurface

        out = tmp_path / "r.txt"
        calls = []

        def _dispatch(text, stop, item=None):
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

    def test_run_starting_changes_nothing(self, tmp_path):
        """run 은 런 시작에 할 일이 없다 (v10.130.6) — 이미지 검사는 부트가 했다."""
        s, out, calls = self._surface(tmp_path)
        before = dict(vars(s))
        s.run_starting(self._req())
        assert vars(s) == before and calls == [] and not out.exists()

    def _req(self, *, wake=False, author=None):
        """런의 시작 — ``wake`` 는 메일이 연 런(사람의 요청이 아니다), ``author`` 가 없으면 argv 질의."""
        return Start(
            query="" if wake else "t",
            author=None if wake else author,
            author_is_user=not wake,
            stop_event=threading.Event(),
        )

    def test_run_ended_writes_only_a_successful_query_run(self, tmp_path):
        s, out, _ = self._surface(tmp_path)
        s.run_ended(ToolResult(False, error="boom"), self._req())
        assert not out.exists()
        s.run_ended(ToolResult(True, output="later"), self._req(wake=True))
        assert not out.exists(), "깨우기 런의 답은 결과 파일이 아니다"
        s.run_ended(ToolResult(True, output="sched"), self._req(author="⏰ nightly"))
        assert not out.exists(), "예약 런의 답도 아니다"
        s.run_ended(ToolResult(True, output="ans"), self._req())
        assert out.read_text(encoding="utf-8") == "ans"
        s.run_ended(ToolResult(True, output="again"), self._req(wake=True))
        assert out.read_text(encoding="utf-8") == "ans"

    def test_routed_skill_answer_lands_in_the_file(self, tmp_path):
        """`/skill` 의 답도 결과 파일로 (v10.34.1) — v10.124.0 부터 어댑터가 아니라
        표면이 쓴다(종료 코드와 같은 자리). 실패한 스킬의 오류문은 쓰지 않는다."""
        from agent_cli.main import _ConsoleDispatchOutput

        def dispatch(text, stop, item=None):
            o = _ConsoleDispatchOutput(report=s.routed)
            o.skill_result(
                "review",
                "boom" if "fail" in text else "looks good",
                ok="fail" not in text,
            )
            return True

        s, out, _ = self._surface(tmp_path, dispatch=dispatch)
        assert s.route("/review fail", _opened({"text": "/review fail"})) is True
        assert not out.exists() and s.exit_code == 1
        s.route("/review", _opened({"text": "/review"}))
        assert out.read_text(encoding="utf-8") == "looks good"
        assert s.exit_code == 0

    def test_errors_propagate_and_ctrl_c_is_the_interrupt(self, tmp_path):
        s, _, _ = self._surface(tmp_path)
        assert s.run_failed(RuntimeError("x")) is False
        assert s.graceful_interrupt is False

    def test_agent_result_file_follows_the_ok_flag(self, tmp_path):
        """``@agent task`` 의 답은 run 의 전용 분기 대신 라우팅이 기록한다 — 실패
        텍스트는 성공 답변처럼 기록하지 않는다(5.6.0 계약)."""
        from agent_cli.main import _ConsoleDispatchOutput

        results = iter([("failed body", False), (None, True), ("the answer", True)])

        def dispatch(text, stop, item=None):
            body, ok = next(results)
            _ConsoleDispatchOutput(report=s.routed).agent_result(body, ok=ok)
            return True

        s, out, _ = self._surface(tmp_path, dispatch=dispatch)
        for code, written in ((1, False), (0, False), (0, True)):
            s.route("@a t", _opened({"text": "@a t"}))
            assert s.exit_code == code and out.exists() is written
        assert out.read_text(encoding="utf-8") == "the answer"


class TestConsoleExitCode:
    """``run`` 의 종료 코드 (v10.124.0) — 결과 파일과 같은 판정. 0 답 · 1 실패/무답."""

    def _surface(self, tmp_path, dispatch=None):
        from agent_cli.main import _ConsoleSurface

        out = tmp_path / "r.txt"
        return _ConsoleSurface(
            result_file=str(out), dispatch=dispatch or (lambda t, e: False)
        ), out

    _req = TestConsoleSurface._req

    @staticmethod
    def _res(ok, out="", *, waiting=False):
        from types import SimpleNamespace

        return SimpleNamespace(success=ok, output=out, waiting=waiting)

    def test_no_verdict_is_no_answer(self, tmp_path):
        s, _ = self._surface(tmp_path)
        assert s.exit_code == 1

    def test_query_run_success_and_failure(self, tmp_path):
        s, _ = self._surface(tmp_path)
        s.run_ended(self._res(False, "max turns"), self._req())
        assert s.exit_code == 1
        s, _ = self._surface(tmp_path)
        s.run_ended(self._res(True, "ans"), self._req())
        assert s.exit_code == 0

    def test_empty_successful_answer_is_0_without_a_file(self, tmp_path):
        """README 의 "실패/무답" 구별이 이제 실제로 된다 — 빈 답 성공은 0, 파일 없음."""
        s, out = self._surface(tmp_path)
        s.run_ended(self._res(True, ""), self._req())
        assert s.exit_code == 0 and not out.exists()

    def test_runs_outside_the_chain_do_not_change_the_verdict(self, tmp_path):
        s, _ = self._surface(tmp_path)
        s.run_ended(self._res(False, "boom"), self._req())
        s.run_ended(self._res(True, "rescued"), self._req(wake=True))
        assert s.exit_code == 1, (
            "닫힌 사슬 뒤의 깨우기가 실패를 성공으로 바꾸면 안 된다"
        )
        s.run_ended(self._res(True, "sched"), self._req(author="⏰ nightly"))
        assert s.exit_code == 1, "예약 런도 판정 밖"
        s, _ = self._surface(tmp_path)
        s.run_ended(self._res(True, "ans"), self._req())
        s.run_ended(self._res(False, "sched boom"), self._req(author="⏰ nightly"))
        s.run_ended(self._res(False, "wake boom"), self._req(wake=True))
        assert s.exit_code == 0

    def test_the_last_run_of_a_waiting_chain_decides(self, tmp_path):
        """질의가 `wait` 로 답을 미루면 깨우기 런이 사슬이다 — 마지막 판정이 코드."""
        s, out = self._surface(tmp_path)
        s.run_ended(self._res(True, "waiting on job", waiting=True), self._req())
        assert s.exit_code == 0
        s.run_ended(self._res(False, "llm error"), self._req(wake=True))
        assert s.exit_code == 1
        assert out.read_text(encoding="utf-8") == "waiting on job"
        s.run_ended(self._res(True, "final"), self._req(wake=True))
        assert s.exit_code == 0
        assert out.read_text(encoding="utf-8") == "final"
        s.run_ended(self._res(False, "after close"), self._req(wake=True))
        assert s.exit_code == 0, "사슬이 닫힌 뒤의 깨우기는 판정 밖"

    def test_only_the_argv_query_route_decides(self, tmp_path):
        """라우팅: 질의였던 명령만 판정 — 런 도중 주입·예약 발화의 명령은 아니다."""
        from agent_cli.main import _ConsoleDispatchOutput

        verdicts = {"/ok": ("done", True), "/bad": (None, False)}

        def dispatch(text, stop, item=None):
            answer, ok = verdicts[text]
            _ConsoleDispatchOutput(report=s.routed).skill_result(
                text[1:], answer, ok=ok
            )
            return True

        s, out = self._surface(tmp_path, dispatch=dispatch)
        s.route("/bad", _opened({"text": "/bad", "nickname": None}))
        assert s.exit_code == 1
        s.route("/ok")  # 런 도중 주입(echo 없이) — 질의의 답이 아니다
        assert s.exit_code == 1 and not out.exists()
        s.route("/ok", _opened({"text": "/ok", "nickname": "⏰ nightly"}))  # 예약 발화
        assert s.exit_code == 1 and not out.exists()
        s.route("/ok", _opened({"text": "/ok", "nickname": None}))
        assert s.exit_code == 0 and out.read_text(encoding="utf-8") == "done"

    def test_plain_query_route_does_not_leak_the_query_flag_into_the_run(
        self, tmp_path
    ):
        """질의가 명령이 아니면(디스패치 False) 그 런 도중의 주입 명령이 질의 자리를
        물려받지 않는다 — 턴 경계의 ``route`` 에는 그 차례의 시작이 없다."""
        from agent_cli.main import _ConsoleDispatchOutput

        def dispatch(text, stop, item=None):
            if not text.startswith("/"):
                return False
            _ConsoleDispatchOutput(report=s.routed).skill_result("x", "done")
            return True

        s, out = self._surface(tmp_path, dispatch=dispatch)
        assert s.route("plain", _opened({"text": "plain", "nickname": None})) is False
        s.route("/x")  # 그 런 도중 주입
        assert s.exit_code == 1 and not out.exists()


class TestQuietPolicyWaitNotice:
    """모니터만 남아 기다릴 때도 한 번 알린다 (v10.34.0) — 종전엔 예약 대기만
    알리고 모니터 대기는 조용해, `complete` 뒤 안 울리는 모니터가 붙든 run 이
    멈춘 것처럼 보였다(넛지 A/B cand-4)."""

    def test_monitor_wait_is_announced_once_and_kind_switch_announces_again(self):
        live = MagicMock(has_active_work=MagicMock(return_value=True))
        dead = MagicMock(has_active_work=MagicMock(return_value=False))
        waits: list[str] = []
        policy = QuietPolicy(
            main_input=MainInput(),
            agent_registry=MagicMock(has_agent_work=MagicMock(return_value=False)),
            monitors=live,
            schedules=dead,
            on_wait=waits.append,
        )
        assert not policy.should_stop() and not policy.should_stop()
        assert waits == ["monitors"]
        live.has_active_work.return_value = False
        dead.has_active_work.return_value = True  # 모니터 은퇴, 예약만 남음
        assert not policy.should_stop()
        assert waits == ["monitors", "schedules"]
        dead.has_active_work.return_value = False
        assert policy.should_stop()

    def test_queue_or_agent_activity_is_not_a_wait(self):
        waits: list[str] = []
        q = MainInput()
        q.enqueue(None, "x")
        policy = QuietPolicy(
            main_input=q,
            agent_registry=MagicMock(has_agent_work=MagicMock(return_value=False)),
            monitors=MagicMock(has_active_work=MagicMock(return_value=True)),
            on_wait=waits.append,
        )
        assert not policy.should_stop()
        assert waits == []  # 큐에 할 일이 있으면 "대기" 가 아니다
