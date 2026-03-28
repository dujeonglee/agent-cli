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
from agent_cli.tools.truncation import (
    truncate_output,
    get_truncation_config,
    TruncationConfig,
)

from agent_cli.tools.read_artifact import tool_read_artifact
from agent_cli.tools.run_skill import tool_run_skill

TOOLS: dict[str, Any] = {
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "edit_file": tool_edit_file,
    "shell": tool_shell,
    "read_context": tool_read_context,
    # Virtual tools — intercepted by the loop before execute_tool
    "complete": lambda args: ToolResult(True, output=args.get("result", "(completed)")),
    "ask": lambda args: ToolResult(True, output=args.get("question", "(ask)")),
    "run_skill": tool_run_skill,
    "read_artifact": tool_read_artifact,
}

# Virtual tool names — used to exclude them where only real tools matter (e.g. planning)
VIRTUAL_TOOLS: frozenset[str] = frozenset(
    {"complete", "ask", "run_skill", "read_artifact"}
)

__all__ = [
    "TOOLS",
    "VIRTUAL_TOOLS",
    "TOOL_SCHEMAS",
    "ToolResult",
    "validate_tool_input",
    "get_tool_descriptions",
    "truncate_output",
    "get_truncation_config",
    "TruncationConfig",
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
