"""서브에이전트 공용 러너 (teammate P0, v4.53.0).

delegate 와 teammate 가 공유하는 실행 3단계를 소유한다:

1. :func:`apply_role_overrides` — 역할 md 의 config(frontmatter)를 명시
   파라미터보다 낮은 우선순위로 오버레이. **로더-불가지론적** — 역할 md
   로더 자체는 공유하지 않는다 (delegate 는 ``agents/``, teammate 는
   ``teammates/`` 로 정의 디렉토리가 분리, D11). 이 모듈은 파싱된 config
   dict 만 받으므로 두 로더의 결과를 동일 의미로 소화한다.
2. :func:`create_subagent_ctx` — none(독립)/fork(히스토리 복사) ctx 생성
   + 현재 스레드의 인스펙터 스코프에 live ctx 등록 (v4.52.0).
3. :func:`run_subagent_message` — ctx 위에서 ``run_loop`` 1회 (메시지
   1건 처리). delegate 는 이걸 한 번 부르고 ctx 를 버리고, teammate 는
   같은 ctx 로 반복해서 부른다 — 이 차이가 두 도구의 전부다.

무거운 의존(context.manager, loop, render)은 함수-내부 import — exec.py
가 지던 registry → delegate → context.manager → tools.registry 순환을
그대로 회피한다.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from agent_cli.context.manager import ContextManager
    from agent_cli.loop import LoopResult
    from agent_cli.providers.base import LLMProvider
    from agent_cli.providers.capabilities import ModelCapabilities


def apply_role_overrides(
    config: dict,
    *,
    allowed_tools: list[str] | None,
    model: str,
    hooks_config: dict | None,
) -> tuple[list[str] | None, str, dict | None]:
    """역할 md config 를 오버레이해 (allowed_tools, model, hooks_config) 반환.

    우선순위는 delegate 의 기존 규칙 그대로: 명시 task 파라미터 >
    역할 config. hooks 는 교체가 아니라 caller 설정 **위에 병합** —
    부모 matcher 가 계속 적용된다 (Skill.hooks 와 동일 의미).
    """
    if allowed_tools is None and config.get("allowed-tools"):
        allowed_tools = config["allowed-tools"]

    role_model = config.get("model")
    if role_model and isinstance(role_model, str):
        model = role_model

    raw_hooks = config.get("hooks")
    if isinstance(raw_hooks, dict):
        from agent_cli.hooks import merge_hooks_configs, parse_hooks_config

        overlay = parse_hooks_config(raw_hooks) or None
        hooks_config = merge_hooks_configs(hooks_config, overlay)

    return allowed_tools, model, hooks_config


def create_subagent_ctx(
    context_mode: str,
    parent_ctx: ContextManager | None,
    subagent_dir: Path,
    *,
    model: str = "",
) -> tuple[ContextManager | None, str]:
    """서브에이전트 ctx 생성 — ``(ctx, error)``.

    ``none`` 은 fresh, ``fork`` 는 부모 history.jsonl 복사 후 resume,
    ``resume`` (teammate P3) 은 **자기 디렉토리의 기존 history** 를 그대로
    이어받는다 — 세션 resume 시 teammate 재생성용 (복사 없음; delegate
    스키마 enum 에는 없어 LLM 이 직접 선택할 수 없다).

    wire format 은 ``model``(role 오버라이드 적용 후의 effective model)의
    models.json 바인딩이 있으면 그것, 없으면 부모 상속 (부모 없으면
    ContextManager 기본값) — multi-wire-format Phase 1 (D1: 모델 프라이어
    존중이 세션 플래그보다 우선). unknown 바인딩 이름은 spawn 거부
    ``(None, error)`` (D2: 조용한 폴백 금지). 토큰 예산은 부모 상속.
    생성된 live ctx 는 현재 스레드의 인스펙터 스코프에 등록 —
    CLI(minimal 렌더러)에선 no-op.
    """
    from agent_cli.context.manager import DEFAULT_COMPACTION_RATIO, ContextManager
    from agent_cli.wire_formats import get as _get_wf
    from agent_cli.wire_formats import wire_format_for_model

    binding = wire_format_for_model(model)
    if binding is not None:
        try:
            sub_wire_format = _get_wf(binding)
        except KeyError as exc:
            return None, str(exc.args[0] if exc.args else exc)
        if parent_ctx is not None and parent_ctx.wire_format.name != binding:
            from agent_cli.verbose import debug_log

            debug_log(
                f"subagent wire_format: {binding} (model binding: {model}) "
                f"— parent uses {parent_ctx.wire_format.name}"
            )
    else:
        sub_wire_format = parent_ctx.wire_format if parent_ctx else None

    # Inherit the parent's live compaction ratio at creation time (web slider
    # value snapshot) — like max_context_tokens. Changing the slider later only
    # affects newly spawned/resumed sub-agents; re-spawn to apply a new value.
    ratio = parent_ctx.compaction_ratio if parent_ctx else DEFAULT_COMPACTION_RATIO
    if context_mode == "fork":
        if parent_ctx is None:
            return None, "fork requires parent context"
        parent_ctx.fork_history_to(subagent_dir)
        ctx = ContextManager(
            session_dir=subagent_dir,
            max_context_tokens=parent_ctx.max_context_tokens,
            resume=True,
            wire_format=sub_wire_format,
            compaction_ratio=ratio,
        )
    elif context_mode == "resume":
        budget = parent_ctx.max_context_tokens if parent_ctx else 0
        ctx = ContextManager(
            session_dir=subagent_dir,
            max_context_tokens=budget,
            resume=True,
            wire_format=sub_wire_format,
            compaction_ratio=ratio,
        )
    else:
        budget = parent_ctx.max_context_tokens if parent_ctx else 0
        ctx = ContextManager(
            session_dir=subagent_dir,
            max_context_tokens=budget,
            wire_format=sub_wire_format,
            compaction_ratio=ratio,
        )

    # P3 (v8.55.0): 스트림 무진전 한도도 spawn 시점 스냅샷 상속 — compaction
    # ratio 와 동형 (이후 부모 변경은 새 spawn 에만 적용).
    if parent_ctx is not None and ctx is not None:
        ctx.stream_idle_timeout_s = parent_ctx.stream_idle_timeout_s

    from agent_cli.render import get_renderer

    get_renderer().note_scope_ctx(ctx)
    return ctx, ""


def run_subagent_message(
    query: str,
    ctx: ContextManager,
    *,
    provider: LLMProvider,
    capabilities: ModelCapabilities,
    model: str,
    timeout: int,
    provider_name: str = "",
    base_url: str = "",
    api_key: str = "",
    max_turns: int = 0,
    depth: int = 0,
    max_depth: int = 2,
    active_tools: list[str] | None = None,
    session=None,
    skill_stack: list[str] | None = None,
    agent_stack: list[str] | None = None,
    agent_name: str = "",
    stop_event=None,
    agent_role: str = "",
    hooks_config: dict | None = None,
    compaction_enabled: bool = True,
    ask_handler=None,
    message_handler=None,
    peer_agents_section: str = "",
) -> tuple[LoopResult, float]:
    """``ctx`` 위에서 메시지 1건을 처리 — ``(loop_result, 소요초)``.

    ``depth`` 는 **호출자의** 깊이다 — 서브루프는 ``depth + 1`` 로 돈다
    (기존 delegate 의미 그대로). ``ask_handler``(teammate P2)가 있으면
    서브루프의 ask 가 사용자 대신 이 훅으로 라우팅된다 — delegate 는
    안 넘겨서 종전대로 사용자에게 간다.
    """
    from agent_cli.loop import run_loop

    t0 = time.monotonic()
    loop_result = run_loop(
        query=query,
        provider=provider,
        capabilities=capabilities,
        model=model,
        provider_name=provider_name,
        base_url=base_url,
        api_key=api_key,
        max_turns=max_turns,
        verbose=False,
        depth=depth + 1,
        max_depth=max_depth,
        agent_timeout=timeout,
        active_tools=active_tools,
        ctx=ctx,
        session=session,
        skill_stack=skill_stack,
        agent_stack=agent_stack,
        agent_name=agent_name,
        stop_event=stop_event,
        agent_role=agent_role,
        hooks_config=hooks_config,
        compaction_enabled=compaction_enabled,
        ask_handler=ask_handler,
        message_handler=message_handler,
        peer_agents_section=peer_agents_section,
    )
    return loop_result, time.monotonic() - t0
