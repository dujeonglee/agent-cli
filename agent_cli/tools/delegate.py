"""In-process subagent delegation tool.

Supports context modes: none (independent), fork (copy), inherit (shared).
Uses tasks array API: single item = sync, multiple items = parallel (threading).
"""

from __future__ import annotations

import copy
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from agent_cli.context.manager import ContextManager
from agent_cli.providers.base import LLMProvider
from agent_cli.providers.compat import ModelCapabilities
from agent_cli.tools.result import ToolResult


@dataclass
class DelegateResult:
    """Structured result from delegate execution."""

    output: str | None = None
    files_read: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    iterations: int = 0


def _fork_context(
    parent_ctx: ContextManager,
    provider: LLMProvider,
    model: str,
    capabilities: ModelCapabilities,
    scratchpad_dir=None,
) -> ContextManager:
    """Deep copy parent context for fork mode."""
    forked = ContextManager(
        provider=provider,
        model=model,
        capabilities=capabilities,
        scratchpad_dir=scratchpad_dir,
    )
    forked.messages = copy.deepcopy(parent_ctx.messages)
    forked._summary = parent_ctx._summary
    forked._msg_chars = parent_ctx._msg_chars
    return forked


def _resolve_scratchpad_dir(session, parent_ctx) -> Path:
    """Determine scratchpad directory from session or parent context."""
    if session and hasattr(session, "session_dir"):
        return Path(session.session_dir)
    if (
        parent_ctx
        and hasattr(parent_ctx, "_scratchpad_dir")
        and parent_ctx._scratchpad_dir
    ):
        return parent_ctx._scratchpad_dir
    return Path(tempfile.mkdtemp(prefix="delegate_"))


def _format_delegate_output(result: DelegateResult) -> str:
    """Format DelegateResult into observation string."""
    parts = []
    if result.output:
        parts.append(result.output)
    else:
        parts.append("(subagent returned no result)")

    if result.files_read or result.files_modified:
        parts.append("")
        parts.append("[Files touched]")
        if result.files_read:
            parts.append(f"- Read: {', '.join(sorted(result.files_read))}")
        if result.files_modified:
            parts.append(f"- Modified: {', '.join(sorted(result.files_modified))}")

    if result.iterations > 0:
        parts.append(f"\n[Subagent used {result.iterations} iterations]")

    return "\n".join(parts)


def _format_parallel_results(
    specs: list[dict], results: list[ToolResult | None]
) -> ToolResult:
    """Combine multiple delegate results into a single observation."""
    parts: list[str] = []
    succeeded = 0
    failed = 0

    for i, (spec, result) in enumerate(zip(specs, results)):
        label = spec["task"][:80]
        parts.append(f"[Task {i + 1}] {label}")
        if result and result.success:
            parts.append(result.output or "(no output)")
            succeeded += 1
        else:
            error = result.error if result else "Thread timed out or crashed"
            parts.append(f"ERROR: {error}")
            failed += 1
        parts.append("")

    summary = f"[Parallel execution: {len(specs)} tasks"
    if failed == 0:
        summary += ", all succeeded]"
    else:
        summary += f", {succeeded} succeeded, {failed} failed]"
    parts.append(summary)

    combined = "\n".join(parts)
    if failed == 0:
        return ToolResult(True, output=f"STATUS: success\nRESULT:\n{combined}")
    else:
        return ToolResult(False, error=f"STATUS: error\n{combined}")


