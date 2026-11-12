"""One session, one process (docs/schedule/DESIGN.md §4.3).

Until v10.12.0 nothing in agent-cli stopped a session from being opened twice:
``--resume <id>`` in two terminals ran both, each appending to the same
``history.jsonl``. The board avoided it by reading ``web.json`` itself. Once a
session owns schedules that fire from inside its process, two processes would
both fire them — so the second one is refused at startup instead.

The lock is an OS advisory lock (``flock``) on ``<session_dir>/session.lock``,
held on an fd this module keeps for the life of the process. The kernel drops
it however the process dies, so there is no stale-lock case to clean up.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

_NAME = "session.lock"

# path → fd. Claiming again from the SAME process is a no-op: the invariant is
# about processes, and one process legitimately re-enters (tests, a resume
# pre-check followed by the real open).
_HELD: dict[str, int] = {}


class SessionBusy(RuntimeError):
    """Another live process holds this session."""

    def __init__(self, session_dir: Path, pid: int | None):
        self.session_dir = session_dir
        self.pid = pid
        who = f"pid {pid}" if pid else "another process"
        super().__init__(f"session {session_dir.name} is already open in {who}")


def claim_session(session_dir: str | Path) -> None:
    """Take the session for this process, or raise :class:`SessionBusy`."""
    sdir = Path(session_dir)
    key = str(sdir.resolve())
    if key in _HELD:
        return
    sdir.mkdir(parents=True, exist_ok=True)
    fd = os.open(sdir / _NAME, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        holder = _read_pid(fd)
        os.close(fd)
        raise SessionBusy(sdir, holder) from None
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    _HELD[key] = fd


def release_session(session_dir: str | Path) -> None:
    """Give the session up before exit (tests; a process normally just exits)."""
    fd = _HELD.pop(str(Path(session_dir).resolve()), None)
    if fd is not None:
        os.close(fd)


def session_is_open(session_dir: str | Path) -> bool:
    """그 세션을 쥔 프로세스가 지금 살아 있나 (v10.127.2) — 읽기 전용 질의.

    ``sessions`` 가 "in progress" 를 살아 있는 것에만 쓰려고 묻는다. 잠금 파일이 없으면
    False 이고 **만들지 않는다**. 있으면 ``O_RDONLY`` 로 열어 공유 잠금을 비차단으로
    시도하고 곧바로 닫는다 — 실패하면 누가 배타 잠금을 쥐고 있다. 파일 속 pid 를 읽어
    살아 있는지 보는 방식은 쓰지 않는다(pid 재사용에 속는다). 이 프로세스가 쥔 세션은 True.

    경합: 이 질의가 공유 잠금을 쥔 찰나에 다른 프로세스의 :func:`claim_session` 이 겹치면
    그쪽이 :class:`SessionBusy` 를 본다. 창은 열기·시도·닫기뿐이라 최소지만 0 은 아니다."""
    sdir = Path(session_dir)
    if str(sdir.resolve()) in _HELD:
        return True
    try:
        fd = os.open(sdir / _NAME, os.O_RDONLY)
    except OSError:
        return False  # 없음(또는 못 엶) — 잠금을 쥔 프로세스가 만든 적이 없다
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(fd)  # 닫으면 공유 잠금도 풀린다
    return False


def _read_pid(fd: int) -> int | None:
    try:
        return int(os.pread(fd, 32, 0).decode().strip())
    except (OSError, ValueError):
        return None
