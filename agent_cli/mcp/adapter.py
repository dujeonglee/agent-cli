"""MCP tool adapter — wraps MCP tools as :class:`~agent_cli.tools.base.Tool`
instances.

Registers MCP tools into the ``TOOLS`` dict so they appear in Available
Tools and are dispatched by the agent loop like any built-in tool. Since
the Tool-ABC refactor (423608e) the registry expects every ``TOOLS`` value
to be a ``Tool`` subclass — it reads ``.parameters`` for input validation
(``validate_tool_input``) and calls ``.run()`` for dispatch
(``_execute_tool``). MCP tools are therefore ``Tool`` subclasses too, not
bare callables, so they flow through the exact same validation/dispatch
path with no special-casing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_cli.mcp.client import McpClientManager
from agent_cli.tools.base import Tool
from agent_cli.tools.registry import render_param_value
from agent_cli.tools.result import ToolResult


class McpTool(Tool):
    """A connected MCP tool exposed as an agent-cli :class:`Tool`.

    ``name`` is the qualified ``{server}.{tool}`` so it never collides with
    a native tool. ``parameters`` is the server-advertised JSON Schema, so
    the registry validates MCP input the same way it validates native
    tools. ``_run`` forwards the (prefix-stripped) args to the MCP server.

    Wire keys: MCP is prefix-less. Servers advertise bare schema keys
    (``query``), so the model emits them bare — the same shape virtual
    tools (``complete`` / ``ask``) use. The base ``key_prefix`` (``{name}_``)
    is therefore a no-op here: bare keys don't carry it, so ``strip_prefix``
    passes them through unchanged and ``claims`` stays False (MCP never
    participates in ``infer_action`` dropped-name recovery). No prefix is
    added or expected — same mechanism as virtual tools.
    """

    def __init__(
        self,
        manager: McpClientManager,
        server: str,
        tool_name: str,
        description: str,
        parameters: dict,
    ) -> None:
        self.name = f"{server}.{tool_name}"
        self.description = description or "(no description)"
        self.parameters = parameters or {"type": "object", "properties": {}}
        self._manager = manager
        self._server = server
        self._tool_name = tool_name

    def _run(self, args: dict, *, session_dir: Path | None = None) -> ToolResult:
        # session_dir is accepted for the uniform Tool.run signature; MCP
        # dispatch is location-independent and ignores it.
        try:
            result = self._manager.call_tool(self._server, self._tool_name, args)
            return ToolResult(True, output=_extract_mcp_result(result))
        except Exception as e:
            return ToolResult(False, error=f"MCP {self.name} failed: {e}")


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
) -> dict[str, Tool]:
    """Register all connected MCP tools as :class:`McpTool` instances.

    Returns dict of ``{"{server}.{tool}": McpTool}`` ready to merge into
    ``TOOLS``. Values are ``Tool`` subclasses (not bare callables) so they
    satisfy the registry's ``.parameters`` / ``.run()`` contract.
    """
    tools: dict[str, Tool] = {}
    for tool_info in manager.list_tools():
        qualified_name = f"{tool_info.server}.{tool_info.name}"
        tools[qualified_name] = McpTool(
            manager,
            tool_info.server,
            tool_info.name,
            tool_info.description,
            tool_info.input_schema,
        )
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
        # Build params summary from input_schema. Same renderer as native
        # tools (registry.render_param_value) so both surfaces show type +
        # required + nested item shape identically. MCP keys are not
        # prefixed — kept verbatim.
        params = ""
        if tool.input_schema and "properties" in tool.input_schema:
            props = tool.input_schema["properties"]
            required = set(tool.input_schema.get("required", []))
            params_str = json.dumps(
                {k: render_param_value(v, k in required) for k, v in props.items()},
                ensure_ascii=False,
            )
            params = f"  Input JSON: {params_str}"
        entry = f"- {qualified_name}: {desc}"
        if params:
            entry += f"\n{params}"
        lines.append(entry)

    return "\n".join(lines)
