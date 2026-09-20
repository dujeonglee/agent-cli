"""run/web 부트스트랩·teardown 조립기 (agent_cli/runtime.py, v8.39.0).

등가성 계약:
- AgentRuntime.as_dict() == 종전 13키 dict 리터럴 (HEAD 3벌에서 추출한
  키·값 매핑을 여기 핀으로 고정; 릴리스 시 HEAD-대조 하네스로도 검증).
- teardown_session 은 종전 run 메인 경로의 시퀀스(경고→registry 종료→
  스피너→MCP 해제→세션 저장)를 그대로 소유하고, 모든 종료 경로가 이
  하나로 수렴한다 (구조 핀은 TestExitPathConvergence).
- 의도된 변화 3건: ①skill 조기-반환 경로도 registry/MCP 정리 ②@agent
  경로도 MCP 정리 ③web 채팅 턴·상주 에이전트에 디스크 훅 배선.
"""

from __future__ import annotations

import os
from dataclasses import fields
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent_cli.runtime import (
    AgentRuntime,
    build_agent_registry,
    teardown_session,
    wire_agent_mail,
)

_MAIN_SRC = Path("agent_cli/main.py").read_text(encoding="utf-8")

# 종전(v8.38.0 HEAD) run/web 의 registry runtime dict 키 — 12키
# (compaction_enabled 부재; 소비측 rt.get("compaction_enabled", True) 기본).
_LEGACY_RUN_WEB_KEYS = frozenset(
    {
        "provider",
        "capabilities",
        "model",
        "provider_name",
        "base_url",
        "api_key",
        "max_turns",
        "depth",
        "max_depth",
        "timeout",
        "session",
        "hooks_config",
    }
)
# 종전 tool_bridge 의 13키 (= 캐노니컬).
_LEGACY_CANONICAL_KEYS = _LEGACY_RUN_WEB_KEYS | {"compaction_enabled"}


class TestAgentRuntimeEquivalence:
    def _rt(self, **overrides):
        base = {
            "provider": object(),
            "capabilities": object(),
            "model": "m",
            "provider_name": "openai",
            "base_url": "http://x/v1",
            "api_key": "k",
            "max_turns": 5,
            "depth": 0,
            "max_depth": 2,
            "timeout": 300,
            "session": object(),
            "hooks_config": {"PreToolUse": []},
        }
        base.update(overrides)
        return AgentRuntime(**base)

    def test_key_set_matches_legacy_canonical(self):
        """as_dict 키 == 종전 tool_bridge 13키 (HEAD 추출 핀)."""
        assert set(self._rt().as_dict()) == _LEGACY_CANONICAL_KEYS
        assert {f.name for f in fields(AgentRuntime)} == _LEGACY_CANONICAL_KEYS

    def test_values_pass_through_by_identity(self):
        """as_dict 는 얕은 사상 — provider/capabilities/session 객체 동일성
        보존 (dataclasses.asdict 의 재귀 dict 변환 금지 계약)."""
        rt = self._rt()
        d = rt.as_dict()
        assert d["provider"] is rt.provider
        assert d["capabilities"] is rt.capabilities
        assert d["session"] is rt.session
        assert d["hooks_config"] is rt.hooks_config
        assert d["model"] == "m" and d["timeout"] == 300 and d["depth"] == 0

    def test_compaction_key_addition_is_behaviorally_equivalent(self):
        """run/web 종전 dict 엔 compaction_enabled 키가 없었다 — 소비측이
        rt.get("compaction_enabled", True) 이므로 키 명시(True 기본)와 행동
        동일함을 고정."""
        legacy_dict = {k: None for k in _LEGACY_RUN_WEB_KEYS}  # 키 부재 재현
        new_dict = self._rt().as_dict()
        assert legacy_dict.get("compaction_enabled", True) == new_dict.get(
            "compaction_enabled", True
        )
        assert new_dict["compaction_enabled"] is True  # 기본값 == 소비측 기본

    def test_from_loop_config_maps_legacy_tool_bridge_fields(self):
        """LoopConfig → AgentRuntime 매핑 == 종전 tool_bridge dict 리터럴의
        필드 대응 (HEAD 추출: cfg.agent_timeout→timeout 등)."""
        cfg = MagicMock()
        cfg.capabilities = object()
        cfg.model = "m"
        cfg.provider_name = "anthropic"
        cfg.base_url = "http://y/v1"
        cfg.api_key = "kk"
        cfg.max_turns = 7
        cfg.depth = 1
        cfg.max_depth = 3
        cfg.agent_timeout = 120
        cfg.session = object()
        cfg.hooks_config = {"PostToolUse": []}
        cfg.compaction_enabled = False
        provider = object()

        d = AgentRuntime.from_loop_config(cfg, provider).as_dict()
        assert d == {
            "provider": provider,
            "capabilities": cfg.capabilities,
            "model": "m",
            "provider_name": "anthropic",
            "base_url": "http://y/v1",
            "api_key": "kk",
            "max_turns": 7,
            "depth": 1,
            "max_depth": 3,
            "timeout": 120,
            "session": cfg.session,
            "hooks_config": cfg.hooks_config,
            "compaction_enabled": False,
        }


