"""run 의 SIGTERM — main 상자를 닫고 main 이 멈추기를 기다린 뒤 정리한다 (6d-3).

처리기(`SigTerm.install_close`)가 부르는 것은 `MainInput.close` — 웹의 닫기와 같은 길이다. 여기서는 그 장면을 실제 조립
(`tests.oracle.driver.Session`, ``path="run"``)으로 본다: 6d-3 전엔 첫 SIGTERM 이 그 자리에서 `Terminated` 를 던져, 돌던
전경 명령의 호출과 결과가 기록에 남지 않았다. 처리기 자체(안전망·두 번째 신호)는 ``test_terminate.py::TestRunClose``.
"""

from __future__ import annotations

import json
import os
import signal

import pytest

import agent_cli.jobs as jobs_mod
import agent_cli.render as render_mod
from agent_cli.terminate import CLOSE_WAIT_S
from tests.oracle.driver import Provider, Session, call, complete_all, until


def test_a_command_that_ignores_term_is_recorded_and_leaves_no_orphan(
    tmp_path, monkeypatch
):
    """6d-3 의 장면(run) — main 의 전경 명령이 TERM 을 무시한다. main 상자를 닫으면(SIGTERM 처리기가 하는 일)
    main 이 제 걸음으로 멈추고 펌프가 돌아온다: 그 호출과 정지 결과가 기록에 남고, 명령의 프로세스 그룹은 KILL 로 끝나
    있다. TERM·KILL 뒤의 유예를 줄여(`jobs.KILL_GRACE_S`) 기다림을 짧게 한다 — 기다림은 프로세스의 끝을 보는 것이지
    sleep 이 아니다."""
    monkeypatch.setattr(jobs_mod, "KILL_GRACE_S", 0.2)
    monkeypatch.chdir(tmp_path)
    prev = render_mod.get_renderer()
    pidfile = tmp_path / "pid"
    cmd = f"trap '' TERM; echo $$ > {pidfile}; sleep 30"
    p = Provider({"MAIN": [call("shell", command=cmd)] + [complete_all("after")] * 3})
    s = Session(tmp_path, p, path="run")
    try:
        s.say("MAIN go")
        t = s.pump_in_thread()
        until(
            lambda: pidfile.exists() and pidfile.read_text().strip(), "command started"
        )
        pgid = int(pidfile.read_text())
        s.main_input.close()  # SIGTERM 처리기가 부르는 것
        t.join(timeout=CLOSE_WAIT_S)
        assert not t.is_alive()  # 펌프가 돌아왔다 — 안전망이 울릴 일이 없다
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
