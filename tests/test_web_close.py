"""웹 방을 닫을 때 — 정리는 main 이 멈춘 뒤 (6d-2).

`main.web` 의 ``finally`` 는 main 상자를 닫고(`WebServer.shutdown` → `MainInput.close`) main 이 멈추기를 기다린 뒤
(`wait_for_main_to_stop`) 정리한다(`close_session`). 기다림의 상한은 `WEB_CLOSE_WAIT_S`, SIGTERM 이면 그 시한 안이다.
가짜 worker 는 받은 ``timeout`` 을 적는다 — 시간을 재지 않는다.
"""

from __future__ import annotations

import inspect
import time

import pytest

import agent_cli.main as M


class _Worker:
    def __init__(self, raises=None):
        self.timeouts: list[float] = []
        self._raises = raises

    def join(self, timeout=None):
        self.timeouts.append(timeout)
        if self._raises is not None:
            raise self._raises


def test_without_a_deadline_it_waits_up_to_the_cap():
    w = _Worker()
    M.wait_for_main_to_stop(w, None)
    assert w.timeouts == [M.WEB_CLOSE_WAIT_S]


def test_the_cap_covers_the_shell_stop():
    """7초 — 전경 셸의 정지(0.2초 + TERM 뒤 3초 + KILL 뒤 3초)와 그 결과 기록까지. 6d-2 전엔 2초라 그보다 늦게 멈춘 main 은
    정리와 겹쳐 그 호출의 기록이 사라지고 자식이 고아로 남았다."""
    from agent_cli.jobs import KILL_GRACE_S
    from agent_cli.tools.shell import STOP_POLL_S

    assert M.WEB_CLOSE_WAIT_S == 7.0
    assert M.WEB_CLOSE_WAIT_S > STOP_POLL_S + 2 * KILL_GRACE_S


def test_a_passed_deadline_does_not_wait():
    w = _Worker()
    M.wait_for_main_to_stop(w, time.monotonic() - 1)
    assert w.timeouts == [0.0]


def test_a_second_ctrl_c_during_the_wait_escapes_the_close():
    """6d-2 전의 값 — 닫는 도중 Ctrl+C 를 한 번 더 누르면 `KeyboardInterrupt` 가 그대로 나가 정리·저장을 건너뛴다."""
    with pytest.raises(KeyboardInterrupt):
        M.wait_for_main_to_stop(_Worker(raises=KeyboardInterrupt()), None)


def test_the_web_host_waits_for_main_before_the_teardown():
    """`web` 의 ``finally`` — main 상자를 닫고, 기다리고, 그 뒤 정리한다. `web` 은 typer 데코레이터에 싸여 원문으로 본다."""
    src = inspect.getsource(M)
    body = src[src.index("def web(") :]
    close, wait, teardown = (
        body.index("server.shutdown()"),
        body.index("wait_for_main_to_stop(worker, deadline)"),
        body.index("close_session(spec, stores, owners, warn_stuck=False"),
    )
    assert close < wait < teardown


def test_a_command_that_ignores_term_is_recorded_and_leaves_no_orphan(
    tmp_path, monkeypatch
):
    """PROPOSAL-6d §2.2 의 장면 — main 의 전경 명령이 TERM 을 무시한다. 웹 호스트와 같은 순서(main 상자 닫기 → main 을
    기다림 → 정리)로 닫으면 그 호출과 정지 결과가 기록에 남고, 명령의 프로세스 그룹은 KILL 로 끝나 있다.
    TERM·KILL 뒤의 유예를 줄여(`jobs.KILL_GRACE_S`) 기다림을 짧게 한다 — 기다림은 프로세스의 끝을 보는 것이지 sleep 이
    아니다."""
    import json
    import os
    import signal

    import agent_cli.jobs as jobs_mod
    import agent_cli.render as render_mod
    from tests.oracle.driver import Provider, Session, call, complete_all, until

    monkeypatch.setattr(jobs_mod, "KILL_GRACE_S", 0.2)
    monkeypatch.chdir(tmp_path)
    prev = render_mod.get_renderer()
    pidfile = tmp_path / "pid"
    cmd = f"trap '' TERM; echo $$ > {pidfile}; sleep 30"
    p = Provider({"MAIN": [call("shell", command=cmd)] + [complete_all("after")] * 3})
    s = Session(tmp_path, p, path="web")
    try:
        s.say("MAIN go", nickname="dj")
        t = s.pump_in_thread()
        until(
            lambda: pidfile.exists() and pidfile.read_text().strip(), "command started"
        )
        pgid = int(pidfile.read_text())
        s.stop_pump()  # WebServer.shutdown — main 상자를 닫고 정지를 세운다
        M.wait_for_main_to_stop(t, None)
        assert not t.is_alive()
        s.teardown()
        records = [
            json.loads(line)
            for line in (s.sdir / "history.jsonl").read_text().splitlines()
            if line.strip()
        ]
        results = [r for r in records if r.get("tool") == "shell"]
        assert len(results) == 1
        assert "This run was stopped, so the command was ended after" in str(
            results[0]["content"]
        )
        with pytest.raises(ProcessLookupError):
            os.killpg(pgid, 0)  # 고아가 없다
    finally:
        render_mod.set_renderer(prev)
        try:
            os.killpg(int(pidfile.read_text()), signal.SIGKILL)
        except (OSError, ValueError):
            pass