class TestTeardownSession:
    def _mocks(self, *, stuck=()):
        registry = MagicMock()
        registry.open_human_question_keys.return_value = list(stuck)
        mcp = MagicMock()
        session = MagicMock()
        session.session_id = "s1"
        return registry, mcp, session

    def test_full_sequence_order(self):
        """시퀀스 = registry 종료 → 스피너 정지 → MCP 해제 → 세션 저장
        (종전 run 메인 경로: shutdown_all → _finalize_run(spinner→mcp→
        finalize) 과 동일 순서)."""
        registry, mcp, session = self._mocks()
        order: list[str] = []
        registry.shutdown_all.side_effect = lambda: order.append("shutdown")
        mcp.disconnect_all.side_effect = lambda: order.append("mcp")
        with (
            patch("agent_cli.render.render_spinner_stop") as spin,
            patch("agent_cli.context.session.finalize_session") as fin,
        ):
            spin.side_effect = lambda: order.append("spinner")
            fin.side_effect = lambda s, c: order.append("finalize")
            teardown_session(session, "CTX", agent_registry=registry, mcp_manager=mcp)
        assert order == ["shutdown", "spinner", "mcp", "finalize"]
        fin.assert_called_once_with(session, "CTX")

    def test_warn_stuck_only_when_enabled(self):
        """답변-대기 경고는 warn_stuck=True 에서만 (종전: 메인 펌프 경로
        전용 표면 — 조기-반환 경로는 경고 없음 유지)."""
        registry, _mcp, session = self._mocks(stuck=["agt-1"])
        with (
            patch("agent_cli.render.render_spinner_stop"),
            patch("agent_cli.context.session.finalize_session"),
        ):
            teardown_session(session, None, agent_registry=registry, warn_stuck=False)
            registry.open_human_question_keys.assert_not_called()
            teardown_session(session, None, agent_registry=registry, warn_stuck=True)
            registry.open_human_question_keys.assert_called_once()

    def test_none_tolerance(self):
        """registry/mcp/session 이 None 이어도 무사 통과 — 세션 None 이면
        저장 생략 (종전 _finalize_run 의 session None 조기 반환과 동형)."""
        with (
            patch("agent_cli.render.render_spinner_stop"),
            patch("agent_cli.context.session.finalize_session") as fin,
        ):
            teardown_session(None, None)
            fin.assert_not_called()

    def test_registry_shutdown_precedes_mcp(self):
        """상주 에이전트가 MCP 도구를 쓰는 중일 수 있으므로 registry 종료가
        MCP 해제보다 반드시 먼저 (순서 계약)."""
        registry, mcp, _session = self._mocks()
        order: list[str] = []
        registry.shutdown_all.side_effect = lambda: order.append("shutdown")
        mcp.disconnect_all.side_effect = lambda: order.append("mcp")
        with (
            patch("agent_cli.render.render_spinner_stop"),
            patch("agent_cli.context.session.finalize_session"),
        ):
            teardown_session(None, None, agent_registry=registry, mcp_manager=mcp)
        assert order.index("shutdown") < order.index("mcp")


