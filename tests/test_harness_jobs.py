"""`HarnessJobs` (agent_cli/harness/jobs.py, v10.42.0) — 하니스 작업은 팀원이 아니다.

여기서 고정하는 것은 **부재**다: 하니스 작업은 레지스트리에 들지 않으므로
로스터·주소 해석(`message`·`agent` request/kill)·`agents.json`·생존 상한·보드
칩 어디에도 없다. 그리고 실행기 자체: 프로파일은 `agents/harness/` 에서만,
기록은 `harness/<job>/<label>/`, 결과는 `deliver` 로, 세션 훅은 싣지 않고,
`stop()` 이 세운다.
"""

from __future__ import annotations

import threading
from pathlib import Path

from agent_cli.context.manager import ContextManager
from agent_cli.harness.jobs import (
    HARNESS_PROFILES_DIR,
    HarnessJobs,
    harness_profile_names,
    load_harness_profile,
)
from agent_cli.loop.ports import LoopPorts
from agent_cli.tools.result import ToolResult
from tests.binding_support import make_binding
from tests.test_agents_live import make_registry, make_runner, wait_until


def _ctx(tmp_path):
    return ContextManager(tmp_path / "s", max_context_tokens=10_000)


def _jobs(tmp_path, runner, **kw):
    delivered: list[tuple[str, bool, str]] = []
    jobs = HarnessJobs(
        session_ctx=_ctx(tmp_path),
        runtime={
            "binding": make_binding(model="m", provider="p"),
            "max_turns": 9,
            "hooks_config": {"x": 1},
        },
        deliver=lambda job, ok, out: delivered.append((job, ok, out)) or "",
        runner=runner,
        **kw,
    )
    return jobs, delivered


class TestProfiles:
    def test_harness_profiles_live_in_their_own_directory(self):
        assert HARNESS_PROFILES_DIR.name == "harness"
        assert HARNESS_PROFILES_DIR.parent.name == "agents"
        assert "auditor" in harness_profile_names()
        body, meta, err = load_harness_profile("auditor")
        assert err == "" and body and meta.get("allowed-tools")

    def test_unknown_profile_is_an_error_not_a_crash(self):
        body, meta, err = load_harness_profile("no-such-job")
        assert body == "" and meta == {} and "not found" in err

    def test_harness_profiles_never_hold_shell_or_monitor(self):
        """읽기 전용 하니스 작업 — 감시를 거는 쪽이 아니라 하니스 과제에 답하는 쪽."""
        for name in harness_profile_names():
            _b, meta, _e = load_harness_profile(name)
            allowed = meta.get("allowed-tools") or []
            assert "shell" not in allowed and "monitor" not in allowed, name


class TestRun:
    def test_runs_the_profile_as_a_oneshot_and_delivers_the_result(self, tmp_path):
        seen: dict = {}

        def runner(query, ctx, **kw):
            seen.update(kw, query=query, ctx=ctx)
            return ToolResult(True, "AUDIT pass"), 0.0

        jobs, delivered = _jobs(tmp_path, runner)
        assert jobs.run("auditor", "1-9", "Audit…") == ""
        assert wait_until(lambda: delivered)
        assert delivered == [("auditor", True, "AUDIT pass")]
        # 런 디렉토리 — 세션 안 harness/<job>/<label>/
        assert (
            seen["ctx"].session_dir
            == jobs.ctx.session_dir / "harness" / "auditor" / "1-9"
        )
        # 일회성 포트: 질문·레지스트리·메시지 없음 → ask/message/reply 가 없다
        ports = seen["ports"]
        assert isinstance(ports, LoopPorts)
        assert ports.owner == "harness:auditor"
        assert ports.questions is None and ports.agent_registry is None
        assert ports.message_handler is None
        assert ports.absorb_inbox is not None  # v10.84.0: 자기 메일함을 읽는 포트
        # 프로파일이 도구 상한·역할이고, 세션 셸 훅은 싣지 않는다
        assert seen["active_tools"] == ["history", "read_file", "code_index"]
        assert seen["agent_name"] == "auditor" and "Auditor" in seen["agent_role"]
        assert seen["hooks_config"] is None
        assert seen["max_turns"] == 9 and seen["binding"].provider == "p"
        assert seen["stop_event"] is jobs._stop

    def test_failure_delivers_the_reason(self, tmp_path):
        jobs, delivered = _jobs(
            tmp_path, lambda q, c, **kw: (ToolResult(False, error="Max turns (3)"), 0)
        )
        jobs.run("auditor", "1-2", "t")
        assert wait_until(lambda: delivered)
        assert delivered == [
            ("auditor", False, "auditor did not complete: Max turns (3)")
        ]

    def test_crash_delivers_too(self, tmp_path):
        jobs, delivered = _jobs(tmp_path, make_runner(exc=RuntimeError("boom")))
        jobs.run("auditor", "1-2", "t")
        assert wait_until(lambda: delivered)
        (_job, ok, out) = delivered[0]
        assert not ok and out == "auditor crashed: RuntimeError: boom"
        assert wait_until(lambda: jobs.in_flight == 0)

    def test_unknown_job_is_refused_before_a_thread_starts(self, tmp_path):
        jobs, delivered = _jobs(tmp_path, make_runner())
        assert "not found" in jobs.run("no-such-job", "1-2", "t")
        assert jobs.in_flight == 0 and delivered == []

    def test_in_flight_and_on_change_bracket_the_delivery(self, tmp_path):
        gate = threading.Event()
        changes: list[int] = []
        jobs, delivered = _jobs(
            tmp_path, make_runner(block=gate), on_change=changes.append
        )
        jobs.run("auditor", "1-2", "t")
        assert jobs.in_flight == 1 and changes == [1] and delivered == []
        gate.set()
        assert wait_until(lambda: changes == [1, 0])
        assert delivered and jobs.in_flight == 0

    def test_stop_refuses_new_jobs_and_joins(self, tmp_path):
        gate = threading.Event()
        jobs, _delivered = _jobs(tmp_path, make_runner(block=gate))
        jobs.run("auditor", "1-2", "t")
        gate.set()
        jobs.stop()
        assert jobs.stopped and jobs.in_flight == 0
        assert jobs.run("auditor", "3-4", "t") == "harness jobs are stopped"

    def test_on_change_errors_never_break_delivery(self, tmp_path):
        def boom(n):
            raise RuntimeError("surface gone")

        jobs, delivered = _jobs(tmp_path, make_runner("ok"), on_change=boom)
        jobs.run("auditor", "1-2", "t")
        assert wait_until(lambda: delivered)


