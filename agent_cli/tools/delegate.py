"""In-process subagent delegation tool.

Supports context modes: none (independent), fork (copy), inherit (shared).
Uses tasks array API: single item = sync, multiple items = parallel (threading).
"""

from __future__ import annotations

import copy
import json
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from agent_cli.context.manager import ContextManager
from agent_cli.providers.base import LLMProvider
from agent_cli.providers.compat import ModelCapabilities
from agent_cli.resource_loader import ResourceLoader
from agent_cli.tools.result import ToolResult

# ── Agent file loading ──────────────────────────

_AGENT_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")

_BUILTIN_AGENTS_DIR = Path(__file__).parent.parent / "agents" / "builtin"

_AGENT_SEARCH_PATHS = [
    Path.cwd() / ".agent-cli" / "agents",
    Path.home() / ".agent-cli" / "agents",
    _BUILTIN_AGENTS_DIR,
]

_FRONTMATTER_PATTERN = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n(.*)",
    re.S,
)

_agent_loader = ResourceLoader(_AGENT_SEARCH_PATHS)


def _reset_agent_loader(search_paths: list[Path] | None = None) -> None:
    """Reset the agent loader with new search paths (for testing)."""
    global _agent_loader
    paths = search_paths if search_paths is not None else _AGENT_SEARCH_PATHS
    _agent_loader = ResourceLoader(paths)


def _validate_agent_name(name: str) -> bool:
    """Validate agent name: alphanumeric, hyphens, underscores only."""
    return bool(_AGENT_NAME_PATTERN.match(name))


def _load_agent(name: str) -> tuple[str | None, dict, str | None]:
    """Load agent definition file.

    Returns:
        (role_prompt, config_dict, error_message)
        - Success: (body, {allowed-tools, model, ...}, None)
        - Failure: (None, {}, error_message)
    """
    if not _validate_agent_name(name):
        return None, {}, f"Invalid agent name '{name}': only [a-zA-Z0-9_-] allowed"

    resource = _agent_loader.load_one(name)
    if resource is None:
        paths_str = ", ".join(str(p / f"{name}.md") for p in _AGENT_SEARCH_PATHS)
        return None, {}, f"Agent '{name}' not found. Searched: {paths_str}"

    if not resource.body:
        return None, {}, f"Agent file '{name}.md' has no content"

    return resource.body, resource.meta, None


@dataclass
class DelegateResult:
    """Structured result from delegate execution."""

    output: str | None = None
    files_read: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    iterations: int = 0
    duration_secs: float = 0.0
    activity_log: list[str] = field(default_factory=list)
    last_actions: list[str] = field(default_factory=list)


def _extract_activity_log(messages: list[dict], max_entries: int = 20) -> list[str]:
    """Extract per-iteration action summaries from context messages.

    Parses assistant messages for ReAct JSON (action/action_input),
    formats each into a one-line summary.

    Returns list of strings like:
      ["iter 1: read_file auth.py", "iter 2: shell pytest"]
    """
    log: list[str] = []
    iter_num = 0

    for msg in messages:
        if msg.get("role") != "assistant":
            continue

        try:
            data = json.loads(msg["content"])
        except (json.JSONDecodeError, TypeError, KeyError):
            continue
        if not isinstance(data, dict):
            continue

        action = data.get("action", "")
        if not action:
            continue

        iter_num += 1
        action_input = data.get("action_input", {})
        summary = _summarize_action(action, action_input)
        log.append(f"iter {iter_num}: {summary}")

    if len(log) > max_entries:
        trimmed = log[:max_entries]
        trimmed.append(f"... and {len(log) - max_entries} more")
        return trimmed
    return log


def _summarize_action(action: str, action_input: dict) -> str:
    """Format a single action into a one-line summary."""
    if not isinstance(action_input, dict):
        return action

    path = action_input.get("path", "")
    if action == "read_file" and path:
        return f"read_file {Path(path).name}"
    elif action in ("write_file", "edit_file") and path:
        return f"{action} {Path(path).name}"
    elif action == "shell":
        cmd = action_input.get("command", "")
        return f"shell {cmd[:60]}" if cmd else "shell"
    elif action == "delegate":
        task = action_input.get("task", "")
        return f'delegate "{task[:40]}"' if task else "delegate"
    else:
        return action


def _extract_last_actions(messages: list[dict], n: int = 5) -> list[str]:
    """Extract last N actions with their observation results.

    Returns list of strings like:
      ["iter 4: shell pytest -> ERROR: 3 tests failed",
       "iter 5: edit_file test_auth.py -> hash mismatch"]
    """
    actions: list[tuple[int, int, str]] = []
    iter_num = 0
    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        try:
            data = json.loads(msg["content"])
        except (json.JSONDecodeError, TypeError, KeyError):
            continue
        if not isinstance(data, dict) or not data.get("action"):
            continue

        iter_num += 1
        summary = _summarize_action(data["action"], data.get("action_input", {}))
        actions.append((i, iter_num, summary))

    last_n = actions[-n:]

    result: list[str] = []
    for msg_idx, it, summary in last_n:
        obs_hint = ""
        if msg_idx + 1 < len(messages) and messages[msg_idx + 1].get("role") == "user":
            obs = messages[msg_idx + 1]["content"]
            for line in obs.split("\n")[:5]:
                if any(
                    kw in line.upper()
                    for kw in ["ERROR", "FAIL", "EXCEPTION", "TRACEBACK"]
                ):
                    obs_hint = f" → {line.strip()[:80]}"
                    break
        result.append(f"iter {it}: {summary}{obs_hint}")

    return result


