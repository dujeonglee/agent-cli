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
    from agent_cli.loop.ports import LoopPorts


class ProfileToolsError(ValueError):
    """An ``agent`` call's ``tools`` shares nothing with the profile's
    ``allowed-tools``. The message is model-facing."""


def apply_role_overrides(
    config: dict,
    *,
    allowed_tools: list[str] | None,
    model: str,
    hooks_config: dict | None,
) -> tuple[list[str] | None, str, dict | None]:
    """역할 md config 를 오버레이해 (allowed_tools, model, hooks_config) 반환.

    model 은 역할 config 가 이긴다. hooks 는 교체가 아니라 caller 설정 **위에
    병합** — 부모 matcher 가 계속 적용된다 (Skill.hooks 와 동일 의미).

    도구는 프로파일의 ``allowed-tools`` 가 **상한**이다 (v10.14.0). 호출의
    ``tools`` 는 그 안에서 좁히기만 한다 — 종전엔 ``tools`` 가 있으면
    프로파일 목록을 통째로 대체해, 읽기 전용 프로파일에
    ``tools: [write_file]`` 를 주면 쓰기 도구가 생겼다. 겹치는 도구가
    하나도 없으면 :class:`ProfileToolsError` (빈 목록을 돌려주면 루프가
    "제한 없음" 으로 읽는다). 프로파일에 목록이 없으면 ``tools`` 그대로.
    """
    profile_tools = config.get("allowed-tools")
    if profile_tools:
        if allowed_tools is None:
            allowed_tools = profile_tools
        else:
            requested = allowed_tools
            allowed_tools = [t for t in requested if t in profile_tools]
            if not allowed_tools:
                raise ProfileToolsError(
                    f"`tools` ({', '.join(requested)}) has nothing in common with "
                    f"what this profile allows ({', '.join(profile_tools)}). "
                    "`tools` can only narrow a profile's set — omit it to use the "
                    "whole set, or use a profile that allows those tools."
                )

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
    binding=None,
) -> tuple[ContextManager | None, str]:
    """서브에이전트 ctx 생성 — ``(ctx, error)``.

    ``none`` 은 fresh, ``fork`` 는 부모 history.jsonl 복사 후 resume,
    ``resume`` (teammate P3) 은 **자기 디렉토리의 기존 history** 를 그대로
    이어받는다 — 세션 resume 시 teammate 재생성용 (복사 없음; delegate
    스키마 enum 에는 없어 LLM 이 직접 선택할 수 없다).

    dialect 은 main 과 같은 체인 — ``--dialect`` 강제 > ``model``(role
    오버라이드 적용 후의 effective model)의 models.json 바인딩. 둘 다
    없으면 spawn 거부 ``(None, error)`` (v10.3.0: 부모 상속·기본값 없음;
    unknown 바인딩 이름도 같은 경로 — D2). 토큰 예산은 부모 상속.
    생성된 live ctx 는 현재 스레드의 인스펙터 스코프에 등록 —
    CLI(minimal 렌더러)에선 no-op.
    """
    from agent_cli.context.manager import DEFAULT_COMPACTION_RATIO, ContextManager

    # 방언·예산은 바인딩의 것 (v10.43.0): 세션 바인딩을 그대로 참조하거나, 프로파일이
    # 모델을 고정했으면 `binding.pinned(model)` 복사본(그 모델의 능력·방언). 바인딩
    # 없이 부르면(테스트·일회성) 부모 ctx 의 방언·예산을 쓴다.
    if binding is None and parent_ctx is not None:
        binding = parent_ctx.binding
    if (
        binding is not None
        and parent_ctx is not None
        and binding.dialect is not parent_ctx.dialect
    ):
        from agent_cli.verbose import debug_log

        debug_log(
            f"subagent dialect: {binding.dialect.name} (model binding: "
            f"{binding.model}) — parent uses {parent_ctx.dialect.name}"
        )
    sub_dialect = (
        binding.dialect
        if binding is not None
        else (parent_ctx.dialect if parent_ctx is not None else None)
    )
    ratio = parent_ctx.compaction_ratio if parent_ctx else DEFAULT_COMPACTION_RATIO
    inherited_ratio = parent_ctx.token_ratio if parent_ctx else None
    # 바인딩 없는 부모(테스트)의 명시 예산만 물려받는다 — 바인딩이 있으면 ctx 가 매번
    # 바인딩에서 읽는다.
    budget = parent_ctx._budget_override if parent_ctx is not None else 0
    if context_mode == "fork":
        if parent_ctx is None:
            return None, "fork requires parent context"
        parent_ctx.fork_history_to(subagent_dir)
    ctx = ContextManager(
        session_dir=subagent_dir,
        max_context_tokens=budget,
        resume=context_mode in ("fork", "resume"),
        dialect=sub_dialect,
        binding=binding,
        compaction_ratio=ratio,
        token_ratio=inherited_ratio,
    )

    if parent_ctx is not None and ctx is not None:
        ctx.stream_idle_timeout_s = parent_ctx.stream_idle_timeout_s
        ctx.stream_max_attempts = parent_ctx.stream_max_attempts
        # 사고·문법도 세션의 현재 값 (v10.43.0) — 종전엔 ctx 마다 env 를 다시
        # 읽어 web 🧠·📐 노브가 팀원·delegate 에는 닿지 않았다.
        ctx.thinking_override = dict(parent_ctx.thinking_override)
        ctx.grammar_override = parent_ctx.grammar_override
        ctx.sampling = dict(parent_ctx.sampling)  # 🎲 (v10.69.0) — 복사본

    from agent_cli.render import get_renderer

    get_renderer().note_scope_ctx(ctx)
    return ctx, ""