class TestIsolationFromTheRegistry:
    """격리는 필터가 아니라 부재다 — 레지스트리의 어느 조회면도 하니스 작업을
    모른다."""

    def test_registry_never_sees_a_running_harness_job(self, tmp_path):
        reg = make_registry(tmp_path)
        gate = threading.Event()
        ctx = _ctx(tmp_path)
        jobs = HarnessJobs(
            session_ctx=ctx,
            runtime={"model": "m"},
            deliver=lambda *a: "",
            runner=make_runner(block=gate),
        )
        try:
            jobs.run("auditor", "1-2", "t")
            assert jobs.in_flight == 1
            assert reg.roster_snapshot() == [] and reg.alive_count() == 0
            assert reg.resolve_key("auditor") is None
            assert reg.kill("auditor").startswith("unknown agent")
            assert reg.request("auditor", "hi", author="main")
            assert not reg.has_active_work() and not reg.any_activity()
            assert not (Path(tmp_path) / "agents.json").exists()
            assert not (ctx.session_dir / "agents").exists()
        finally:
            gate.set()
            jobs.stop()
            reg.shutdown_all()

    def test_user_catalog_and_peer_roster_do_not_list_harness_profiles(self, tmp_path):
        from agent_cli.prompts.system_prompt import build_live_agents_section
        from agent_cli.subagent.profiles import available_profiles

        names = {n for n, _ in available_profiles(include_meta=True)}
        assert names.isdisjoint(set(harness_profile_names()))
        reg = make_registry(tmp_path)
        assert build_live_agents_section(reg, via_message_tool=True) == ""

    def test_main_tool_schema_cannot_spawn_a_harness_profile(self, tmp_path):
        """`agent` 도구의 spawn 은 사용자 카탈로그 프로파일만 안다."""
        from agent_cli.subagent.agents_live import _agent_spawn

        reg = make_registry(tmp_path)
        res = _agent_spawn(
            reg,
            {"profile": "auditor"},
            parent_ctx=_ctx(tmp_path),
            runtime={"model": "m"},
        )
        assert not res.success and "not found" in (res.error or "")


