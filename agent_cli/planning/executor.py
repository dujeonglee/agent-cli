"""Plan execution: Phase 3 of Planning Mode."""

from __future__ import annotations

import re

from agent_cli.loop import run_loop
from agent_cli.planning.models import Plan
from agent_cli.providers.base import LLMProvider
from agent_cli.providers.compat import ModelCapabilities
from agent_cli.render import console, C, render_plan_progress, render_status
from agent_cli.tools import TOOLS, VIRTUAL_TOOLS

# Tool RAG: keyword → tool mapping (includes Korean for multilingual support)
_TOOL_KEYWORDS = {
    "read_file": re.compile(r"\b(read|읽|view|show|check|inspect|examine)\b", re.I),
    "write_file": re.compile(r"\b(write|create|생성|작성|new file|save to)\b", re.I),
    "edit_file": re.compile(
        r"\b(edit|modify|수정|변경|update|change|replace|refactor|fix)\b", re.I
    ),
    "shell": re.compile(
        r"\b(run|execute|실행|테스트|test|install|build|command|pip|npm)\b", re.I
    ),
}


def _infer_tools_for_step(description: str) -> list[str]:
    """Infer which tools a step needs from its description."""
    tools = set()
    for tool_name, pattern in _TOOL_KEYWORDS.items():
        if pattern.search(description):
            tools.add(tool_name)

    # edit requires read (need hashlines first)
    if "edit_file" in tools:
        tools.add("read_file")

    # Fallback: no match → all tools
    if not tools:
        return [t for t in TOOLS if t not in VIRTUAL_TOOLS]

    return sorted(tools)


def execute_plan(
    plan: Plan,
    provider: LLMProvider,
    capabilities: ModelCapabilities,
    model: str,
    provider_name: str = "ollama",
    base_url: str = "",
    api_key: str = "",
    max_iter: int = 0,
    step_max_iter: int = 10,
    verbose: bool = False,
    quiet: bool = False,
    max_depth: int = 2,
    delegate_timeout: int = 300,
    save_path: str | None = None,
) -> str | None:
    """Execute a plan step by step, returning summary of results."""
    total = len(plan.steps)
    effective_max_iter = max_iter if max_iter > 0 else step_max_iter

    # While loop (not for loop) to support retry without skipping
    idx = 0
    while idx < len(plan.steps):
        step = plan.steps[idx]

        if step.status in ("done", "skipped"):
            idx += 1
            continue

        step.status = "in_progress"
        plan.current_step = idx

        if not quiet:
            console.print()
            render_status("running", f"Step {step.id}/{total}: {step.description}")

        step_context = _build_step_context(plan, idx, capabilities)
        active_tools = _infer_tools_for_step(step.description)

        result = run_loop(
            query=step.description,
            provider=provider,
            capabilities=capabilities,
            model=model,
            provider_name=provider_name,
            base_url=base_url,
            api_key=api_key,
            max_iter=effective_max_iter,
            verbose=verbose,
            quiet=quiet,
            max_depth=max_depth,
            delegate_timeout=delegate_timeout,
            plan_context=step_context,
            active_tools=active_tools,
        )

        if result:
            step.status = "done"
            step.result = result
        else:
            step.status = "failed"
            step.result = "Step execution failed or reached max iterations."

            if not quiet:
                action = _handle_failure(plan, step)
                if action == "abort":
                    _save_if_needed(plan, save_path)
                    return None
                elif action == "skip":
                    step.status = "skipped"
                elif action == "retry":
                    step.status = "pending"
                    _save_if_needed(plan, save_path)
                    continue  # while loop — same idx, retry

        if not quiet:
            render_plan_progress(plan)

        _save_if_needed(plan, save_path)
        idx += 1

    return _summarize_results(plan)


def _save_if_needed(plan: Plan, save_path: str | None) -> None:
    """Save plan progress if save_path is set."""
    if save_path:
        plan.save(save_path)


def _build_step_context(
    plan: Plan, current_idx: int, capabilities: ModelCapabilities | None = None
) -> str:
    """Build context string injected into system prompt for step execution."""
    # Step result summary length: proportional to context window (1% per step)
    if capabilities:
        max_summary = max(100, min(2000, capabilities.context_window // 100))
    else:
        max_summary = 100

    parts = [f'CONTEXT: You are executing a plan to: "{plan.goal}"']

    completed = []
    for step in plan.steps[:current_idx]:
        if step.status == "done":
            summary = (step.result or "completed")[:max_summary].replace("\n", " ")
            completed.append(f"  {step.id}. [✓] {step.description} — {summary}")
        elif step.status == "skipped":
            completed.append(f"  {step.id}. [~] {step.description} — skipped")

    if completed:
        parts.append("Previous steps completed:")
        parts.extend(completed)

    current_step = plan.steps[current_idx]
    total = len(plan.steps)
    parts.append(
        f"\nCurrent step ({current_step.id} of {total}): {current_step.description}"
    )
    parts.append("\nExecute this step now. Use tools as needed.")

    return "\n".join(parts)


def _handle_failure(plan: Plan, step) -> str:
    """Prompt user for failure handling. Returns: 'retry' | 'skip' | 'abort'."""
    console.print(f"\n[{C['error']}]Step {step.id} failed:[/] {step.description}")
    if step.result:
        console.print(f"[{C['muted']}]{step.result[:200]}[/]")

    while True:
        try:
            choice = (
                console.input(f"  [{C['accent']}][R]etry / [S]kip / [A]bort:[/] ")
                .strip()
                .lower()
            )
        except (EOFError, KeyboardInterrupt):
            return "abort"

        if choice in ("r", "retry"):
            return "retry"
        elif choice in ("s", "skip"):
            return "skip"
        elif choice in ("a", "abort"):
            return "abort"
        else:
            console.print(f"[{C['muted']}]Invalid choice. Try R/S/A.[/]")


def _summarize_results(plan: Plan) -> str:
    """Summarize plan execution results."""
    parts = [f"Plan completed: {plan.goal}\n"]
    for step in plan.steps:
        icon = {"done": "✓", "failed": "✗", "skipped": "~"}.get(step.status, "?")
        parts.append(f"  {step.id}. [{icon}] {step.description}")
        if step.result and step.status == "done":
            summary = step.result[:100].replace("\n", " ")
            parts.append(f"      → {summary}")
    return "\n".join(parts)
