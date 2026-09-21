"""run/web 부트스트랩·teardown 조립기 (리뷰 §4.1 P1, v8.39.0).

세 조각을 소유한다:

- :class:`AgentRuntime` — 상주/서브 에이전트에 넘기는 provider 배선
  13키의 **단일 정의**. 종전엔 run/web(main.py)/tool_bridge 세 곳이 같은
  dict 리터럴을 손으로 나열했고, 키 구성부터 어긋나 있었다(run/web 엔
  ``compaction_enabled`` 부재, web 은 ``hooks_config`` 를 None 으로 고정).
- :func:`build_agent_registry` / :func:`wire_agent_mail` — registry 생성+
  main 슬롯 등록, 그리고 waker·회신 훅·restore/auto_spawn 조립의 공용화.
  두 단계로 쪼갠 이유: run 은 skill 조기-반환 **이전**에 registry 만 만들고
  restore 는 그 뒤에 하므로(순서 보존 = 등가성), 생성과 배선을 분리해야
  기존 순서를 바이트 그대로 유지한다.
- :func:`teardown_session` — 종료 시퀀스의 단일 소유자: 상주 에이전트
  전원 종료 → 스피너 정지 → MCP 해제 → 세션 저장. 종전엔 run 의 종료
  경로 4갈래 + web 1갈래가 각자 나열했고, 그 결과 2경로는 MCP 미해제
  (stdio 자식 프로세스 잔존), 1경로는 registry 미종료였다 — 모든 경로가
  이 함수 하나로 수렴하면 그 누락 클래스가 구조적으로 사라진다.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

from agent_cli.loop.ports import LoopPorts


@dataclass(frozen=True)
class AgentRuntime:
    """상주/서브 에이전트 실행에 필요한 provider 배선 묶음.

    소비자(AgentRegistry._run_message / runner)는 dict 를 기대하므로
    경계에서는 :meth:`as_dict` 로 넘긴다 — 필드명 == 종전 dict 키
    (계약은 tests 의 키-셋 핀 테스트가 고정). ``compaction_enabled`` 는
    소비 측 기본값(True)과 같아, 키를 늘 싣는 것이 종전 run/web 의
    키-생략과 행동 동일하다.
    """

    provider: Any
    capabilities: Any
    model: str
    provider_name: str
    base_url: str
    api_key: str
    max_turns: int
    depth: int
    max_depth: int
    timeout: int
    session: Any
    hooks_config: dict | None = None
    compaction_enabled: bool = True

    def as_dict(self) -> dict:
        # dataclasses.asdict 는 재귀 변환이라 capabilities(dataclass)까지
        # dict 로 풀어버린다 — 얕은 사상으로 객체 동일성을 보존한다.
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_loop_config(cls, cfg, provider) -> AgentRuntime:
        """LoopConfig → AgentRuntime (tool_bridge 의 상주 모드 인터셉트용)."""
        return cls(
            provider=provider,
            capabilities=cfg.capabilities,
            model=cfg.model,
            provider_name=cfg.provider_name,
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            max_turns=cfg.max_turns,
            depth=cfg.depth,
            max_depth=cfg.max_depth,
            timeout=cfg.agent_timeout,
            session=cfg.session,
            hooks_config=cfg.hooks_config,
            compaction_enabled=cfg.compaction_enabled,
        )


def build_agent_registry(session_dir, runtime: AgentRuntime, max_agents=None):
    """AgentRegistry 생성 + main registry 슬롯 등록 (v7.17.0 배선 통일).

    runtime 프리필: restore/auto-spawn 된 에이전트가 도구 호출(스폰) 없이
    첫 접촉(웹 창 인간 개입 등)을 받아도 provider 배선이 있도록.

    ``max_agents`` None = 미지정 → registry 가 env/기본값을 고른다 (v8.61.0,
    CLI ``--max-agents``)."""
    from agent_cli.subagent.agents_live import AgentRegistry, set_main_registry

    registry = AgentRegistry(
        session_dir, runtime=runtime.as_dict(), max_agents=max_agents
    )
    set_main_registry(registry)
    return registry


def build_monitor_registry(session_dir=None):
    """monitor 레지스트리 생성 + 프로세스 전역 등록 (run/web 공용).

    `build_agent_registry` 의 형제다. 도구는 모듈 전역으로, 루프는
    `LoopConfig.monitor_registry` 로 닿는데 **같은 객체**여야 한다 — 여기서
    하나를 만들어 둘 다에 준다.
    """
    from agent_cli.monitor.registry import MonitorRegistry
    from agent_cli.monitor.runtime import set_monitor_registry

    registry = MonitorRegistry(session_dir=session_dir)
    set_monitor_registry(registry)
    return registry


def wire_agent_mail(
    registry, *, enqueue_wake, on_mail_notice, parent_ctx=None, monitors=None
):
    """MailWaker + 회신 알림 훅 + restore/auto_spawn 조립 (run/web 공용).

    ``monitors`` 가 오면 **깨우기를 공유한다**: 모니터 보고도 턴 경계에서만
    소비되는데(`AgentLoop._deliver_monitor_reports`), 모니터의 존재 이유가
    "오래 걸리는 걸 걸어 두고 딴 일 하라" 라 발화 시점에 main 이 유휴인 것이
    정상이다. 깨우지 않으면 보고가 큐에 앉은 채 사용자는 아무것도 못 본다
    (사용자 제보: 등록은 됐는데 2분 무반응). 설계(docs/monitor)가 처음부터
    "waker 술어에 `or monitors.has_pending()` 를 얹어 합치기를 공짜로
    얻는다" 고 적어 뒀는데 배선만 빠져 있었다.

    Returns ``(waker, revived, auto)`` — 부활/auto-spawn 수는 호출자가
    자기 표면(콘솔/렌더러)으로 알린다."""
    from agent_cli.subagent.agents_live import MailWaker

    def _pending() -> bool:
        if registry.has_pending_replies():
            return True
        return bool(monitors is not None and monitors.has_pending())

    waker = MailWaker(enqueue_wake, _pending)
    if monitors is not None:
        # 보고가 **도착한 순간** 깨운다. 술어만 얹으면 다음 `mark_idle` 까지
        # 기다리는데, 유휴로 접어든 뒤 발화하면 그 시점이 영영 안 온다.
        monitors.on_report = waker.on_mail

    def _on_agent_mail(reply: dict) -> None:
        on_mail_notice(reply)
        waker.on_mail()

    registry.on_reply = _on_agent_mail
    # P3 (D7): resume 세션이면 이전 에이전트 자동 재생성 + 미배달 회신
    # 복원 (fresh 세션은 agents.json 이 없어 no-op).
    revived = registry.restore(parent_ctx=parent_ctx)
    auto = registry.auto_spawn(parent_ctx=parent_ctx)
    return waker, revived, auto


def main_run_ended(agent_registry) -> int:
    """main 의 런이 끝났다 — 미답 질문이 있으면 독촉한다 (agent-ask §3.4).

    상주 에이전트는 워커 루프의 디스패치 수렴점에서 같은 일을 한다. main 은
    펌프가 둘(run/web)이라 호출부가 둘인데, **정의는 하나**여야 한다 —
    한쪽만 고치는 것이 이 파일이 존재하는 이유의 사고 유형이다
    (모듈 docstring 의 teardown 4갈래 참조).

    독촉은 메일박스로 가고 ``MailWaker`` 가 유휴 main 도 깨운다. 건 수를
    돌려준다(0 = 빚 없음).
    """
    if agent_registry is None:
        return 0
    return agent_registry.remind_owed("main")


def teardown_session(
    session,
    ctx,
    *,
    agent_registry=None,
    mcp_manager=None,
    warn_stuck: bool = False,
) -> None:
    """공용 종료 시퀀스 — 모든 run/web 종료 경로가 여기로 수렴한다.

    순서(종전 run 메인 경로와 동일): ①(옵션) 미답 질문 경고
    ② 상주 에이전트 전원 종료 ③ 스피너 정지 ④ MCP 해제(stdio 자식
    프로세스·errlog fd 정리) ⑤ 세션 저장. 저장-완료 메시지는 표면별로
    다르므로 호출자가 출력한다."""
    if warn_stuck and agent_registry is not None:
        stuck = agent_registry.open_human_question_keys()
        if stuck:
            from agent_cli.render import C, console

            console.print(
                f"[{C['accent']}]❓ 에이전트 {', '.join(stuck)} 의 질문에 답하지 "
                f"않은 채 종료 — 다음 세션에서 트레이에 다시 뜹니다[/]"
            )
    if agent_registry is not None:
        agent_registry.shutdown_all()

    from agent_cli.render import render_spinner_stop

    render_spinner_stop()
    if mcp_manager:
        mcp_manager.disconnect_all()
    if session is not None:
        from agent_cli.context.session import finalize_session

        finalize_session(session, ctx)


# ── 루프 포트 빌더 — 조립 지점 다섯의 단일 정의 (DESIGN.md §4.4) ──────
#
# 종전엔 다섯 곳이 각자 부분집합을 손으로 나열했고, 포트 여덟 중 일곱이
# 어딘가에선 빠져 있었다 — 그게 의도인지 사고인지 코드 어디에도 없었다.
# 여기 모아 두면 §2.2 의 표가 **코드가 되고**, 나란히 비교된다.
#
# ``LoopPorts`` 에 기본값이 없으므로 새 포트를 더하면 **아래 다섯이 전부
# 즉시 안 만들어진다** — 호스트마다 "연결" 또는 "이래서 미연결" 중 하나를
# 쓸 수밖에 없다. 사유 문자열은 **문서**지 검증물이 아니다(진위는 아무도
# 못 잡는다). 그래서 확인된 사실만 적고, 확인 못 한 것은 "종전 배선
# 유지" 로 적어 둔다 — 지어낸 근거보다 낫다.

_WEB_ONLY = "웹 전용 — 대화창 입력 큐가 있는 호스트에만 있다"
_HOOKS_LATER = "배선만 준비 — 응용이 생기면 연결한다 (사용자 의도, 2026-09)"
_NO_REGISTRY_IN_SUBLOOP = (
    "서브루프에 레지스트리가 닿으면 안 된다 — 'teammate 안 teammate 금지'의 "
    "단일 가드가 LoopConfig.agent_registry 다"
)
_AS_BEFORE = "종전 배선 유지 — 이 호스트는 이 포트를 받은 적이 없다"


def _main_questions(agent_registry):
    """main 의 답변 수단 (docs/agent-ask/DESIGN.md §4)."""
    return agent_registry.question_port(None) if agent_registry else None


def ports_for_run(*, agent_registry, monitor_registry, mcp_manager) -> LoopPorts:
    """CLI 한 방 실행 (`agent-cli run …`)."""
    return LoopPorts(
        owner="main",
        questions=_main_questions(agent_registry),
        agent_registry=agent_registry,
        monitor_registry=monitor_registry,
        mcp_manager=mcp_manager,
        message_handler=None,
        hook_runner=None,
        route_message=None,
        dequeue_user_message=None,
        unwired={
            "message_handler": "상주 에이전트 전용 — main 은 agent 도구로 보낸다",
            "hook_runner": _HOOKS_LATER,
            "route_message": _WEB_ONLY,
            "dequeue_user_message": _WEB_ONLY,
        },
    )


def ports_for_web(
    *,
    agent_registry,
    monitor_registry,
    mcp_manager,
    dequeue_user_message,
    route_message,
) -> LoopPorts:
    """웹 워커의 턴 — **메시지마다** 새로 짓는다.

    ``dequeue_user_message``/``route_message`` 가 반복마다 새로 만드는
    클로저라(main.py, ``noqa: B023``) 세션 수명 객체로 둘 수 없다.
    """
    return LoopPorts(
        owner="main",
        questions=_main_questions(agent_registry),
        agent_registry=agent_registry,
        monitor_registry=monitor_registry,
        mcp_manager=mcp_manager,
        dequeue_user_message=dequeue_user_message,
        route_message=route_message,
        message_handler=None,
        hook_runner=None,
        unwired={
            "message_handler": "상주 에이전트 전용 — main 은 agent 도구로 보낸다",
            "hook_runner": _HOOKS_LATER,
        },
    )


def ports_for_skill(*, agent_registry) -> LoopPorts:
    """스킬 실행 루프.

    ``owner`` 는 C2 전까지 **자리표시자**다 — 부모 owner 를 나르는 seam 이
    C2 의 몫이라 지금은 알 수 없다. C2 전에는 아무도 ``owner`` 를 읽지
    않으므로 행동은 불변이다(§6). 영구 기본값과는 다르다: 저건 누락을
    영영 가리고, 이건 값이 생길 때까지의 한시적 자리다.
    """
    return LoopPorts(
        owner="main",
        agent_registry=agent_registry,
        questions=None,
        monitor_registry=None,
        mcp_manager=None,
        message_handler=None,
        hook_runner=None,
        route_message=None,
        dequeue_user_message=None,
        unwired={
            "questions": _AS_BEFORE,
            "monitor_registry": _AS_BEFORE,
            "mcp_manager": _AS_BEFORE,
            "message_handler": _AS_BEFORE,
            "hook_runner": _HOOKS_LATER,
            "route_message": _WEB_ONLY,
            "dequeue_user_message": _WEB_ONLY,
        },
    )


def ports_for_oneshot() -> LoopPorts:
    """one-shot delegate — 포트 없음. ``owner`` 는 스킬과 같은 자리표시자."""
    return LoopPorts(
        owner="main",
        questions=None,
        agent_registry=None,
        monitor_registry=None,
        mcp_manager=None,
        message_handler=None,
        hook_runner=None,
        route_message=None,
        dequeue_user_message=None,
        unwired={
            "agent_registry": _NO_REGISTRY_IN_SUBLOOP,
            "questions": _AS_BEFORE,
            "monitor_registry": _AS_BEFORE,
            "mcp_manager": _AS_BEFORE,
            "message_handler": "one-shot 은 상주가 아니다 — 받을 상대가 없다",
            "hook_runner": _HOOKS_LATER,
            "route_message": _WEB_ONLY,
            "dequeue_user_message": _WEB_ONLY,
        },
    )


def ports_for_resident(*, key: str, message_handler, questions) -> LoopPorts:
    """상주 서브에이전트의 런. ``owner`` 만 진짜 값을 갖는다."""
    return LoopPorts(
        owner=f"agent:{key}",
        message_handler=message_handler,
        questions=questions,
        agent_registry=None,
        monitor_registry=None,
        mcp_manager=None,
        hook_runner=None,
        route_message=None,
        dequeue_user_message=None,
        unwired={
            "agent_registry": _NO_REGISTRY_IN_SUBLOOP,
            "monitor_registry": _AS_BEFORE,
            "mcp_manager": _AS_BEFORE,
            "hook_runner": _HOOKS_LATER,
            "route_message": _WEB_ONLY,
            "dequeue_user_message": _WEB_ONLY,
        },
    )
