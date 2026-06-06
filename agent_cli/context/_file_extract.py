"""File path extraction from evicted messages (compaction file-list).

Scans evicted *assistant* records and returns the file-list entries each
tool touched, by delegating to :meth:`agent_cli.tools.base.Tool.touched_paths`.
Keeping the path/array/prefix schema knowledge in each tool (next to its
own ``parameters``) means a tool changing its input shape — e.g. read_file
moving to the ``read_file_reads`` array — updates extraction automatically,
without this module knowing the per-tool key layout.

Used by :class:`ContextManager._compact` to build the accumulated file list
surfaced in the system prompt.

Scope:
  - Only the *assistant action* side carries ``action_input`` (the path
    source). Tool-result entries are ``{role, tool, success, content}`` with
    no args/path, so they contribute nothing.
  - Shell is skipped naturally (``ShellTool.touched_paths`` returns []).
    Regex-extracting paths from ``rm -rf`` / ``cat foo`` has too many false
    positives; a pre-hook redirect is the cleaner follow-up.
  - ``delegate`` contributes ``<delegate:agent>`` markers (no real file path)
    so the file list still reflects "a subagent was spawned".
"""

from __future__ import annotations

from typing import Any


def extract_file_paths(messages: list[dict[str, Any]]) -> list[str]:
    """Return de-duplicated, insertion-ordered file-list entries from
    ``messages`` (cache records as stored by ``ContextManager.add``).

    Each assistant record's ``action`` selects the owning ``Tool``; its
    ``action_input`` is passed to :meth:`Tool.touched_paths`, which knows
    that tool's own key shape (prefixed ``write_file_path``, arrays like
    ``read_file_reads[].path`` / ``code_index_queries[].path``, or the
    ``delegate_tasks[].agent`` markers). Tools without paths return [].
    """
    # Lazy import: ``registry`` pulls in tools that transitively import
    # ``context.manager``, which imports THIS module — a module-load cycle.
    # Importing inside the function defers it to call time, after all modules
    # are loaded (same pattern as recovery.detectors / tools.delegate).
    from agent_cli.tools.registry import TOOLS

    paths: list[str] = []
    seen: set[str] = set()

    for msg in messages:
        action = msg.get("action")
        if not isinstance(action, str):
            continue
        tool = TOOLS.get(action)
        action_input = msg.get("action_input")
        if tool is None or not isinstance(action_input, dict):
            continue
        for entry in tool.touched_paths(action_input):
            if entry and entry not in seen:
                seen.add(entry)
                paths.append(entry)

    return paths
