"""Per-session web instance file — ``.agent-cli/sessions/<id>/web.json``.

Written when ``agent-cli web`` starts and removed when it exits, so an external
orchestrator (the "board") can answer *"is this session's web up, and where?"*
by reading one file::

    {"session_id": ..., "host": ..., "port": ..., "token": ..., "pid": ...}

The board reads it to spawn-or-attach: present + pid alive → redirect/proxy to
``host:port`` with ``token``; missing or dead pid → (re)spawn
``agent-cli web --resume <id> --idle-timeout N`` (which rewrites the file). The
instance self-reaps on idle (``--idle-timeout``) and removes the file on exit,
so the board never tracks or kills processes itself.

Pure read/write/remove — no server dependency, no global state.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

_NAME = "web.json"


def instance_file_path(session_dir: str | Path) -> Path:
    return Path(session_dir) / _NAME


def write_instance_file(
    session_dir: str | Path,
    *,
    session_id: str,
    host: str,
    port: int,
    token: str,
    pid: int | None = None,
) -> Path:
    """Write (overwrite) the instance file. ``pid`` defaults to this process.
    Creates the session dir if missing. Returns the path."""
    info = {
        "session_id": session_id,
        "host": host,
        "port": port,
        "token": token,
        "pid": os.getpid() if pid is None else pid,
    }
    path = instance_file_path(session_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(info), encoding="utf-8")
    return path


def read_instance_file(session_dir: str | Path) -> dict | None:
    """Return the instance info, or ``None`` if absent / unreadable / corrupt."""
    try:
        return json.loads(instance_file_path(session_dir).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def remove_instance_file(session_dir: str | Path) -> None:
    """Remove the instance file if present (idempotent, best-effort)."""
    try:
        instance_file_path(session_dir).unlink()
    except (FileNotFoundError, OSError):
        pass
