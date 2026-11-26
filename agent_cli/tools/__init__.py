"""Tool registry and execution.

Tools live as :class:`~agent_cli.tools.base.Tool` subclasses in their own
modules; :mod:`agent_cli.tools.registry` instantiates them into ``TOOLS``
and derives the schema/dispatch/inference helpers. This package re-exports
that surface so existing imports (``from agent_cli.tools import TOOLS,
_execute_tool``) keep working.
"""

from __future__ import annotations

from agent_cli.tools.base import RunContext
from agent_cli.tools.registry import (
    TOOLS,
    _execute_tool,
    infer_action,
)

__all__ = [
    "TOOLS",
    "RunContext",
    "_execute_tool",
    "infer_action",
]
