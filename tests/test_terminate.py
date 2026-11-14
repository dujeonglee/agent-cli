"""SIGTERM 정리 (v10.134.1, `agent_cli.terminate`) — 받으면 정상 정리 경로를 타고, 끝나면
신호 15 로 죽는다. 정리 대기는 기한(`TEARDOWN_BUDGET_S`) 안으로 줄인다 — SIGTERM 경로만."""

from __future__ import annotations

import signal
import subprocess
import sys
import textwrap
import time
from unittest.mock import MagicMock

from agent_cli.terminate import TEARDOWN_BUDGET_S, SigTerm, Terminated, wait_left


def _child(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


class TestRunUnwind:
    def test_sigterm_unwinds_the_main_thread_then_dies_with_15(self):
        """run 의 모양 — 처리기가 메인 스레드를 풀고, finally 의 정리가 돈 뒤 신호 15."""
        r = _child(
            """
            import os, signal, threading
            from agent_cli.terminate import SigTerm, Terminated
            t = SigTerm()
            t.install_unwind()
            try:
                try:
                    os.kill(os.getpid(), signal.SIGTERM)
                    threading.Event().wait(30)  # 막혀 있던 메인 스레드
                except Terminated:
                    print("unwound", flush=True)
                    # 정리 중 두 번째 SIGTERM 은 기본 동작이어야 한다
                    print("dfl", signal.getsignal(signal.SIGTERM) is signal.SIG_DFL, flush=True)
            finally:
                print("cleanup", flush=True)
            if t.received:
                SigTerm.die()
            print("not reached", flush=True)
            """
        )
        assert r.returncode == -signal.SIGTERM
        assert r.stdout.split() == ["unwound", "dfl", "True", "cleanup"]

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
