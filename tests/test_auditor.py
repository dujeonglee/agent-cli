"""세션 감사자 (docs/auditor/DESIGN.md 3판, v10.42.0) — 하니스 몫의 회귀 가드.

하니스가 하는 일은 셋뿐이다: ① 노브대로 감사자를 하나 띄우거나 내린다
(살아 있는 감사자는 늘 0 또는 1) ② main 의 **사용자 런**이 `complete` 로
끝날 때마다 그 런의 history 구간을 `request(expects_reply=True)` 로 보낸다
(깨우기 런·실패한 런은 보내지 않는다 — 고리 상한은 코드) ③ 프로파일이
약속한 도구 집합·판정 규칙을 그대로 싣는다. 감사 자체(구간 조회·판정)는
모델의 일이라 실측(§6)으로만 잰다. 회신은 기존 회신 빚이 되돌린다.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_cli.constants import AUDIT_REQUEST, AUDITOR_NAME, auditor_enabled
from agent_cli.context.manager import ContextManager
from agent_cli.pump import WAKE_AUTHOR, RunRequest
from agent_cli.runtime import SessionAuditor, ensure_auditor
from agent_cli.subagent.profiles import available_profiles, load_profile
from agent_cli.tools.result import ToolResult
from tests.test_agents_live import _RecordingRunner, make_registry, wait_until


def _ctx(tmp_path):
    return ContextManager(tmp_path / "s", max_context_tokens=10_000)


def _auditors(reg):
    return [
        tm
        for tm in reg._agents.values()
        if (tm.profile_name, tm.instance_name) == (AUDITOR_NAME, AUDITOR_NAME)
    ]


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


class TestKnob:
    def test_default_off_and_truthy_words(self, monkeypatch):
        monkeypatch.delenv("AGENT_CLI_AUDITOR", raising=False)
        assert auditor_enabled() is False
        for v in ("1", "true", "YES", " on "):
            monkeypatch.setenv("AGENT_CLI_AUDITOR", v)
            assert auditor_enabled() is True, v
        for v in ("0", "false", "no", "off", ""):
            monkeypatch.setenv("AGENT_CLI_AUDITOR", v)
            assert auditor_enabled() is False, v


class TestProfile:
    def test_tool_set_is_read_only_and_cannot_ask_or_delegate(self):
        _, cfg, err = load_profile(AUDITOR_NAME)
        assert err is None
        assert cfg["allowed-tools"] == ["history", "read_file", "code_index", "memory"]
        for forbidden in ("shell", "ask", "write_file", "edit_file", "agent", "fetch"):
            assert forbidden not in cfg["allowed-tools"]
        # 하니스 소유 — 모델에게 광고하지 않는다(main 이 두 번째를 띄우지 않게)
        assert cfg.get("disable-model-invocation") is True
        assert AUDITOR_NAME not in [n for n, _ in available_profiles()]

    def test_body_carries_the_rules_the_design_relies_on(self):
        body, _, _ = load_profile(AUDITOR_NAME)
        for phrase in (
            "seq BETWEEN",  # 서수 구간만
            'sessions:"<main session id from the request>"',  # 자기 history 가 아니라 main 의
            "loc LIKE '<session id>/history.jsonl:%'",  # 에이전트 파일(자기 것 포함)을 뺀다
            "unverifiable",  # 근거 없는 주장
            "A `met` without a seq is not a `met`",  # 예스맨 방지
            "sent with `reply`",  # 보고는 회신 — 하니스가 빚으로 강제 (1·2차 실측 2/10 전달)
            "`complete` is refused",
            "`pass` and `findings` alike go back the same way",
            "A deliverable file is judged by the file",  # 전사 인용이 아니라 파일 (B-1/B-3 오탐)
            "A cut column is not evidence",  # substr 잘림을 부재로 읽지 않는다 (B-1)
            "Do not keep probing",  # 없는 파일을 40턴 찾던 B-3
        ):
            assert phrase in body, phrase
        # 2판까지의 구독·message 경로는 사라졌다 — 모델이 고를 길을 남기지 않는다
        assert "monitor(" not in body and "message(" not in body


class TestRequestText:
    def test_request_names_range_session_filter_and_reply(self):
        text = AUDIT_REQUEST.format(a=12, b=40, session="1791", history="/w/h.jsonl")
        assert "seq 12..40" in text and "seq BETWEEN 12 AND 40" in text
        assert 'sessions:"1791"' in text  # 감사자의 history 기본값은 자기 것
        assert (
            "loc LIKE '1791/history.jsonl:%'" in text
        )  # 같은 테이블에 에이전트 파일도
        assert "/w/h.jsonl" in text
        assert "`reply`" in text and "`complete` is not a report" in text

    def test_history_seq_is_the_one_based_line_number(self, tmp_path):
        """요청의 `seq a..b` 와 프로파일의 SQL 은 `seq` 가 줄 번호(1 기준)라는
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
        assert ctx.next_ordinal == 4  # 레코드 4개 = 마지막 seq 4


