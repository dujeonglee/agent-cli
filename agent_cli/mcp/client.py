"""MCP client manager — connects to MCP servers and executes tools.

Uses the mcp Python SDK for stdio and SSE transports.
Provides sync wrappers around async MCP client operations.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from typing import Any

from agent_cli.mcp.config import McpServerConfig


@dataclass
class McpToolInfo:
    """Metadata for an MCP tool."""

    server: str
    name: str
    description: str
    input_schema: dict


@dataclass
class McpResourceInfo:
    """Metadata for an MCP resource."""

    server: str
    uri: str
    name: str
    description: str


class McpClientManager:
    """Manages connections to MCP servers.

    Each server gets its own client session. Tools are accessed via
    {server_name}.{tool_name} namespace.
    """

    def __init__(self):
        self._clients: dict[str, Any] = {}  # server_name → (session, cleanup)
        self._tools: dict[str, list[McpToolInfo]] = {}  # server_name → tools
        self._loop: asyncio.AbstractEventLoop | None = None

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        """Get or create event loop for sync wrappers."""
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
        return self._loop

    def _run_sync(self, coro):
        """Run async coroutine synchronously."""
        loop = self._get_loop()
        return loop.run_until_complete(coro)

    # ── Connection management ────────────────────────

    def connect_all(self, configs: dict[str, McpServerConfig]) -> dict[str, str]:
        """Connect to all configured servers.

        Returns dict of {server_name: status} where status is
        "connected" or error message.
        """
        results = {}
        for name, config in configs.items():
            try:
                self._run_sync(self._connect_one(name, config))
                results[name] = "connected"
            except Exception as e:
                results[name] = f"error: {e}"
                print(
                    f"[warn] MCP server '{name}' connection failed: {e}",
                    file=sys.stderr,
                )
        return results

    async def _connect_one(self, name: str, config: McpServerConfig) -> None:
        """Connect to a single MCP server."""
        if config.is_stdio:
            await self._connect_stdio(name, config)
        elif config.is_sse:
            await self._connect_sse(name, config)
        else:
            raise ValueError(
                f"Invalid config for '{name}': need 'command' (stdio) or 'url' (sse)"
            )

    async def _connect_stdio(self, name: str, config: McpServerConfig) -> None:
        """Connect via stdio transport."""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=config.command,
            args=config.args,
            env={**dict(__import__("os").environ), **config.env}
            if config.env
            else None,
        )

        # Create context managers and enter them
        transport_cm = stdio_client(params)
        read, write = await transport_cm.__aenter__()

        session = ClientSession(read, write)
        await session.__aenter__()
        await session.initialize()

        # Store session and cleanup info
        self._clients[name] = {
            "session": session,
            "transport_cm": transport_cm,
            "session_cm": session,
        }

        # Fetch tool list
        tools_result = await session.list_tools()
        self._tools[name] = [
            McpToolInfo(
                server=name,
                name=t.name,
                description=t.description or "",
                input_schema=t.inputSchema if hasattr(t, "inputSchema") else {},
            )
            for t in tools_result.tools
        ]

    async def _connect_sse(self, name: str, config: McpServerConfig) -> None:
        """Connect via SSE transport."""
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        transport_cm = sse_client(config.url)
        read, write = await transport_cm.__aenter__()

        session = ClientSession(read, write)
        await session.__aenter__()
        await session.initialize()

        self._clients[name] = {
            "session": session,
            "transport_cm": transport_cm,
            "session_cm": session,
        }

        tools_result = await session.list_tools()
        self._tools[name] = [
            McpToolInfo(
                server=name,
                name=t.name,
                description=t.description or "",
                input_schema=t.inputSchema if hasattr(t, "inputSchema") else {},
            )
            for t in tools_result.tools
        ]

    def disconnect_all(self) -> None:
        """Disconnect from all servers."""
        for name in list(self._clients.keys()):
            self.disconnect(name)
        if self._loop and not self._loop.is_closed():
            self._loop.close()
            self._loop = None

    def disconnect(self, name: str) -> None:
        """Disconnect from a specific server."""
        client = self._clients.pop(name, None)
        if client is None:
            return
        self._tools.pop(name, None)
        try:
            loop = self._get_loop()
            session = client["session"]
            transport_cm = client["transport_cm"]
            loop.run_until_complete(session.__aexit__(None, None, None))
            loop.run_until_complete(transport_cm.__aexit__(None, None, None))
        except Exception:
            pass

    # ── Tool operations ──────────────────────────────

    def list_tools(self, server: str | None = None) -> list[McpToolInfo]:
        """List tools for a specific server or all servers."""
        if server:
            return self._tools.get(server, [])
        all_tools = []
        for tools in self._tools.values():
            all_tools.extend(tools)
        return all_tools

    def call_tool(self, server: str, tool_name: str, arguments: dict) -> Any:
        """Call an MCP tool synchronously. Returns the tool result."""
        return self._run_sync(self._call_tool_async(server, tool_name, arguments))

    async def _call_tool_async(
        self, server: str, tool_name: str, arguments: dict
    ) -> Any:
        """Call an MCP tool asynchronously."""
        client = self._clients.get(server)
        if client is None:
            raise ConnectionError(f"MCP server '{server}' not connected")

        session = client["session"]
        result = await session.call_tool(tool_name, arguments)
        return result

    # ── Resource operations ──────────────────────────

    def list_resources(self, server: str) -> list[McpResourceInfo]:
        """List resources for a specific server."""
        client = self._clients.get(server)
        if client is None:
            return []
        try:
            result = self._run_sync(client["session"].list_resources())
            return [
                McpResourceInfo(
                    server=server,
                    uri=str(r.uri),
                    name=r.name or "",
                    description=r.description or "",
                )
                for r in result.resources
            ]
        except Exception:
            return []

    def read_resource(self, server: str, uri: str) -> str:
        """Read a resource by URI."""
        return self._run_sync(self._read_resource_async(server, uri))

    async def _read_resource_async(self, server: str, uri: str) -> str:
        """Read a resource asynchronously."""
        client = self._clients.get(server)
        if client is None:
            raise ConnectionError(f"MCP server '{server}' not connected")

        session = client["session"]
        result = await session.read_resource(uri)
        # Extract text content
        if result.contents:
            return result.contents[0].text or ""
        return ""

    # ── Status ───────────────────────────────────────

    @property
    def connected_servers(self) -> list[str]:
        """List of currently connected server names."""
        return list(self._clients.keys())

    def is_connected(self, server: str) -> bool:
        return server in self._clients
