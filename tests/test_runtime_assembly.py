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

import pytest

from agent_cli.runtime import (
    AgentRuntime,
    build_agent_registry,
    teardown_session,
    wire_agent_mail,
)
from tests.binding_support import make_binding

_MAIN_SRC = Path("agent_cli/main.py").read_text(encoding="utf-8")
_RUNTIME_SRC = Path("agent_cli/runtime.py").read_text(encoding="utf-8")


def _calls(src: str, name: str) -> list:
    """``src`` 안에서 이름 ``name`` 을 직접 부르는 호출 노드들."""
    import ast

    return [
        n
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == name
    ]


def _enclosing_def(src: str, node) -> str:
    """``node`` 를 품은 가장 안쪽 최상위·메서드 ``def`` 의 이름."""
    import ast

    best = None
    for fn in ast.walk(ast.parse(src)):
        inside = isinstance(fn, ast.FunctionDef) and (
            fn.lineno <= node.lineno <= (fn.end_lineno or fn.lineno)
        )
        if inside and (best is None or fn.lineno > best.lineno):
            best = fn
    return best.name if best else ""


def _def_src(src: str, name: str) -> str:
    """``def name(`` 하나의 소스."""
    import ast

    (fn,) = [
        n
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == name
    ]
    return ast.get_source_segment(src, fn)


# run/web 의 registry runtime dict 키 (v10.43.0: 모델·능력·프로바이더 여섯 값은
# `binding` 하나 — 세션의 ModelBinding 을 참조한다). compaction_enabled 는 소비측
# 기본(True)과 같아 늘 싣는다.
_LEGACY_RUN_WEB_KEYS = frozenset(
    {"binding", "max_turns", "depth", "max_depth", "session", "hooks_config"}
)
_LEGACY_CANONICAL_KEYS = _LEGACY_RUN_WEB_KEYS | {"compaction_enabled"}


