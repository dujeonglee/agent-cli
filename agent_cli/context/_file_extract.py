"""File path extraction from evicted messages.

Scans a list of cache messages and returns the file paths touched by
known tool invocations. Used by :class:`ContextManager._compact` to
build the accumulated file list surfaced in the system prompt.

Scope decision (RFC §5 FR-CC-5):
  - Only tools whose schema has an explicit ``path`` field are read.
  - Shell commands are *skipped* — extracting paths from ``rm -rf``
    or ``cat foo`` via regex has too many false positives. A separate
    pre-hook redirect (mapping ``cat`` → ``read_file`` etc.) is the
    follow-up PR that addresses shell coverage cleanly.
  - ``delegate`` records a ``<delegate:agent>`` placeholder so the
    LLM knows a subagent was invoked, without pretending we tracked
    the subagent's own file actions (those live in a different
    session directory).
"""

from __future__ import annotations

from typing import Any


_PATH_TOOLS: frozenset[str] = frozenset(
    {"write_file", "edit_file", "read_file", "read_symbols"}
)


def extract_file_paths(messages: list[dict[str, Any]]) -> list[str]:
    """Return de-duplicated, insertion-ordered paths from ``messages``.

    Two record shapes contribute paths (matching how
    :class:`ContextManager` stores tool flow):

      Tool result entry (``role=user``):
          ``{"role": "user", "tool": "<name>", "args": {...}, "content": "..."}``
          Path source: ``args["path"]``.

      Assistant action entry (``role=assistant``):
          ``{"role": "assistant", "action": "<name>",
             "action_input": {...}}``
          Path source: ``action_input["path"]``.

    ``delegate`` is special: its ``tasks`` array carries ``agent``
    names, not paths. We append ``<delegate:agent_name>`` markers so
    the file list section still reflects "a subagent was spawned to
    work on this" — useful for the LLM to recall the topology of
    earlier work even when the subagent's own touched files are out
    of reach.
    """
    paths: list[str] = []
    seen: set[str] = set()

    def _add(p: str) -> None:
        if p and p not in seen:
            seen.add(p)
            paths.append(p)

    for msg in messages:
        # Tool result side
        tool = msg.get("tool")
        if isinstance(tool, str) and tool in _PATH_TOOLS:
            args = msg.get("args") or {}
            if isinstance(args, dict):
                path = args.get("path")
                if isinstance(path, str):
                    _add(path)

        # Assistant action side
        action = msg.get("action")
        if isinstance(action, str):
            action_input = msg.get("action_input") or {}
            if action in _PATH_TOOLS and isinstance(action_input, dict):
                path = action_input.get("path")
                if isinstance(path, str):
                    _add(path)
            elif action == "delegate" and isinstance(action_input, dict):
                tasks = action_input.get("tasks") or []
                if isinstance(tasks, list):
                    for t in tasks:
                        if isinstance(t, dict):
                            agent = t.get("agent")
                            if isinstance(agent, str) and agent:
                                _add(f"<delegate:{agent}>")

    return paths
