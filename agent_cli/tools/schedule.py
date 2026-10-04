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
  "⏰ Scheduler").
- ``delete`` — ``id`` (from ``list``). Named ``id`` like every other tool
  (memory, monitor, reply, answer): as ``schedule_id`` a live main was refused
  nine times in a day and never adapted (v9.25.3).
- ``list`` — the session's schedules.
"""

from __future__ import annotations

from typing import ClassVar

from agent_cli.schedule.registry import ScheduleError
from agent_cli.schedule.runtime import get_schedule_registry
from agent_cli.tools.base import Tool
from agent_cli.tools.result import ToolResult


def _fmt_schedules(registry) -> str:
    """The model-facing listing — English only, so the cron is shown raw
    (``cron.describe`` is a Korean label for the UI)."""
    scheds = registry.list_all()
    if not scheds:
        return "No schedules in this session."
    lines = []
    for s in scheds:
        nxt = registry.next_fire(s)
        bits = [f"cron '{s.cron}'"]
        if not s.enabled:
            bits.append("disabled")
        elif nxt is not None:
            bits.append(f"next {nxt.strftime('%Y-%m-%d %H:%M')}")
        if s.missed_at:
            bits.append(
                f"MISSED {s.missed_at} — not run; the user decides run-now or skip"
            )
        name = f"{s.label} — " if s.label else ""
        lines.append(f"- [{s.id}] {name}{' · '.join(bits)}\n    → {s.prompt}")
    return "Schedules in this session:\n" + "\n".join(lines)


def _sched_id(args: dict) -> str:
    return (args.get("id") or "").strip()


class ScheduleTool(Tool):
    name = "schedule"
    description = (
        "Schedule a recurring request for THIS session: at each cron time the "
        "prompt arrives as if the user had sent it. Use for standing/periodic "
        "work the user asked to automate (e.g. a weekly report). Modes: add "
        "(cron + prompt [+ label, nickname]), delete (id), list. cron is 5 "
        "fields 'min hour day month weekday' in local time (e.g. '0 9 * * 1' = "
        "Mondays 09:00). 'nickname' sets the display name the prompt appears "
        "under (default '⏰ Scheduler'). Schedules are saved with the session "
        "and this process stays up while one is enabled. A fire that came due "
        "while the process was down is NOT run automatically — the user is "
        "asked whether to run it."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "description": "add | delete | list"},
            "cron": {
                "type": "string",
                "description": "5-field cron (add), e.g. '0 9 * * 1' = weekly Mon 09:00",
            },
            "prompt": {
                "type": "string",
                "description": "The request to inject on each fire (required for add)",
            },
            "label": {
                "type": "string",
                "description": "Short name for the schedule (optional, add)",
            },
            "nickname": {
                "type": "string",
                "description": (
                    "Display name the injected prompt shows under (optional, "
                    "add; defaults to '⏰ Scheduler')"
                ),
            },
            "id": {
                "type": "string",
                "description": "Which schedule to delete (delete; from list)",
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
        if mode not in ("add", "delete", "list"):
            return f"invalid mode: {mode!r}. Valid: ['add', 'delete', 'list']"
        if mode == "add" and not (args.get("prompt") or "").strip():
            return "'prompt' is required for mode='add'"
        if mode == "add" and not (args.get("cron") or "").strip():
            return "'cron' is required for mode='add'"
        if mode == "delete" and not _sched_id(args):
            return (
                "'id' is required for mode='delete' — the schedule id from mode='list'"
            )
        return None

    def _run(self, args: dict, *, ctx=None) -> ToolResult:
        registry = get_schedule_registry()
        if registry is None:
            return ToolResult(False, error="schedule unavailable: no active session.")
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
                return ToolResult(False, error=f"schedule add rejected: {e}")
            return ToolResult(
                True, output=f"Scheduled [{s.id}].\n{_fmt_schedules(registry)}"
            )
        if mode == "delete":
            sid = _sched_id(args)
            if not registry.delete(sid):
                return ToolResult(
                    False,
                    error=f"schedule delete rejected: no schedule {sid!r}.\n"
                    f"{_fmt_schedules(registry)}",
                )
            return ToolResult(True, output=f"Deleted.\n{_fmt_schedules(registry)}")
        return ToolResult(True, output=_fmt_schedules(registry))
