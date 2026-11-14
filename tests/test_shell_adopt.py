"""전경 `shell` 이 남긴 프로세스 그룹을 작업으로 넘겨받는다 (v10.135.0, 사용자 결정 2026-11-14).

출력을 파이프 밖으로 돌린 `&` 자식은 `sh` 가 끝나도 같은 그룹에 남는다 — 종전엔 하니스가
몰라 세션이 끝나도 살았다(사용자 머신의 21일 된 `http.server`). 남긴 것이 없는 명령은 결과·
동작이 종전과 바이트까지 같다.
"""

from __future__ import annotations

import os
import signal
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent_cli import jobs
from agent_cli.jobs import GroupHandle, JobRegistry, group_alive
from agent_cli.session_slots import set_job_registry
from agent_cli.tools.shell import ShellTool, tool_shell


@pytest.fixture
def reg(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = JobRegistry(cwd=tmp_path)
    r.deliver = MagicMock()
    set_job_registry(r)
    yield r
    r.stop_all()


def _wait_gone(pgid: int, s: float = 5.0) -> bool:
    end = time.monotonic() + s
    while time.monotonic() < end:
        if not group_alive(pgid):
            return True
        time.sleep(0.05)
    return False


class TestAdopt:
    def test_a_redirected_background_child_becomes_a_job(self, reg):
        r = tool_shell(
            {"command": "sleep 30 > /dev/null 2>&1 & echo started"}, owner="agent:k"
        )
        assert r.success
        lines = r.output.splitlines()
        assert lines[0] == "started"
        [job] = reg.running()
        assert job.adopted and job.owner == "agent:k"
        assert lines[1] == (
            f"[1 process kept running after this command; now tracked as job #{job.id} "
            f"— stopped when the session ends, `kill -- -{job.pid}` stops it now. Start "
            "long-running processes with `background` true.]"
        )
        assert f"job #{job.id}" in reg.render_tail("agent:k")
        log = (reg._cwd / job.log).read_text()
        assert log.startswith("$ sleep 30 > /dev/null 2>&1 & echo started\n")
        assert "not captured" in log
        reg.stop_all()
        assert _wait_gone(job.pid)

    def test_natural_end_reports_unknown_exit_code(self, reg):
        tool_shell({"command": "sleep 0.5 > /dev/null 2>&1 &"}, owner="main")
        [job] = reg.running()
        assert _wait_gone(job.pid)
        reg.tick()
        assert not job.running
        report = reg.deliver.call_args.kwargs["text"]
        assert report.startswith(
            f"🧵 job #{job.id} finished (exit code unknown) after "
        )
        assert "rc=" not in report

    def test_owner_release_stops_it(self, reg):
        from agent_cli.wake import release_owner

        tool_shell({"command": "sleep 30 > /dev/null 2>&1 &"}, owner="run:x")
        [job] = reg.running()
        note = release_owner("run:x").note
        assert "1 background job still running was stopped" in note
        assert _wait_gone(job.pid)


class TestNothingLeft:
    @pytest.mark.parametrize(
        ("cmd", "out"),
        [
            ("echo hi", "hi"),
            ("true", "(no output)"),
            ("echo a | tr a b", "b"),
            ("sleep 0.05; echo done", "done"),
            ("exit 3", "[exit code: 3]"),
        ],
    )
    def test_output_and_jobs_are_unchanged(self, reg, cmd, out):
        assert tool_shell({"command": cmd}).output == out
        assert reg.running() == [] and reg._jobs == {}

    def test_without_a_registry_nothing_is_adopted(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        set_job_registry(None)
        r = tool_shell({"command": "sleep 30 > /dev/null 2>&1 & echo $$"})
        assert "kept running" not in r.output
        pgid = int(r.output.split()[0])
        os.killpg(pgid, signal.SIGKILL)  # 넘겨받을 곳이 없다 — 종전과 같다
        assert _wait_gone(pgid)


class TestGroupHandle:
    def test_never_signals_a_group_it_saw_gone(self):
        h = GroupHandle(424242)
        with patch.object(jobs.os, "killpg", side_effect=ProcessLookupError) as k:
            assert h.poll() == 0
            h.stop()
            h.force()
            assert h.poll() == 0
        assert [c.args for c in k.call_args_list] == [(424242, 0)]

    def test_stop_hint_is_the_group(self):
        assert GroupHandle(77).stop_hint == "`kill -- -77`"


class TestOwnerFromTheTool:
    def test_shell_tool_passes_the_callers_address(self):
        with patch("agent_cli.tools.shell.tool_shell", return_value=MagicMock()) as t:
            ShellTool()._run(
                {"command": "echo x"}, ctx=SimpleNamespace(owner="agent:k")
            )
        assert t.call_args.kwargs == {"owner": "agent:k"}
