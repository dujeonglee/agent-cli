"""Tool registry and execution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_cli.tools.result import ToolResult
from agent_cli.tools.read_file import tool_read_file
from agent_cli.tools.write_file import tool_write_file
from agent_cli.tools.edit_file import tool_edit_file
from agent_cli.tools.shell import tool_shell
from agent_cli.tools.context import tool_read_context
from agent_cli.tools.code_index import tool_code_index
from agent_cli.tools.registry import (
    TOOL_SCHEMAS,
    validate_tool_input,
    get_tool_descriptions,
)

from agent_cli.tools.fetch import tool_fetch

TOOLS: dict[str, Any] = {
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "edit_file": tool_edit_file,
    "shell": tool_shell,
    "read_context": tool_read_context,
    "code_index": tool_code_index,
    "delegate": lambda args: ToolResult(True, output="(delegate: intercepted by loop)"),
    # Virtual tools — intercepted by the loop before _execute_tool
    "complete": lambda args: ToolResult(
        True,
        output=args.get(
            "result",
            "(Completed without result — model may lack capability for this task)",
        ),
    ),
    "ask": lambda args: ToolResult(True, output=args.get("question", "(ask)")),
    "run_skill": lambda args: ToolResult(
        True, output="(run_skill: intercepted by loop)"
    ),
    "ready_for_review": lambda args: ToolResult(True, output=args.get("summary", "")),
    "fetch": tool_fetch,
}

__all__ = [
    "TOOLS",
    "TOOL_SCHEMAS",
    "ToolResult",
    "validate_tool_input",
    "get_tool_descriptions",
]


def _execute_tool(
    tool_name: str,
    action_input: dict,
    *,
    session_dir: Path | None = None,
) -> ToolResult:
    """Internal dispatch primitive — call a tool from the registry.

    Caller contract: ``tool_name`` MUST exist in ``TOOLS``. The loop's
    recovery layer (``detect_unknown_tool`` in ``_dispatch_text_path``)
    is the single source of truth for that validation; bad names never
    reach this function from the live loop. Direct callers (tests, future
    integrations) are responsible for the same guarantee — a ``KeyError``
    on a missing name is the intended failure mode.

    ``session_dir`` is forwarded to tools that need session context
    (currently only ``read_context``); other tools ignore it.
    """
    tool_fn = TOOLS[tool_name]
    if tool_name == "read_context":
        return tool_fn(action_input, session_dir=session_dir)
    return tool_fn(action_input)