class TestRegistryAssembly:
    def test_build_agent_registry_registers_main_slot(self):
        """생성 + main 슬롯 등록 (v7.17.0 배선) — runtime 은 as_dict 로 전달."""
        rt = AgentRuntime(
            provider=object(),
            capabilities=object(),
            model="m",
            provider_name="p",
            base_url="u",
            api_key="k",
            max_turns=0,
            depth=0,
            max_depth=2,
            timeout=300,
            session=None,
        )
        with (
            patch("agent_cli.subagent.agents_live.AgentRegistry") as reg_cls,
            patch("agent_cli.subagent.agents_live.set_main_registry") as set_main,
        ):
            reg = build_agent_registry("/sess", rt)
        # max_agents=None = 미지정 → registry 가 env/기본값을 고른다
        # (v8.61.0 --max-agents). 여기서 값을 정하면 env 가 무시된다.
        reg_cls.assert_called_once_with("/sess", runtime=rt.as_dict(), max_agents=None)
        set_main.assert_called_once_with(reg)

    def test_build_agent_registry_passes_explicit_cap(self):
        """CLI ``--max-agents`` 가 registry 까지 닿는지 — 이 배선이 끊기면
        플래그가 조용히 무시된다(기본 10 으로 돈다)."""
        rt = AgentRuntime(
            provider=object(),
            capabilities=object(),
            model="m",
            provider_name="p",
            base_url="u",
            api_key="k",
            max_turns=0,
            depth=0,
            max_depth=2,
            timeout=300,
            session=None,
        )
        with (
            patch("agent_cli.subagent.agents_live.AgentRegistry") as reg_cls,
            patch("agent_cli.subagent.agents_live.set_main_registry"),
        ):
            build_agent_registry("/sess", rt, max_agents=3)
        assert reg_cls.call_args.kwargs["max_agents"] == 3

    def test_wire_agent_mail_assembly(self):
        """waker 조립 + on_reply(알림→waker.on_mail 순) + restore/auto_spawn
        호출·카운트 반환 — 종전 run/web 인라인 배선과 동일 시퀀스."""
        registry = MagicMock()
        registry.restore.return_value = 2
        registry.auto_spawn.return_value = 1
        notices: list[dict] = []
        enq = MagicMock()

        waker, revived, auto = wire_agent_mail(
            registry,
            enqueue_wake=enq,
            on_mail_notice=notices.append,
            parent_ctx="CTX",
        )
        assert (revived, auto) == (2, 1)
        registry.restore.assert_called_once_with(parent_ctx="CTX")
        registry.auto_spawn.assert_called_once_with(parent_ctx="CTX")
        # on_reply 훅: 알림 먼저, waker.on_mail 다음
        with patch.object(waker, "on_mail") as om:
            registry.on_reply({"kind": "reply"})
            assert notices == [{"kind": "reply"}]
            om.assert_called_once()

    def test_monitor_reports_share_the_wake(self):
        """★재발 방지(사용자 제보: 등록은 됐는데 2분 무반응).

        모니터 보고는 **턴 경계**에서만 소비되는데(`_deliver_monitor_reports`),
        모니터의 존재 이유가 "오래 걸리는 걸 걸어 두고 딴 일 하라" 라 발화
        시점에 main 이 유휴인 것이 정상이다 — 깨우지 않으면 보고가 큐에 앉은
        채 화면에 아무것도 안 나온다. 설계(docs/monitor)가 처음부터 "waker
        술어에 `or monitors.has_pending()`" 라고 적어 뒀는데 배선만 빠졌다.
        """
        from agent_cli.monitor.registry import MonitorRegistry

        registry = MagicMock()
        registry.restore.return_value = 0
        registry.auto_spawn.return_value = 0
        registry.has_pending_replies.return_value = False
        monitors = MonitorRegistry()

        enq2 = MagicMock()
        waker, _r, _a = wire_agent_mail(
            registry,
            enqueue_wake=enq2,
            on_mail_notice=lambda _r: None,
            monitors=monitors,
        )
        # ① 술어: 에이전트 회신이 없어도 모니터 보고가 있으면 깨울 거리다.
        assert waker._has_pending() is False
        monitors._pending.append("보고")
        assert waker._has_pending() is True

        # ② 훅: 보고가 **도착한 순간** 깨운다. 술어만 얹으면 다음 mark_idle
        #    까지 기다리는데, 유휴로 접어든 뒤 발화하면 그 시점이 안 온다.
        #    `_notify` 를 직접 부르면 **발화 경로**를 안 타므로, 실제로
        #    조건을 만족시켜 fire 시킨다(사용자가 겪은 그 상황).
        assert monitors.on_report == waker.on_mail
        monitors._pending.clear()
        waker.idle.set()  # main 이 큐에서 대기 중 = 모니터가 발화하는 정상 상황

        import tempfile
        import time as _t
        from pathlib import Path as _P

        from agent_cli.monitor.conditions import build

        with tempfile.TemporaryDirectory() as td:
            f = _P(td) / "w.log"
            f.write_text("x")
            old_ts = _t.time() - 600
            os.utime(f, (old_ts, old_ts))
            # ``once=False``: 발화만 하고 은퇴하지 않는다. once 면 `_retire`
            # 도 보고를 남기며 알리므로, 발화 경로의 알림이 빠져도 가려진다.
            mon = monitors.add(
                build({"type": "silence", "file": str(f), "seconds": 1}),
                deadline_s=600,
                once=False,
            )
            mon.state["registered_at"] = old_ts  # 등록 직후 억제 해제
            monitors.stop()  # 폴링 스레드 대신 동기 tick 으로 재현
            monitors.tick(_t.time())

        assert monitors.has_pending(), "조건을 만족했는데 보고가 안 쌓였다"
        assert enq2.call_count == 1, "유휴 main 에 wake 가 안 들어갔다"

    def test_monitor_wiring_is_passed_at_every_call_site(self):
        """배선은 `main.py` 두 펌프(run/web) **모두**에서 넘어가야 한다 —
        한쪽만 고치는 것이 이 파일이 존재하는 이유의 사고 유형이다."""
        import ast
        from pathlib import Path

        src = Path(__import__("agent_cli.main", fromlist=["x"]).__file__).read_text()
        calls = [
            n
            for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "wire_agent_mail"
        ]
        assert len(calls) == 2, f"호출부가 {len(calls)}개 — 테스트가 낡았다"
        for c in calls:
            names = {kw.arg for kw in c.keywords if kw.arg}
            assert "monitors" in names, (
                "wire_agent_mail 호출부 하나가 monitors 를 안 넘긴다 — "
                "그 경로에서는 모니터 보고가 유휴 main 을 못 깨운다"
            )


