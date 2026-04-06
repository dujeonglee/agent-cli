"""Tool registry and execution."""

from __future__ import annotations

from typing import Any

from agent_cli.tools.result import ToolResult
from agent_cli.tools.read_file import tool_read_file
from agent_cli.tools.write_file import tool_write_file
from agent_cli.tools.edit_file import tool_edit_file
from agent_cli.tools.shell import tool_shell
from agent_cli.tools.context import tool_read_context
from agent_cli.tools.registry import (
    TOOL_SCHEMAS,
    validate_tool_input,
    get_tool_descriptions,
)

from agent_cli.tools.run_skill import tool_run_skill
from agent_cli.tools.fetch import tool_fetch

TOOLS: dict[str, Any] = {
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "edit_file": tool_edit_file,
    "shell": tool_shell,
    "read_context": tool_read_context,
    "delegate": lambda args: ToolResult(True, output="(delegate: intercepted by loop)"),
    # Virtual tools — intercepted by the loop before execute_tool
    "complete": lambda args: ToolResult(
        True,
        output=args.get(
            "result",
            "(Completed without result — model may lack capability for this task)",
        ),
    ),
    "ask": lambda args: ToolResult(True, output=args.get("question", "(ask)")),
    "run_skill": tool_run_skill,
    "ready_for_review": lambda args: ToolResult(True, output=args.get("summary", "")),
    "fetch": tool_fetch,
}

# Virtual tool names — intercepted by loop, excluded from tool descriptions
VIRTUAL_TOOLS: frozenset[str] = frozenset(
    {"complete", "ask", "run_skill", "ready_for_review", "delegate"}
)

__all__ = [
    "TOOLS",
    "VIRTUAL_TOOLS",
    "TOOL_SCHEMAS",
    "ToolResult",
    "validate_tool_input",
    "get_tool_descriptions",
    "execute_tool",
]


def execute_tool(tool_name: str, action_input: dict) -> ToolResult:
    """Execute a tool by name and return a ToolResult."""
    tool_fn = TOOLS.get(tool_name)
    if tool_fn is None:
        return ToolResult(
            False,
            error=f"Unknown tool: '{tool_name}'. Available: {', '.join(TOOLS)}",
        )
    return tool_fn(action_input)