class TestAgentRuntimeEquivalence:
    def _rt(self, **overrides):
        base = {
            "binding": make_binding(
                provider=object(),
                capabilities=object(),
                model="m",
                provider_name="openai",
                base_url="http://x/v1",
                api_key="k",
            ),
            "max_turns": 5,
            "depth": 0,
            "max_depth": 2,
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
        assert d["binding"] is rt.binding
        assert d["session"] is rt.session
        assert d["hooks_config"] is rt.hooks_config
        assert d["binding"].model == "m" and d["depth"] == 0

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
        """LoopConfig → AgentRuntime 매핑 — 바인딩은 **같은 객체**(복사 아님)."""
        cfg = MagicMock()
        cfg.binding = make_binding(model="m", provider_name="anthropic")
        cfg.max_turns = 7
        cfg.depth = 1
        cfg.max_depth = 3
        cfg.session = object()
        cfg.hooks_config = {"PostToolUse": []}
        cfg.compaction_enabled = False

        d = AgentRuntime.from_loop_config(cfg).as_dict()
        assert d == {
            "binding": cfg.binding,
            "max_turns": 7,
            "depth": 1,
            "max_depth": 3,
            "session": cfg.session,
            "hooks_config": {"PostToolUse": []},
            "compaction_enabled": False,
        }
        assert d["binding"] is cfg.binding

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

    def test_schedules_stop_with_the_other_producers(self):
        """예약 스레드는 모니터를 멈추는 자리에서 같이 멈춘다 (v10.130.4) — 에이전트
        종료·세션 저장보다 먼저. 종전엔 호스트가 정리 **뒤에** 따로 멈춰, 정리 도중
        죽은 큐로 발화할 틈이 있었다."""
        registry, _mcp, session = self._mocks()
        order: list[str] = []
        monitors = MagicMock()
        schedules = MagicMock()
        monitors.stop.side_effect = lambda: order.append("monitors")
        schedules.stop.side_effect = lambda: order.append("schedules")
        registry.shutdown_all.side_effect = lambda: order.append("shutdown")
        with (
            patch("agent_cli.render.render_spinner_stop"),
            patch("agent_cli.context.session.finalize_session") as fin,
        ):
            fin.side_effect = lambda s, c: order.append("finalize")
            teardown_session(
                session,
                None,
                agent_registry=registry,
                monitors=monitors,
                schedules=schedules,
            )
        assert order == ["monitors", "schedules", "shutdown", "finalize"]
        schedules.stop.assert_called_once_with()

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
            binding=make_binding(
                provider=object(),
                capabilities=object(),
                model="m",
                provider_name="p",
                base_url="u",
                api_key="k",
            ),
            max_turns=0,
            depth=0,
            max_depth=2,
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
            binding=make_binding(
                provider=object(),
                capabilities=object(),
                model="m",
                provider_name="p",
                base_url="u",
                api_key="k",
            ),
            max_turns=0,
            depth=0,
            max_depth=2,
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

        ctx = MagicMock()
        waker, revived, auto = wire_agent_mail(
            registry,
            enqueue_wake=enq,
            on_mail_notice=lambda r, busy: notices.append((r, busy)),
            parent_ctx=ctx,
        )
        assert (revived, auto) == (2, 1)
        registry.restore.assert_called_once_with(parent_ctx=ctx)
        registry.auto_spawn.assert_called_once_with(parent_ctx=ctx)
        # on_reply 훅: 알림 먼저(바쁨 = waker 가 유휴 아님), waker.on_mail 다음
        with patch.object(waker, "on_mail") as om:
            registry.on_reply({"kind": "reply"})
            waker.idle.set()
            registry.on_reply({"kind": "reply"})
            assert notices == [({"kind": "reply"}, True), ({"kind": "reply"}, False)]
            assert om.call_count == 2
        # web 은 알림을 걸지 않는다 — 대기열이 그 자리 (v10.51.0)
        reg2 = MagicMock()
        w2, *_ = wire_agent_mail(reg2, enqueue_wake=enq, parent_ctx=ctx)
        with patch.object(w2, "on_mail") as om2:
            reg2.on_reply({"kind": "reply"})  # 알림 없이 깨우기만
            om2.assert_called_once()

    def test_monitor_delivery_is_wired_at_registry_build(self):
        """★재발 방지 (사용자 제보: 등록은 됐는데 2분 무반응).

        종전엔 `wire_agent_mail` 이 술어에 `or monitors.has_pending()` 을
        얹고 `on_report` 를 꽂아 main 을 깨웠는데, **배선만 빠져 있었다** —
        설계 문서에 미리 적혀 있었는데도. 지금은 그 항 자체가 없다: 보고가
        주소 배달로 메일박스에 들어오므로 `on_reply` 가 이미 깨운다.

        대신 지켜야 할 것은 **배달 배선이 어떤 루프보다 먼저 선다**는 것이다.
        `wire_monitor_delivery` 를 `build_agent_registry` 가 부르므로
        (run `:1387`, web `:2363`) run 의 스킬 조기-반환보다도 앞선다.
        """
        from agent_cli.monitor.registry import MonitorRegistry
        from agent_cli.runtime import wire_monitor_delivery

        registry = MagicMock()
        monitors = MonitorRegistry()
        monitors.stop()
        wire_monitor_delivery(registry, monitors)
        assert monitors.deliver == registry.deliver, "보고가 갈 곳이 없다"
        assert registry.monitors is monitors, (
            "역방향이 없으면 워커 finally 가 죽은 소유자의 감시를 못 지운다"
        )

    def test_build_agent_registry_wires_delivery(self):
        """조립기가 부르는지 — 손으로 부르게 두면 한쪽을 빠뜨린다."""
        from agent_cli.monitor.registry import MonitorRegistry

        monitors = MonitorRegistry()
        monitors.stop()
        rt = AgentRuntime(
            binding=make_binding(
                provider=object(),
                capabilities=object(),
                model="m",
                provider_name="p",
                base_url="u",
                api_key="k",
            ),
            max_turns=0,
            depth=0,
            max_depth=2,
            session=None,
        )
        with patch("agent_cli.subagent.agents_live.set_main_registry"):
            reg = build_agent_registry(None, rt, monitors=monitors)
        assert monitors.deliver == reg.deliver
        assert reg.monitors is monitors

    def test_monitor_wiring_is_passed_at_every_call_site(self):
        """배선은 레지스트리를 만드는 **유일한** 자리에서 넘어가야 한다 — 종전엔
        `main.py` 두 부트스트랩(run/web)이 각자 불렀고, 한쪽만 고치는 것이 이 파일이
        존재하는 이유의 사고 유형이었다. v10.131.0 부터 그 자리는
        `runtime.build_session_owners` 하나다."""
        calls = _calls(_RUNTIME_SRC, "build_agent_registry")
        assert len(calls) == 1, f"runtime 호출부가 {len(calls)}개 — 테스트가 낡았다"
        assert _enclosing_def(_RUNTIME_SRC, calls[0]) == "build_session_owners"
        names = {kw.arg for kw in calls[0].keywords if kw.arg}
        assert {"monitors", "jobs"} <= names, (
            "build_agent_registry 가 monitors·jobs 를 안 넘긴다 — "
            "그 경로에서는 monitor add·작업 통지가 통째로 거부된다"
        )
        assert _calls(_MAIN_SRC, "build_agent_registry") == [], (
            "호스트가 레지스트리를 직접 만든다 — 조립이 두 벌로 돌아갔다"
        )

    def test_teardown_stops_the_poll_thread_at_every_call_site(self):
        """폴링 스레드를 세션 종료가 멈춰야 한다. 종전엔 `stop()` 에
        **호출자가 하나도 없었다** — 지금까지는 아무도 안 읽는 리스트에
        쌓을 뿐이라 무해했지만, 이제는 종료 뒤 발화가 **배달**된다.
        v10.131.0: 정리의 호출부는 `runtime.close_session` 하나이고 두 호스트는
        그것만 부른다(run 은 `_finalize_run` 경유)."""
        import ast

        calls = _calls(_RUNTIME_SRC, "teardown_session")
        assert len(calls) == 1, f"runtime 호출부가 {len(calls)}개 — 테스트가 낡았다"
        assert _enclosing_def(_RUNTIME_SRC, calls[0]) == "close_session"
        names = {kw.arg for kw in calls[0].keywords if kw.arg}
        assert "monitors" in names, "종료 경로가 감시를 안 멈춘다"
        # v10.130.4: 예약 스레드도 정리가 멈춘다 — 호스트의 별도 `stop()` 은 없다
        assert "schedules" in names, "종료 경로가 예약 스레드를 안 멈춘다"
        assert {"agent_registry", "mcp_manager", "harness", "jobs"} <= names
        assert _calls(_MAIN_SRC, "teardown_session") == []
        closes = _calls(_MAIN_SRC, "close_session")
        assert {_enclosing_def(_MAIN_SRC, c) for c in closes} == {
            "_finalize_run",
            "web",
        }
        stops = [
            n
            for n in ast.walk(ast.parse(_MAIN_SRC))
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "stop"
            and "schedule" in ast.unparse(n.func.value)
        ]
        assert stops == [], (
            "호스트가 예약을 정리 밖에서 따로 멈춘다 — teardown 이 소유한다"
        )


class TestExitPathConvergence:
    """run/web 종료 경로 수렴의 구조 핀 (소스 레벨 — 이 레포의 배선 핀
    관례). 경로별 나열이 부활하면(=finally 밖 teardown 호출) 실패한다.
    v10.131.0: 조립·정리는 `runtime` 의 함수 넷 — 호스트는 그것을 부르는 스크립트."""

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
        # finally 가 spec(=mcp_manager·session)·저장소·소유자를 모두 넘긴다 — 조립
        # 도중 죽어도(None) 정리가 돈다 (종전 2경로 MCP 누락 수리, v10.131.0 조립도 try 안)
        assert "_finalize_run(spec, stores, owners, warn_stuck=warn_stuck)" in body
        assert "stores = owners = None" in body
        tried = body[body.index("    try:\n") :]
        assert "build_session_stores(" in tried and "build_session_owners(" in tried
        assert "mcp_manager=mcp_manager" in body  # spec 에 실린다
        fin = _MAIN_SRC[_MAIN_SRC.index("\ndef _finalize_run(") :]
        fin = fin[: fin.find("\ndef ", 1)]
        assert "close_session(spec, stores, owners, warn_stuck=warn_stuck)" in fin

    def test_web_converges_on_close_session(self):
        body = self._body("web")
        assert body.count("close_session(") == 1
        assert "close_session(spec, stores, owners" in body
        assert "teardown_session(" not in body
        # registry 직접 나열 부활 금지 — 연결 허브의 종료(``renderer.hub.shutdown_all()``,
        # v10.115.0)는 별개라 그것만 허용한다.
        assert body.count("shutdown_all()") == body.count("renderer.hub.shutdown_all()")
        assert "disconnect_all()" not in body

    def test_web_hooks_wired_like_run(self):
        """v8.39.0 수리 핀: web 도 디스크 훅을 로드해 main 루프와 registry runtime 에
        배선한다 — 종전 hooks_config 미전달/None 고정 금지. v10.131.0: 두 호스트가
        같은 `SessionSpec.for_session` 으로 훅을 읽고, 조립 함수가 그 한 값을
        `AgentRuntime`·`MainLoop` 양쪽에 싣는다."""
        for name in ("run", "web"):
            body = self._body(name)
            assert body.count("SessionSpec.for_session(") == 1, name
            assert "load_hooks" not in body, f"{name}: 훅을 spec 밖에서 따로 읽는다"
            assert '"hooks_config": None' not in body  # 종전 None 고정 소멸
        spec_src = _def_src(_RUNTIME_SRC, "for_session")
        assert (
            "load_hooks" in spec_src and "hooks_config=load_hooks() or None" in spec_src
        )
        # registry runtime 과 main 루프가 같은 훅
        assert "hooks_config=spec.hooks_config" in _def_src(
            _RUNTIME_SRC, "build_session_owners"
        )
        assert "hooks_config=spec.hooks_config" in _def_src(
            _RUNTIME_SRC, "wire_session"
        )

    def test_schedule_thread_starts_last_in_both_hosts(self):
        """v10.130.5: 예약 스레드는 소비자(펌프)를 다 세운 뒤에 켜고, 부트 알림은
        그 뒤에 나간다 — 두 호스트가 같은 순서. 발화 자리(``enqueue``)는 저장소를
        만드는 자리에서 묶는다(레지스트리보다 앞). 종전 web 은 워커가 조립을
        시작하기도 전에 메인 스레드에서 켰다 — 워커가 부트에서 죽으면 아무도 읽지
        않는 큐로 발화하고 "발화함" 으로 적었다. v10.131.0: 그 순서는 조립 함수
        안에 있다."""
        wire = _def_src(_RUNTIME_SRC, "wire_session")
        assert wire.count("stores.schedules.start()") == 1
        start = wire.index("stores.schedules.start()")
        assert wire.index("SessionPump(") < start, "펌프보다 먼저 켠다"
        assert wire.index("wire_agent_mail(") < start
        assert wire.index("MainLoop(") < start
        assert "schedules.enqueue =" in _def_src(_RUNTIME_SRC, "build_session_stores")
        for name, notice in (
            ("run", "if wired.revived:"),
            ("web", "_announce_agent_boot("),
        ):
            body = self._body(name)
            assert ".start()" not in body.replace("worker.start()", "").replace(
                "idle_thread.start()", ""
            ), f"{name}: 호스트가 스레드를 켠다"
            assert body.index("build_session_stores(") < body.index(
                "build_session_owners("
            ), name
            wired = body.index("wire_session(")
            assert wired < body.index(notice), f"{name}: 부트 알림이 스레드보다 먼저"
            assert wired < body.index("_previous_monitors_notice("), name
            assert wired < body.index("wired.pump.run()"), name

    def test_web_server_handles_are_one_block_before_agents_revive(self):
        """v10.130.7: web 의 서버 손잡이(레지스트리·MCP·감사자·노브 상태)는 레지스트리
        → 감사자 → 노브 적용(`build_session_owners`)이 끝난 뒤 한 블록으로, 그리고
        에이전트가 살아나기(`wire_session` 의 복원·auto-spawn) **전에** — 되살아난
        에이전트의 창을 서버가 찾을 수 있어야 한다. 메일 대기열 훅은 그 뒤."""
        body = self._body("web")
        handles = [
            "server.agent_registry =",
            "server.mcp_manager =",
            "server.auditor =",
            "renderer.knob_state =",
            "renderer.broadcast_knobs(",
        ]
        at = [body.index(h) for h in handles]
        assert all(body.count(h) == 1 for h in handles)
        assert at == sorted(at), "손잡이 블록의 순서가 바뀌었다"
        assert body.index("build_session_owners(") < at[0], "노브 적용보다 먼저 꽂는다"
        block = body[at[0] : at[-1]]
        for between in ("build_session_owners(", "wire_session("):
            assert between not in block, f"손잡이 사이에 {between} — 한 블록이 아니다"
        revive = body.index("wire_session(")
        assert at[-1] < revive, "에이전트가 살아난 뒤에야 서버가 레지스트리를 안다"
        for hook in ("server.mail_source =", "on_pending_change ="):
            assert revive < body.index(hook), hook

    def test_both_hosts_wire_the_same_queue_the_same_way(self):
        """v10.130.8: web 서버가 큐를 내놓고(`server.queue`) 두 호스트가 같은
        `InputQueue` 를 같은 배선으로 쓴다 — 종전 web 은 서버의 대리 메서드
        (`enqueue_named`·`enqueue_system`·`dequeue_nowait`, 펌프 큐 = 서버)였다.
        v10.131.0: 그 배선은 조립 함수 안에 **한 번** 쓰이고, 호스트는 큐를 넘긴다."""
        import re

        wiring = (
            "schedules.enqueue = lambda prompt, nickname: queue.enqueue(",
            "enqueue_wake=lambda cid, t: queue.enqueue(cid, t, system=True)",
            "dequeue_user_message=queue.dequeue_nowait",
            "enqueue_user_message=lambda text, nickname: queue.enqueue(",
            "PumpDeps(queue=queue,",
        )
        runtime = re.sub(r"\(\s+", "(", re.sub(r"\s+", " ", _RUNTIME_SRC))
        for line in wiring:
            assert runtime.count(line) == 1, line
        for name in ("run", "web"):
            body = re.sub(r"\(\s+", "(", re.sub(r"\s+", " ", self._body(name)))
            assert "build_session_stores(spec, queue=input_queue" in body, name
            assert "queue=input_queue, surface=surface" in body, name
            assert "server.enqueue_" not in body and "server.dequeue_" not in body
            assert "queue=server," not in body
        assert "input_queue = server.queue" in self._body("web")

    def test_run_and_web_share_assembly_helpers(self):
        """v10.131.0: 두 호스트가 같은 조립 함수 넷을 부르고, 조립 부품을 직접
        부르지 않는다 — 부품을 직접 부르기 시작하면 다시 두 벌이다."""
        for name in ("run", "web"):
            body = self._body(name)
            for call in (
                "build_session_stores(",
                "build_session_owners(",
                "wire_session(",
            ):
                assert body.count(call) == 1, f"{name}: {call}"
            assert ("_finalize_run(" if name == "run" else "close_session(") in body
            for direct in (
                "build_agent_registry(",
                "wire_agent_mail(",
                "MainLoop(",
                "SessionPump(",
                "teardown_session(",
                "AgentRuntime(",
                "build_auditor(",
                "apply_knobs(",
                "disconnect_all()",
            ):
                assert direct not in body, f"{name} 가 {direct} 를 직접 부른다"
            assert body.count("shutdown_all()") == body.count(
                "renderer.hub.shutdown_all()"
            )

    def test_assembly_does_not_know_its_host(self):
        """조립 함수는 어느 호스트에서 불리는지 모른다 — `main` 을 import 하지 않고,
        호스트 표면(서버·콘솔·웹)의 이름이 본문에 없다. 호스트마다 다른 것은 기본값
        None 의 키워드 인자(`on_*`)와 호스트가 만든 값(표면·수명 정책·큐)뿐."""
        import ast
        import inspect
        import re

        from agent_cli import runtime

        assert "agent_cli.main" not in _RUNTIME_SRC
        assert "from agent_cli import main" not in _RUNTIME_SRC
        for fn in (
            runtime.build_session_stores,
            runtime.build_session_owners,
            runtime.wire_session,
            runtime.close_session,
        ):
            tree = ast.parse(_def_src(_RUNTIME_SRC, fn.__name__))
            idents = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
                n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
            }
            leaks = {
                i for i in idents if re.search(r"web|server|console", i, re.IGNORECASE)
            }
            assert leaks == set(), f"{fn.__name__}: 호스트 이름 {leaks}"
            optional = [
                p
                for p in inspect.signature(fn).parameters.values()
                if p.default is not inspect.Parameter.empty
            ]
            assert all(p.default is None for p in optional), fn.__name__
            assert all(p.name.startswith("on_") for p in optional), fn.__name__


