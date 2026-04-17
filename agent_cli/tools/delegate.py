"""In-process subagent delegation tool.

Supports context modes: none (independent), fork (copy conversation history).
Uses tasks array API: single item = sync, multiple items = parallel (threading).
"""

from __future__ import annotations

import json
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from agent_cli.constants import DELEGATE_DEFAULT_TIMEOUT
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


def _generate_delegate_dir_name(agent_name: str) -> str:
    """Generate a unique delegate directory name: delegate_{name}_{hash}_{ts}"""
    import os

    name = agent_name or "task"
    hash_part = os.urandom(3).hex()  # 6-char hex
    ts = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    ms = f"{int(time.time() * 1000) % 1000:03d}"
    return f"delegate_{name}_{hash_part}_{ts}{ms}"


def _resolve_session_dir(session, parent_ctx) -> Path:
    """Determine session directory from session or parent context."""
    if session and hasattr(session, "session_dir"):
        return Path(session.session_dir)
    if parent_ctx and hasattr(parent_ctx, "session_dir"):
        return parent_ctx.session_dir
    return Path(tempfile.mkdtemp(prefix="delegate_"))


def _persist_delegate_result(
    formatted: str,
    delegate_dir: Path,
) -> None:
    """Save delegate result as result.md in delegate directory."""
    try:
        delegate_dir.mkdir(parents=True, exist_ok=True)
        result_path = delegate_dir / "result.md"
        result_path.write_text(formatted, encoding="utf-8")
    except Exception:
        pass


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

    # 4. Duration + Iterations
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
    timeout: int = DELEGATE_DEFAULT_TIMEOUT,
    session=None,
    skill_stack: list[str] | None = None,
    agent_stack: list[str] | None = None,
    stop_event=None,
) -> ToolResult:
    """Execute a single delegate task."""
    # Inline import: circular dependency — loop.py imports tool_delegate from this module
    from agent_cli.loop import run_loop

    if not task.strip():
        return ToolResult(False, error="Delegation rejected: empty task")

    if provider is None or capabilities is None:
        return ToolResult(
            False, error="Delegation rejected: missing provider/capabilities"
        )

    # Agent stack: prevent recursive calls (A→B→A)
    if agent_name and agent_stack and agent_name in agent_stack:
        return ToolResult(
            False,
            error=f"Recursive agent call blocked: '{agent_name}' is already in the call stack {agent_stack}.",
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

    # Resolve parent session dir and create delegate subdir
    parent_session_dir = _resolve_session_dir(session, parent_ctx)
    delegate_dir_name = _generate_delegate_dir_name(agent_name or "task")
    delegate_dir = parent_session_dir / delegate_dir_name

    # Create context based on mode
    if context_mode == "fork":
        if parent_ctx is None:
            return ToolResult(
                False, error="Delegation rejected: fork requires parent context"
            )
        # Fork: copy parent history.jsonl to delegate dir
        parent_ctx.fork_history_to(delegate_dir)
        budget = parent_ctx.max_context_tokens
        ctx = ContextManager(
            session_dir=delegate_dir, max_context_tokens=budget, resume=True
        )
    else:
        # none: fresh context (inherit parent budget if available)
        budget = parent_ctx.max_context_tokens if parent_ctx else 0
        ctx = ContextManager(session_dir=delegate_dir, max_context_tokens=budget)

    t0 = time.monotonic()

    loop_result = run_loop(
        query=task,
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
        delegate_timeout=timeout,
        active_tools=allowed_tools,
        ctx=ctx,
        session=session,
        skill_stack=skill_stack,
        agent_stack=agent_stack,
        agent_name=agent_name,
        stop_event=stop_event,
        agent_role=agent_role,
    )

    duration = time.monotonic() - t0

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
    _persist_delegate_result(formatted, delegate_dir)

    artifact = f"{delegate_dir_name}/"

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
    timeout: int = DELEGATE_DEFAULT_TIMEOUT,
    session=None,
    skill_stack: list[str] | None = None,
    agent_stack: list[str] | None = None,
    stop_event=None,
) -> ToolResult:
    """Execute multiple delegate tasks in parallel using threading."""
    from agent_cli.render import (
        render_push_depth,
        render_pop_depth,
        render_start_capture,
        render_stop_capture,
        render_replay_captured,
        render_group_start,
        render_group_end,
        get_renderer,
        console,
    )
    from rich.live import Live
    from rich.text import Text

    results: list[ToolResult | None] = [None] * len(task_specs)
    captured: list[list[str]] = [[] for _ in task_specs]
    durations: list[float] = [0.0] * len(task_specs)
    thread_ids: list[int] = [0] * len(task_specs)
    done_flags: list[bool] = [False] * len(task_specs)
    if stop_event is None:
        stop_event = threading.Event()

    renderer = get_renderer()

    def worker(index: int, spec: dict) -> None:
        thread_ids[index] = threading.get_ident()
        render_start_capture()
        t0 = time.monotonic()
        try:
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
                session=session,
                skill_stack=skill_stack,
                agent_stack=agent_stack,
                stop_event=stop_event,
            )
        finally:
            durations[index] = time.monotonic() - t0
            captured[index] = render_stop_capture()
            done_flags[index] = True

    threads = []
    for i, spec in enumerate(task_specs):
        t = threading.Thread(target=worker, args=(i, spec), daemon=True)
        threads.append(t)
        t.start()

    # Live status panel during parallel execution
    spinner_frames = ["⣾", "⣽", "⣻", "⢿", "⡿", "⣟", "⣯", "⣷"]
    frame_idx = [0]

    def render_live():
        lines = [Text(f"Running {len(task_specs)} tasks in parallel:", style="grey46")]
        frame = spinner_frames[frame_idx[0] % len(spinner_frames)]
        frame_idx[0] += 1
        for i, spec in enumerate(task_specs):
            task_text = spec.get("task", "")
            agent = spec.get("agent", "")
            label = (
                f"[{i + 1}] {agent}: {task_text}" if agent else f"[{i + 1}] {task_text}"
            )
            if done_flags[i]:
                ok = results[i] and results[i].success
                icon = "✓" if ok else "✗"
                lines.append(
                    Text(f"  {icon} {label} ({durations[i]:.1f}s)", style="grey46")
                )
            else:
                status = (
                    renderer.get_thread_status(thread_ids[i])
                    if thread_ids[i]
                    else "starting..."
                )
                lines.append(Text(f"  {frame} {label}", style="grey46"))
                # Show thought on separate indented line below task
                lines.append(Text(f"       {status}", style="grey46"))
        group = Text("\n").join(lines)
        return group

    # Skip Live panel if we're already inside a capturing thread
    # (nested parallel delegate — outer Live would compete with this one).
    show_live = not renderer.is_capturing
    try:
        if show_live:
            try:
                with Live(
                    render_live(),
                    console=console,
                    refresh_per_second=8,
                    transient=True,
                    get_renderable=render_live,
                ):
                    for t in threads:
                        t.join()
            except Exception:
                for t in threads:
                    t.join()
        else:
            for t in threads:
                t.join()
    finally:
        # Restore terminal state after Rich Live (prevents readline cursor
        # confusion with CJK input on subsequent prompts).
        try:
            import sys
            import termios

            termios.tcflush(sys.stdin, termios.TCIFLUSH)
        except (ImportError, OSError):
            pass

    # Replay each task as a group block
    for i, spec in enumerate(task_specs):
        task_text = spec.get("task", "")[:40]
        agent = spec.get("agent", "")
        label = f"[{i + 1}] {agent}: {task_text}" if agent else f"[{i + 1}] {task_text}"
        success = bool(results[i] and results[i].success)

        render_group_start(label, icon="🦀")
        render_push_depth()
        if captured[i]:
            render_replay_captured(captured[i])
        render_pop_depth()
        render_group_end(label, success=success, duration_s=durations[i])

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
    timeout: int = DELEGATE_DEFAULT_TIMEOUT,
    session=None,
    skill_stack: list[str] | None = None,
    agent_stack: list[str] | None = None,
    stop_event=None,
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
        agent_stack=agent_stack,
    )

    if len(tasks) == 1:
        # Single delegate: grouped nested rendering
        from agent_cli.render import (
            render_group_start,
            render_group_end,
            render_push_depth,
            render_pop_depth,
        )

        spec = tasks[0]
        agent_name = spec.get("agent", "")
        label = f"delegate:{agent_name}" if agent_name else "delegate"

        render_group_start(label, icon="🦀")
        render_push_depth()
        t0 = time.monotonic()
        result = None
        try:
            result = _run_single(
                task=spec.get("task", ""),
                context_mode=spec.get("context", "none"),
                allowed_tools=spec.get("tools"),
                agent_name=agent_name,
                stop_event=stop_event,
                **common_kwargs,
            )
            return result
        finally:
            render_pop_depth()
            render_group_end(
                label,
                success=result.success if result else False,
                duration_s=time.monotonic() - t0,
            )
    else:
        # Parallel: suppress during execution, collect and display after
        return _run_parallel(task_specs=tasks, stop_event=stop_event, **common_kwargs)
