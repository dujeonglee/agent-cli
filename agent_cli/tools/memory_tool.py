"""Session memory tool — the LLM's interface to agent_cli.memory.

flat-native (one op = one memory operation, ``{mode, ...}``): ``add`` a salient
note, ``get`` its full detail, ``update``/``delete`` to correct or drop, ``list``
to filter. The compact index is always shown in the system prompt (``## Session
Memory``); this tool is how the model writes it and pulls full detail on demand.
"""

from __future__ import annotations

from agent_cli import memory
from agent_cli.tools.base import Tool
from agent_cli.tools.result import ToolResult


def _dispatch(args: dict, session_dir) -> ToolResult:
    if session_dir is None:
        return ToolResult(
            False,
            error="memory unavailable: no active session (memory is session-scoped).",
        )
    mode = (args.get("mode") or "").strip()
    try:
        if mode == "add":
            e = memory.add(
                session_dir,
                type=(args.get("type") or "").strip(),
                summary=args.get("summary") or "",
                detail=args.get("detail"),
                tags=args.get("tags"),
            )
            return ToolResult(
                True, output=f"Saved memory #{e['id']} [{e['type']}]: {e['summary']}"
            )
        if mode == "get":
            e = memory.get(session_dir, _as_id(args.get("id")))
            if e is None:
                return ToolResult(False, error=f"no memory with id {args.get('id')}.")
            return ToolResult(True, output=memory.format_entry(e))
        if mode == "update":
            e = memory.update(
                session_dir,
                _as_id(args.get("id")),
                type=(args.get("type") or None),
                summary=args.get("summary"),
                detail=args.get("detail"),
                tags=args.get("tags"),
            )
            return ToolResult(True, output=f"Updated memory #{e['id']}: {e['summary']}")
        if mode == "delete":
            memory.delete(session_dir, _as_id(args.get("id")))
            return ToolResult(True, output=f"Deleted memory #{args.get('id')}.")
        if mode == "list":
            entries = memory.list_entries(
                session_dir,
                type=(args.get("type") or None),
                tag=(args.get("tag") or None),
            )
            if not entries:
                return ToolResult(True, output="(no matching memories)")
            body = "\n".join(
                f"{memory._TYPE_ICONS.get(e.get('type'), '•')} #{e['id']} "
                f"[{e['type']}] {e['summary']}"
                for e in entries
            )
            return ToolResult(True, output=body)
        return ToolResult(
            False,
            error=(f"unknown mode '{mode}'. Use add | get | update | delete | list."),
        )
    except memory.MemoryError as ex:
        return ToolResult(False, error=str(ex))


def _as_id(v):
    """Accept an int id or a bare-int string (models sometimes stringify)."""
    if isinstance(v, bool):  # bool is an int subclass — reject explicitly
        raise memory.MemoryError("id must be an integer.")
    if isinstance(v, int):
        return v
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        raise memory.MemoryError(f"id must be an integer, got {v!r}.") from None


class MemoryTool(Tool):
    name = "memory"
    description = (
        "Record and recall salient session notes that must survive context "
        "compaction — critical FAILURES (so you don't repeat them), important "
        "DISCOVERIES (to reuse), DECISIONS (why you chose X), and NOTES. The "
        "compact index (id, type, summary) is always shown to you under "
        "'## Session Memory'; use mode=get to pull an entry's full detail. "
        "Record proactively when you hit a real dead-end or learn something "
        "non-obvious. Modes: add (type+summary, optional detail/tags → id), "
        "get (id), update (id + fields to change), delete (id), list "
        "(optional type/tag filter). type ∈ failure|discovery|decision|note."
    )
    parameters = {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "description": "add | get | update | delete | list",
            },
            "type": {
                "type": "string",
                "description": "failure | discovery | decision | note (add/update/list filter)",
            },
            "summary": {
                "type": "string",
                "description": "One-line index label (required for add)",
            },
            "detail": {
                "type": "string",
                "description": "Full content, pulled on demand via get (optional)",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional keywords for list filtering",
            },
            "id": {
                "type": "integer",
                "description": "Memory id (get/update/delete)",
            },
            "tag": {
                "type": "string",
                "description": "list mode: filter by a single tag",
            },
        },
        "required": ["mode"],
    }

    def wrap_single_op(self, flat: dict) -> dict:
        return flat

    def summary_arg(self, action_input: dict) -> str:
        std = self.strip_prefix(action_input)
        return f"{std.get('mode', '')} {std.get('summary', std.get('id', ''))}".strip()

    def _run(self, args: dict, *, session_dir=None) -> ToolResult:
        return _dispatch(args, session_dir)