class TestExitPathConvergence:
    """run/web 종료 경로 수렴의 구조 핀 (소스 레벨 — 이 레포의 배선 핀
    관례). 경로별 나열이 부활하면(=finally 밖 teardown 호출) 실패한다."""

    def _body(self, name: str) -> str:
        start = _MAIN_SRC.index(f"\ndef {name}(")
        nxt = _MAIN_SRC.find("\ndef ", start + 1)
        return _MAIN_SRC[start:nxt]

    def test_run_converges_on_single_finalize(self):
        body = self._body("run")
        # teardown 진입점은 finally 의 _finalize_run 단 한 곳
        assert body.count("_finalize_run(") == 1
        # 경로별 직접 정리 나열 부활 금지 (teardown_session 소유)
        assert "shutdown_all()" not in body
        assert "disconnect_all()" not in body
        # finally 가 registry 와 mcp 를 모두 넘긴다 (종전 2경로 MCP 누락 수리)
        assert "agent_registry=agent_registry" in body
        assert "mcp_manager," in body or "mcp_manager)" in body

    def test_web_converges_on_teardown_session(self):
        body = self._body("web")
        assert "teardown_session(" in body
        assert "shutdown_all()" not in body  # registry 직접 나열 부활 금지
        assert "disconnect_all()" not in body

    def test_web_hooks_wired_like_run(self):
        """v8.39.0 수리 핀: web 도 디스크 훅을 로드해 run_loop 와 registry
        runtime 에 배선한다 — 종전 hooks_config 미전달/None 고정 금지."""
        body = self._body("web")
        assert "load_hooks" in body
        assert "hooks_config=_disk_hooks" in body  # run_loop 호출 배선
        assert '"hooks_config": None' not in body  # 종전 None 고정 소멸
        # registry runtime 도 같은 훅 (AgentRuntime 생성 인자)
        assert "hooks_config=_disk_hooks" in body

    def test_run_and_web_share_assembly_helpers(self):
        for name in ("run", "web"):
            body = self._body(name)
            assert "build_agent_registry(" in body
            assert "wire_agent_mail(" in body
            assert "AgentRuntime(" in body


