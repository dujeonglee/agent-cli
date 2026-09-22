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


def build_agent_registry(
    session_dir, runtime: AgentRuntime, max_agents=None, *, monitors=None
):
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
    if monitors is not None:
        wire_monitor_delivery(registry, monitors)
    return registry


def wire_monitor_delivery(registry, monitors) -> None:
    """모니터 보고를 주소 배달에 잇는다 — 양방향 (docs/wiring §3.3).

    **위치가 아니라 제약이 본질이다.** 이 배선은 `wire_agent_mail` 의
    `restore`/`auto_spawn` **보다 먼저** 끝나야 한다: 부활한 에이전트의
    inbox 로 질문이 재배달되고 그 워커들이 즉시 루프를 시작한다. 또 run 의
    스킬 조기-반환(`try_dispatch_agent_or_skill`)은 `wire_agent_mail` 보다
    앞서므로, 거기 배선하면 `agent-cli run "/skill …"` 에서 등록이 거부된다.
    `build_agent_registry` 직후가 두 제약을 만족하는 가장 이른 지점이다.

    역방향(`registry.monitors`)이 필요한 이유: 워커의 `finally` 가
    `drop_owner` 를 불러야 하는데, `AgentRegistry` 는 모니터 레지스트리를
    **전혀 몰랐다**. 도구가 쓰는 프로세스 전역을 집어올 수도 있지만
    주입으로 둔다 — 레지스트리 테스트가 가짜를 꽂을 수 있어야 한다.
    """
    monitors.deliver = registry.deliver
    registry.monitors = monitors


def build_monitor_registry(session_dir=None):
    """monitor 레지스트리 생성 + 프로세스 전역 등록 (run/web 공용).

    `build_agent_registry` 의 형제다. 도구는 모듈 전역(`monitor/runtime.py`)
    으로 닿는다 — 루프가 `LoopConfig.monitor_registry` 로도 닿던 두 번째
    경로는 주소 배달로 바뀌며 사라졌다(보고를 루프가 drain 하지 않는다).
    배달 배선은 `wire_monitor_delivery` 가 건다.
    """
    from agent_cli.monitor.registry import MonitorRegistry
    from agent_cli.monitor.runtime import set_monitor_registry

    registry = MonitorRegistry(session_dir=session_dir)
    set_monitor_registry(registry)
    return registry


def wire_agent_mail(registry, *, enqueue_wake, on_mail_notice, parent_ctx=None):
    """MailWaker + 회신 알림 훅 + restore/auto_spawn 조립 (run/web 공용).

    종전엔 ``monitors`` 인자를 받아 술어에 ``or monitors.has_pending()`` 을
    얹고 ``on_report`` 를 꽂았다. **둘 다 없앴다** — 모니터 보고가 이제
    주소 배달로 메일박스에 들어오므로, ``has_pending_replies()`` 가 이미
    참이 되고 ``on_reply`` 가 이미 깨운다. 얹을 항이 없는 것이 가장
    안전하다: 그 항을 빠뜨린 것이 v9.11.0 의 "발화해도 조용함" 이었다.

    Returns ``(waker, revived, auto)`` — 부활/auto-spawn 수는 호출자가
    자기 표면(콘솔/렌더러)으로 알린다."""
    from agent_cli.subagent.agents_live import MailWaker

    waker = MailWaker(enqueue_wake, registry.has_pending_replies)

    def _on_agent_mail(reply: dict) -> None:
        on_mail_notice(reply)
        waker.on_mail()

    registry.on_reply = _on_agent_mail
    # P3 (D7): resume 세션이면 이전 에이전트 자동 재생성 + 미배달 회신
    # 복원 (fresh 세션은 agents.json 이 없어 no-op).
    revived = registry.restore(parent_ctx=parent_ctx)
    auto = registry.auto_spawn(parent_ctx=parent_ctx)
    return waker, revived, auto


