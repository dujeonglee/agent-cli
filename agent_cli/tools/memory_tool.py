"""Session memory tool — the LLM's interface to agent_cli.memory.

flat-native (one op = one memory operation, ``{mode, ...}``): ``add`` a salient
note, ``get`` its full detail, ``update``/``delete`` to correct or drop. The
compact index is always shown in the tail block (``## Session Memory``); this
tool is how the model writes it and pulls full detail on demand.

v10.81.0 (docs/session-memory/DESIGN.md §11): ``list`` 삭제(벤치 0회, 색인과 중복 —
사용자 결정) · ``mode`` 없이 ``summary`` 가 오면 ``add`` 로 받는다(벤치 add 16회
중 3회가 "invalid mode: ''" 로 거절됐다 — 모호함이 없는 모양은 구제한다) · 설명의
대문자 강조·자리 안내 삭제 · 항목에 ``turn``.
"""

from __future__ import annotations

from typing import ClassVar

from agent_cli import memory
from agent_cli.tools.base import Tool
from agent_cli.tools.result import ToolResult

#: The modes ``tool_memory`` dispatches on — single source for the schema
#: ``enum`` (v9.24.4: the decoding grammar and the validator both read it).
MODES = ("add", "get", "update", "delete")


def effective_mode(args: dict) -> str:
    """``mode`` 가 없고 ``summary`` 가 있으면 "add" — 다른 모드는 ``summary`` 를 필수로
    하지 않으니 모호하지 않다. 검증과 실행이 같은 판정을 본다."""
    mode = str(args.get("mode") or "").strip()
    if not mode and str(args.get("summary") or "").strip():
        return "add"
    return mode


def _dispatch(args: dict, session_dir, turn: int | None = None) -> ToolResult:
    if session_dir is None:
        return ToolResult(
            False,
            error="memory unavailable: no active session (memory is session-scoped).",
        )
    mode = effective_mode(args)
    try:
        if mode == "add":
            e = memory.add(
                session_dir,
                type=(args.get("type") or "").strip(),
                summary=args.get("summary") or "",
                detail=args.get("detail"),
                tags=args.get("tags"),
                turn=turn,
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
        return ToolResult(
            False,
            error=(f'unknown `mode` "{mode}". Use {" | ".join(MODES)}.'),
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
        "Record a session note that must survive context compaction: a failure (so "
        "you do not repeat it), a discovery (to reuse), a decision (why you chose X), "
        "or a note. The index of what you recorded is always shown to you under "
        '"## Session Memory". `mode` "add" takes `type` + `summary` (optional `detail`, '
        '`tags`) and returns an id; "get" (`id`) pulls the full detail; "update" (`id` '
        '+ the fields to change) and "delete" (`id`) correct or drop a note. `type` is '
        'one of "failure", "discovery", "decision", "note".'
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": list(MODES),
                "description": (
                    '"add" | "get" | "update" | "delete" (omitted with a `summary` '
                    'means "add")'
                ),
            },
            "type": {
                "type": "string",
                "enum": list(memory.VALID_TYPES),
                "description": '"failure" | "discovery" | "decision" | "note" ("add"/"update")',
            },
            "summary": {
                "type": "string",
                "minLength": 1,
                "description": 'One-line index label (required for "add")',
            },
            "detail": {
                "type": "string",
                "description": 'Full content, pulled on demand by "get" (optional)',
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional keywords, shown with the entry",
            },
            "id": {
                "type": "integer",
                "description": 'Memory id ("get"/"update"/"delete")',
            },
        },
        "required": ["mode"],
    }

    def schema_gaps(self, action_input: object) -> tuple[list[str], list[str]]:
        """`mode` 없이 `summary` 가 온 호출은 빠진 것이 없다 — "add" 다 (`effective_mode`).
        스키마의 `required` 는 그대로 두어 디코딩 문법은 여전히 `mode` 를 강제한다
        (문법을 쓰는 서버에선 이 모양이 나올 수 없고, 못 쓰는 서버의 구제만 남는다)."""
        missing, unknown = super().schema_gaps(action_input)
        if isinstance(action_input, dict) and effective_mode(action_input) == "add":
            missing = [m for m in missing if m != "mode"]
        return missing, unknown

    def wrap_single_op(self, flat: dict) -> dict:
        return flat

    def summary_arg(self, action_input: dict) -> str:
        std = self.strip_prefix(action_input)
        return f"{std.get('mode', '')} {std.get('summary', std.get('id', ''))}".strip()

    def validate(self, args: dict) -> str | None:
        """C7: mode enum + get/update/delete 의 id 필수 — 실행 없이 판정
        가능한 의미론. id 값 형식 coercion(_as_id)은 실행 소관 유지. ``mode`` 가 없고
        ``summary`` 가 있으면 "add" 로 읽는다(`schema_gaps` 가 같은 판정)."""
        mode = effective_mode(args)
        if mode not in MODES:
            allowed = " | ".join(f'"{m}"' for m in MODES)
            if not mode:
                return f"`mode` is required unless a `summary` is given; use {allowed}"
            return f'unknown `mode` "{mode}"; use {allowed}'
        if mode in ("get", "update", "delete") and args.get("id") in (None, ""):
            return f'`id` is required for `mode` "{mode}"'
        return None

    def _run(self, args: dict, *, ctx=None) -> ToolResult:
        turn = None
        if ctx is not None and ctx.turn_now is not None:
            try:
                turn = int(ctx.turn_now())
            except (TypeError, ValueError):
                turn = None
        return _dispatch(args, ctx.session_dir if ctx else None, turn)
