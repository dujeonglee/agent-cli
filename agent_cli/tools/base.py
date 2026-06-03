"""Tool abstraction surface.

Each tool is a :class:`Tool` subclass that owns its schema, its dispatch,
and its wire-key namespace in one place:

- **schema** (``name`` / ``description`` / ``parameters``) — what used to
  live in the central ``registry.TOOL_SCHEMAS`` dict,
- **dispatch** (``_run``) — what used to be a free ``tool_*`` function
  referenced from the central ``__init__.TOOLS`` dict,
- **prefix** — the wire surface namespaces ``action_input`` keys as
  ``{name}_{param}`` (e.g. ``read_file_path``). Everything prefix-related
  is derived from ``name`` on this base class: :meth:`strip_prefix`
  (wire → standard keys, applied in :meth:`run`) and :meth:`claims`
  (does this payload's key shape belong to me, for recovering a dropped
  action name). Subclasses never override them — they just set ``name``.

``Tool`` instances are the values of ``registry.TOOL_SCHEMAS`` (and
``TOOLS``): they expose the same ``.name`` / ``.description`` /
``.parameters`` attributes the old ``ToolSchema`` dataclass did, so every
schema consumer (system prompt, input validation, MCP adapter) keeps
working unchanged.

Virtual tools (complete/ask/...) keep standard keys, so for them
:meth:`strip_prefix` is a no-op and :meth:`claims` is always False — they
fall through to the normal NO_ACTION recovery rather than being inferred.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from agent_cli.tools.result import ToolResult


class Tool(ABC):
    """Base class for every dispatchable tool.

    Subclasses set ``name`` / ``description`` / ``parameters`` as class
    attributes and implement :meth:`_run`. ``parameters`` is a JSON Schema
    object identical in shape to what the old ``ToolSchema.parameters``
    held.
    """

    name: str
    description: str
    parameters: dict

    @property
    def key_prefix(self) -> str:
        """Wire-key namespace for this tool: ``{name}_``."""
        return self.name + "_"

    def strip_prefix(self, args: dict) -> dict:
        """Strip ``key_prefix`` from top-level ``args`` keys (wire →
        standard). Keys without the prefix pass through unchanged — a
        model that emits a bare standard key still works — and nested
        keys inside arrays/objects are never touched.
        """
        if not isinstance(args, dict):
            return args
        p = self.key_prefix
        return {(k[len(p) :] if k.startswith(p) else k): v for k, v in args.items()}

    def add_prefix(self, args: dict) -> dict:
        """Inverse of :meth:`strip_prefix`: namespace top-level ``args``
        keys with ``key_prefix``. Idempotent — keys already carrying the
        prefix are left as-is, and nested keys are untouched. Used to
        render inline-guide examples that are authored in standard keys.
        """
        if not isinstance(args, dict):
            return args
        p = self.key_prefix
        return {(k if k.startswith(p) else p + k): v for k, v in args.items()}

    def claims(self, action_input: dict) -> bool:
        """Whether *action_input* belongs to this tool by key shape, used
        to recover a missing action name (parse_stage 3). True iff any
        top-level key carries this tool's prefix. ``registry.infer_action``
        selects a tool only when exactly one claims, so the prefix
        namespace keeps claims mutually exclusive by construction.
        """
        if not isinstance(action_input, dict):
            return False
        return any(k.startswith(self.key_prefix) for k in action_input)

    def run(self, args: dict, *, session_dir: Path | None = None) -> ToolResult:
        """Public dispatch: strip the tool-name prefix from ``action_input``
        keys, then hand standard keys to :meth:`_run`. ``session_dir`` is
        forwarded uniformly; tools that do not need it ignore it."""
        return self._run(self.strip_prefix(args), session_dir=session_dir)

    @abstractmethod
    def _run(self, args: dict, *, session_dir: Path | None = None) -> ToolResult:
        """Execute the tool with standard (un-prefixed) keys."""
