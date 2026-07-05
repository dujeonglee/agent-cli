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
import tempfile
from pathlib import Path

_NAME = "web.json"
_STATUS_NAME = "status.json"


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


# ── Live status sidecar ────────────────────────────────────────────────
# ``status.json`` holds the frequently-changing liveness the board used to poll
# via ``GET /api/health`` — ``{busy, awaiting_input, viewers}``. Kept SEPARATE
# from ``web.json`` (the quasi-static host/port/token/pid handshake) because it
# is rewritten on every viewer/busy/awaiting change; the board reads this file
# instead of an HTTP round-trip. Writes are atomic (temp + ``os.replace``) so a
# concurrent reader never sees a half-written file.


def status_file_path(session_dir: str | Path) -> Path:
    return Path(session_dir) / _STATUS_NAME


def write_status_file(
    session_dir: str | Path,
    *,
    busy: bool,
    awaiting_input: bool,
    viewers: int,
) -> Path:
    """Atomically (over)write the live status sidecar. Returns the path."""
    info = {
        "busy": bool(busy),
        "awaiting_input": bool(awaiting_input),
        "viewers": int(viewers),
    }
    path = status_file_path(session_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique temp per write: this status is (re)written from BOTH the agent-loop
    # thread (busy/awaiting via set_sticky) and the web thread (viewers via
    # register_connection). A FIXED tmp name races — writer A's os.replace
    # consumes the shared tmp, then writer B's os.replace hits FileNotFoundError
    # and (unguarded) crashes the instance. mkstemp gives each writer its own
    # tmp so the atomic swaps are independent (last writer wins, no crash).
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".status-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(info))
        os.replace(tmp, path)  # atomic swap — readers see all-or-nothing
    except BaseException:
        try:
            os.unlink(tmp)  # don't leak the temp on a failed write
        except OSError:
            pass
        raise
    return path


def read_status_file(session_dir: str | Path) -> dict | None:
    """Return the live status, or ``None`` if absent / unreadable / corrupt."""
    try:
        return json.loads(status_file_path(session_dir).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def remove_status_file(session_dir: str | Path) -> None:
    """Remove the status sidecar if present (idempotent, best-effort)."""
    try:
        status_file_path(session_dir).unlink()
    except (FileNotFoundError, OSError):
        pass