class _ItemsPump:
    """SessionPump 대역 — 큐 대신 ``items`` ``[(text, wake), …]`` 를 펌프와 같은
    순서(echo → 라우팅 → run_main → run_ended)로 한 번씩 돌린다."""

    items = (("hi", False),)

    def __init__(self, deps):
        self.deps = deps

    def run(self):
        import threading

        from agent_cli.pump import WAKE_AUTHOR, RunRequest

        for text, wake in self.items:
            self.deps.surface.echo({"text": text, "nickname": None}, wake=wake)
            if not wake and self.deps.surface.route(text):
                continue
            req = RunRequest(
                text=text,
                author=WAKE_AUTHOR if wake else None,
                author_is_user=not wake,
                request_id="",
                stop_event=threading.Event(),
                wake=wake,
            )
            res = self.deps.run_main(req)
            self.deps.surface.run_ended(res, req)
            self.after_item(res)

    def after_item(self, res):
        pass

    def route(self, text):  # run_main 의 ports 가 참조한다 (런 도중 주입 라우팅)
        return self.deps.surface.route(text)


@pytest.fixture
def _run_in_tmp(tmp_path, monkeypatch):
    """진짜 run 경로가 cwd 상대 ``.agent-cli/``(세션·스냅샷 저장소)에 쓴다 —
    ``_run_with`` 를 타는 클래스는 임시 디렉터리에서 돈다."""
    monkeypatch.chdir(tmp_path)