def main_run_ended(agent_registry, output: str = "") -> int:
    """main 의 런이 끝났다 — 남은 빚을 정리한다 (v9.22.0).

    독촉은 런 **안**에서 3회 있었다(dispatch 의 빚 목록 — 상주와 같은 기계).
    그러고도 남은 것: 회신 빚은 라벨 붙은 런 요약(``output``)을 그 에이전트
    inbox 로 폴백 배달하고, 답 안 한 질문은 사유와 함께 닫는다.

    상주 에이전트는 워커 루프의 디스패치 수렴점에서 같은 일을 한다. main 은
    펌프가 둘(run/web)이라 호출부가 둘인데, **정의는 하나**여야 한다 —
    한쪽만 고치는 것이 이 파일이 존재하는 이유의 사고 유형이다
    (모듈 docstring 의 teardown 4갈래 참조).

    독촉은 메일박스로 가고 ``MailWaker`` 가 유휴 main 도 깨운다. 건 수를
    돌려준다(0 = 빚 없음).
    """
    if agent_registry is None:
        return 0
    return agent_registry.end_main_run(output or "")


def teardown_session(
    session,
    ctx,
    *,
    agent_registry=None,
    mcp_manager=None,
    monitors=None,
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
    if monitors is not None:
        # **에이전트 종료보다 먼저.** 폴링 스레드는 종전에 아무도 안 멈췄고
        # (`stop()` 은 호출자가 없었다) 지금까지는 아무도 안 읽는 리스트에
        # 쌓을 뿐이라 무해했다. 이제는 **배달된다** — 종료 뒤 발화하면
        # `finalize_session` 뒤에 `agents.json` 이 다시 쓰이거나, 세션과
        # 함께 죽은 감시의 흔적이 다음 세션에 떠오른다.
        #
        # 전체 드롭이 아니라 `closed` 인 이유: `drop_owner` 가 `delete` 처럼
        # 저장하면 살아 있는 행만 쓰는 `_save` 가 `monitors.json` 을 비워,
        # 다음 세션의 "이전 세션 모니터 N건" 통지가 사라진다.
        monitors.stop()
        monitors.closed = True
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


def ports_for_run(*, agent_registry, mcp_manager) -> LoopPorts:
    """CLI 한 방 실행 (`agent-cli run …`)."""
    return LoopPorts(
        owner="main",
        questions=_main_questions(agent_registry),
        agent_registry=agent_registry,
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


def ports_for_skill(*, agent_registry, owner: str) -> LoopPorts:
    """스킬 실행 루프.

    ``owner`` 는 **부모에게서 물려받는다** — 상주 에이전트 안에서 돈 스킬이
    건 모니터는 그 에이전트에게 보고해야 한다. 기본값을 두지 않는 이유는
    그 상속이 빠지면 조용히 main 으로 가기 때문이다.
    """
    return LoopPorts(
        owner=owner,
        agent_registry=agent_registry,
        questions=None,
        mcp_manager=None,
        message_handler=None,
        hook_runner=None,
        route_message=None,
        dequeue_user_message=None,
        unwired={
            "questions": _AS_BEFORE,
            "mcp_manager": _AS_BEFORE,
            "message_handler": _AS_BEFORE,
            "hook_runner": _HOOKS_LATER,
            "route_message": _WEB_ONLY,
            "dequeue_user_message": _WEB_ONLY,
        },
    )


def ports_for_oneshot(*, owner: str) -> LoopPorts:
    """one-shot delegate — 포트 없음. ``owner`` 는 부모에게서 물려받는다."""
    return LoopPorts(
        owner=owner,
        questions=None,
        agent_registry=None,
        mcp_manager=None,
        message_handler=None,
        hook_runner=None,
        route_message=None,
        dequeue_user_message=None,
        unwired={
            "agent_registry": _NO_REGISTRY_IN_SUBLOOP,
            "questions": _AS_BEFORE,
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
        mcp_manager=None,
        hook_runner=None,
        route_message=None,
        dequeue_user_message=None,
        unwired={
            "agent_registry": _NO_REGISTRY_IN_SUBLOOP,
            "mcp_manager": _AS_BEFORE,
            "hook_runner": _HOOKS_LATER,
            "route_message": _WEB_ONLY,
            "dequeue_user_message": _WEB_ONLY,
        },
    )
