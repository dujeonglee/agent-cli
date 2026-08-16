"""``schedule`` tool — register/delete recurring prompt injections for THIS post.

Only active under an external scheduler (env ``AGENT_CLI_SCHEDULER=1``, set by
agent-board when it spawns the instance). agent-cli itself has no scheduler and
knows nothing about the board: the tool just appends a JSONL request to
``<workspace>/.agent-cli/schedule-requests.jsonl`` and reads the board's ack
back from ``schedule-state.json`` (the file contract in agent-board's
docs/schedule-design.md §7). Without the env var the tool is never registered,
so a plain CLI session's surface is unchanged.

Modes:
- ``add`` — ``cron`` (5-field, e.g. "0 9 * * 1") + ``prompt`` (the request to
  inject) + optional ``label`` + optional ``nickname`` (display name the injected
  prompt shows up under; defaults to "⏰ Scheduler" on the board when omitted).
  Schedules FUTURE autonomous work on this post.
- ``delete`` — ``schedule_id`` (from ``list``).
- ``list`` — the post's current schedules.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import ClassVar

from agent_cli.tools.base import Tool
from agent_cli.tools.result import ToolResult

_REQUESTS = "schedule-requests.jsonl"
_STATE = "schedule-state.json"
_ACK_TIMEOUT_S = 6.0  # board 스캐너(1s)+rearm 반영 대기 상한


def is_enabled() -> bool:
    """True when an external scheduler is present (agent-board spawned us)."""
    import os

    return os.environ.get("AGENT_CLI_SCHEDULER") == "1"


def _agent_cli_dir(session_dir: Path) -> Path:
    # session_dir = <ws>/.agent-cli/sessions/<sid>  →  <ws>/.agent-cli
    return Path(session_dir).parent.parent


def _read_state(acdir: Path) -> dict:
    try:
        return json.loads((acdir / _STATE).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _append_request(acdir: Path, req: dict) -> None:
    acdir.mkdir(parents=True, exist_ok=True)
    with (acdir / _REQUESTS).open("a", encoding="utf-8") as f:
        f.write(json.dumps(req, ensure_ascii=False) + "\n")


def _await_ack(acdir: Path, req_id: str) -> dict | None:
    """Poll the state file until the board records THIS request's result."""
    deadline = time.monotonic() + _ACK_TIMEOUT_S
    while time.monotonic() < deadline:
        res = _read_state(acdir).get("results", {}).get(req_id)
        if res is not None:
            return res
        time.sleep(0.2)
    return None


def _fmt_schedules(state: dict) -> str:
    scheds = state.get("schedules", [])
    if not scheds:
        return "No schedules for this post."
    lines = []
    for s in scheds:
        flag = "" if s.get("enabled", True) else " (disabled)"
        nxt = f" · next {s['next_fire']}" if s.get("next_fire") else ""
        lines.append(
            f"- [{s['schedule_id']}] {s.get('label') or s.get('human') or s['cron']} "
            f"({s['human']}){flag}{nxt}\n    → {s.get('prompt', '')}"
        )
    return "Schedules for this post:\n" + "\n".join(lines)


class ScheduleTool(Tool):
    name = "schedule"
    description = (
        "Schedule a recurring request to be injected into THIS post later, even "
        "when you're not running — the board restarts this session at the due "
        "time and delivers the prompt. Use for standing/periodic work the user "
        "asked to automate (e.g. a weekly report). Modes: add (cron + prompt "
        "[+ label]), delete (schedule_id), list. cron is 5 fields "
        "'min hour day month weekday' (e.g. '0 9 * * 1' = Mondays 09:00). "
        "Pass 'nickname' to set the display name the injected prompt appears "
        "under (defaults to '⏰ Scheduler'). "
        "Only available when a scheduler backs this session."
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
            "schedule_id": {
                "type": "string",
                "description": "Which schedule to delete (from list)",
            },
        },
        "required": ["mode"],
    }

    @staticmethod
    def env_enabled() -> bool:
        """Registry gate — only present when an external scheduler is set."""
        return is_enabled()

    def wrap_single_op(self, flat: dict) -> dict:
        return flat

    def summary_arg(self, action_input: dict) -> str:
        std = self.strip_prefix(action_input)
        return f"{std.get('mode', '')} {std.get('label') or std.get('cron') or std.get('schedule_id') or ''}".strip()

    def validate(self, args: dict) -> str | None:
        mode = (args.get("mode") or "").strip()
        if mode not in ("add", "delete", "list"):
            return f"invalid mode: {mode!r}. Valid: ['add', 'delete', 'list']"
        if mode == "add" and not (args.get("prompt") or "").strip():
            return "'prompt' is required for mode='add'"
        if mode == "add" and not (args.get("cron") or "").strip():
            return "'cron' is required for mode='add'"
        if mode == "delete" and not (args.get("schedule_id") or "").strip():
            return "'schedule_id' is required for mode='delete'"
        return None

    def _run(self, args: dict, *, ctx=None) -> ToolResult:
        session_dir = ctx.session_dir if ctx else None
        if session_dir is None:
            return ToolResult(
                False, error="schedule unavailable: no active session directory."
            )
        acdir = _agent_cli_dir(session_dir)
        mode = (args.get("mode") or "").strip()
        req_id = uuid.uuid4().hex[:8]
        req: dict = {"op": mode, "req_id": req_id}
        if mode == "add":
            req.update(
                cron=(args.get("cron") or "").strip(),
                prompt=(args.get("prompt") or "").strip(),
                label=(args.get("label") or "").strip(),
                nickname=(args.get("nickname") or "").strip(),
            )
        elif mode == "delete":
            req["schedule_id"] = (args.get("schedule_id") or "").strip()

        try:
            _append_request(acdir, req)
        except OSError as e:
            return ToolResult(False, error=f"schedule: could not write request: {e}")

        res = _await_ack(acdir, req_id)
        state = _read_state(acdir)
        if res is None:
            return ToolResult(
                True,
                output=(
                    "Request submitted; the scheduler hasn't acknowledged yet "
                    "(it may apply within a few seconds). Use mode=list to confirm."
                ),
            )
        if res.get("error"):
            return ToolResult(False, error=f"schedule {mode} rejected: {res['error']}")
        if mode == "add":
            return ToolResult(
                True,
                output=f"Scheduled [{res.get('schedule_id')}].\n{_fmt_schedules(state)}",
            )
        if mode == "delete":
            return ToolResult(True, output=f"Deleted.\n{_fmt_schedules(state)}")
        return ToolResult(True, output=_fmt_schedules(state))
