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
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from agent_cli.constants import DELEGATE_DEFAULT_TIMEOUT
from agent_cli.providers.base import LLMProvider
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.resource_loader import ResourceLoader
from agent_cli.tools.base import Tool
from agent_cli.tools.result import ToolResult

if TYPE_CHECKING:
    # Runtime-imported inside the dispatch path to avoid a
    # registry → delegate → context.manager → tools.action_summary →
    # tools import cycle (registry now imports DelegateTool at module load).
    from agent_cli.context.manager import ContextManager

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


# Caps for the per-iteration summaries handed back to the parent
# loop in ``DelegateResult``. The parent splices these into its
# observation card, so they're token cost in the parent's context —
# a runaway sub-agent (50+ iterations) would otherwise drown out
# the actual answer. Module-level so the values are discoverable
# without spelunking through the extractor body.
_ACTIVITY_LOG_MAX_ENTRIES = 20
_LAST_ACTIONS_KEEP = 5


def _extract_activity_log(messages: list[dict]) -> list[str]:
    """Extract per-iteration action summaries from context messages.

    Parses assistant messages for ReAct JSON (action/action_input),
    formats each into a one-line summary. Capped at
    ``_ACTIVITY_LOG_MAX_ENTRIES``; overflow surfaces as a single
    ``... and N more`` footer so the parent knows the log was trimmed.

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

    if len(log) > _ACTIVITY_LOG_MAX_ENTRIES:
        trimmed = log[:_ACTIVITY_LOG_MAX_ENTRIES]
        trimmed.append(f"... and {len(log) - _ACTIVITY_LOG_MAX_ENTRIES} more")
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


def _extract_last_actions(messages: list[dict]) -> list[str]:
    """Extract the last ``_LAST_ACTIONS_KEEP`` actions with their
    observation results.

    Distinct from ``_extract_activity_log`` in two ways: (1) it's
    intentionally a tail-slice, not a head-with-overflow, since the
    parent uses these lines to spot the most recent failures, and
    (2) it scrapes the immediately-following user-role message for
    error keywords (``ERROR``/``FAIL``/``EXCEPTION``/``TRACEBACK``)
    and appends a one-line hint when found.

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

    last_n = actions[-_LAST_ACTIONS_KEEP:]

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
    hooks_config: dict | None = None,
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

        # Agent-local shell hooks — merged on top of the caller's
        # hooks_config so parent matchers still apply. Mirrors Skill.hooks
        # semantics: the overlay only applies while this agent is running.
        raw_agent_hooks = agent_config.get("hooks")
        if isinstance(raw_agent_hooks, dict):
            from agent_cli.hooks import merge_hooks_configs, parse_hooks_config

            agent_hooks = parse_hooks_config(raw_agent_hooks) or None
            hooks_config = merge_hooks_configs(hooks_config, agent_hooks)

    # Resolve parent session dir and create delegate subdir
    parent_session_dir = _resolve_session_dir(session, parent_ctx)
    delegate_dir_name = _generate_delegate_dir_name(agent_name or "task")
    delegate_dir = parent_session_dir / delegate_dir_name

    # Create context based on mode
    # Inherit parent's wire_format so delegate ctx renders history with
    # the same plugin the parent uses. Falls back to ContextManager's
    # own default (react) when there's no parent.
    inherited_wire_format = parent_ctx.wire_format if parent_ctx else None

    from agent_cli.context.manager import ContextManager

    if context_mode == "fork":
        if parent_ctx is None:
            return ToolResult(
                False, error="Delegation rejected: fork requires parent context"
            )
        # Fork: copy parent history.jsonl to delegate dir
        parent_ctx.fork_history_to(delegate_dir)
        budget = parent_ctx.max_context_tokens
        ctx = ContextManager(
            session_dir=delegate_dir,
            max_context_tokens=budget,
            resume=True,
            wire_format=inherited_wire_format,
        )
    else:
        # none: fresh context (inherit parent budget if available)
        budget = parent_ctx.max_context_tokens if parent_ctx else 0
        ctx = ContextManager(
            session_dir=delegate_dir,
            max_context_tokens=budget,
            wire_format=inherited_wire_format,
        )

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
        hooks_config=hooks_config,
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
    hooks_config: dict | None = None,
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
        renderer.begin_delegate_task(
            task_id=task_id,
            index=index,
            agent=agent,
            task_text=task_text,
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
            )
            result_for_marker = results[index]
        finally:
            durations[index] = time.monotonic() - t0
            success = bool(result_for_marker and result_for_marker.success)
            if result_for_marker and not result_for_marker.success:
                error_msg = result_for_marker.error or ""
            renderer.end_delegate_task(
                task_id=task_id,
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
    timeout: int = DELEGATE_DEFAULT_TIMEOUT,
    session=None,
    skill_stack: list[str] | None = None,
    agent_stack: list[str] | None = None,
    stop_event=None,
    hooks_config: dict | None = None,
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
        hooks_config=hooks_config,
    )

    if len(tasks) == 1:
        # Single delegate: grouped nested rendering
        from agent_cli.render import (
            get_renderer,
            render_group_start,
            render_group_end,
            render_push_depth,
            render_pop_depth,
        )

        spec = tasks[0]
        agent_name = spec.get("agent", "")
        label = f"delegate:{agent_name}" if agent_name else "delegate"

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
        renderer.begin_delegate_task(
            task_id=task_id,
            index=0,
            agent=agent_name,
            task_text=spec.get("task", ""),
        )
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
            duration = time.monotonic() - t0
            render_pop_depth()
            render_group_end(
                label,
                success=result.success if result else False,
                duration_s=duration,
            )
            success = bool(result and result.success)
            error_msg = ""
            if result and not result.success:
                error_msg = result.error or ""
            renderer.end_delegate_task(
                task_id=task_id,
                success=success,
                duration_s=duration,
                error=error_msg,
            )
    else:
        # Parallel: suppress during execution, collect and display after
        return _run_parallel(task_specs=tasks, stop_event=stop_event, **common_kwargs)


class DelegateTool(Tool):
    name = "delegate"
    description = (
        "Delegate tasks to subagents. "
        "Single task = sync, multiple tasks = parallel. "
        "Use context mode to control what the subagent knows."
    )
    parameters = {
        "type": "object",
        "properties": {
            "delegate_tasks": {
                "type": "array",
                "description": "List of tasks. Single item = sync, multiple = parallel.",
                "items": {
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": "Task description for the subagent",
                        },
                        "context": {
                            "type": "string",
                            "enum": ["none", "fork"],
                            "description": "none (independent), fork (copy conversation history)",
                        },
                        "tools": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Allowed tools (omit for default set)",
                        },
                        "agent": {
                            "type": "string",
                            "description": "Agent name to load role/config from .agent-cli/agents/{name}.md",
                        },
                    },
                    "required": ["task"],
                },
            },
        },
        "required": ["delegate_tasks"],
    }

    def _run(self, args: dict, *, session_dir=None) -> ToolResult:
        # delegate is intercepted by the loop (it needs parent context,
        # provider, capabilities) before execute_tool — this placeholder
        # only fires for direct/test callers.
        return ToolResult(True, output="(delegate: intercepted by loop)")