class TestEnsureAuditor:
    def test_off_without_an_auditor_is_an_inert_handle(self, tmp_path):
        reg = make_registry(tmp_path)
        h = ensure_auditor(reg, parent_ctx=_ctx(tmp_path), enabled=False)
        assert h.outcome == "" and not h.active
        assert _auditors(reg) == []
        assert h.run_ended(_req(), ok=True) == ""

    def test_no_session_means_nothing_to_audit(self, tmp_path):
        reg = make_registry(tmp_path)
        assert ensure_auditor(reg, parent_ctx=None, enabled=True) is None
        assert _auditors(reg) == []

    def test_on_spawns_exactly_one_and_sends_nothing_at_boot(self, tmp_path):
        runner = _RecordingRunner()
        reg = make_registry(tmp_path, runner=runner)
        ctx = _ctx(tmp_path)
        try:
            h = ensure_auditor(reg, parent_ctx=ctx, enabled=True)
            assert h.outcome == "spawned" and h.active
            (tm,) = _auditors(reg)
            assert tm.state != "dead" and h.key == tm.key
            # 두 번째 부팅(restore 로 되살아난 것과 같다): 새로 띄우지 않는다
            again = ensure_auditor(reg, parent_ctx=ctx, enabled=True)
            assert again.outcome == "" and again.key == tm.key
            assert len(_auditors(reg)) == 1
            # 시작 브리프·구독 의식이 없다 — 감사자는 요청이 올 때까지 논다
            assert not wait_until(lambda: runner.queries(), timeout=0.3)
        finally:
            reg.shutdown_all()

    def test_off_kills_a_live_auditor_once(self, tmp_path):
        reg = make_registry(tmp_path)
        ctx = _ctx(tmp_path)
        try:
            ensure_auditor(reg, parent_ctx=ctx, enabled=True)
            assert (
                ensure_auditor(reg, parent_ctx=ctx, enabled=False).outcome == "killed"
            )
            (tm,) = _auditors(reg)
            assert wait_until(lambda: tm.state == "dead")
            assert ensure_auditor(reg, parent_ctx=ctx, enabled=False).outcome == ""
        finally:
            reg.shutdown_all()

    def test_on_after_off_resumes_the_tombstone_instead_of_a_second_instance(
        self, tmp_path
    ):
        reg = make_registry(tmp_path)
        ctx = _ctx(tmp_path)
        try:
            ensure_auditor(reg, parent_ctx=ctx, enabled=True)
            ensure_auditor(reg, parent_ctx=ctx, enabled=False)
            (tm,) = _auditors(reg)
            assert wait_until(lambda: tm.state == "dead")
            h = ensure_auditor(reg, parent_ctx=ctx, enabled=True)
            assert h.outcome == "resumed" and h.key == tm.key
            (revived,) = _auditors(
                reg
            )  # 부활은 같은 키의 새 개체 — 툼스톤이 둘이 되지 않는다
            assert revived.key == tm.key and revived.state != "dead"
        finally:
            reg.shutdown_all()

    def test_boot_line_for_each_outcome(self):
        from agent_cli.main import _auditor_boot_line

        line = lambda o: _auditor_boot_line(SimpleNamespace(outcome=o))
        assert "상주 시작" in line("spawned")
        assert "부활" in line("resumed")
        assert "내렸습니다" in line("killed")
        assert "boom" in line("error: boom")
        assert line("") == "" and _auditor_boot_line(None) == ""


