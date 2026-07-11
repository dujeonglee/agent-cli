"""In-process subagent delegation tool.

Supports context modes: none (independent), fork (copy conversation history).
Uses tasks array API: single item = sync, multiple items = parallel (threading).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from agent_cli.tools.base import (
    Tool,
    default_oversized_nudge,
    on_disk_oversized_nudge,
)
from agent_cli.tools.result import ToolResult

if TYPE_CHECKING:
    # Runtime-imported inside the dispatch path to avoid a
    # registry → delegate → context.manager → tools.registry →
    # tools import cycle (registry now imports DelegateTool at module load).
    pass

# ── Agent file loading ──────────────────────────


class DelegateTool(Tool):
    name = "delegate"
    description = (
        "Delegate ONE task to a subagent. Emit several delegate ops in the "
        "same turn to run independent tasks in PARALLEL. "
        "Use context mode to control what the subagent knows."
    )
    # Flat-native (consolidation roadmap Step 3): the schema is the plain
    # single-task shape — no `delegate_tasks` batch array and no `delegate_`
    # wire-key prefix. One op = one subagent task; several delegate ops in a
    # turn run concurrently (``parallel_safe=True`` → the loop batches a run of
    # delegate ops and drives ``_run_parallel``). `wrap_single_op` is identity;
    # `key_prefix` is left at its default (latent — strip is a no-op on flat
    # keys, `claims` is False for a flat `{task}`).
    parallel_safe = True
    parameters = {
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
    }

    def touched_paths(self, action_input: dict) -> list[str]:
        # delegate has no file paths — record a ``<delegate:agent>`` marker so
        # the compaction file-list still reflects "a subagent was spawned"
        # (the subagent's own files live in a different session).
        agent = self.strip_prefix(action_input).get("agent")
        return [f"<delegate:{agent}>"] if isinstance(agent, str) and agent else []

    def summary_arg(self, action_input: dict) -> str:
        return self.strip_prefix(action_input).get("agent", "")

    def wrap_single_op(self, flat: dict) -> dict:
        return flat

    def render_oversized(self, result, args, *, body, tokens, ctx) -> str:
        """Over-cap policy for a large subagent answer: ``tool_delegate`` already
        persisted the full formatted answer to ``<delegate_dir>/result.md`` (the
        relative dir is ``result.artifact``), so point at that file and reuse the
        shared on-disk nudge — (a) read a specific part / search ``result.md``,
        (b) delegate fan-out over its sections — plus a re-delegate-narrower
        option that attacks the root cause (the task was too broad). If the
        artifact / session dir is missing (e.g. the parallel path, or headless)
        fall back to the generic nudge."""
        artifact = getattr(result, "artifact", "") or ""
        if ctx.session_dir and artifact:
            path = str(Path(ctx.session_dir) / artifact / "result.md")
            return on_disk_oversized_nudge(
                "delegate",
                "subagent answer",
                f"full answer saved to '{path}'",
                path,
                tokens,
                ctx.oversized_cap,
                ctx.tools_available,
                nlines=body.count("\n") + 1,
                tail_bullets=(
                    "Or re-delegate a NARROWER task so the subagent returns a "
                    "focused result (attack the root cause: the task was too "
                    "broad).",
                ),
            )
        return default_oversized_nudge("delegate", tokens, ctx.oversized_cap)

    def _run(self, args: dict, *, ctx=None) -> ToolResult:
        # delegate is intercepted by the loop (it needs parent context,
        # provider, capabilities) before execute_tool — this placeholder
        # only fires for direct/test callers.
        return ToolResult(True, output="(delegate: intercepted by loop)")
