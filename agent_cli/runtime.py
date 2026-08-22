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


def build_agent_registry(session_dir, runtime: AgentRuntime):
    """AgentRegistry 생성 + main registry 슬롯 등록 (v7.17.0 배선 통일).

    runtime 프리필: restore/auto-spawn 된 에이전트가 도구 호출(스폰) 없이
    첫 접촉(웹 창 인간 개입 등)을 받아도 provider 배선이 있도록."""
    from agent_cli.subagent.agents_live import AgentRegistry, set_main_registry

    registry = AgentRegistry(session_dir, runtime=runtime.as_dict())
    set_main_registry(registry)
    return registry


def wire_agent_mail(registry, *, enqueue_wake, on_mail_notice, parent_ctx=None):
    """MailWaker + 회신 알림 훅 + restore/auto_spawn 조립 (run/web 공용).

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


def teardown_session(
    session,
    ctx,
    *,
    agent_registry=None,
    mcp_manager=None,
    warn_stuck: bool = False,
) -> None:
    """공용 종료 시퀀스 — 모든 run/web 종료 경로가 여기로 수렴한다.

    순서(종전 run 메인 경로와 동일): ①(옵션) 답변-대기 에이전트 경고
    ② 상주 에이전트 전원 종료 ③ 스피너 정지 ④ MCP 해제(stdio 자식
    프로세스·errlog fd 정리) ⑤ 세션 저장. 저장-완료 메시지는 표면별로
    다르므로 호출자가 출력한다."""
    if warn_stuck and agent_registry is not None:
        stuck = agent_registry.waiting_ask_keys()
        if stuck:
            from agent_cli.render import C, console

            console.print(
                f"[{C['accent']}]⚠ 에이전트 {', '.join(stuck)} 이(가) 답변 대기 "
                f"중인 채 종료 — resume 시 질문은 STALE 처리됩니다[/]"
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
