"""``snapshot`` tool — restore a file from the workspace git snapshots (docs/snapshot/DESIGN.md).

The snapshots themselves are taken by the built-in hook
(``hooks/builtin/10_snapshot.py``) — at session start and just before every
shell / edit_file / write_file call. This tool is the model's way back: one
call, no PATH or git knowledge needed. It is native like ``monitor`` and
``schedule`` (the store lives in this process — ``snapshot.get_snapshot``).

A CLI (``agent-cli snapshot …``) existed briefly in the same release and was
removed before shipping: telling the model to shell out to our own binary
depended on PATH and duplicated this tool.

Modes:
- ``restore`` — ``path`` (+ optional ``at`` commit). Without ``at`` the most
  recent snapshot whose content differs from what is on disk now — so a damaged
  state that got committed at the next checkpoint does not shadow the good one.
- ``list`` — the snapshots (newest first).
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from agent_cli.tools.base import Tool
from agent_cli.tools.result import ToolResult

MODES = ("restore", "list")


class SnapshotTool(Tool):
    name = "snapshot"
    #: 세션 저장소(`runtime.snapshot_resource`)가 없으면 — git 없음·`AGENT_CLI_SNAPSHOT=0`·
    #: init 실패 — tools_list 에서 벗겨져 프롬프트에 나오지 않는다 (없는 기능을 약속하지 않는다).
    requires_handler = "snapshot"
    description = (
        "Restore a file from the workspace snapshots the harness takes "
        "automatically (at session start and before each shell / edit_file / "
        "write_file call). Use it when an input file got damaged or disappeared: "
        "mode 'restore' with the file path brings back the most recent snapshot "
        "whose content differs from what is on disk now (optional 'at' picks a "
        "specific snapshot). mode 'list' shows the snapshots, newest first."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": list(MODES),
                "description": "restore | list",
            },
            "path": {
                "type": "string",
                "minLength": 1,
                "description": "restore: file or directory to bring back (relative to the working directory)",
            },
            "at": {
                "type": "string",
                "description": "restore: a snapshot id from mode 'list' (default: most recent differing one)",
            },
        },
        "required": ["mode"],
    }

    def validate(self, args: dict) -> str | None:
        mode = str(args.get("mode", "")).strip()
        if mode not in MODES:
            return f"snapshot: mode must be one of {', '.join(MODES)}"
        if mode == "restore" and not str(args.get("path", "")).strip():
            return "snapshot: restore requires `path`"
        return None

    def summary_arg(self, action_input: dict) -> str:
        mode = str(action_input.get("mode", ""))
        path = str(action_input.get("path", "") or "")
        return f"{mode} {path}".strip()

    def _run(self, args: dict, *, ctx=None) -> ToolResult:
        from agent_cli.snapshot import get_snapshot

        store = get_snapshot(Path.cwd())
        if store is None or not store.enabled:
            why = (
                store.reason
                if store is not None
                else "no snapshot store for this workspace"
            )
            return ToolResult(False, error=f"snapshot: not available — {why}")
        mode = str(args.get("mode", "")).strip()
        if mode == "list":
            rows = store.log()
            if not rows:
                return ToolResult(True, output="No snapshots yet.")
            return ToolResult(
                True,
                output="Snapshots (newest first):\n"
                + "\n".join(f"- {sha}  {msg}" for sha, msg in rows),
            )
        path = str(args.get("path", "")).strip()
        at = (str(args.get("at", "")).strip() or None) if args.get("at") else None
        ok, msg = store.restore(path, at=at)
        return (
            ToolResult(ok, output=msg)
            if ok
            else ToolResult(False, error=f"snapshot: {msg}")
        )