def _persist_delegate_result(
    formatted: str,
    task: str,
    duration: float,
    iterations: int,
    success: bool,
    scratchpad_dir: Path,
    depth: int,
) -> None:
    """Save delegate result as session artifact and update scratchpad progress.

    Uses existing scratchpad infrastructure (save_artifact, append_progress).
    Errors are silently caught to avoid disrupting delegate flow.
    """
    from agent_cli.context.scratchpad import append_progress, save_artifact

    try:
        status = "success" if success else "failed"
        save_artifact(
            step=0,
            content=formatted,
            tags=["delegate", f"depth:{depth}", status],
            summary=f"delegate: {task[:60]}",
            base=scratchpad_dir,
        )

        status_str = "completed" if success else "FAILED"
        append_progress(
            step=0,
            summary=(
                f"delegate {status_str}: {task[:60]} "
                f"({duration:.1f}s, {iterations} iters)"
            ),
            base=scratchpad_dir,
        )
    except Exception:
        pass


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

    # 1. Output
    if result.output:
        parts.append(result.output)
    else:
        parts.append("(subagent returned no result)")

    # 2. Activity log
    if result.activity_log:
        parts.append("")
        parts.append("[Subagent activity]")
        for entry in result.activity_log:
            parts.append(f"- {entry}")

    # 3. Last actions on failure
    if result.last_actions:
        parts.append("")
        parts.append("[Last actions before failure]")
        for entry in result.last_actions:
            parts.append(f"- {entry}")

    # 4. Files touched
    if result.files_read or result.files_modified:
        parts.append("")
        parts.append("[Files touched]")
        if result.files_read:
            parts.append(f"- Read: {', '.join(sorted(result.files_read))}")
        if result.files_modified:
            parts.append(f"- Modified: {', '.join(sorted(result.files_modified))}")

    # 5. Duration + Iterations
    footer = []
    if result.duration_secs > 0:
        footer.append(f"[Duration: {result.duration_secs:.1f}s]")
    if result.iterations > 0:
        footer.append(f"[Subagent used {result.iterations} iterations]")
    if footer:
        parts.append("")
        parts.append(" ".join(footer))

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
    agent_name: str = "",
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

    # ── Agent loading ──
    agent_role = ""
    if agent_name:
        role_prompt, agent_config, error = _load_agent(agent_name)
        if error:
            return ToolResult(False, error=f"Delegation rejected: {error}")

        agent_role = role_prompt

        # Agent config overrides (lower priority than explicit task params)
        if allowed_tools is None and agent_config.get("allowed-tools"):
            allowed_tools = agent_config["allowed-tools"]

        agent_model = agent_config.get("model")
        if agent_model and isinstance(agent_model, str):
            model = agent_model

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

    t0 = time.monotonic()

    result_str = run_loop(
        query=task,
        provider=provider,
        capabilities=capabilities,
        model=model,
        provider_name=provider_name,
        base_url=base_url,
        api_key=api_key,
        max_turns=max_turns,
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
        agent_role=agent_role,
    )

    duration = time.monotonic() - t0

    delegate_result = DelegateResult(output=result_str, duration_secs=duration)

    if context_mode != "inherit":
        files_read, files_modified = ctx._extract_files_touched(ctx.messages)
        delegate_result.files_read = sorted(files_read)
        delegate_result.files_modified = sorted(files_modified)

    # Activity log extraction
    delegate_result.activity_log = _extract_activity_log(ctx.messages)

    # Iterations count from activity log
    real_entries = [e for e in delegate_result.activity_log if not e.startswith("...")]
    delegate_result.iterations = len(real_entries)

    # Last actions on failure
    if result_str is None:
        delegate_result.last_actions = _extract_last_actions(ctx.messages)

    formatted = _format_delegate_output(delegate_result)

    # Persist to disk
    _persist_delegate_result(
        formatted=formatted,
        task=task,
        duration=duration,
        iterations=delegate_result.iterations,
        success=result_str is not None,
        scratchpad_dir=scratchpad_dir,
        depth=depth,
    )

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
    max_turns: int = 0,
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
            agent_name=spec.get("agent", ""),
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

    for t in threads:
        t.join()

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
        max_turns=max_turns,
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
            agent_name=spec.get("agent", ""),
            suppress_output=suppress_output,
            **common_kwargs,
        )
    else:
        return _run_parallel(task_specs=tasks, **common_kwargs)