def _run_single(
    task: str,
    context_mode: str = "none",
    allowed_tools: list[str] | None = None,
    parent_ctx: ContextManager | None = None,
    provider: LLMProvider | None = None,
    model: str = "",
    capabilities: ModelCapabilities | None = None,
    provider_name: str = "",
    base_url: str = "",
    api_key: str = "",
    depth: int = 0,
    max_depth: int = 2,
    max_iter: int = 0,
    timeout: int = 300,
    suppress_output: bool = False,
    session=None,
    skill_stack: list[str] | None = None,
    stop_event=None,
) -> ToolResult:
    """Execute a single delegate task."""
    from agent_cli.loop import run_loop

    if not task.strip():
        return ToolResult(False, error="Delegation rejected: empty task")

    if provider is None or capabilities is None:
        return ToolResult(
            False, error="Delegation rejected: missing provider/capabilities"
        )

    scratchpad_dir = _resolve_scratchpad_dir(session, parent_ctx)

    if context_mode == "fork":
        if parent_ctx is None:
            return ToolResult(
                False, error="Delegation rejected: fork requires parent context"
            )
        ctx = _fork_context(parent_ctx, provider, model, capabilities, scratchpad_dir)
    elif context_mode == "inherit":
        if parent_ctx is None:
            return ToolResult(
                False, error="Delegation rejected: inherit requires parent context"
            )
        ctx = parent_ctx
    else:
        ctx = ContextManager(
            provider=provider,
            model=model,
            capabilities=capabilities,
            scratchpad_dir=scratchpad_dir,
        )

    result_str = run_loop(
        query=task,
        provider=provider,
        capabilities=capabilities,
        model=model,
        provider_name=provider_name,
        base_url=base_url,
        api_key=api_key,
        max_iter=max_iter,
        verbose=False,
        suppress_output=suppress_output,
        depth=depth + 1,
        max_depth=max_depth,
        delegate_timeout=timeout,
        active_tools=allowed_tools,
        ctx=ctx,
        session=session,
        skill_stack=skill_stack,
        stop_event=stop_event,
    )

    delegate_result = DelegateResult(output=result_str)
    if context_mode != "inherit":
        files_read, files_modified = ctx._extract_files_touched(ctx.messages)
        delegate_result.files_read = sorted(files_read)
        delegate_result.files_modified = sorted(files_modified)

    formatted = _format_delegate_output(delegate_result)
    if result_str is not None:
        return ToolResult(True, output=f"STATUS: success\nRESULT:\n{formatted}")
    else:
        return ToolResult(
            False,
            error=f"STATUS: error\nERROR: Subagent did not complete\n{formatted}",
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
    max_iter: int = 0,
    timeout: int = 300,
    session=None,
    skill_stack: list[str] | None = None,
) -> ToolResult:
    """Execute multiple delegate tasks in parallel using threading."""
    # Validate: inherit not allowed with multiple tasks
    for spec in task_specs:
        if spec.get("context", "none") == "inherit":
            return ToolResult(
                False,
                error="Delegation rejected: inherit mode cannot be used with multiple tasks",
            )

    results: list[ToolResult | None] = [None] * len(task_specs)
    stop_event = threading.Event()

    def worker(index: int, spec: dict) -> None:
        results[index] = _run_single(
            task=spec["task"],
            context_mode=spec.get("context", "none"),
            allowed_tools=spec.get("tools"),
            parent_ctx=parent_ctx,
            provider=provider,
            model=model,
            capabilities=capabilities,
            provider_name=provider_name,
            base_url=base_url,
            api_key=api_key,
            depth=depth,
            max_depth=max_depth,
            max_iter=max_iter,
            timeout=timeout,
            suppress_output=True,  # Always suppress for parallel
            session=session,
            skill_stack=skill_stack,
            stop_event=stop_event,
        )

    threads = []
    for i, spec in enumerate(task_specs):
        t = threading.Thread(target=worker, args=(i, spec), daemon=True)
        threads.append(t)
        t.start()

    deadline = time.monotonic() + timeout
    for t in threads:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        t.join(timeout=remaining)

    # Signal remaining threads to stop gracefully
    if any(t.is_alive() for t in threads):
        stop_event.set()

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
    max_iter: int = 0,
    timeout: int = 300,
    suppress_output: bool = False,
    session=None,
    skill_stack: list[str] | None = None,
) -> ToolResult:
    """Delegate tasks to in-process subagents.

    Args:
        args: Dict with "tasks" array. Each item has "task", optional "context", "tools".
              Single item = sync execution. Multiple items = parallel (threading).
    """
    tasks = args.get("tasks", [])
    if not tasks:
        return ToolResult(False, error="Delegation rejected: empty tasks array")

    common_kwargs = dict(
        parent_ctx=parent_ctx,
        provider=provider,
        model=model,
        capabilities=capabilities,
        provider_name=provider_name,
        base_url=base_url,
        api_key=api_key,
        depth=depth,
        max_depth=max_depth,
        max_iter=max_iter,
        timeout=timeout,
        session=session,
        skill_stack=skill_stack,
    )

    if len(tasks) == 1:
        spec = tasks[0]
        return _run_single(
            task=spec.get("task", ""),
            context_mode=spec.get("context", "none"),
            allowed_tools=spec.get("tools"),
            suppress_output=suppress_output,
            **common_kwargs,
        )
    else:
        return _run_parallel(task_specs=tasks, **common_kwargs)