@pytest.mark.usefixtures("_run_in_tmp")
class TestRunCommandTeardownIntegration:
    """run 커맨드를 CliRunner 로 실제 구동해 경로별 teardown 수렴을 검증 —
    구조 핀(소스 스크레이프)보다 강한 실행 증거. 종전엔 skill 조기-반환이
    registry/MCP, @agent 경로가 MCP 정리를 누락했다(리뷰 §4.1) — 이제 모든
    경로(예외 포함)가 teardown_session 1회 호출로 끝난다."""

    def _run_with(
        self, query, *, pump=None, dispatch_result=False, run_loop=None, args=()
    ):
        """``pump`` 는 SessionPump 대역 클래스(기본 :class:`_ItemsPump` — 큐
        대신 ``items`` 를 한 번씩 돌린다)."""
        from contextlib import ExitStack

        from agent_cli.tools.result import ToolResult

        if run_loop is None:
            run_loop = MagicMock(return_value=ToolResult(True, output="ans"))
        if pump is None:
            pump = _ItemsPump
        boot = MagicMock()
        boot.dialect.name = "json_fc"
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
                    **(
                        {"side_effect": dispatch_result}
                        if callable(dispatch_result)
                        else {"return_value": dispatch_result}
                    ),
                )
            )
            st.enter_context(patch("agent_cli.loop.run_loop", run_loop))
            st.enter_context(patch("agent_cli.pump.SessionPump", pump))
            td = st.enter_context(patch("agent_cli.runtime.teardown_session"))
            result = self._invoke_cli(query, *args)
            calls["teardown"] = td
            calls["registry"] = registry
            calls["result"] = result
        return calls

    def _invoke_cli(self, query, *args):
        from typer.testing import CliRunner

        from agent_cli.main import app

        return CliRunner().invoke(app, ["run", query, *args])

    def test_main_path_full_teardown(self):
        c = self._run_with("hi")
        assert c["result"].exit_code == 0, c["result"].output
        c["teardown"].assert_called_once()
        kw = c["teardown"].call_args.kwargs
        assert kw["agent_registry"] is c["registry"]
        assert kw["mcp_manager"] == "MCP"  # 종전에도 메인 경로는 MCP 정리
        assert kw["warn_stuck"] is True  # 메인 펌프 경로 전용 경고 표면 유지
        from agent_cli.schedule.registry import ScheduleRegistry

        assert isinstance(kw["schedules"], ScheduleRegistry)  # v10.130.4

    def test_skill_dispatch_tears_down_completely(self):
        """수리 계약: skill 디스패치도 registry+MCP 를 정리한다 (종전:
        _finalize_run(session, ctx) 만 — registry 미종료 + MCP 미해제).
        v10.33.0 부터 디스패치가 펌프 안의 라우팅이라 조기-반환 경로가 없고,
        답변-대기 경고(warn_stuck)도 메인 경로와 같이 켜진다."""

        def _skill(text, output, **_kw):
            output.skill_result("some-skill", "done")
            return True

        c = self._run_with("/some-skill args", dispatch_result=_skill)
        assert c["result"].exit_code == 0, c["result"].output
        c["teardown"].assert_called_once()
        kw = c["teardown"].call_args.kwargs
        assert kw["agent_registry"] is c["registry"]
        assert kw["mcp_manager"] == "MCP"
        assert kw["warn_stuck"] is True

    def test_pump_exception_still_tears_down(self):
        """크래시 경로도 finally 로 수렴 — 종전엔 예외 시 MCP 미해제·세션
        미저장(펌프 내부 finally 는 registry 만 정리)이었다."""

        class _Boom(_ItemsPump):
            def run(self):
                raise RuntimeError("pump crashed")

        c = self._run_with("hi", pump=_Boom)
        assert c["result"].exit_code != 0  # 예외는 그대로 전파 (동작 보존)
        c["teardown"].assert_called_once()
        kw = c["teardown"].call_args.kwargs
        assert kw["agent_registry"] is c["registry"]
        assert kw["mcp_manager"] == "MCP"


