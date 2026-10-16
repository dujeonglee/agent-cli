"""세션 감사자 (docs/auditor/DESIGN.md, v10.42.0) — 하니스 몫의 회귀 가드.

하니스가 하는 일은 셋뿐이다: ① 노브대로 감사자를 하나 띄우거나 내린다
(살아 있는 감사자는 늘 0 또는 1) ② 어떻게 살아났든 시작 브리프(main 의
history 경로·깨우기 발신자·모니터 패턴)를 보낸다 ③ 프로파일이 약속한 도구
집합·판정 규칙을 그대로 싣는다. 감사 자체(구간 조회·판정·message)는 모델의
일이라 실측(§6)으로만 잰다.
"""

from __future__ import annotations

import json
import re

from agent_cli.constants import AUDITOR_NAME, AUDITOR_START_BRIEF, auditor_enabled
from agent_cli.context.manager import ContextManager
from agent_cli.pump import WAKE_AUTHOR
from agent_cli.runtime import ensure_auditor
from agent_cli.subagent.profiles import available_profiles, load_profile
from tests.test_agents_live import _RecordingRunner, make_registry, wait_until


def _ctx(tmp_path):
    return ContextManager(tmp_path / "s", max_context_tokens=10_000)


def _auditors(reg):
    return [
        tm
        for tm in reg._agents.values()
        if (tm.profile_name, tm.instance_name) == (AUDITOR_NAME, AUDITOR_NAME)
    ]


def _pattern():
    """브리프가 감사자에게 시키는 모니터 정규식 — 브리프 문장에서 그대로 뽑는다."""
    brief = AUDITOR_START_BRIEF.format(history="/h", wake=WAKE_AUTHOR)
    m = re.search(r'"pattern":"((?:\\.|[^"\\])*)"', brief)
    assert m, brief
    return json.loads(f'"{m.group(1)}"')


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
        assert cfg["allowed-tools"] == [
            "history",
            "monitor",
            "read_file",
            "code_index",
            "memory",
        ]
        for forbidden in ("shell", "ask", "write_file", "edit_file", "agent", "fetch"):
            assert forbidden not in cfg["allowed-tools"]
        # 하니스 소유 — 모델에게 광고하지 않는다(main 이 두 번째를 띄우지 않게)
        assert cfg.get("disable-model-invocation") is True
        assert AUDITOR_NAME not in [n for n, _ in available_profiles()]

    def test_body_carries_the_rules_the_design_relies_on(self):
        body, _, _ = load_profile(AUDITOR_NAME)
        for phrase in (
            '\\"kind\\": \\"final\\"',  # 구독 패턴 (md 안에선 JSON 이스케이프)
            "seq BETWEEN",  # 서수 구간만
            "kind='final' ORDER BY seq DESC LIMIT 1",  # b = 최신 final
            "skip: not a user run",  # 깨우기 런 건너뛰기 = 고리 상한
            "unverifiable",  # 근거 없는 주장
            "A `met` without a seq is not a `met`",  # 예스맨 방지
            'message(to:"main"',  # findings 만 main 에게
            "Do not message main about a pass",
            "once:false",
        ):
            assert phrase in body, phrase


class TestBrief:
    def test_brief_names_history_path_wake_author_and_pattern(self):
        brief = AUDITOR_START_BRIEF.format(
            history="/w/.agent-cli/s/history.jsonl", wake=WAKE_AUTHOR
        )
        assert "/w/.agent-cli/s/history.jsonl" in brief
        assert repr(WAKE_AUTHOR) in brief
        assert '"type":"match"' in brief and "once=false" in brief

    def test_pattern_matches_only_final_records(self, tmp_path):
        """실제 history.jsonl 줄에 대고 — complete 레코드 한 줄만 맞는다."""
        ctx = _ctx(tmp_path)
        ctx.add({"role": "system", "content": "sys"})
        ctx.set_turn(1)
        ctx.add({"role": "user", "content": "do the thing", "author": "Bob"})
        ctx.add(
            {
                "role": "assistant",
                "thought": "final",  # 산문에 'final' 이 있어도 kind 가 아니다
                "ops": [{"action": "shell", "action_input": {"cmd": "ls"}}],
            }
        )
        ctx.add({"role": "user", "tool": "shell", "success": True, "content": "a.py"})
        ctx.add(
            {
                "role": "assistant",
                "thought": "",
                "ops": [{"action": "complete", "action_input": {"result": "done"}}],
            }
        )
        rx = re.compile(_pattern())
        lines = ctx.history_path.read_text().splitlines()
        hits = [i for i, ln in enumerate(lines) if rx.search(ln)]
        assert hits == [len(lines) - 1]
        assert json.loads(lines[-1])["kind"] == "final"

    def test_history_seq_is_the_one_based_line_number(self, tmp_path):
        """프로파일의 SQL 은 `seq` 가 줄 번호(1 기준)라는 전제 위에 있다."""
        from agent_cli.tools.history import tool_history

        ctx = _ctx(tmp_path)
        ctx.add({"role": "system", "content": "sys"})
        ctx.set_turn(1)
        ctx.add({"role": "user", "content": "q1"})
        ctx.add(
            {
                "role": "assistant",
                "thought": "",
                "ops": [{"action": "complete", "action_input": {"result": "r1"}}],
            }
        )
        ctx.add({"role": "user", "content": "q2", "author": WAKE_AUTHOR})
        res = tool_history(
            {"query": "SELECT seq, kind, author FROM history ORDER BY seq"},
            session_dir=ctx.history_path.parent,
        )
        assert res.success, res.error
        rows = res.output
        assert "seq=3 | kind=final" in rows, rows
        assert f"seq=4 | kind=query | author={WAKE_AUTHOR}" in rows, rows


