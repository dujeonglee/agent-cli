"""SIGTERM 정리 (v10.134.1, `agent_cli.terminate`) — 받으면 정상 정리 경로를 타고, 끝나면
신호 15 로 죽는다. 정리 대기는 기한(`TEARDOWN_BUDGET_S`) 안으로 줄인다 — SIGTERM 경로만."""

from __future__ import annotations

import signal
import subprocess
import sys
import textwrap
import threading
import time
from typing import ClassVar
from unittest.mock import MagicMock

import pytest

from agent_cli import terminate
from agent_cli.terminate import (
    CLOSE_WAIT_S,
    TEARDOWN_BUDGET_S,
    SigTerm,
    Terminated,
    wait_left,
)


def _child(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


class _Timer:
    """안전망 대역 — 받은 것을 적고, 시작·취소만 센다(시간이 흐르지 않는다)."""

    made: ClassVar[list] = []

    def __init__(self, wait, fn, args):
        self.wait, self.fn, self.args = wait, fn, args
        self.daemon = False
        self.started = self.cancelled = False
        _Timer.made.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


class TestRunClose:
    """run 의 SIGTERM (6d-3) — 첫 신호는 main 상자를 닫고 안전망을 건다, 두 번째는 `Terminated` 로 푼다."""

    @pytest.fixture
    def handler(self, monkeypatch):
        _Timer.made = []
        monkeypatch.setattr(terminate, "wait_left", lambda deadline, cap: ("left", cap))
        closed: list[int] = []
        t = SigTerm()
        prev = t.install_close(lambda: closed.append(1), timer=_Timer)
        yield t, signal.getsignal(signal.SIGTERM), closed
        SigTerm.restore(prev)

    def test_the_first_sigterm_closes_and_arms_the_net(self, handler):
        t, h, closed = handler
        h(signal.SIGTERM, None)  # 예외 없음 — main 은 제 걸음으로 멈춘다
        assert t.received and closed == [1]
        (net,) = _Timer.made
        assert net.started and net.daemon
        assert net.wait == ("left", CLOSE_WAIT_S)  # 시한(6초) 안의 상한 7초
        assert net.fn is signal.pthread_kill
        assert net.args == (threading.main_thread().ident, signal.SIGTERM)

    def test_the_second_sigterm_unwinds(self, handler):
        """안전망이 보낸 것이든 밖에서 또 온 것이든 — `Terminated` 로 메인 스레드를 푼다(정리·저장 뒤 신호 15)."""
        _t, h, closed = handler
        h(signal.SIGTERM, None)
        with pytest.raises(Terminated):
            h(signal.SIGTERM, None)
        assert closed == [1]

    def test_the_net_is_taken_down_when_the_pump_returns(self, handler):
        t, h, _closed = handler
        t.cancel_safety_net()  # 걸지 않았으면 아무것도 안 한다
        h(signal.SIGTERM, None)
        t.cancel_safety_net()
        assert _Timer.made[0].cancelled

    def test_without_pthread_kill_it_only_closes(self, monkeypatch):
        monkeypatch.delattr(signal, "pthread_kill")
        _Timer.made = []
        closed: list[int] = []
        t = SigTerm()
        prev = t.install_close(lambda: closed.append(1), timer=_Timer)
        try:
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        finally:
            SigTerm.restore(prev)
        assert closed == [1] and _Timer.made == []

    def test_the_net_wakes_a_blocked_main_thread_then_dies_with_15(self):
        """실제 신호 — main 이 멈추지 않으면(막힌 기다림) 안전망이 메인 스레드에 SIGTERM 을 보내 `Terminated` 로 풀고,
        정리 뒤 신호 15. 상한을 줄여 곧바로 울리게 한다."""
        r = _child(
            """
            import os, signal, threading
            import agent_cli.terminate as T
            T.CLOSE_WAIT_S = 0.05
            t = T.SigTerm()
            t.install_close(lambda: print("closed", flush=True))
            try:
                try:
                    os.kill(os.getpid(), signal.SIGTERM)
                    threading.Event().wait(30)  # 정지를 보지 않는 막힌 기다림
                except T.Terminated:
                    print("unwound", flush=True)
            finally:
                t.cancel_safety_net()
                T.SigTerm.default()
                print("cleanup", flush=True)
            if t.received:
                T.SigTerm.die()
            print("not reached", flush=True)
            """
        )
        assert r.returncode == -signal.SIGTERM
        assert r.stdout.split() == ["closed", "unwound", "cleanup"]

    def test_terminated_is_not_swallowed_by_common_handlers(self):
        assert not issubclass(Terminated, Exception)
        assert not issubclass(Terminated, KeyboardInterrupt)


class TestServerMark:
    def test_handle_exit_marks_sigterm_and_keeps_uvicorns_behaviour(self):
        server = MagicMock()
        orig = server.handle_exit
        t = SigTerm()
        prev = t.install_for_server(server)
        try:
            server.handle_exit(signal.SIGINT, None)
            assert not t.received
            server.handle_exit(signal.SIGTERM, None)
            assert t.received
            assert [c.args for c in orig.call_args_list] == [
                (signal.SIGINT, None),
                (signal.SIGTERM, None),
            ]
        finally:
            SigTerm.restore(prev)

    def test_signal_outside_the_server_run_asks_it_to_stop(self):
        server = MagicMock(should_exit=False)
        t = SigTerm()
        prev = t.install_for_server(server)
        try:
            handler = signal.getsignal(signal.SIGTERM)
            handler(signal.SIGTERM, None)
            assert t.received and server.should_exit is True
        finally:
            SigTerm.restore(prev)


class TestDeadline:
    def test_no_signal_no_deadline(self):
        assert SigTerm().deadline() is None
        assert wait_left(None, 5.0) == 5.0

    def test_deadline_is_the_budget_after_the_signal(self):
        t = SigTerm()
        t._mark()
        left = t.deadline() - time.monotonic()
        assert TEARDOWN_BUDGET_S - 0.5 < left <= TEARDOWN_BUDGET_S
        assert wait_left(t.deadline(), 2.0) == 2.0
        assert wait_left(time.monotonic() - 1, 2.0) == 0.0

    def test_budget_fits_the_boards_kill(self):
        """보드는 SIGTERM 8초 뒤 SIGKILL — 대기 예산 + 서버 종료·저장 몫이 그 안."""
        assert TEARDOWN_BUDGET_S <= 6.0


class TestTeardownPassesTheDeadline:
    def test_normal_path_calls_are_unchanged(self):
        from agent_cli.runtime import teardown_session

        reg, jobs, harness = MagicMock(), MagicMock(), MagicMock()
        jobs.stop_all.return_value = []
        teardown_session(None, None, agent_registry=reg, jobs=jobs, harness=harness)
        assert reg.shutdown_all.call_args.kwargs == {}
        assert jobs.stop_all.call_args.kwargs == {}
        assert harness.stop.call_args.kwargs == {}

    def test_sigterm_path_passes_it_down(self):
        from agent_cli.runtime import teardown_session

        reg, jobs, harness = MagicMock(), MagicMock(), MagicMock()
        jobs.stop_all.return_value = []
        d = time.monotonic() + 3
        teardown_session(
            None, None, agent_registry=reg, jobs=jobs, harness=harness, deadline=d
        )
        assert reg.shutdown_all.call_args.kwargs == {"deadline": d}
        assert jobs.stop_all.call_args.kwargs == {"deadline": d}
        assert harness.stop.call_args.kwargs == {"deadline": d}


class TestJobsGrace:
    def test_a_term_ignoring_job_is_killed_within_the_deadline(self, tmp_path):
        from agent_cli.jobs import KILL_GRACE_S, JobRegistry

        reg = JobRegistry(cwd=tmp_path)
        reg.deliver = MagicMock()
        job = reg.start("trap '' TERM; sleep 30", owner="main")
        time.sleep(0.3)  # trap 이 걸린 뒤
        t0 = time.monotonic()
        reg.stop_all(deadline=time.monotonic() + 0.5)
        took = time.monotonic() - t0
        assert not job.running or job.handle.poll() is not None
        assert took < KILL_GRACE_S  # 종전 유예(3 s)를 다 기다리지 않았다

    def test_await_stops_at_the_deadline(self):
        from agent_cli.jobs import JobRegistry

        handle = MagicMock()
        handle.poll.return_value = None
        t0 = time.monotonic()
        assert JobRegistry._await(handle, time.monotonic() + 0.2) is None
        assert time.monotonic() - t0 < 1.0
