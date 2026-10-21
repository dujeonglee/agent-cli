"""세션 감사자 (docs/auditor/DESIGN.md 4판, v10.42.0) — 하니스 몫의 회귀 가드.

4판에서 감사자는 **팀원이 아니라 하니스 작업**이다(`agent_cli/harness/`).
하니스가 하는 일은 셋뿐이다: ① main 의 **사용자 런**이 `complete` 로 끝날 때마다
그 런의 history 구간을 적은 과제로 감사 런 하나를 돌린다(깨우기 런·실패한 런은
돌리지 않는다 — 고리 상한은 코드) ② 그 런의 `complete` 결과를 main 메일함에 넣는다
(모델의 선택이 아니다) ③ 프로파일이 약속한 도구 집합·판정 규칙을 그대로 싣는다.
감사 자체(구간 조회·판정)는 모델의 일이라 실측(§6)으로만 잰다.

노브는 `AGENT_CLI_AUDITOR`(부팅값, 기본 꺼짐) + web 🔎 칩(`/api/auditor`, 세션
도중). 레지스트리 격리(로스터·주소·agents.json·상한에 없음)는
`tests/test_harness_jobs.py` 가 고정한다.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_cli.constants import AUDIT_REQUEST, AUDITOR_NAME
from agent_cli.context.manager import ContextManager
from agent_cli.harness.auditor import SessionAuditor, build_auditor
from agent_cli.harness.jobs import HarnessJobs, load_harness_profile
from agent_cli.knobs import KNOB_BY_NAME, KnobSession
from agent_cli.pump import WAKE_AUTHOR, RunRequest
from agent_cli.subagent.profiles import available_profiles, load_profile
from agent_cli.tools.result import ToolResult
from tests.test_agents_live import make_registry, make_runner, wait_until


def _ctx(tmp_path):
    return ContextManager(tmp_path / "s", max_context_tokens=10_000)


def _req(*, wake: bool = False, author=None) -> RunRequest:
    return RunRequest(
        text="q",
        author=WAKE_AUTHOR if wake else author,
        author_is_user=not wake,
        request_id="",
        stop_event=threading.Event(),
        wake=wake,
    )


def _complete(ctx, result="done"):
    ctx.add(
        {
            "role": "assistant",
            "thought": "",
            "ops": [{"action": "complete", "action_input": {"result": result}}],
        }
    )


class _FakeJobs:
    """`HarnessJobs` 의 표면만 — run() 호출을 기록한다."""

    def __init__(self, error: str = ""):
        self.runs: list[tuple[str, str, str]] = []
        self.error = error
        self.stopped = False
        self.in_flight = 0

    def run(self, job, label, task):
        self.runs.append((job, label, task))
        return self.error

    def stop(self):
        self.stopped = True


class TestProfile:
    def test_lives_outside_the_user_catalog(self):
        """하니스 프로파일은 `agents/harness/` 에만 있다 — 사용자 카탈로그
        (`@agents`·spawn·delegate)가 그 이름을 **모른다**. 광고 표지가 아니라
        검색 경로의 부재가 격리다."""
        body, cfg, err = load_harness_profile(AUDITOR_NAME)
        assert err == "" and body and cfg
        assert AUDITOR_NAME not in [n for n, _ in available_profiles()]
        assert AUDITOR_NAME not in [n for n, _ in available_profiles(include_meta=True)]
        _b, _c, user_err = load_profile(AUDITOR_NAME)
        assert user_err and "not found" in user_err

    def test_tool_set_is_read_only_and_cannot_ask_message_or_delegate(self):
        _, cfg, _ = load_harness_profile(AUDITOR_NAME)
        assert cfg["allowed-tools"] == ["history", "read_file", "code_index"]
        for forbidden in ("shell", "ask", "write_file", "edit_file", "agent", "fetch"):
            assert forbidden not in cfg["allowed-tools"]
        assert "disable-model-invocation" not in cfg  # 카탈로그 밖이라 표지가 필요 없다

    def test_body_carries_the_rules_the_design_relies_on(self):
        body, _, _ = load_harness_profile(AUDITOR_NAME)
        for phrase in (
            "seq BETWEEN",  # 서수 구간만
            'sessions:"<main session id from the task>"',  # 자기 history 가 아니라 main 의
            "loc LIKE '<session id>/history.jsonl:%'",  # 다른 에이전트 파일을 뺀다
            "unverifiable",  # 근거 없는 주장
            "A `met` without a seq is not a `met`",  # 예스맨 방지
            "Your `complete` result IS the report",  # 4판: 보고 = complete 결과
            "A deliverable file is judged by the file",  # 전사 인용이 아니라 파일
            "A cut column is not evidence",  # substr 잘림을 부재로 읽지 않는다
            "Do not keep probing",  # 없는 파일을 40턴 찾던 B-3
        ):
            assert phrase in body, phrase
        # 3판까지의 reply·message·구독 경로는 사라졌다 — 모델이 고를 길이 없다
        for gone in ("`reply`", "message(", "monitor(", "`complete` is refused"):
            assert gone not in body, gone


class TestRequestText:
    def test_request_names_range_session_filter_and_complete(self):
        text = AUDIT_REQUEST.format(a=12, b=40, session="1791", history="/w/h.jsonl")
        assert "seq 12..40" in text and "seq BETWEEN 12 AND 40" in text
        assert 'sessions:"1791"' in text
        assert "loc LIKE '1791/history.jsonl:%'" in text
        assert "/w/h.jsonl" in text
        assert "`complete`" in text and "IS the report" in text
        assert "reply" not in text

    def test_history_seq_is_the_one_based_line_number(self, tmp_path):
        """과제의 `seq a..b` 와 프로파일의 SQL 은 `seq` 가 줄 번호(1 기준)라는
        전제 위에 있다 — 서수(0 기준)와 다르니 핸들이 +1 한다."""
        from agent_cli.tools.history import tool_history

        ctx = _ctx(tmp_path)
        ctx.add({"role": "system", "content": "sys"})
        ctx.set_turn(1)
        ctx.add({"role": "user", "content": "q1"})
        _complete(ctx, "r1")
        ctx.add({"role": "user", "content": "q2", "author": WAKE_AUTHOR})
        res = tool_history(
            {"query": "SELECT seq, kind, author FROM history ORDER BY seq"},
            session_dir=ctx.history_path.parent,
        )
        assert res.success, res.error
        rows = res.output
        assert "seq=3 | kind=final" in rows, rows
        assert f"seq=4 | kind=query | author={WAKE_AUTHOR}" in rows, rows
        assert ctx.next_ordinal == 4


class TestRunEnded:
    """main 의 런이 끝났을 때 — 사용자 런이 complete 로 끝났을 때만, 그 런의
    레코드 구간만, 감사 작업 하나로."""

    def _handle(self, ctx, *, enabled=True, jobs=None):
        jobs = jobs if jobs is not None else _FakeJobs()
        return SessionAuditor(jobs, ctx, enabled=enabled), jobs

    def test_user_run_starts_one_audit_job_for_the_runs_range(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.add({"role": "system", "content": "sys"})  # seq 1 — 핸들 전의 history
        h, jobs = self._handle(ctx)  # 커서 = 다음 서수 (seq 2)
        ctx.set_turn(1)
        ctx.add({"role": "user", "content": "do it"})  # seq 2
        _complete(ctx)  # seq 3
        text = h.run_ended(_req(), ok=True)
        assert "seq 2..3" in text and "seq BETWEEN 2 AND 3" in text
        assert f'sessions:"{ctx.session_dir.name}"' in text
        assert str(ctx.history_path) in text
        assert jobs.runs == [(AUDITOR_NAME, "2-3", text)]

    def test_consecutive_runs_get_disjoint_ranges(self, tmp_path):
        ctx = _ctx(tmp_path)
        h, jobs = self._handle(ctx)
        ctx.add({"role": "user", "content": "one"})
        _complete(ctx)
        assert "seq 1..2" in h.run_ended(_req(), ok=True)
        ctx.add({"role": "user", "content": "two"})
        ctx.add({"role": "user", "tool": "shell", "success": True, "content": "x"})
        _complete(ctx)
        assert "seq 3..5" in h.run_ended(_req(), ok=True)
        assert [label for _, label, _ in jobs.runs] == ["1-2", "3-5"]

    def test_wake_run_is_not_audited_but_still_moves_the_cursor(self, tmp_path):
        """고리 상한: 감사 결과로 깨어난 런은 감사하지 않는다 — 그리고 그
        레코드가 다음 사용자 런의 구간에 섞이지도 않는다."""
        ctx = _ctx(tmp_path)
        h, jobs = self._handle(ctx)
        ctx.add({"role": "user", "content": "wake", "author": WAKE_AUTHOR})
        _complete(ctx)
        assert h.run_ended(_req(wake=True), ok=True) == ""
        assert jobs.runs == []
        ctx.add({"role": "user", "content": "next"})
        _complete(ctx)
        assert "seq 3..4" in h.run_ended(_req(), ok=True)

    def test_failed_run_has_no_final_to_audit(self, tmp_path):
        ctx = _ctx(tmp_path)
        h, jobs = self._handle(ctx)
        ctx.add({"role": "user", "content": "q"})
        assert h.run_ended(_req(), ok=False) == ""  # 턴 소진·중단
        assert jobs.runs == []
        ctx.add({"role": "user", "content": "q2"})
        _complete(ctx)
        assert "seq 2..3" in h.run_ended(_req(), ok=True)

    def test_empty_run_sends_nothing(self, tmp_path):
        ctx = _ctx(tmp_path)
        h, jobs = self._handle(ctx)
        assert h.run_ended(_req(), ok=True) == ""  # 레코드가 없다
        assert jobs.runs == []

    def test_disabled_knob_skips_but_keeps_the_cursor_moving(self, tmp_path):
        """🔎 칩으로 끈 동안의 런은 감사하지 않고, 다시 켜면 그 뒤 런만 본다 —
        꺼져 있던 동안의 레코드가 다음 구간에 섞이지 않는다."""
        ctx = _ctx(tmp_path)
        h, jobs = self._handle(ctx, enabled=False)
        assert not h.active and not h.enabled and h.in_flight == 0
        ctx.add({"role": "user", "content": "q"})
        _complete(ctx)
        assert h.run_ended(_req(), ok=True) == "" and jobs.runs == []
        h.enabled = True
        ctx.add({"role": "user", "content": "q2"})
        _complete(ctx)
        assert "seq 3..4" in h.run_ended(_req(), ok=True)

    def test_no_session_handle_and_stopped_jobs_send_nothing(self, tmp_path):
        ctx = _ctx(tmp_path)
        h = SessionAuditor(None, ctx, enabled=True)
        assert not h.active and h.in_flight == 0 and h.jobs is None
        ctx.add({"role": "user", "content": "q"})
        _complete(ctx)
        assert h.run_ended(_req(), ok=True) == ""
        h.stop()  # jobs 없음 — no-op
        h2, jobs = self._handle(ctx)
        h2.stop()
        assert jobs.stopped and not h2.active
        ctx.add({"role": "user", "content": "q2"})
        _complete(ctx)
        assert h2.run_ended(_req(), ok=True) == "" and jobs.runs == []

    def test_job_start_error_reports_nothing_sent(self, tmp_path):
        ctx = _ctx(tmp_path)
        h, _jobs = self._handle(ctx, jobs=_FakeJobs(error="no profile"))
        ctx.add({"role": "user", "content": "q"})
        _complete(ctx)
        assert h.run_ended(_req(), ok=True) == ""


class TestLiveDelivery:
    """실 레지스트리 + 실 `HarnessJobs`(가짜 러너): 감사 런의 complete 결과가 main
    메일함에 하니스 결과로 들어온다 — 모델이 고르는 전달이 아니다."""

    def _live(self, tmp_path, runner):
        # 가짜 러너는 기록을 남기지 않으므로, 런 디렉토리 검증용으로 한 줄 적는다
        inner = runner

        def runner(query, ctx, **kw):
            ctx.add({"role": "user", "content": query})
            return inner(query, ctx, **kw)

        reg = make_registry(tmp_path)
        ctx = _ctx(tmp_path)
        auditor = build_auditor(reg, parent_ctx=ctx)
        auditor.enabled = True
        auditor.jobs._runner = runner
        return reg, ctx, auditor

    def test_complete_result_lands_in_mains_mailbox(self, tmp_path):
        reg, ctx, auditor = self._live(tmp_path, make_runner("AUDIT pass"))
        ctx.add({"role": "user", "content": "q"})
        _complete(ctx)
        text = auditor.run_ended(_req(), ok=True)
        assert text
        assert wait_until(reg.has_pending_replies)
        (mail,) = reg.drain_replies()
        assert mail["kind"] == "harness" and mail["job"] == AUDITOR_NAME
        assert mail["success"] is True and mail["output"] == f"AUDIT pass:{text}"
        assert wait_until(lambda: auditor.in_flight == 0)
        # 기록은 세션 안 harness/ 밑 — 팀원 자리(agents/)도, agents.json 도 아니다
        run_dir = ctx.session_dir / "harness" / AUDITOR_NAME / "1-2"
        assert (run_dir / "history.jsonl").is_file()
        assert not (ctx.session_dir / "agents").exists()

    def test_unfinished_run_delivers_the_reason_instead(self, tmp_path):
        """턴 소진·오류로 complete 가 없어도 main 은 결과 한 장을 받는다."""

        def runner(query, ctx, **kw):
            return ToolResult(False, error="Max turns (16) reached"), 0.0

        reg, ctx, auditor = self._live(tmp_path, runner)
        ctx.add({"role": "user", "content": "q"})
        _complete(ctx)
        assert auditor.run_ended(_req(), ok=True)
        assert wait_until(reg.has_pending_replies)
        (mail,) = reg.drain_replies()
        assert mail["success"] is False
        assert mail["output"] == "auditor did not complete: Max turns (16) reached"

    def test_in_flight_counts_until_delivered_and_stop_ends_it(self, tmp_path):
        gate = threading.Event()
        reg, ctx, auditor = self._live(tmp_path, make_runner("x", block=gate))
        ctx.add({"role": "user", "content": "q"})
        _complete(ctx)
        assert auditor.run_ended(_req(), ok=True)
        assert auditor.in_flight == 1 and not reg.has_pending_replies()
        gate.set()
        assert wait_until(lambda: auditor.in_flight == 0)
        assert reg.has_pending_replies()
        auditor.stop()
        assert not auditor.active and auditor.jobs.stopped


class TestMailRecord:
    def test_harness_result_is_an_observation_with_no_debt(self):
        from agent_cli.subagent.agents_live import build_reply_record

        rec = build_reply_record(
            {"kind": "harness", "job": "auditor", "success": True, "output": "AUDIT"}
        )
        assert rec["role"] == "user" and rec["tool"] == "auditor"
        assert rec["source"] == "harness" and rec["success"] is True
        assert rec["content"].startswith("── auditor ──\nAUDIT")
        assert "nothing to send back" in rec["content"]

    def test_mail_notice_line(self, monkeypatch):
        from agent_cli.main import _agent_mail_notice

        hints: list[dict] = []
        r = SimpleNamespace(agent_mail_hint=lambda **kw: hints.append(kw))
        monkeypatch.setattr("agent_cli.render.get_renderer", lambda: r)
        _agent_mail_notice(
            {"kind": "harness", "job": "auditor", "output": "AUDIT"}, True
        )
        assert hints and "[auditor] AUDIT" in hints[0]["text"]


class TestPumpWiring:
    def test_pump_tells_the_auditor_after_every_main_run(self):
        from agent_cli.input_queue import InputQueue
        from tests.pump_support import make_pump, run_pump_in_thread

        calls: list[tuple[bool, bool]] = []
        spy = SimpleNamespace(
            run_ended=lambda req, *, ok: calls.append((req.author_is_user, ok))
        )
        q = InputQueue()
        q.enqueue(None, "hi")
        done = run_pump_in_thread(
            make_pump(queue=q, run_main=lambda r: ToolResult(True, "x"), auditor=spy)
        )
        assert done.wait(2.0)
        assert calls == [(True, True)]

    def test_pump_without_an_auditor_is_unchanged(self):
        from agent_cli.input_queue import InputQueue
        from tests.pump_support import make_pump, run_pump_in_thread

        q = InputQueue()
        q.enqueue(None, "hi")
        done = run_pump_in_thread(
            make_pump(queue=q, run_main=lambda r: ToolResult(False, error="e"))
        )
        assert done.wait(2.0)


class TestLiveness:
    """진행 중인 감사는 결과를 배달해야 끝난다 — run 펌프와 web 의 idle self-reap
    둘 다 같은 술어(`session_has_live_work`)로 기다린다."""

    def test_session_has_live_work_counts_in_flight_harness_jobs(self):
        from agent_cli.runtime import session_has_live_work

        busy = SimpleNamespace(in_flight=1)
        idle = SimpleNamespace(in_flight=0)
        assert session_has_live_work(pending_count=0, harness=busy)
        assert not session_has_live_work(pending_count=0, harness=idle)
        assert not session_has_live_work(pending_count=0, harness=None)

    def test_quiet_policy_waits_and_announces_once(self):
        from agent_cli.input_queue import InputQueue
        from agent_cli.pump import QuietPolicy

        reg = SimpleNamespace(has_active_work=lambda: False)
        harness = SimpleNamespace(in_flight=1)
        waits: list[str] = []
        pol = QuietPolicy(
            queue=InputQueue(),
            agent_registry=reg,
            harness=harness,
            on_wait=waits.append,
        )
        assert pol.should_stop() is False and pol.should_stop() is False
        assert waits == ["harness"]
        harness.in_flight = 0
        assert pol.should_stop() is True

    def test_web_instance_stays_active_while_auditing(self):
        from agent_cli.main import web_instance_is_active

        r = SimpleNamespace(
            has_live_connections=lambda: False, worker_is_busy=lambda: False
        )
        s = SimpleNamespace(pending_count=lambda: 0)
        assert web_instance_is_active(r, s, None, harness=SimpleNamespace(in_flight=1))
        assert not web_instance_is_active(
            r, s, None, harness=SimpleNamespace(in_flight=0)
        )

    def test_teardown_stops_the_harness_before_the_registry(self):
        from agent_cli.runtime import teardown_session

        order: list[str] = []
        harness = SimpleNamespace(stop=lambda: order.append("harness"))
        reg = SimpleNamespace(
            shutdown_all=lambda: order.append("registry"),
            open_human_question_keys=list,
        )
        teardown_session(None, None, agent_registry=reg, harness=harness)
        assert order == ["harness", "registry"]


class TestAssembly:
    def _registry(self):
        registry = MagicMock()
        registry.restore.return_value = 0
        registry.auto_spawn.return_value = 0
        registry.runtime = {"binding": "b"}
        return registry

    def test_build_auditor_starts_disabled_until_the_knob_sets_it(self, tmp_path):
        """켜는 것은 노브 `auditor`(`knobs.apply_all`: CLI > env > 기본값) — 여기선
        env 를 읽지 않는다. 실행기는 세션이 있으면 늘 선다."""
        changes: list[int] = []
        auditor = build_auditor(
            self._registry(), parent_ctx=_ctx(tmp_path), on_change=changes.append
        )
        assert not auditor.enabled and not auditor.active and auditor.jobs is not None
        assert auditor.jobs.on_change == changes.append
        assert auditor.jobs.runtime == {"binding": "b"}
        KNOB_BY_NAME["auditor"].set(KnobSession(auditor=auditor), True)
        assert auditor.enabled and auditor.active and auditor.in_flight == 0

    def test_no_session_means_no_handle(self):
        assert build_auditor(self._registry(), parent_ctx=None) is None

    def test_wire_agent_mail_does_not_build_it(self):
        """감사자는 wire 앞에서 만들고 노브를 적용한다 — 상한 노브가 restore/
        auto-spawn 의 게이트라서. wire 는 (waker, revived, auto) 셋만 돌려준다."""
        from agent_cli.runtime import wire_agent_mail

        out = wire_agent_mail(
            self._registry(),
            enqueue_wake=MagicMock(),
            on_mail_notice=lambda r: None,
            parent_ctx=MagicMock(),
        )
        assert len(out) == 3

    def test_delivery_goes_through_the_registry_mailbox(self, tmp_path):
        """`build_auditor` 의 배달은 모니터 보고와 같은 `registry.deliver("main")`."""
        registry = self._registry()
        registry.deliver.return_value = ""
        auditor = build_auditor(registry, parent_ctx=_ctx(tmp_path))
        auditor.jobs._deliver(AUDITOR_NAME, True, "AUDIT pass")
        registry.deliver.assert_called_once_with(
            "main",
            mail={
                "kind": "harness",
                "job": AUDITOR_NAME,
                "success": True,
                "output": "AUDIT pass",
            },
            text="AUDIT pass",
            author=AUDITOR_NAME,
            expects_reply=False,
        )

    def test_boot_line_only_when_enabled(self):
        from agent_cli.main import _auditor_boot_line

        assert "켜짐" in _auditor_boot_line(SimpleNamespace(enabled=True))
        assert _auditor_boot_line(SimpleNamespace(enabled=False)) == ""
        assert _auditor_boot_line(None) == ""

    def test_web_boot_announces_the_auditor(self):
        from agent_cli.main import _announce_agent_boot
        from agent_cli.render.web import WebConnection, WebRenderer

        r = WebRenderer()
        conn = WebConnection(id="c")
        r.register_connection(conn)
        _announce_agent_boot(
            r, revived=0, auto=0, auditor=SimpleNamespace(enabled=True)
        )
        events = []
        while not conn.queue.empty():
            events.append(conn.queue.get_nowait())
        assert any(e == "status" and "🔎" in str(d) for e, d in events), events


class TestWebKnob:
    """🔎 칩 — 노브 `auditor` 의 web 표면은 `/api/knobs` (2단계): 세션 도중 켜고
    끄고, sticky `knob:auditor` 로 뷰어를 맞추고, 진행 중 수는 status.json
    `auditing` 과 같은 sticky 의 `in_flight` 로 보드·칩에 간다."""

    def _app(self, tmp_path, auditor):
        from fastapi.testclient import TestClient

        from agent_cli.render.web import WebRenderer
        from agent_cli.web.server import WebServer, create_app

        renderer = WebRenderer(session_dir=str(tmp_path))
        server = WebServer(renderer, token="t")
        server.auditor = auditor
        return renderer, server, TestClient(create_app(server))

    def _state(self, client, name="auditor"):
        ks = client.get("/api/knobs?token=t").json()["knobs"]
        return next(k for k in ks if k["name"] == name)

    def test_api_reads_and_flips_the_knob(self, tmp_path):
        ctx = _ctx(tmp_path)
        auditor = SessionAuditor(_FakeJobs(), ctx, enabled=False)
        renderer, _server, client = self._app(tmp_path, auditor)
        st = self._state(client)
        assert st["available"] and st["value"] is False and st["in_flight"] == 0
        r = client.post("/api/knobs/auditor?token=t", json={"value": True}).json()
        assert r["ok"] and r["value"] is True and auditor.enabled
        assert renderer._sticky["knob:auditor"]["payload"]["value"] is True
        assert client.get("/api/health?token=t").json()["auditing"] is False
        r = client.post("/api/knobs/auditor?token=t", json={"value": "off"}).json()
        assert r["ok"] and not auditor.enabled

    def test_no_session_hides_the_chip(self, tmp_path):
        _r, _s, client = self._app(tmp_path, None)
        assert self._state(client)["available"] is False
        assert (
            client.post("/api/knobs/auditor?token=t", json={"value": True}).json()["ok"]
            is False
        )

    def test_in_flight_reaches_status_file_and_the_chip(self, tmp_path):
        from agent_cli.knobs import KNOB_BY_NAME, web_state
        from agent_cli.web.instance_file import read_status_file

        ctx = _ctx(tmp_path)
        auditor = SessionAuditor(_FakeJobs(), ctx, enabled=True)
        renderer, server, _client = self._app(tmp_path, auditor)
        # web 부트의 배선: 진행 수가 바뀌면 상태 한 장을 다시 계산해 싣는다 (v10.45.0)
        renderer.knob_state = lambda name: web_state(
            KNOB_BY_NAME[name], server.knob_session()
        )
        auditor.jobs.in_flight = 1
        renderer.note_harness(1)
        assert read_status_file(tmp_path)["auditing"] is True
        payload = renderer._sticky["knob:auditor"]["payload"]
        assert payload["in_flight"] == 1 and payload["badge"] == "auditing"
        auditor.jobs.in_flight = 0
        renderer.note_harness(0)
        assert read_status_file(tmp_path)["auditing"] is False
        assert renderer._sticky["knob:auditor"]["payload"]["badge"] == "on"

    def test_without_the_wiring_status_still_updates(self, tmp_path):
        renderer, _server, _client = self._app(tmp_path, None)
        renderer.note_harness(1)  # knob_state 없음 — status.json 만
        from agent_cli.web.instance_file import read_status_file

        assert read_status_file(tmp_path)["auditing"] is True
        assert "knob:auditor" not in renderer._sticky

    def test_chip_comes_from_the_table(self, tmp_path):
        """🔎 칩은 표의 선언이다 (v10.45.0) — 아이콘·선택지 글자·배지·설명·켜짐."""
        from agent_cli.knobs import KNOB_BY_NAME, KnobSession, web_state

        k = KNOB_BY_NAME["auditor"]
        assert k.ui.icon == "🔎" and k.ui.placement == "chip"
        assert k.ui.choice_labels == {"off": "off (default)", "on": "on"}
        auditor = SessionAuditor(_FakeJobs(), _ctx(tmp_path), enabled=True)
        st = web_state(k, KnobSession(auditor=auditor))
        assert (
            st["badge"] == "on"
            and st["on"] is True
            and "From the next user run" in st["note"]
        )
        auditor.jobs.in_flight = 2
        st = web_state(k, KnobSession(auditor=auditor))
        assert st["badge"] == "auditing" and "in progress" in st["note"]


class TestJobsRuntimeContract:
    def test_runtime_keeps_only_runner_keys(self, tmp_path):
        jobs = HarnessJobs(
            session_ctx=_ctx(tmp_path),
            runtime={"binding": "b", "timeout": 5, "provider": "p"},
            deliver=lambda *a: "",
        )
        assert jobs.runtime == {"binding": "b"}  # 여섯 값은 바인딩 하나 (v10.43.0)
