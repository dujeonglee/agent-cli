"""``schedule`` tool — recurring prompts for THIS session (docs/schedule/DESIGN.md).

The scheduler lives in this process (``agent_cli/schedule/``): the tool talks
to the session's :class:`ScheduleRegistry` directly and the answer is
immediate. At each cron time the prompt enters the input queue as if the user
had sent it, under the schedule's nickname.

Until v10.12.0 this tool was a thin client of agent-board's scheduler — it
appended to ``schedule-requests.jsonl`` and polled ``schedule-state.json`` for
the board's ack, and was registered only under ``AGENT_CLI_SCHEDULER=1``. A
plain ``agent-cli web``/``run`` had no scheduling at all. The file contract and
the env gate are gone.

Modes:
- ``add`` — ``cron`` (5-field, local time) + ``prompt`` + optional ``label`` +
  optional ``nickname`` (display name of the injected prompt; default
  ``Scheduler-<label|id>`` — a virtual user per schedule, v10.56.0).
- ``delete`` — ``id`` (from ``list``). Named ``id`` like every other tool
  (memory, monitor, reply, answer): as ``schedule_id`` a live main was refused
  nine times in a day and never adapted (v9.25.3).
- ``list`` — the session's schedules.
"""

from __future__ import annotations

from typing import ClassVar

from agent_cli.schedule.registry import ScheduleError
from agent_cli.session_slots import get_schedule_registry
from agent_cli.tools.base import Tool
from agent_cli.tools.result import ToolResult

MODES = ("add", "delete", "list")

# 모델이 보는 고정 문구 (v10.108.0) — 실행과 ``prompts.inventory`` 의 표본이 같은
# 출처를 쓴다 (표기 가드 tests/test_prompt_notation.py).
NO_SCHEDULES = "No schedules in this session."
LIST_HEAD = "Schedules in this session:"
#: 놓친 발화 — 모델이 할 수 있는 일이 아니라 사실만 (web 화면의 ⏰ 서랍이 묻는다).
MISSED_NOTE = (
    "missed {at} — not run; waiting for the user's run-now or skip in the web UI"
)
UNAVAILABLE = "schedule unavailable: no active session."
ADD_REJECTED = "schedule add rejected: {why}"
DELETE_REJECTED = 'schedule delete rejected: no schedule "{sid}".'
SCHEDULED = "Scheduled [{id}]."
DELETED = "Deleted."


def _fmt_schedules(registry) -> str:
    """The model-facing listing — English only, so the cron is shown raw
    (``cron.describe`` is a Korean label for the UI)."""
    scheds = registry.list_all()
    if not scheds:
        return NO_SCHEDULES
    lines = []
    for s in scheds:
        nxt = registry.next_fire(s)
        bits = [f'cron "{s.cron}"']
        if not s.enabled:
            bits.append("disabled")
        elif nxt is not None:
            bits.append(f"next {nxt.strftime('%Y-%m-%d %H:%M')}")
        if s.missed_at:
            bits.append(MISSED_NOTE.format(at=s.missed_at))
        name = f"{s.label} — " if s.label else ""
        lines.append(f"- [{s.id}] {name}{' · '.join(bits)}\n    → {s.prompt}")
    return f"{LIST_HEAD}\n" + "\n".join(lines)


def _sched_id(args: dict) -> str:
    return (args.get("id") or "").strip()


class ScheduleTool(Tool):
    name = "schedule"
    #: 지연 (v10.110.0, docs/tool-help/DESIGN.md D1) — 프롬프트엔 이 한 줄과 인자 뼈대,
    #: 전문은 `tool_help`.
    deferred = True
    summary = "Run a request on a cron schedule in this session, for periodic work the user asked to automate."
    description = (
        "Schedule a recurring request for this session: at each cron time the prompt "
        "arrives as if the user had sent it. Use for standing or periodic work the "
        "user asked to automate (e.g. a weekly report). Schedules are saved with the "
        "session, and this process stays up while one is enabled. A fire that came "
        "due while the process was down is not run; it waits in the web UI for the "
        "user to run it now or skip it."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": list(MODES),
                "description": '"add" | "delete" | "list"',
            },
            "cron": {
                "type": "string",
                "description": (
                    '5-field cron "min hour day month weekday" in local time ("add"), '
                    'e.g. "0 9 * * 1" = Mondays 09:00'
                ),
            },
            "prompt": {
                "type": "string",
                "description": 'The request to inject on each fire (required for "add")',
            },
            "label": {
                "type": "string",
                "description": 'Short name for the schedule (optional, "add")',
            },
            "nickname": {
                "type": "string",
                "description": (
                    'Display name the injected prompt shows under (optional, "add"; '
                    'defaults to "Scheduler-<label or id>")'
                ),
            },
            "id": {
                "type": "string",
                "description": 'Which schedule to delete ("delete"; from "list")',
            },
        },
        "required": ["mode"],
    }

    def wrap_single_op(self, flat: dict) -> dict:
        return flat

    def summary_arg(self, action_input: dict) -> str:
        std = self.strip_prefix(action_input)
        return f"{std.get('mode', '')} {std.get('label') or std.get('cron') or _sched_id(std)}".strip()

    def validate(self, args: dict) -> str | None:
        mode = (args.get("mode") or "").strip()
        if mode not in MODES:
            return f'invalid `mode` "{mode}" — must be one of "add", "delete", "list"'
        if mode == "add" and not (args.get("prompt") or "").strip():
            return '`prompt` is required for `mode` "add"'
        if mode == "add" and not (args.get("cron") or "").strip():
            return '`cron` is required for `mode` "add"'
        if mode == "delete" and not _sched_id(args):
            return '`id` is required for `mode` "delete" — the schedule id from `mode` "list"'
        return None

    def _run(self, args: dict, *, ctx=None) -> ToolResult:
        registry = get_schedule_registry()
        if registry is None:
            return ToolResult(False, error=UNAVAILABLE)
        mode = (args.get("mode") or "").strip()
        if mode == "add":
            try:
                s = registry.add(
                    args.get("cron") or "",
                    args.get("prompt") or "",
                    label=args.get("label") or "",
                    nickname=args.get("nickname") or "",
                    source="agent",
                )
            except ScheduleError as e:
                return ToolResult(False, error=ADD_REJECTED.format(why=e))
            return ToolResult(
                True, output=f"{SCHEDULED.format(id=s.id)}\n{_fmt_schedules(registry)}"
            )
        if mode == "delete":
            sid = _sched_id(args)
            if not registry.delete(sid):
                return ToolResult(
                    False,
                    error=f"{DELETE_REJECTED.format(sid=sid)}\n{_fmt_schedules(registry)}",
                )
            return ToolResult(True, output=f"{DELETED}\n{_fmt_schedules(registry)}")
        return ToolResult(True, output=_fmt_schedules(registry))