class TestRunEnded:
    """main 의 런이 끝났을 때 — 사용자 런이 complete 로 끝났을 때만, 그 런의
    레코드 구간만, 회신을 빚지는 요청으로."""

    def _handle(self, ctx, *, registry=None, key="agt-aud"):
        reg = registry if registry is not None else MagicMock()
        if registry is None:
            reg.instance_for.return_value = SimpleNamespace(key=key, state="idle")
            reg.request.return_value = ""
        return SessionAuditor(reg, ctx, key, "spawned"), reg

    def test_user_run_sends_the_runs_range_as_a_reply_owing_request(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.add({"role": "system", "content": "sys"})  # seq 1 — 핸들 전의 history
        h, reg = self._handle(ctx)  # 커서 = 다음 서수 (seq 2)
        ctx.set_turn(1)
        ctx.add({"role": "user", "content": "do it"})  # seq 2
        _complete(ctx)  # seq 3
        text = h.run_ended(_req(), ok=True)
        assert "seq 2..3" in text and "seq BETWEEN 2 AND 3" in text
        assert f'sessions:"{ctx.session_dir.name}"' in text
        assert str(ctx.history_path) in text
        reg.request.assert_called_once_with(
            "agt-aud", text, author="main", expects_reply=True
        )

    def test_consecutive_runs_get_disjoint_ranges(self, tmp_path):
        ctx = _ctx(tmp_path)
        h, _reg = self._handle(ctx)
        ctx.add({"role": "user", "content": "one"})
        _complete(ctx)
        assert "seq 1..2" in h.run_ended(_req(), ok=True)
        ctx.add({"role": "user", "content": "two"})
        ctx.add({"role": "user", "tool": "shell", "success": True, "content": "x"})
        _complete(ctx)
        assert "seq 3..5" in h.run_ended(_req(), ok=True)

    def test_wake_run_is_not_audited_but_still_moves_the_cursor(self, tmp_path):
        """고리 상한: 감사 회신으로 깨어난 런은 감사하지 않는다 — 그리고 그
        레코드가 다음 사용자 런의 구간에 섞이지도 않는다."""
        ctx = _ctx(tmp_path)
        h, reg = self._handle(ctx)
        ctx.add({"role": "user", "content": "wake", "author": WAKE_AUTHOR})
        _complete(ctx)
        assert h.run_ended(_req(wake=True), ok=True) == ""
        reg.request.assert_not_called()
        ctx.add({"role": "user", "content": "next"})
        _complete(ctx)
        assert "seq 3..4" in h.run_ended(_req(), ok=True)

    def test_failed_run_has_no_final_to_audit(self, tmp_path):
        ctx = _ctx(tmp_path)
        h, reg = self._handle(ctx)
        ctx.add({"role": "user", "content": "q"})
        assert h.run_ended(_req(), ok=False) == ""  # 턴 소진·중단
        reg.request.assert_not_called()
        ctx.add({"role": "user", "content": "q2"})
        _complete(ctx)
        assert "seq 2..3" in h.run_ended(_req(), ok=True)

    def test_empty_run_and_dead_auditor_send_nothing(self, tmp_path):
        ctx = _ctx(tmp_path)
        h, reg = self._handle(ctx)
        assert h.run_ended(_req(), ok=True) == ""  # 레코드가 없다
        ctx.add({"role": "user", "content": "q"})
        _complete(ctx)
        reg.instance_for.return_value = SimpleNamespace(key="agt-aud", state="dead")
        assert h.run_ended(_req(), ok=True) == ""
        reg.request.assert_not_called()

    def test_live_registry_delivers_the_request_as_the_auditors_run(self, tmp_path):
        runner = _RecordingRunner()
        reg = make_registry(tmp_path, runner=runner)
        ctx = _ctx(tmp_path)
        try:
            h = ensure_auditor(reg, parent_ctx=ctx, enabled=True)
            ctx.add({"role": "user", "content": "q"})
            _complete(ctx)
            text = h.run_ended(_req(), ok=True)
            assert text and wait_until(lambda: runner.queries() == [text])
            # 러너가 reply 를 안 했으니 폴백 요약이 main 의 메일박스로 돌아온다 —
            # 어느 쪽이든 main 은 감사 결과를 받는다.
            assert wait_until(reg.has_pending_replies)
        finally:
            reg.shutdown_all()


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


class TestAssembly:
    def test_wire_agent_mail_returns_the_handle(self, monkeypatch):
        from agent_cli.runtime import wire_agent_mail

        monkeypatch.setenv("AGENT_CLI_AUDITOR", "1")
        registry = MagicMock()
        registry.restore.return_value = 0
        registry.auto_spawn.return_value = 0
        registry.instance_for.return_value = None
        registry.spawn.return_value = ("agt-1", "")
        ctx = MagicMock()
        ctx.next_ordinal = 7
        _w, _r, _a, auditor = wire_agent_mail(
            registry,
            enqueue_wake=MagicMock(),
            on_mail_notice=lambda r: None,
            parent_ctx=ctx,
        )
        assert auditor.outcome == "spawned" and auditor.key == "agt-1"
        registry.spawn.assert_called_once_with(
            profile=AUDITOR_NAME, name=AUDITOR_NAME, parent_ctx=ctx
        )
        registry.request.assert_not_called()  # 부팅 때 보낼 것이 없다

    def test_web_boot_announces_the_auditor(self):
        from agent_cli.main import _announce_agent_boot
        from agent_cli.render.web import WebConnection, WebRenderer

        r = WebRenderer()
        conn = WebConnection(id="c")
        r.register_connection(conn)
        _announce_agent_boot(
            r, revived=0, auto=0, auditor=SimpleNamespace(outcome="spawned")
        )
        events = []
        while not conn.queue.empty():
            events.append(conn.queue.get_nowait())
        assert any(e == "status" and "🔎" in str(d) for e, d in events), events


class TestHistoryTableMixesAgentFiles:
    """실측(A-smoke): `sessions:"<id>"` 테이블은 `agents/<key>/history.jsonl` 도
    싣고 `seq` 는 파일별 줄 번호라, 감사자 자신의 런 `final` 이 main 의 것으로
    잡혀 구간이 틀렸다. `loc LIKE` 필터가 답이다 — 요청문과 프로파일 둘 다 싣는다."""

    def test_loc_filter_keeps_only_mains_file(self, tmp_path):
        from agent_cli.tools.history import tool_history

        ctx = _ctx(tmp_path)
        ctx.add({"role": "user", "content": "main q"})
        _complete(ctx, "main done")
        agent_dir = ctx.session_dir / "agents" / "agt-1"
        agent_ctx = ContextManager(agent_dir, max_context_tokens=10_000)
        agent_ctx.add({"role": "user", "content": "agent q"})
        _complete(agent_ctx, "agent done")
        sid = ctx.session_dir.name
        plain = tool_history(
            {"query": "SELECT loc FROM history WHERE kind='final' ORDER BY seq"},
            session_dir=ctx.session_dir,
        )
        assert plain.success and "agents/agt-1/history.jsonl" in plain.output
        filtered = tool_history(
            {
                "query": (
                    "SELECT loc FROM history WHERE kind='final' "
                    f"AND loc LIKE '{sid}/history.jsonl:%' ORDER BY seq"
                )
            },
            session_dir=ctx.session_dir,
        )
        assert filtered.success and "agents/" not in filtered.output
        assert f"{sid}/history.jsonl:2" in filtered.output
