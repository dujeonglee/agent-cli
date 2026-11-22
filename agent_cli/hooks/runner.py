"""Hook runner — execute Python hooks for lifecycle events."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_cli.hooks.context import HookContext
from agent_cli.hooks.events import ALL_EVENTS
from agent_cli.hooks.loader import load_python_hooks


class HookRunner:
    """Executes Python hooks for events.

    Usage::

        runner = HookRunner()          # scans hook dirs
        ctx = runner.fire("PreLLMCall", messages=messages, turn=3)
        # inspect ctx.system_sections, ctx.messages, etc.
    """

    def __init__(
        self,
        hook_dirs: list[Path] | None = None,
    ):
        self._python_hooks: dict[str, list[Callable]] = load_python_hooks(hook_dirs)

    def reload(self, hook_dirs: list[Path] | None = None) -> None:
        """Re-scan hook directories and reload all Python hooks."""
        self._python_hooks = load_python_hooks(hook_dirs)

    def fire(
        self,
        event: str,
        *,
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
    ) -> HookContext:
        """Fire an event — run all registered hooks and return context.

        Python hooks execute first (file-name order), then shell hooks.
        """
        if event not in ALL_EVENTS:
            raise ValueError(f"Unknown hook event: {event}")

        ctx = HookContext(
            event=event,
            messages=messages,
            session_dir=session_dir,
            turn=turn,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_result=tool_result,
            llm_response=llm_response,
            delegate_result=delegate_result,
            skill_result=skill_result,
            mcp_manager=mcp_manager,
        )

        # 1. Python hooks
        self._run_python_hooks(event, ctx)

        return ctx

    def _run_python_hooks(self, event: str, ctx: HookContext) -> None:
        """Execute all Python hook functions for the event."""
        for func in self._python_hooks.get(event, []):
            try:
                func(ctx)
            except Exception:
                # Bad hook — skip silently, don't break the agent loop
                pass
