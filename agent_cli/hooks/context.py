"""HookContext — the object passed to every Python hook function."""

from __future__ import annotations

from pathlib import Path
from typing import Any


class HookContext:
    """Context object provided to hook functions.

    Gives hooks access to current state and methods to modify it.
    All MCP memory methods are no-ops if no MCP manager is available.
    """

    def __init__(
        self,
        event: str,
        messages: list[dict] | None = None,
        session_dir: Path | None = None,
        turn: int = 0,
        tool_name: str | None = None,
        tool_input: dict | None = None,
        tool_result: Any = None,
        llm_response: str | None = None,
        delegate_result: Any = None,
        skill_result: Any = None,
        mcp_manager: Any = None,
    ):
        self.event = event
        self.messages = messages if messages is not None else []
        self.session_dir = session_dir
        self.turn = turn
        self.tool_name = tool_name
        self.tool_input = tool_input
        self.tool_result = tool_result
        self.llm_response = llm_response
        self.delegate_result = delegate_result
        self.skill_result = skill_result
        self._mcp_manager = mcp_manager

        # Dynamic system prompt sections (injected by hooks)
        self._system_sections: dict[str, str] = {}

        # PreToolUse control
        self._blocked = False
        self._block_reason = ""
        self._modified_input: dict | None = None

    @property
    def history_path(self) -> Path | None:
        if self.session_dir:
            return self.session_dir / "history.jsonl"
        return None

    # ── Context manipulation ─────────────────────────

    def inject_message(self, role: str, content: str) -> None:
        """Add a message to the current messages list."""
        self.messages.append({"role": role, "content": content})

    def inject_system_section(self, title: str, content: str) -> None:
        """Add or replace a dynamic section in the system prompt."""
        self._system_sections[title] = content

    def remove_system_section(self, title: str) -> None:
        """Remove a dynamic section from the system prompt."""
        self._system_sections.pop(title, None)

    @property
    def system_sections(self) -> dict[str, str]:
        """Dynamic system prompt sections added by hooks."""
        return dict(self._system_sections)

    # ── PreToolUse control ───────────────────────────

    def block(self, reason: str = "") -> None:
        """Block tool execution (PreToolUse only)."""
        self._blocked = True
        self._block_reason = reason

    def modify_input(self, new_input: dict) -> None:
        """Modify tool input (PreToolUse only)."""
        self._modified_input = new_input

    @property
    def is_blocked(self) -> bool:
        return self._blocked

    @property
    def block_reason(self) -> str:
        return self._block_reason

    @property
    def modified_input(self) -> dict | None:
        return self._modified_input

    # ── Memory methods (MCP wrapper) ─────────────────

    def store_memory(self, entities: list[dict]) -> None:
        """Store entities in MCP memory. No-op if no MCP manager."""
        if not self._mcp_manager or not self._mcp_manager.is_connected("memory"):
            return
        try:
            self._mcp_manager.call_tool(
                "memory", "create_entities", {"entities": entities}
            )
        except Exception:
            pass

    def search_memory(self, query: str) -> list[dict]:
        """Search MCP memory. Returns empty list if unavailable."""
        if not self._mcp_manager or not self._mcp_manager.is_connected("memory"):
            return []
        try:
            result = self._mcp_manager.call_tool(
                "memory", "search_nodes", {"query": query}
            )
            if hasattr(result, "content") and result.content:
                import json

                text = (
                    result.content[0].text if hasattr(result.content[0], "text") else ""
                )
                return json.loads(text) if text else []
        except Exception:
            pass
        return []

    def read_memory(self) -> dict:
        """Read entire MCP memory graph. Returns empty dict if unavailable."""
        if not self._mcp_manager or not self._mcp_manager.is_connected("memory"):
            return {}
        try:
            result = self._mcp_manager.call_tool("memory", "read_graph", {})
            if hasattr(result, "content") and result.content:
                import json

                text = (
                    result.content[0].text if hasattr(result.content[0], "text") else ""
                )
                return json.loads(text) if text else {}
        except Exception:
            pass
        return {}
