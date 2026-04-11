"""MCP tool adapter — wraps MCP tools as agent-cli ToolResult functions.

Registers MCP tools into TOOLS dict so they appear in Available Tools
and can be executed by the agent loop like any built-in tool.
"""

from __future__ import annotations

from typing import Any

from agent_cli.mcp.client import McpClientManager
from agent_cli.tools.result import ToolResult


def wrap_mcp_tool(manager: McpClientManager, server: str, tool_name: str) -> callable:
    """Create a tool function that calls an MCP tool and returns ToolResult.

    The returned function has signature: (args: dict) -> ToolResult
    Compatible with agent-cli's TOOLS dict.
    """

    def _tool_fn(args: dict) -> ToolResult:
        try:
            result = manager.call_tool(server, tool_name, args)
            # Extract text from MCP result
            output = _extract_mcp_result(result)
            return ToolResult(True, output=output)
        except Exception as e:
            return ToolResult(False, error=f"MCP {server}.{tool_name} failed: {e}")

    return _tool_fn


def _extract_mcp_result(result: Any) -> str:
    """Extract text output from MCP tool result."""
    if result is None:
        return "(no output)"

    # MCP SDK returns CallToolResult with content list
    if hasattr(result, "content"):
        parts = []
        for item in result.content:
            if hasattr(item, "text"):
                parts.append(item.text)
            elif hasattr(item, "data"):
                parts.append(str(item.data))
            else:
                parts.append(str(item))
        return "\n".join(parts) if parts else "(no output)"

    return str(result)


def register_mcp_tools(
    manager: McpClientManager,
) -> dict[str, callable]:
    """Register all connected MCP tools as agent-cli tool functions.

    Returns dict of {"{server}.{tool}": function} ready to merge into TOOLS.
    """
    tools = {}
    for tool_info in manager.list_tools():
        qualified_name = f"{tool_info.server}.{tool_info.name}"
        tools[qualified_name] = wrap_mcp_tool(manager, tool_info.server, tool_info.name)
    return tools


def build_mcp_tool_descriptions(manager: McpClientManager) -> str:
    """Build tool description text for MCP tools (for system prompt).

    Returns formatted string compatible with get_tool_descriptions output.
    """
    all_tools = manager.list_tools()
    if not all_tools:
        return ""

    lines = []
    for tool in all_tools:
        qualified_name = f"{tool.server}.{tool.name}"
        desc = tool.description or "(no description)"
        # Build params summary from input_schema
        params = ""
        if tool.input_schema and "properties" in tool.input_schema:
            props = tool.input_schema["properties"]
            params_str = ", ".join(
                f'"{k}": "{v.get("description", v.get("type", ""))}"'
                for k, v in props.items()
            )
            params = f"  Input JSON: {{{params_str}}}"
        entry = f"- {qualified_name}: {desc}"
        if params:
            entry += f"\n{params}"
        lines.append(entry)

    return "\n".join(lines)
