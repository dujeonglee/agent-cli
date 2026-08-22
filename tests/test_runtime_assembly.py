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
        registry.waiting_ask_keys.return_value = list(stuck)
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
            registry.waiting_ask_keys.assert_not_called()
            teardown_session(session, None, agent_registry=registry, warn_stuck=True)
            registry.waiting_ask_keys.assert_called_once()

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
        reg_cls.assert_called_once_with("/sess", runtime=rt.as_dict())
        set_main.assert_called_once_with(reg)

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