class TestRunCommandTeardownIntegration:
    """run 커맨드를 CliRunner 로 실제 구동해 경로별 teardown 수렴을 검증 —
    구조 핀(소스 스크레이프)보다 강한 실행 증거. 종전엔 skill 조기-반환이
    registry/MCP, @agent 경로가 MCP 정리를 누락했다(리뷰 §4.1) — 이제 모든
    경로(예외 포함)가 teardown_session 1회 호출로 끝난다."""

    def _run_with(self, query, *, pump=None, dispatch_result=False):
        from contextlib import ExitStack

        from agent_cli.tools.result import ToolResult

        boot = MagicMock()
        boot.wire_format.name = "json_fc"
        session = MagicMock()
        session.session_id = "sess-1"
        registry = MagicMock()
        calls = {}

        with ExitStack() as st:
            st.enter_context(
                patch("agent_cli.main._bootstrap_provider", return_value=boot)
            )
            st.enter_context(
                patch("agent_cli.main._setup_mcp", return_value=("MCP", {}))
            )
            st.enter_context(
                patch("agent_cli.context.session.create_session", return_value=session)
            )
            st.enter_context(patch("agent_cli.context.session.save_meta"))
            st.enter_context(patch("agent_cli.main._build_context", return_value=None))
            st.enter_context(patch("agent_cli.hooks.load_hooks", return_value={}))
            st.enter_context(
                patch("agent_cli.runtime.build_agent_registry", return_value=registry)
            )
            st.enter_context(
                patch(
                    "agent_cli.runtime.wire_agent_mail",
                    return_value=(MagicMock(), 0, 0),
                )
            )
            st.enter_context(
                patch(
                    "agent_cli.main.try_dispatch_agent_or_skill",
                    return_value=dispatch_result,
                )
            )
            st.enter_context(
                patch(
                    "agent_cli.main.run_loop",
                    return_value=ToolResult(True, output="ans"),
                )
            )
            if pump is None:

                def pump(input_queue, waker, reg, run_one, **kw):
                    run_one("hi", wake=False)

            st.enter_context(patch("agent_cli.main._run_message_pump", pump))
            td = st.enter_context(patch("agent_cli.runtime.teardown_session"))
            result = self._invoke_cli(query)
            calls["teardown"] = td
            calls["registry"] = registry
            calls["result"] = result
        return calls

    def _invoke_cli(self, query):
        from typer.testing import CliRunner

        from agent_cli.main import app

        return CliRunner().invoke(app, ["run", query])

    def test_main_path_full_teardown(self):
        c = self._run_with("hi")
        assert c["result"].exit_code == 0, c["result"].output
        c["teardown"].assert_called_once()
        kw = c["teardown"].call_args.kwargs
        assert kw["agent_registry"] is c["registry"]
        assert kw["mcp_manager"] == "MCP"  # 종전에도 메인 경로는 MCP 정리
        assert kw["warn_stuck"] is True  # 메인 펌프 경로 전용 경고 표면 유지

    def test_skill_early_return_now_tears_down_completely(self):
        """수리 계약: skill 조기-반환도 registry+MCP 를 정리한다 (종전:
        _finalize_run(session, ctx) 만 — registry 미종료 + MCP 미해제)."""
        c = self._run_with("/some-skill args", dispatch_result=True)
        assert c["result"].exit_code == 0, c["result"].output
        c["teardown"].assert_called_once()
        kw = c["teardown"].call_args.kwargs
        assert kw["agent_registry"] is c["registry"]
        assert kw["mcp_manager"] == "MCP"
        assert kw["warn_stuck"] is False  # 조기-반환 경로는 경고 없음 (종전 표면)

    def test_pump_exception_still_tears_down(self):
        """크래시 경로도 finally 로 수렴 — 종전엔 예외 시 MCP 미해제·세션
        미저장(펌프 내부 finally 는 registry 만 정리)이었다."""

        def boom(input_queue, waker, reg, run_one, **kw):
            raise RuntimeError("pump crashed")

        c = self._run_with("hi", pump=boom)
        assert c["result"].exit_code != 0  # 예외는 그대로 전파 (동작 보존)
        c["teardown"].assert_called_once()
        kw = c["teardown"].call_args.kwargs
        assert kw["agent_registry"] is c["registry"]
        assert kw["mcp_manager"] == "MCP"