class TestScopeCard:
    """v10.53.0: 하니스 런 하나 = 스코프 하나(``kind="harness"``). 종전엔 스코프를
    열지 않아 렌더러가 감사 스레드의 카드를 전부 main 의 것으로 그렸다 —
    main 이 쉬는 동안 스스로 history·read_file 을 도는 것처럼 보였다."""

    class _Rec:
        """scope 사건만 적고 나머지는 실제 CLI 렌더러(StringIO)에 넘긴다 — 런
        경로가 부르는 다른 표면(``note_scope_ctx`` 등)이 그대로 돈다."""

        def __init__(self):
            import io

            from rich.console import Console

            from agent_cli.render.minimal import MinimalRenderer

            self.events: list[tuple[str, dict, str]] = []
            self._inner = MinimalRenderer(Console(file=io.StringIO()))

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def begin_scope(self, **kw):
            self.events.append(("begin", kw, threading.current_thread().name))

        def end_scope(self, **kw):
            self.events.append(("end", kw, threading.current_thread().name))

    def _patch(self, monkeypatch, r):
        from agent_cli import render

        monkeypatch.setattr(render, "get_renderer", lambda: r)

    def test_the_run_is_bracketed_on_its_own_thread(self, tmp_path, monkeypatch):
        r = self._Rec()
        self._patch(monkeypatch, r)
        seen: list[str] = []

        def runner(q, c, **kw):
            seen.append(threading.current_thread().name)
            return ToolResult(True, output="AUDIT ok"), 0

        jobs, delivered = _jobs(tmp_path, runner)
        jobs.run("auditor", "3-9", "t")
        assert wait_until(lambda: delivered)
        (b, bkw, bt), (e, ekw, et) = r.events
        assert (b, e) == ("begin", "end") and bt == et == seen[0]  # 런과 같은 스레드
        assert bkw == {
            "task_id": "harness-auditor-3-9",
            "kind": "harness",
            "label": "3-9",
            "agent": "auditor",
            "parent": "",  # main 의 어떤 스코프 안도 아니다
            "ctx_dir": "harness/auditor/3-9",  # resume 이 카드 안을 채우는 자리
        }
        assert ekw["task_id"] == "harness-auditor-3-9" and ekw["success"] is True
        assert ekw["error"] == ""

    def test_a_crash_still_closes_the_card_with_the_reason(self, tmp_path, monkeypatch):
        r = self._Rec()
        self._patch(monkeypatch, r)
        jobs, delivered = _jobs(tmp_path, make_runner(exc=RuntimeError("boom")))
        jobs.run("auditor", "1-2", "t")
        assert wait_until(lambda: delivered)
        end = r.events[-1][1]
        assert end["success"] is False and "boom" in end["error"]

    def test_a_broken_renderer_never_blocks_delivery(self, tmp_path, monkeypatch):
        class _Broken(self._Rec):
            def begin_scope(self, **kw):
                raise RuntimeError("renderer down")

            def end_scope(self, **kw):
                raise AssertionError("not opened — must not close")

        self._patch(monkeypatch, _Broken())
        jobs, delivered = _jobs(
            tmp_path, lambda q, c, **kw: (ToolResult(True, output="ok"), 0)
        )
        jobs.run("auditor", "1-2", "t")
        assert wait_until(lambda: delivered) and delivered[0][1] is True

    def test_web_routes_the_audit_threads_cards_into_the_card(
        self, tmp_path, monkeypatch
    ):
        """보고서의 탐침 그대로: 스코프 없는 스레드의 카드는 task_id=None(main).
        이제 그 스레드의 카드가 하니스 카드의 task_id 를 단다."""
        from agent_cli import render
        from agent_cli.render.web import WebConnection, WebRenderer

        r = WebRenderer()
        conn = WebConnection(id="c")
        r.register_connection(conn)
        monkeypatch.setattr(render, "_renderer", r)
        monkeypatch.setattr(render, "get_renderer", lambda: r)

        def runner(q, c, **kw):
            render.render_step("action", "{}", 1, tool_name="history")
            return ToolResult(True, output="AUDIT ok"), 0

        jobs, delivered = _jobs(tmp_path, runner)
        jobs.run("auditor", "1-4", "t")
        assert wait_until(lambda: delivered)
        events = []
        while not conn.queue.empty():
            item = conn.queue.get_nowait()
            if isinstance(item, tuple):
                events.append(item)
        kinds = [(e, (d or {}).get("task_id")) for e, d in events]
        assert ("scope_start", "harness-auditor-1-4") in kinds
        cards = [d for e, d in events if e == "assistant_turn"]
        assert cards and all(d.get("task_id") == "harness-auditor-1-4" for d in cards)
        assert ("scope_end", "harness-auditor-1-4") in kinds


class TestCliHarnessScope:
    def test_output_is_swallowed_and_one_line_is_printed(self):
        import io

        from rich.console import Console

        from agent_cli.render.minimal import MinimalRenderer

        buf = io.StringIO()
        r = MinimalRenderer(Console(file=buf, force_terminal=False, width=200))
        out: list[str] = []

        def job():
            r.begin_scope(
                task_id="harness-auditor-1-4",
                kind="harness",
                label="1-4",
                agent="auditor",
            )
            r._p("⚡ history → {query}")  # 감사 스레드의 단계 — main 에 섞이면 안 된다
            r.end_scope(
                task_id="harness-auditor-1-4",
                kind="harness",
                success=True,
                duration_s=3.2,
            )
            out.append(buf.getvalue())

        t = threading.Thread(target=job)
        t.start()
        t.join()
        text = out[0]
        assert "history" not in text
        assert "🔎 auditor · 1-4 ✓ (3.2s)" in text