class TestEnsureAuditor:
    def test_off_without_an_auditor_is_a_noop(self, tmp_path):
        reg = make_registry(tmp_path)
        assert ensure_auditor(reg, parent_ctx=_ctx(tmp_path), enabled=False) == ""
        assert _auditors(reg) == []

    def test_no_session_means_nothing_to_watch(self, tmp_path):
        reg = make_registry(tmp_path)
        assert ensure_auditor(reg, parent_ctx=None, enabled=True) == ""
        assert _auditors(reg) == []

    def test_on_spawns_exactly_one_and_briefs_it_every_time(self, tmp_path):
        runner = _RecordingRunner()
        reg = make_registry(tmp_path, runner=runner)
        ctx = _ctx(tmp_path)
        try:
            assert ensure_auditor(reg, parent_ctx=ctx, enabled=True) == "spawned"
            (tm,) = _auditors(reg)
            assert tm.state != "dead"
            assert wait_until(lambda: len(runner.queries()) == 1)
            brief = runner.queries()[0]
            assert str(ctx.history_path) in brief and repr(WAKE_AUTHOR) in brief
            assert json.dumps(_pattern())[1:-1] in brief  # JSON 안에 이스케이프된 채로
            # 두 번째 부팅(restore 로 되살아난 것과 같다): 새로 띄우지 않고 브리프만 다시
            assert ensure_auditor(reg, parent_ctx=ctx, enabled=True) == ""
            assert len(_auditors(reg)) == 1
            assert wait_until(lambda: len(runner.queries()) == 2)
        finally:
            reg.shutdown_all()

    def test_off_kills_a_live_auditor_once(self, tmp_path):
        reg = make_registry(tmp_path)
        ctx = _ctx(tmp_path)
        try:
            ensure_auditor(reg, parent_ctx=ctx, enabled=True)
            assert ensure_auditor(reg, parent_ctx=ctx, enabled=False) == "killed"
            (tm,) = _auditors(reg)
            assert wait_until(lambda: tm.state == "dead")
            assert ensure_auditor(reg, parent_ctx=ctx, enabled=False) == ""
        finally:
            reg.shutdown_all()

    def test_on_after_off_resumes_the_tombstone_instead_of_a_second_instance(
        self, tmp_path
    ):
        runner = _RecordingRunner()
        reg = make_registry(tmp_path, runner=runner)
        ctx = _ctx(tmp_path)
        try:
            ensure_auditor(reg, parent_ctx=ctx, enabled=True)
            ensure_auditor(reg, parent_ctx=ctx, enabled=False)
            (tm,) = _auditors(reg)
            assert wait_until(lambda: tm.state == "dead")
            assert ensure_auditor(reg, parent_ctx=ctx, enabled=True) == "resumed"
            (revived,) = _auditors(
                reg
            )  # 부활은 같은 키의 새 개체 — 툼스톤이 둘이 되지 않는다
            assert revived.key == tm.key and revived.state != "dead"
            assert wait_until(lambda: len(runner.queries()) == 2)
        finally:
            reg.shutdown_all()

    def test_boot_line_for_each_outcome(self):
        from agent_cli.main import _auditor_boot_line

        assert "상주 시작" in _auditor_boot_line("spawned")
        assert "부활" in _auditor_boot_line("resumed")
        assert "내렸습니다" in _auditor_boot_line("killed")
        assert "boom" in _auditor_boot_line("error: boom")


class TestAssembly:
    def test_wire_agent_mail_returns_the_auditor_outcome(self, monkeypatch):
        from unittest.mock import MagicMock

        from agent_cli.runtime import wire_agent_mail

        monkeypatch.setenv("AGENT_CLI_AUDITOR", "1")
        registry = MagicMock()
        registry.restore.return_value = 0
        registry.auto_spawn.return_value = 0
        registry.instance_for.return_value = None
        registry.spawn.return_value = ("agt-1", "")
        registry.request.return_value = ""
        ctx = MagicMock()
        ctx.history_path = "/w/history.jsonl"
        _w, _r, _a, auditor = wire_agent_mail(
            registry,
            enqueue_wake=MagicMock(),
            on_mail_notice=lambda r: None,
            parent_ctx=ctx,
        )
        assert auditor == "spawned"
        registry.spawn.assert_called_once_with(
            profile=AUDITOR_NAME, name=AUDITOR_NAME, parent_ctx=ctx
        )
        brief = registry.request.call_args.args[1]
        assert "/w/history.jsonl" in brief
        assert registry.request.call_args.kwargs == {
            "author": "main",
            "expects_reply": False,
        }

    def test_web_boot_announces_the_auditor(self):
        from agent_cli.main import _announce_agent_boot
        from agent_cli.render.web import WebConnection, WebRenderer

        r = WebRenderer()
        conn = WebConnection(id="c")
        r.register_connection(conn)
        _announce_agent_boot(r, revived=0, auto=0, auditor="spawned")
        events = []
        while not conn.queue.empty():
            events.append(conn.queue.get_nowait())
        assert any(e == "status" and "🔎" in str(d) for e, d in events), events
