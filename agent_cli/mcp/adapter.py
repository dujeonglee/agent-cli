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

import hashlib
import re
from typing import Any

from agent_cli.mcp.client import McpClientManager
from agent_cli.tools.base import Tool
from agent_cli.tools.result import ToolResult

#: Every MCP tool name starts with this — a hook matcher or an
#: ``allowed-tools`` reader can tell an MCP tool from a built-in by name alone.
MCP_PREFIX = "mcp__"

#: Function-name limit of the OpenAI and Anthropic tool APIs.
_NAME_MAX = 64
_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


def mcp_tool_name(server: str, tool: str) -> str:
    """The name a model calls an MCP tool by: ``mcp__{server}__{tool}``.

    It has to be a legal function name for a server-parsed dialect
    (``^[A-Za-z0-9_-]{1,64}$``), so one name works in every dialect. MCP
    itself allows more (dots, up to 128 characters): other characters become
    ``_``, and a name over the limit keeps its head and ends in a hash of the
    full name so two long names stay distinct.
    """
    name = f"{MCP_PREFIX}{_NAME_UNSAFE.sub('_', server)}__{_NAME_UNSAFE.sub('_', tool)}"
    if len(name) <= _NAME_MAX:
        return name
    digest = hashlib.sha1(f"{server}\0{tool}".encode()).hexdigest()[:8]
    return f"{name[: _NAME_MAX - 9]}_{digest}"


class McpTool(Tool):
    """A connected MCP tool exposed as an agent-cli :class:`Tool`.

    ``name`` is ``mcp__{server}__{tool}`` (:func:`mcp_tool_name`) so it never
    collides with a native tool. ``parameters`` is the server-advertised JSON Schema, so
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
        self.name = mcp_tool_name(server, tool_name)
        self.description = description or "(no description)"
        self.parameters = parameters or {"type": "object", "properties": {}}
        self._manager = manager
        self._server = server
        self._tool_name = tool_name

    def wrap_single_op(self, flat: dict) -> dict:
        # MCP is prefix-less (bare schema keys), so a multi-op flat op IS the
        # canonical input — identity, like the flat-native builtin tools.
        #
        # This override predates the base default being flipped to identity
        # (v9.12.0): back then the default was ``add_prefix``, which namespaced
        # the bare keys (``{query}`` → ``{srv.tool_query}``) and made the
        # prefixed schema-less input fail validate — MCP hit the same trap that
        # killed ``monitor`` and worked around it here. The default no longer
        # bites, so this is now redundant; kept as an explicit statement of the
        # shape (a prefix-less tool's flat op needs no re-wrap), and pinned by
        # tests/test_tool_wire_contract.py either way.
        return flat

    def _run(self, args: dict, *, ctx=None) -> ToolResult:
        # ctx (RunContext) is accepted for the uniform Tool.run signature; MCP
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

    Returns dict of ``{"mcp__{server}__{tool}": McpTool}`` ready to merge into
    ``TOOLS``. Values are ``Tool`` subclasses (not bare callables) so they
    satisfy the registry's ``.parameters`` / ``.run()`` contract.
    """
    tools: dict[str, Tool] = {}
    for tool_info in manager.list_tools():
        tool = McpTool(
            manager,
            tool_info.server,
            tool_info.name,
            tool_info.description,
            tool_info.input_schema,
        )
        tools[tool.name] = tool
    return tools