def run_subagent_message(
    query: str,
    ctx: ContextManager,
    *,
    ports: LoopPorts,
    binding,
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
    user_requests: list | None = None,
    query_request_id: str = "",
    compaction_enabled: bool = True,
    peer_agents_section: str = "",
    query_images: list[dict] | None = None,
) -> tuple[LoopResult, float]:
    """``ctx`` 위에서 메시지 1건을 처리 — ``(loop_result, 소요초)``.

    ``query_images`` (v10.61.0): 시작 메시지에 딸린 ``ImageRef`` dict — 사람이
    에이전트 창에서 붙인 이미지(docs/vision/DESIGN.md §5 에이전트 채널).

    ``depth`` 는 **호출자의** 깊이다 — 서브루프는 ``depth + 1`` 로 돈다
    (기존 delegate 의미 그대로). ``ask_handler``(teammate P2)가 있으면
    서브루프의 ask 가 사용자 대신 이 훅으로 라우팅된다 — delegate 는
    안 넘겨서 종전대로 사용자에게 간다.
    """
    from agent_cli.loop import run_loop

    t0 = time.monotonic()
    loop_result = run_loop(
        query=query,
        query_images=query_images,
        binding=binding,
        max_turns=max_turns,
        verbose=False,
        depth=depth + 1,
        max_depth=max_depth,
        active_tools=active_tools,
        ctx=ctx,
        session=session,
        skill_stack=skill_stack,
        agent_stack=agent_stack,
        agent_name=agent_name,
        stop_event=stop_event,
        agent_role=agent_role,
        hooks_config=hooks_config,
        user_requests=user_requests,
        # 상주 런의 시작 메시지가 **사람의 요청**인가 = 회계에 오른 요청이
        # 있는가. 에이전트·main 항목(message/ask/회신)으로 시작한 런은 사용자
        # 런이 아니다 — 빚이 남은 `complete` 은 거기서 거부된다(v9.22.1).
        query_author_is_user=bool(user_requests),
        # 시작 항목의 장부 id (v10.56.0) — 그 레코드에 찍혀 열려 있는 동안 핀된다
        # (사람 요청 `u-…`, 동료 요청의 회신 빚 `r-…`).
        query_request_id=query_request_id,
        compaction_enabled=compaction_enabled,
        ports=ports,
        peer_agents_section=peer_agents_section,
    )
    return loop_result, time.monotonic() - t0
