"""백그라운드 작업 — 하니스가 소유하는 긴 명령 (agent_cli/jobs.py, v10.47.0,
docs/jobs/DESIGN.md).

셸 안내 문구 A/B(2026-10-17)의 결론을 고정한다: 긴 명령은 하니스가 자식으로
띄워 종료 코드를 알고, 끝나면 소유자에게 통지하며, 돌고 있는 동안 꼬리에 PID·
로그로 보인다. 그래서 폴링도, 패턴 kill 도, 끝 줄을 영영 기다리는 monitor 도
필요 없다. 수명(run 대기·web 미회수)·정리(소유자 사망·세션 종료)까지 본다.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_cli.jobs import (
    JobRegistry,
    JobsUnavailable,
    get_job_registry,
    set_job_registry,
)
from agent_cli.loop.ledger import Ledger
from tests.loop_kinds import make_kind
from tests.loop_ports import make_ports
from tests.monitor_delivery import RecordingDelivery


def _wait(cond, timeout=8.0, step=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(step)
    return False


@pytest.fixture
def reg(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = JobRegistry(tmp_path)
    r.deliver = RecordingDelivery()
    set_job_registry(r)
    yield r
    r.stop_all()
    set_job_registry(None)


class TestStartAndFinish:
    def test_start_returns_at_once_with_pid_and_log(self, reg, tmp_path):
        t0 = time.time()
        job = reg.start("sleep 2; echo done", owner="main")
        assert time.time() - t0 < 1.0  # 기다리지 않는다
        assert job.id == 1 and job.pid > 0 and job.running
        assert job.log == Path(".agent-cli/tmp/jobs/1.log")
        assert (tmp_path / job.log).read_text().startswith("$ sleep 2; echo done\n")
        assert reg.running("main") == [job] and reg.has_active_work()

    def test_exit_is_logged_and_the_owner_is_told(self, reg, tmp_path):
        job = reg.start("echo hello; echo world; exit 3", owner="main")
        assert _wait(lambda: reg.deliver.calls)  # 통지 배달까지(rc 는 그보다 먼저 선다)
        assert job.rc == 3 and job.ended_at is not None
        log = (tmp_path / job.log).read_text()
        assert "hello\nworld\n" in log and "[job #1 exited rc=3 after" in log
        d = reg.deliver
        assert d.addrs == ["main"]
        _addr, kw = d.calls[0]
        assert kw["mail"]["kind"] == "job" and kw["mail"]["success"] is False
        assert kw["author"] == "main" and kw["expects_reply"] is False
        text = kw["text"]
        assert text.startswith("🧵 job #1 finished rc=3 after")
        assert "`echo hello; echo world; exit 3`" in text
        assert "log: .agent-cli/tmp/jobs/1.log (last 2 line(s))" in text
        assert "  hello" in text and "  world" in text
        assert (
            "$ echo" not in text and "[job #1" not in text
        )  # 머리·꼬리는 보고에 안 싣는다
        assert not reg.has_active_work()

    def test_success_mail_and_agent_owner(self, reg):
        job = reg.start("true", owner="agent:x1")
        assert _wait(lambda: reg.deliver.calls)  # 통지 배달까지(rc 는 그보다 먼저 선다)
        assert job.rc == 0
        assert reg.deliver.addrs == ["agent:x1"]
        assert reg.deliver.calls[0][1]["mail"]["success"] is True

    def test_tail_is_capped(self, reg):
        reg.start("for i in $(seq 1 20); do echo line$i; done", owner="main")
        assert _wait(lambda: reg.deliver.calls)  # 통지 배달까지(rc 는 그보다 먼저 선다)
        text = reg.deliver.reports[0]
        assert "line13" in text and "line12" not in text  # 마지막 8줄
        assert "(last 8 line(s))" in text

    def test_ids_increase_and_running_lists_only_live_ones(self, reg):
        a = reg.start("true", owner="main")
        b = reg.start("sleep 5", owner="main")
        assert (a.id, b.id) == (1, 2)
        assert _wait(lambda: not a.running)
        assert reg.running() == [b]

    def test_unwired_or_closed_registry_refuses(self, tmp_path):
        r = JobRegistry(tmp_path)
        with pytest.raises(JobsUnavailable, match="not wired"):
            r.start("true", owner="main")
        r.deliver = RecordingDelivery()
        r.closed = True
        with pytest.raises(JobsUnavailable, match="shutting down"):
            r.start("true", owner="main")

    def test_elapsed_formats(self, reg):
        job = reg.start("sleep 30", owner="main")
        assert job.elapsed(job.started_at + 5) == "5s"
        assert job.elapsed(job.started_at + 151) == "2m31s"
        assert job.elapsed(job.started_at + 3725) == "1h02m"


class TestKillAndCleanup:
    def test_kill_stops_the_process_group_without_a_notice(self, reg):
        job = reg.start("sleep 60 & sleep 60; wait", owner="main")
        pid = job.pid
        reg.kill(job)
        assert not job.running and job.rc != 0
        assert _wait(lambda: not _alive(pid))
        assert reg.deliver.calls == []  # 우리가 죽인 것은 통지하지 않는다

    def test_drop_owner_kills_only_that_owners_jobs(self, reg):
        mine = reg.start("sleep 60", owner="agent:x1")
        other = reg.start("sleep 60", owner="main")
        assert reg.drop_owner("agent:x1") == 1
        assert not mine.running and other.running

    def test_stop_all_kills_everything_and_closes(self, reg):
        a = reg.start("sleep 60", owner="main")
        b = reg.start("sleep 60", owner="agent:q")
        killed = reg.stop_all()
        assert {j.id for j in killed} == {a.id, b.id}
        assert reg.closed and not reg.has_active_work()
        assert _wait(lambda: not _alive(a.pid) and not _alive(b.pid))

    def test_finish_after_close_is_silent(self, reg):
        job = reg.start("sleep 0.3", owner="main")
        reg.closed = True
        assert _wait(lambda: job.handle.poll() is not None)
        reg.tick()
        assert not job.running and reg.deliver.calls == []


class TestTail:
    def test_running_jobs_of_this_owner_only(self, reg):
        reg.start("sleep 60", owner="main")
        reg.start("sleep 60", owner="agent:z")
        out = reg.render_tail("main", now=time.time() + 90)
        assert out.startswith("## Background jobs (running")
        assert "job #1 · pid " in out and "1m30s" in out and "`sleep 60`" in out
        assert "→ .agent-cli/tmp/jobs/1.log" in out and "stop it: `kill " in out
        assert "job #2" not in out
        assert "do not poll" in out and "wait" not in out  # v10.68.0: 상태만

    def test_nothing_when_nothing_runs(self, reg):
        job = reg.start("true", owner="main")
        assert _wait(lambda: not job.running)
        assert reg.render_tail("main") == ""

    def test_long_commands_are_shortened(self, reg):
        reg.start("echo " + "x" * 200 + "; sleep 60", owner="main")
        out = reg.render_tail("main")
        assert "…" in out and len(out) < 400

    def test_session_state_block_carries_the_jobs_section(self):
        from agent_cli.prompts.session_state import build_session_state

        out = build_session_state(turn=1, jobs="## Background jobs\n- job #1")
        assert "## Background jobs" in out and "- job #1" in out

    def test_loop_renders_the_tail_for_its_owner(self, reg, monkeypatch):
        """`_build_session_state` 가 전역 레지스트리에서 자기 주소의 작업을 싣는다."""
        from agent_cli.loop import llm as llm_mod

        reg.start("sleep 60", owner="agent:k")
        captured = {}

        def fake_build(**kw):
            captured.update(kw)
            return ""

        monkeypatch.setattr(
            "agent_cli.prompts.session_state.build_session_state", fake_build
        )
        self_ = SimpleNamespace(
            cfg=SimpleNamespace(
                agent_registry=None,
                tools_list=[],
                questions=None,
                max_turns=0,
                depth=1,
                owner="agent:k",
                kind=make_kind(ports=make_ports(owner="agent:k"), depth=1),
            ),
            ctx=None,
            state=SimpleNamespace(turn=1, ledger=Ledger()),
            _compaction_armed=set(),
        )
        llm_mod.LLMCaller._build_session_state(self_, 1000)
        assert "job #1" in captured["jobs"]


class TestShellToolSurface:
    def test_background_true_starts_a_job_with_the_loop_owner(self, reg, tmp_path):
        from agent_cli.tools.base import RunContext
        from agent_cli.tools.shell import ShellTool

        ctx = RunContext(owner="agent:k")
        r = ShellTool()._run(
            {"command": "echo hi; sleep 0.2", "background": True}, ctx=ctx
        )
        assert r.success, r.error
        assert r.output.startswith("job #1 started (pid ")
        assert ".agent-cli/tmp/jobs/1.log" in r.output and "`kill " in r.output
        assert reg.running("agent:k")
        assert _wait(lambda: not reg.has_active_work())
        assert reg.deliver.addrs == ["agent:k"]

    def test_background_string_true_and_default_owner(self, reg):
        from agent_cli.tools.shell import ShellTool

        r = ShellTool()._run({"command": "sleep 0.2", "background": "true"})
        assert r.success and reg.running("main")

    def test_foreground_is_unchanged(self, reg):
        from agent_cli.tools.shell import ShellTool

        r = ShellTool()._run({"command": "echo fg"})
        assert r.success and r.output == "fg" and reg.running() == []

    def test_no_registry_is_a_clear_error(self, tmp_path, monkeypatch):
        from agent_cli.tools.shell import ShellTool

        set_job_registry(None)
        r = ShellTool()._run({"command": "sleep 1", "background": True})
        assert not r.success and "not available" in r.error

    def test_guards_apply_to_background_commands(self, reg, monkeypatch):
        from agent_cli.tools import shell as shell_mod
        from agent_cli.tools.shell import ShellTool

        monkeypatch.setattr(
            shell_mod, "confirm_dangerous", lambda cmd: ("Refused: nope", "")
        )
        r = ShellTool()._run({"command": "rm -rf x", "background": True})
        assert not r.success and r.error == "Refused: nope" and reg.running() == []

    @pytest.mark.parametrize(
        "cmd, plain, removed",
        [
            (
                "nohup sleep 0.3 > x.log 2>&1 & echo $!",
                "sleep 0.3 > x.log 2>&1 ; echo $!",
                ["`nohup`", "`&` before the tail (now `;`)"],
            ),
            (
                "(sleep 0.3; echo EXIT:$?) &",
                "(sleep 0.3; echo EXIT:$?)",
                ["the trailing `&`"],
            ),
            ("sleep 0.3 &", "sleep 0.3", ["the trailing `&`"]),
            ("setsid sleep 0.3", "sleep 0.3", ["`setsid`"]),
            (
                "sleep 0.3 & sleep 0.1; echo started",
                "sleep 0.3 ; sleep 0.1; echo started",
                ["`&` before the tail (now `;`)"],
            ),
            ("sleep 0.3 & disown", "sleep 0.3", ["`disown`", "the trailing `&`"]),
        ],
    )
    def test_self_detaching_pieces_are_removed_and_said(self, reg, cmd, plain, removed):
        """v10.47.1 재측정에서는 `nohup … &` 를 거부했다(모델이 다시 보냈다). v10.91.0:
        하니스가 이미 데몬화를 맡으므로 그 조각을 지우고 결과에 적는다 — 다시 보내는
        턴이 사라진다 (사용자 결정 2026-10-31). `setsid` 는 그룹을 빠져나가 kill 이
        닿지 않으니 반드시 지운다."""
        from agent_cli.tools.shell import ShellTool, strip_detachers

        assert strip_detachers(cmd) == (plain, removed)
        r = ShellTool()._run({"command": cmd, "background": True})
        assert r.success, r.error
        assert r.output.startswith(
            "Removed " + ", ".join(removed) + " — a background job"
        )
        assert "use the pid below instead of `$!`" in r.output
        assert "job #1 started" in r.output
        assert reg.running()[0].command == plain

    @pytest.mark.parametrize(
        "cmd",
        [
            'echo "a & b" && echo ok',
            "grep -c 'x &' f.txt; true",
            "curl 'http://h/?a=1&b=2'",
            "a && b || c",
            'printf "%s\\n" "end &"',
        ],
    )
    def test_quoted_or_logical_ampersands_are_left_alone(self, cmd):
        from agent_cli.tools.shell import strip_detachers

        assert strip_detachers(cmd) == (cmd, [])

    def test_only_a_detacher_leaves_nothing_to_run(self, reg):
        from agent_cli.tools.shell import ShellTool

        r = ShellTool()._run({"command": "nohup &", "background": True})
        assert not r.success and r.error.endswith("Nothing is left to run.")
        assert reg.running() == []

    def test_timeout_above_the_limit_becomes_a_background_job(self, reg):
        """기본 = 상한 (v10.91.0). 모델이 600 을 넣고 10분을 막혀 있다가 죽는 대신
        작업으로 띄우고 결과 첫 줄에 말한다."""
        from agent_cli.tools.shell import FOREGROUND_LIMIT, ShellTool

        assert FOREGROUND_LIMIT == 120
        r = ShellTool()._run({"command": "sleep 0.3; echo late", "timeout": 600})
        assert r.success, r.error
        assert r.output.startswith(
            "`timeout` 600 is above the foreground limit of 120 seconds, so the "
            "command was started as a background job instead. job #1 started"
        )
        assert reg.running()[0].command == "sleep 0.3; echo late"

    def test_timeout_at_or_below_the_limit_stays_in_the_foreground(self, reg):
        from agent_cli.tools.shell import ShellTool

        r = ShellTool()._run({"command": "echo now", "timeout": 120})
        assert r.success and r.output.strip() == "now" and reg.running() == []

    def test_without_a_registry_the_timeout_is_capped_instead(
        self, tmp_path, monkeypatch
    ):
        """통지가 갈 곳이 없는 런(레지스트리 없음)은 전경에서 상한으로 깎여 돈다."""
        from agent_cli.tools.shell import ShellTool

        set_job_registry(None)
        monkeypatch.chdir(tmp_path)
        r = ShellTool()._run({"command": "echo capped", "timeout": 600})
        assert r.success and r.output.strip() == "capped"

    @pytest.mark.parametrize(
        "cmd",
        ["python3 -m pytest -q && echo ok", "make build 2>&1", "sh scripts/check.sh"],
    )
    def test_plain_commands_pass(self, reg, cmd):
        from agent_cli.tools.shell import ShellTool

        r = ShellTool()._run({"command": cmd + "; sleep 0.2", "background": True})
        assert r.success, r.error

    def test_schema_advertises_background(self):
        from agent_cli.tools.shell import ShellTool

        props = ShellTool.parameters["properties"]
        assert props["background"]["type"] == "boolean"
        assert "default and maximum 120" in props["timeout"]["description"]
        assert "background job instead" in props["timeout"]["description"]
        assert "set `background` to true" in ShellTool.description


class TestLifetime:
    def test_live_work_counts_running_jobs(self, reg):
        from agent_cli.runtime import session_has_live_work

        assert not session_has_live_work(pending_count=0, jobs=reg)
        job = reg.start("sleep 60", owner="main")
        assert session_has_live_work(pending_count=0, jobs=reg)
        reg.kill(job)
        assert not session_has_live_work(pending_count=0, jobs=reg)

    def test_quiet_policy_waits_and_announces_jobs(self, reg):
        from agent_cli.pump import QuietPolicy

        waits = []
        policy = QuietPolicy(
            queue=SimpleNamespace(pending_count=lambda: 0),
            agent_registry=SimpleNamespace(has_active_work=lambda: False),
            jobs=reg,
            on_wait=waits.append,
        )
        assert policy.should_stop() is True
        job = reg.start("sleep 60", owner="main")
        assert policy.should_stop() is False and waits == ["jobs"]
        assert policy.should_stop() is False and waits == ["jobs"]  # 한 번만
        reg.kill(job)
        assert policy.should_stop() is True

    def test_web_active_predicate_sees_jobs(self, reg):
        from agent_cli.main import web_instance_is_active

        renderer = SimpleNamespace(
            has_live_connections=lambda: False, worker_is_busy=lambda: False
        )
        server = SimpleNamespace(pending_count=lambda: 0)
        assert not web_instance_is_active(renderer, server, None, jobs=reg)
        reg.start("sleep 60", owner="main")
        assert web_instance_is_active(renderer, server, None, jobs=reg)

    def test_teardown_kills_leftovers_and_says_so(self, reg, tmp_path, capsys):
        from agent_cli.runtime import teardown_session

        job = reg.start("sleep 60", owner="main")
        session = SimpleNamespace(session_id="s", save=lambda *a, **k: None)
        try:
            teardown_session(session, None, jobs=reg)
        except Exception:
            pass  # 세션 저장 등 다른 단계는 이 테스트의 관심이 아니다
        assert not job.running and _wait(lambda: not _alive(job.pid))
        out = capsys.readouterr().out
        assert "stopped 1 running background job(s)" in out and f"pid {job.pid}" in out

    def test_agent_death_drops_its_jobs(self, reg):
        from agent_cli.subagent.agents_live import AgentRegistry

        registry = AgentRegistry.__new__(AgentRegistry)
        registry.monitors = None
        registry.jobs = reg
        job = reg.start("sleep 60", owner="agent:k")
        assert registry._drop_monitors("k") == 1
        assert not job.running

    def test_job_mail_renders_as_an_observation(self):
        from agent_cli.subagent.agents_live import build_reply_record

        rec = build_reply_record(
            {"kind": "job", "success": True, "output": "🧵 job #1 finished rc=0"}
        )
        assert rec["role"] == "user" and rec["tool"] == "job" and rec["success"] is True
        assert rec["content"].startswith("🧵 job #1")


class TestResultFileContinuation:
    """`--result-file` (v10.34.1 규칙의 v10.47.0 예외): 질의 런이 자기 작업을
    남긴 채 끝나면 그 통지로 깨어난 런의 답이 질의의 나머지 답이다."""

    def _surface(self, tmp_path, reg):
        from agent_cli.main import _ConsoleSurface

        out = tmp_path / "RESULT.txt"
        return _ConsoleSurface(
            result_file=str(out), dispatch=lambda *a: False, jobs=reg
        ), out

    def _req(self, *, query: bool):
        from agent_cli.pump import RunRequest

        return RunRequest(
            text="t",
            author=None if query else "🤝 agent",
            author_is_user=query,
            request_id="",
            stop_event=__import__("threading").Event(),
            wake=not query,
        )

    def _ok(self, text):
        return SimpleNamespace(success=True, output=text)

    def test_query_without_jobs_keeps_the_old_rule(self, reg, tmp_path):
        surface, out = self._surface(tmp_path, reg)
        surface.run_ended(self._ok("answer"), self._req(query=True))
        surface.run_ended(self._ok("wake chatter"), self._req(query=False))
        assert out.read_text() == "answer"

    def test_query_that_leaves_a_job_takes_the_wake_answer(self, reg, tmp_path):
        surface, out = self._surface(tmp_path, reg)
        surface.busy()
        job = reg.start("sleep 60", owner="main")
        surface.run_ended(self._ok("still waiting on job #1"), self._req(query=True))
        assert out.read_text() == "still waiting on job #1"
        reg.kill(job)  # 작업이 끝났다(통지 → 깨우기 런)
        surface.busy()
        surface.run_ended(self._ok("14 passed in 150s"), self._req(query=False))
        assert out.read_text() == "14 passed in 150s"
        surface.busy()
        surface.run_ended(self._ok("later chatter"), self._req(query=False))
        assert out.read_text() == "14 passed in 150s"  # 사슬이 닫혔다

    def test_chain_follows_jobs_started_by_wake_runs(self, reg, tmp_path):
        """재측정 E-fix-2: 질의 런의 1초짜리 작업이 런 종료와 같은 순간에 끝나고,
        깨우기 런이 진짜 스위트(job #3)를 띄웠다 — 사슬은 그 작업의 통지 런까지
        이어져야 한다."""
        surface, out = self._surface(tmp_path, reg)
        surface.busy()
        quick = reg.start("true", owner="main")
        assert _wait(lambda: not quick.running)  # 질의 런이 끝나기 전에 이미 끝났다
        surface.run_ended(self._ok("waiting for job #1"), self._req(query=True))
        surface.busy()  # 깨우기 런 #1 — 여기서 진짜 스위트를 띄운다
        full = reg.start("sleep 60", owner="main")
        surface.run_ended(self._ok("waiting for job #2"), self._req(query=False))
        assert out.read_text() == "waiting for job #2"
        reg.kill(full)
        surface.busy()  # 깨우기 런 #2 — 작업 없음 → 답이고 사슬이 닫힌다
        surface.run_ended(self._ok("14 passed"), self._req(query=False))
        assert out.read_text() == "14 passed"
        surface.busy()
        surface.run_ended(self._ok("chatter"), self._req(query=False))
        assert out.read_text() == "14 passed"

    def test_a_wait_ended_run_continues_the_chain_without_jobs(self, reg, tmp_path):
        """v10.65.0: 모니터·에이전트를 `wait` 로 기다린 질의 런 — 작업이 없어도 다음
        깨우기 런의 답이 결과 파일에 쓰인다."""
        surface, out = self._surface(tmp_path, reg)
        surface.busy()
        waiting = SimpleNamespace(
            success=True, output="⏸ waiting on monitor m1", waiting=True
        )
        surface.run_ended(waiting, self._req(query=True))
        assert out.read_text() == "⏸ waiting on monitor m1"
        surface.busy()
        surface.run_ended(self._ok("the real answer"), self._req(query=False))
        assert out.read_text() == "the real answer"
        surface.busy()
        surface.run_ended(self._ok("chatter"), self._req(query=False))
        assert out.read_text() == "the real answer"

    def test_failed_runs_never_write(self, reg, tmp_path):
        surface, out = self._surface(tmp_path, reg)
        reg.start("sleep 60", owner="main")
        surface.run_ended(
            SimpleNamespace(success=False, output="x"), self._req(query=True)
        )
        assert not out.exists()

    def test_plain_command_wording(self):
        from agent_cli.prompts.system_prompt import TASK_GUIDELINES
        from agent_cli.tools.shell import ShellTool

        assert "plain command (no `&`, no redirection" in TASK_GUIDELINES
        assert (
            "Give the plain command"
            in ShellTool.parameters["properties"]["background"]["description"]
        )


class TestAssembly:
    def test_build_and_wire(self, tmp_path):
        from agent_cli.runtime import build_job_registry, wire_job_delivery

        reg = build_job_registry(tmp_path)
        assert get_job_registry() is reg and reg.deliver is None
        fake = SimpleNamespace(deliver=lambda *a, **k: "")
        wire_job_delivery(fake, reg)
        assert reg.deliver is fake.deliver and fake.jobs is reg
        set_job_registry(None)

    def test_guideline_and_tool_text_point_at_background(self):
        from agent_cli.prompts.system_prompt import TASK_GUIDELINES

        assert "`background` true" in TASK_GUIDELINES
        assert ".agent-cli/tmp/jobs/" in TASK_GUIDELINES
        assert "Do not poll the log" in TASK_GUIDELINES
        assert "call `wait`" in TASK_GUIDELINES  # v10.65.0: 기다림은 wait
        assert "do not fill turns with unrelated actions" in TASK_GUIDELINES
        assert "`kill -- -<pid>`" in TASK_GUIDELINES  # v10.79.0: 프로세스 묶음


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        # 좀비면 waitpid 로 거둬지고, 살아 있으면 0 이 돌아온다
        wpid, _ = os.waitpid(pid, os.WNOHANG)
        return wpid == 0
    except ChildProcessError:
        return True


class _FakeHandle:
    """레지스트리가 핸들에 묻는 것만 구현한 몸 — `adopt` 계약 (v10.91.0)."""

    def __init__(self) -> None:
        self.pid = 4242
        self.rc = None
        self.stopped = 0
        self.forced = 0
        self.stop_hint = "`stop me`"

    def poll(self):
        return self.rc

    def stop(self):
        self.stopped += 1

    def force(self):
        self.forced += 1
        self.rc = -9

    def report_lines(self):
        return ["=> 42", "state: n (7)"]


class TestAdoptedHandles:
    """작업의 몸은 핸들이다 — 셸 명령(`ProcHandle`)과 `python` 셀(`PendingCell`)이
    같은 번호·로그·꼬리·통지·수명을 쓴다."""

    def test_adopt_numbers_lists_and_renders_like_a_shell_job(self, reg, tmp_path):
        log = Path(".agent-cli/tmp/python/main.log")
        (tmp_path / log).parent.mkdir(parents=True)
        (tmp_path / log).write_text("$ python: step\nline1\n")
        h = _FakeHandle()
        job = reg.adopt(
            h, command="python: step", log=log, owner="main", started_at=time.time()
        )
        assert job.id == 1 and job.pid == 4242 and job.running
        assert reg.running("main") == [job] and reg.has_active_work()
        tail = reg.render_tail("main")
        assert "job #1 · pid 4242" in tail and "stop it: `stop me`" in tail
        assert job.stop_hint == "`stop me`"
        h.rc = 0
        reg.tick()
        assert job.rc == 0 and not job.running
        text = reg.deliver.reports[0]
        assert text.startswith("🧵 job #1 finished rc=0 after")
        assert "  line1\n  => 42\n  state: n (7)" in text

    def test_kill_asks_the_handle_to_stop_then_forces(self, reg, tmp_path):
        log = Path(".agent-cli/tmp/python/main.log")
        (tmp_path / log).parent.mkdir(parents=True)
        (tmp_path / log).write_text("$ python: step\n")
        h = _FakeHandle()
        job = reg.adopt(
            h, command="python: step", log=log, owner="main", started_at=time.time()
        )
        reg.kill(job)
        assert h.stopped == 1 and h.forced == 1 and job.rc == -9
        assert reg.deliver.calls == []

    def test_adopt_needs_delivery_like_start(self, tmp_path):
        r = JobRegistry(tmp_path)
        with pytest.raises(JobsUnavailable):
            r.adopt(
                _FakeHandle(),
                command="x",
                log=Path("x.log"),
                owner="main",
                started_at=time.time(),
            )
        assert not r.available


class TestBashExecutable:
    """bash 가 있으면 bash 로 띄운다 (v10.91.0) — 벤치에서 `[[ ]]`·`<(…)` 가 dash 에서
    깨져 턴을 잃었다. 실행기를 맞추면 그 실패 종류가 모델과 무관하게 사라진다."""

    def test_background_jobs_run_bash_syntax(self, reg, tmp_path):
        from agent_cli import jobs as jobs_mod

        if not jobs_mod.SHELL:
            pytest.skip("no bash on this machine")
        job = reg.start("[[ 1 == 1 ]] && echo bashok", owner="main")
        assert _wait(lambda: reg.deliver.calls)
        assert job.rc == 0 and "bashok" in (tmp_path / job.log).read_text()

    def test_foreground_runs_bash_syntax(self, tmp_path, monkeypatch):
        from agent_cli import jobs as jobs_mod
        from agent_cli.tools.shell import tool_shell

        if not jobs_mod.SHELL:
            pytest.skip("no bash on this machine")
        monkeypatch.chdir(tmp_path)
        r = tool_shell({"command": "diff <(echo a) <(echo a) && echo psub"})
        assert r.success and r.output.strip() == "psub"

    def test_shell_name_follows_the_executable(self, monkeypatch):
        from agent_cli import jobs as jobs_mod

        monkeypatch.setattr(jobs_mod, "SHELL", "/bin/bash")
        assert jobs_mod.shell_name() == "bash"
        monkeypatch.setattr(jobs_mod, "SHELL", None)
        assert jobs_mod.shell_name() == "sh"
