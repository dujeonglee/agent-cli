"""In-process subagent delegation tool.

Supports context modes: none (independent), fork (copy conversation history).
Uses tasks array API: single item = sync, multiple items = parallel (threading).
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import TYPE_CHECKING

from agent_cli.constants import AGENT_DEFAULT_TIMEOUT
from agent_cli.providers.base import LLMProvider
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.tools.result import ToolResult

if TYPE_CHECKING:
    # Runtime-imported inside the dispatch path to avoid a
    # registry → delegate → context.manager → tools.registry →
    # tools import cycle (registry now imports DelegateTool at module load).
    from agent_cli.context.manager import ContextManager

# ── Agent file loading ──────────────────────────

from agent_cli.subagent.profiles import load_profile
from agent_cli.subagent.report import (
    DelegateResult,
    _extract_activity_log,
    _extract_last_actions,
    _format_delegate_output,
    _format_parallel_results,
    _generate_run_dir_name,
    _persist_run_result,
    _resolve_session_dir,
)


def _run_single(
    task: str,
    context_mode: str = "none",
    allowed_tools: list[str] | None = None,
    agent_name: str = "",
    instructions: str = "",
    parent_ctx: ContextManager | None = None,
    provider: LLMProvider | None = None,
    model: str = "",
    capabilities: ModelCapabilities | None = None,
    provider_name: str = "",
    base_url: str = "",
    api_key: str = "",
    depth: int = 0,
    max_depth: int = 2,
    max_turns: int = 0,
    timeout: int = AGENT_DEFAULT_TIMEOUT,
    session=None,
    skill_stack: list[str] | None = None,
    agent_stack: list[str] | None = None,
    stop_event=None,
    hooks_config: dict | None = None,
    compaction_enabled: bool = True,
    run_dir_name: str = "",
) -> ToolResult:
    """Execute a single delegate task.

    P0(teammate, v4.53.0): 실행 몸통(역할 config 오버레이·ctx 생성·
    run_loop 1회)은 :mod:`agent_cli.subagent.runner` 로 추출 — teammate
    와 공유. 이 함수는 delegate 고유분(가드·디렉토리 명명·리포트 조립)
    만 남긴 thin wrapper 다. 관찰 문구·result.md 는 바이트 불변.
    """
    from agent_cli.subagent.agents_live import compose_role_prompt
    from agent_cli.subagent.runner import (
        apply_role_overrides,
        create_subagent_ctx,
        run_subagent_message,
    )

    if not task.strip():
        return ToolResult(False, error="Delegation rejected: empty task")

    if provider is None or capabilities is None:
        return ToolResult(
            False, error="Delegation rejected: missing provider/capabilities"
        )

    # Agent cycle check (A → B → A). Named delegates only — anonymous
    # ones aren't pushed onto ``agent_stack`` so they can't loop on
    # name, only on depth (handled below).
    if agent_name and agent_stack and agent_name in agent_stack:
        from agent_cli.recovery.recursion import format_recursion_error

        return ToolResult(
            False,
            error=format_recursion_error("agent", agent_name, list(agent_stack)),
        )

    # Depth ceiling — belt-and-suspenders. Mirrors the run_skill
    # path: AgentLoop init strips ``delegate`` from tools_list when
    # we hit the limit, so the live loop never reaches here through
    # normal dispatch. Direct callers (tests, the parallel-tasks
    # path with a custom ``active_tools``) hit it here so the
    # caller gets the same actionable message instead of a silent
    # bounce.
    if depth >= max_depth:
        from agent_cli.recovery.recursion import format_depth_limit_error

        label = agent_name or "(anonymous)"
        return ToolResult(
            False,
            error=format_depth_limit_error("agent", label, depth, max_depth),
        )

    # ── Agent loading ──
    agent_role = ""
    if agent_name:
        role_prompt, agent_config, error = load_profile(agent_name)
        if error:
            return ToolResult(False, error=f"Delegation rejected: {error}")

        agent_role = role_prompt

        # Agent config overrides (lower priority than explicit task params)
        allowed_tools, model, hooks_config = apply_role_overrides(
            agent_config,
            allowed_tools=allowed_tools,
            model=model,
            hooks_config=hooks_config,
        )

    # instant (5.0.0 run 모드): 인라인 지시를 프로파일 본문 뒤에 합성 —
    # 상주 spawn 과 동일 규칙 (파일=일반, 인라인=세션 특정).
    agent_role = compose_role_prompt(agent_role, instructions)

    # Resolve parent session dir and create delegate subdir
    parent_session_dir = _resolve_session_dir(session, parent_ctx)
    # Pre-named by the scope-opening caller (so the resume sidecar's ctx_dir
    # matches this ctx's actual directory); self-naming stays for direct
    # callers that never open a scope.
    if not run_dir_name:
        run_dir_name = _generate_run_dir_name(agent_name or "task")
    run_dir = parent_session_dir / run_dir_name

    # Context + run_loop — 공용 러너 (subagent/runner.py). wire_format
    # 해석(effective model 바인딩 > 부모 상속)·예산 상속, fork 히스토리
    # 복사, 인스펙터 스코프 등록(v4.52.0)이 전부 러너 안이다.
    ctx, ctx_error = create_subagent_ctx(context_mode, parent_ctx, run_dir, model=model)
    if ctx is None:
        return ToolResult(False, error=f"Delegation rejected: {ctx_error}")

    loop_result, duration = run_subagent_message(
        task,
        ctx,
        provider=provider,
        capabilities=capabilities,
        model=model,
        timeout=timeout,
        provider_name=provider_name,
        base_url=base_url,
        api_key=api_key,
        max_turns=max_turns,
        depth=depth,
        max_depth=max_depth,
        active_tools=allowed_tools,
        session=session,
        skill_stack=skill_stack,
        agent_stack=agent_stack,
        agent_name=agent_name,
        stop_event=stop_event,
        agent_role=agent_role,
        hooks_config=hooks_config,
        compaction_enabled=compaction_enabled,
    )

    result_str = loop_result.output if loop_result.success else None
    delegate_result = DelegateResult(output=result_str, duration_secs=duration)

    # Activity log extraction
    delegate_result.activity_log = _extract_activity_log(ctx.get_raw_messages())

    # Iterations count from activity log
    real_entries = [e for e in delegate_result.activity_log if not e.startswith("...")]
    delegate_result.iterations = len(real_entries)

    # Last actions on failure
    if result_str is None:
        delegate_result.last_actions = _extract_last_actions(ctx.get_raw_messages())

    formatted = _format_delegate_output(delegate_result)

    # Persist result.md to delegate directory
    _persist_run_result(formatted, run_dir)

    artifact = f"{run_dir_name}/"

    if result_str is not None:
        return ToolResult(
            True,
            output=f"STATUS: success\nRESULT:\n{formatted}",
            artifact=artifact,
        )
    else:
        return ToolResult(
            False,
            error=f"STATUS: error\nERROR: Subagent did not complete\n{formatted}",
            artifact=artifact,
        )


def _run_parallel(
    task_specs: list[dict],
    parent_ctx: ContextManager | None = None,
    provider: LLMProvider | None = None,
    model: str = "",
    capabilities: ModelCapabilities | None = None,
    provider_name: str = "",
    base_url: str = "",
    api_key: str = "",
    depth: int = 0,
    max_depth: int = 2,
    max_turns: int = 0,
    timeout: int = AGENT_DEFAULT_TIMEOUT,
    session=None,
    skill_stack: list[str] | None = None,
    agent_stack: list[str] | None = None,
    stop_event=None,
    hooks_config: dict | None = None,
    compaction_enabled: bool = True,
) -> ToolResult:
    """Execute multiple delegate tasks in parallel using threading.

    UI orchestration (Live panel, per-task capture, group framing on
    completion) lives entirely in the renderer now — this function
    only signals the begin/end lifecycle markers. MinimalRenderer
    starts a Live region on the first ``begin_delegate_task`` and
    tears it down + dumps captured per-task output on the last
    ``end_delegate_task``. WebRenderer routes per-task events via
    the same markers into SSE collapsible cards.
    """
    from agent_cli.render import get_renderer

    results: list[ToolResult | None] = [None] * len(task_specs)
    durations: list[float] = [0.0] * len(task_specs)
    if stop_event is None:
        stop_event = threading.Event()

    renderer = get_renderer()
    # Captured HERE, on the spawning thread, for every worker below:
    #   parent — a worker's own scope stack is empty, so it cannot see that it
    #     was started from inside a skill; this thread can.
    #   batch_ts — one start time for the whole fan-out. Letting each worker
    #     stamp its own scattered a simultaneous batch across several rows of
    #     the swimlane's event-ordinal axis (~ms apart, one row each), which
    #     reads as "ran one after another". One shared value → one row → the
    #     view draws a fork. End times stay per-worker: which one took longest
    #     is real information.
    parent_scope = renderer.current_scope()
    batch_ts = time.time()

    def worker(index: int, spec: dict) -> None:
        # Per-task identity for renderer routing (CLI Live panel slot,
        # Web SSE collapsible card). A fresh uuid4 per worker — NOT the
        # thread id: ``threading.get_ident()`` is recycled once a worker
        # thread exits, so a later parallel delegate's workers could be
        # handed the same id as an earlier call's. The web frontend's
        # ``ensureTaskGroup`` then short-circuits on the stale entry and
        # never draws the new cards. uuid4 guarantees cross-call
        # uniqueness regardless of thread lifecycle.
        task_id = f"delegate-{index}-{uuid.uuid4().hex}"
        agent = spec.get("agent", "")
        task_text = spec.get("task", "")
        # Dir named HERE so the scope event carries it — resume replays the
        # card's inner turns from that directory (main history only has the
        # final observation).
        run_dir_name = _generate_run_dir_name(agent or "task")
        renderer.begin_scope(
            task_id=task_id,
            kind="run",
            label=task_text or agent or f"task #{index + 1}",
            agent=agent,
            index=index,
            parent=parent_scope,
            ts=batch_ts,
            ctx_dir=run_dir_name,
        )
        t0 = time.monotonic()
        result_for_marker = None
        error_msg = ""
        try:
            results[index] = _run_single(
                task=spec["task"],
                context_mode=spec.get("context", "none"),
                allowed_tools=spec.get("tools"),
                agent_name=spec.get("agent", ""),
                instructions=spec.get("instructions", ""),
                parent_ctx=parent_ctx,
                provider=provider,
                model=model,
                capabilities=capabilities,
                provider_name=provider_name,
                base_url=base_url,
                api_key=api_key,
                depth=depth,
                max_depth=max_depth,
                max_turns=max_turns,
                timeout=timeout,
                session=session,
                skill_stack=skill_stack,
                agent_stack=agent_stack,
                stop_event=stop_event,
                hooks_config=hooks_config,
                compaction_enabled=compaction_enabled,
                run_dir_name=run_dir_name,
            )
            result_for_marker = results[index]
        finally:
            durations[index] = time.monotonic() - t0
            success = bool(result_for_marker and result_for_marker.success)
            if result_for_marker and not result_for_marker.success:
                error_msg = result_for_marker.error or ""
            renderer.end_scope(
                task_id=task_id,
                kind="run",
                success=success,
                duration_s=durations[index],
                error=error_msg,
            )

    threads = []
    for i, spec in enumerate(task_specs):
        t = threading.Thread(target=worker, args=(i, spec), daemon=True)
        threads.append(t)
        t.start()
    try:
        for t in threads:
            t.join()
    finally:
        # Restore terminal state after the renderer's Live region (if
        # any) — prevents readline cursor confusion with CJK input on
        # subsequent prompts. Only meaningful when stdin is an actual
        # TTY; ``agent-cli web`` run in the background detaches stdin
        # and ``termios.tcflush`` on a non-TTY raises OSError(ENODEV)
        # on macOS, which has surfaced as a worker error in the SSE
        # stream.
        try:
            import sys
            import termios

            if sys.stdin is not None and sys.stdin.isatty():
                termios.tcflush(sys.stdin, termios.TCIFLUSH)
        except (ImportError, OSError, ValueError):
            # ValueError catches "I/O operation on closed file" from
            # ``isatty()`` when the stream was already disposed.
            pass

    return _format_parallel_results(task_specs, results)


def tool_delegate(
    args: dict,
    parent_ctx: ContextManager | None = None,
    provider: LLMProvider | None = None,
    model: str = "",
    capabilities: ModelCapabilities | None = None,
    provider_name: str = "",
    base_url: str = "",
    api_key: str = "",
    depth: int = 0,
    max_depth: int = 2,
    max_turns: int = 0,
    timeout: int = AGENT_DEFAULT_TIMEOUT,
    session=None,
    skill_stack: list[str] | None = None,
    agent_stack: list[str] | None = None,
    stop_event=None,
    hooks_config: dict | None = None,
    compaction_enabled: bool = True,
) -> ToolResult:
    """Delegate tasks to in-process subagents.

    Args:
        args: Dict with "tasks" array. Each item has "task", optional "context", "tools".
              Single item = sync execution. Multiple items = parallel (threading).
    """
    tasks = args.get("tasks", [])
    if not tasks:
        return ToolResult(False, error="Delegation rejected: empty tasks array")
    # Normalize: LLM may send ["task text"] instead of [{"task": "task text"}]
    tasks = [{"task": t} if isinstance(t, str) else t for t in tasks]

    common_kwargs = {
        "parent_ctx": parent_ctx,
        "provider": provider,
        "model": model,
        "capabilities": capabilities,
        "provider_name": provider_name,
        "base_url": base_url,
        "api_key": api_key,
        "depth": depth,
        "max_depth": max_depth,
        "max_turns": max_turns,
        "timeout": timeout,
        "session": session,
        "skill_stack": skill_stack,
        "agent_stack": agent_stack,
        "hooks_config": hooks_config,
        "compaction_enabled": compaction_enabled,
    }

    if len(tasks) == 1:
        # Single run: grouped nested rendering
        from agent_cli.render import (
            get_renderer,
            render_pop_depth,
            render_push_depth,
        )

        spec = tasks[0]
        agent_name = spec.get("agent", "")
        label = f"agent:{agent_name}" if agent_name else "agent"

        # Pair the CLI's group-block rendering with the same
        # ``begin_delegate_task`` / ``end_delegate_task`` lifecycle
        # the parallel path uses, so the web frontend opens a
        # collapsible card here too. Single-task delegate runs on the
        # main worker thread; the lifecycle markers register that
        # thread in ``WebRenderer._thread_to_task`` so every nested
        # emit gets the same ``task_id`` and is routed into the
        # card. CLI's renderer treats begin/end as no-ops.
        renderer = get_renderer()
        # uuid4 (not thread id) — same rationale as the parallel worker:
        # recycled thread idents would collide across delegate calls and
        # suppress the frontend card. See ``worker`` above.
        task_id = f"delegate-single-{uuid.uuid4().hex}"
        run_dir_name = _generate_run_dir_name(agent_name or "task")
        renderer.begin_scope(
            task_id=task_id,
            kind="run",
            label=label,
            agent=agent_name,
            index=0,
            ctx_dir=run_dir_name,
        )
        render_push_depth()
        t0 = time.monotonic()
        result = None
        try:
            result = _run_single(
                task=spec.get("task", ""),
                context_mode=spec.get("context", "none"),
                allowed_tools=spec.get("tools"),
                agent_name=agent_name,
                instructions=spec.get("instructions", ""),
                stop_event=stop_event,
                run_dir_name=run_dir_name,
                **common_kwargs,
            )
            return result
        finally:
            duration = time.monotonic() - t0
            render_pop_depth()
            success = bool(result and result.success)
            error_msg = ""
            if result and not result.success:
                error_msg = result.error or ""
            renderer.end_scope(
                task_id=task_id,
                kind="run",
                success=success,
                duration_s=duration,
                error=error_msg,
            )
    else:
        # Parallel: suppress during execution, collect and display after
        return _run_parallel(task_specs=tasks, stop_event=stop_event, **common_kwargs)
