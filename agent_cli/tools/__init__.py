"""Tool registry and execution."""

from __future__ import annotations

from typing import Any

from agent_cli.tools.read_file import tool_read_file
from agent_cli.tools.write_file import tool_write_file
from agent_cli.tools.edit_file import tool_edit_file
from agent_cli.tools.shell import tool_shell
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

TOOLS: dict[str, Any] = {
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "edit_file": tool_edit_file,
    "shell": tool_shell,
}

__all__ = [
    "TOOLS",
    "TOOL_SCHEMAS",
    "validate_tool_input",
    "get_tool_descriptions",
    "truncate_output",
    "get_truncation_config",
    "TruncationConfig",
    "execute_tool",
]


def execute_tool(tool_name: str, action_input: dict) -> str:
    """Execute a tool by name and return the raw output."""
    tool_fn = TOOLS.get(tool_name)
    if tool_fn is None:
        raise RuntimeError(
            f"Unknown tool: '{tool_name}'. Available: {', '.join(TOOLS)}"
        )
    return tool_fn(action_input)