class TestSessionHasLiveWork:
    """큐·모니터·예약 항을 run 펌프와 web self-reap 이 같은 함수로 본다
    (v10.32.1). 초판 모니터가 web 쪽 한 줄을 빠뜨린 자리의 구조적 봉합."""

    def test_each_term_alone_is_live(self):
        from agent_cli.runtime import session_has_live_work

        live = MagicMock(has_active_work=MagicMock(return_value=True))
        dead = MagicMock(has_active_work=MagicMock(return_value=False))
        assert session_has_live_work(pending_count=1)
        assert session_has_live_work(pending_count=0, monitors=live)
        assert session_has_live_work(pending_count=0, schedules=live)
        assert not session_has_live_work(pending_count=0, monitors=dead, schedules=dead)
        assert not session_has_live_work(pending_count=0)

    def test_both_hosts_consult_it(self):
        """구조 핀: 펌프의 `_quiet` 와 `web_instance_is_active` 둘 다 — 한쪽만
        고치면 다시 두 벌이 된다."""
        import inspect

        from agent_cli import main
        from agent_cli.pump import QuietPolicy

        for fn in (QuietPolicy.should_stop, main.web_instance_is_active):
            assert "session_has_live_work(" in inspect.getsource(fn), fn.__name__


@pytest.mark.usefixtures("_run_in_tmp")
class TestResultFileIsWrittenPerRun:
    """`--result-file` 은 런이 성공으로 끝난 자리에서 쓴다 (v10.31.2).

    종전엔 펌프가 반환한 뒤 한 번 썼는데, 펌프는 살아 있는 모니터가 있으면
    그 deadline(기본 2h)까지 돈다 — 모델이 건 모니터가 안 울리면 `complete`
    한 답이 파일로는 영영 안 나왔다(A/B 실측: 420s 타임아웃까지 파일 없음)."""

    def _res(self, ok, out):
        from agent_cli.tools.result import ToolResult

        return ToolResult(ok, output=out) if ok else ToolResult(False, error=out)

    def test_written_before_the_pump_returns(self, tmp_path):
        out = tmp_path / "r.txt"
        seen = {}

        class _Pump(_ItemsPump):
            def after_item(self, res):
                seen["exists_while_pump_alive"] = out.exists()  # 모니터 대기 중인 자리

        c = TestRunCommandTeardownIntegration()._run_with(
            "hi", pump=_Pump, args=("--result-file", str(out))
        )
        assert c["result"].exit_code == 0, c["result"].output
        assert seen["exists_while_pump_alive"] is True
        assert out.read_text(encoding="utf-8") == "ans"

    def test_a_wake_run_does_not_overwrite_the_query_answer(self, tmp_path):
        """모니터 만료 통지에 깨어난 런의 "No new action required" 가 진짜 답을
        덮어썼다(v10.34.0 스모크) — 결과 파일은 argv 질의 런의 답뿐이다."""
        out = tmp_path / "r.txt"
        run_loop = MagicMock(
            side_effect=[self._res(True, "first"), self._res(True, "second")]
        )

        class _Two(_ItemsPump):
            items = (("hi", False), ("wake", True))

        c = TestRunCommandTeardownIntegration()._run_with(
            "hi", pump=_Two, run_loop=run_loop, args=("--result-file", str(out))
        )
        assert c["result"].exit_code == 0, c["result"].output
        assert out.read_text(encoding="utf-8") == "first"

    def test_a_wake_run_does_not_fill_in_for_a_failed_query_run(self, tmp_path):
        """질의 런이 실패했으면 파일은 없어야 한다 — 뒤 깨우기 런의 답을 채우면
        실패가 성공으로 둔갑한다(max-turns 뒤 만료 통지 → "nothing to do")."""
        out = tmp_path / "r.txt"
        run_loop = MagicMock(
            side_effect=[self._res(False, "max turns"), self._res(True, "rescued")]
        )

        class _Two(_ItemsPump):
            items = (("hi", False), ("wake", True))

        c = TestRunCommandTeardownIntegration()._run_with(
            "hi", pump=_Two, run_loop=run_loop, args=("--result-file", str(out))
        )
        assert c["result"].exit_code == 1, "종료 코드도 같은 판정 (v10.124.0)"
        assert not out.exists()

    def test_write_is_atomic_and_skips_an_empty_answer(self, tmp_path):
        from agent_cli.main import _write_result_file

        out = tmp_path / "r.txt"
        _write_result_file(str(out), "")
        assert not out.exists()
        _write_result_file(str(out), "ans")
        assert out.read_text(encoding="utf-8") == "ans"
        assert [p.name for p in tmp_path.iterdir()] == ["r.txt"], "temp 파일이 남았다"

    def test_a_later_failed_run_keeps_the_earlier_answer(self, tmp_path):
        """실패 런은 파일을 안 쓴다(기존 계약) — 앞 런의 성공 답이 남는다."""
        out = tmp_path / "r.txt"
        run_loop = MagicMock(
            side_effect=[self._res(True, "first"), self._res(False, "boom")]
        )

        class _Two(_ItemsPump):
            items = (("hi", False), ("wake", True))

        c = TestRunCommandTeardownIntegration()._run_with(
            "hi", pump=_Two, run_loop=run_loop, args=("--result-file", str(out))
        )
        assert c["result"].exit_code == 0, c["result"].output
        assert out.read_text(encoding="utf-8") == "first"

    def test_no_success_means_no_file(self, tmp_path):
        out = tmp_path / "r.txt"
        run_loop = MagicMock(return_value=self._res(False, "boom"))
        c = TestRunCommandTeardownIntegration()._run_with(
            "hi", run_loop=run_loop, args=("--result-file", str(out))
        )
        assert c["result"].exit_code == 1, c["result"].output
        assert not out.exists()
