"""Shell output artifact: save oversized stdout to disk and hand the LLM
a compact preview that points back at the full log via read_file.

Why this exists — pattern shared with read_file's full-read guard:

1. Shell commands are run exactly once (side-effect-safe). The command
   has already executed; all we control is what goes into the LLM's
   context window.
2. When stdout is large, we write the full output to a session-scoped
   artifact file under ``session_dir/shell/`` and return a compact
   preview (head+tail + recovery options). The LLM dereferences the
   artifact with read_file(path, search=...) / line_start+line_end /
   full=true — reusing the existing read_file guard without any new
   hidden parameters on shell itself.
3. LRU eviction keeps ``session_dir/shell/`` bounded: newest
   ``AGENT_CLI_SHELL_ARTIFACT_KEEP`` files live, older ones get pruned
   on each write. Reads also bump mtime (touched via the loop-level
   post-process), so an artifact that's actively being queried doesn't
   get evicted out from under the LLM.

Thresholds and limits are configurable via env vars so operators can
tune per workload:

- ``AGENT_CLI_SHELL_OUTPUT_LIMIT_LINES`` (default 500; 0 disables)
- ``AGENT_CLI_SHELL_OUTPUT_LIMIT_BYTES`` (default 20480; 0 disables)
- ``AGENT_CLI_SHELL_ARTIFACT_MAX_SIZE`` (default 5_242_880 bytes; per-file cap)
- ``AGENT_CLI_SHELL_ARTIFACT_KEEP`` (default 20; 0 disables LRU)
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

_DEFAULT_LIMIT_LINES = 500
_DEFAULT_LIMIT_BYTES = 20 * 1024  # 20 KB
_DEFAULT_ARTIFACT_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
_DEFAULT_KEEP = 20

_HEAD_LINES = 20
_TAIL_LINES_DEFAULT = 20
_TAIL_LINES_ON_FAILURE = 30  # err logs cluster near the end


def _env_int(name: str, default: int) -> int:
    """Read an int env var; fall back to default on missing / bad value."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _limit_lines() -> int:
    return _env_int("AGENT_CLI_SHELL_OUTPUT_LIMIT_LINES", _DEFAULT_LIMIT_LINES)


def _limit_bytes() -> int:
    return _env_int("AGENT_CLI_SHELL_OUTPUT_LIMIT_BYTES", _DEFAULT_LIMIT_BYTES)


def _artifact_max_bytes() -> int:
    return _env_int("AGENT_CLI_SHELL_ARTIFACT_MAX_SIZE", _DEFAULT_ARTIFACT_MAX_BYTES)


def _keep_count() -> int:
    return _env_int("AGENT_CLI_SHELL_ARTIFACT_KEEP", _DEFAULT_KEEP)


def exceeds_limit(output: str) -> bool:
    """True when either the line or byte ceiling is crossed.

    Both are OR-combined: a long-line log trips the byte limit even
    with few lines; a many-short-line log trips the line limit even
    at low byte count. Either limit at 0 disables that axis.
    """
    line_limit = _limit_lines()
    byte_limit = _limit_bytes()
    if line_limit > 0 and output.count("\n") + 1 > line_limit:
        return True
    if byte_limit > 0 and len(output.encode("utf-8", errors="replace")) > byte_limit:
        return True
    return False


def _artifact_filename(command: str) -> str:
    """Stable per-command filename: <unix-ts>-<cmd-hash8>.log.

    Sorts by time, hash distinguishes simultaneous commands. Same
    command in the same second writes the same filename — idempotent.
    """
    ts = int(time.time())
    digest = hashlib.sha1(command.encode("utf-8", errors="replace")).hexdigest()[:8]
    return f"{ts}-{digest}.log"


def _write_atomic(target: Path, content: str) -> None:
    """Write via temp + rename so partial writes never appear in LRU
    scans. Enforce the per-file byte cap."""
    max_bytes = _artifact_max_bytes()
    encoded = content.encode("utf-8", errors="replace")
    if max_bytes > 0 and len(encoded) > max_bytes:
        truncated = encoded[:max_bytes].decode("utf-8", errors="replace")
        content = (
            truncated
            + f"\n[truncated: artifact cap {max_bytes} bytes exceeded; "
            + f"original was {len(encoded)} bytes]"
        )
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.rename(target)


def _lru_evict(shell_dir: Path, keep: int) -> None:
    """Prune oldest *.log files until at most `keep` remain.

    Ordering is by mtime ascending (oldest first). Best-effort:
    permission / race failures on individual unlinks are ignored so
    one bad file can't stall subsequent writes.
    """
    if keep <= 0:
        return
    try:
        files = sorted(shell_dir.glob("*.log"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    excess = len(files) - keep
    if excess <= 0:
        return
    for old in files[:excess]:
        try:
            old.unlink()
        except OSError:
            pass


def save_artifact(session_dir: Path, command: str, output: str) -> Path | None:
    """Persist `output` under session_dir/shell/ and prune old entries.

    Returns the absolute Path on success, None on any IO failure — the
    caller is expected to fall back to returning the full output inline
    rather than silently dropping it.
    """
    try:
        shell_dir = session_dir / "shell"
        shell_dir.mkdir(parents=True, exist_ok=True)
        target = shell_dir / _artifact_filename(command)
        _write_atomic(target, output)
        _lru_evict(shell_dir, _keep_count())
        return target.resolve()
    except OSError:
        return None


def build_preview(
    command: str,
    output: str,
    artifact_path: Path,
    succeeded: bool = True,
) -> str:
    """Compose the compact observation the LLM sees in place of full output.

    Layout — head + tail + recovery options. Tail is weighted heavier
    for failed commands (build/test errors cluster at the end).
    """
    lines = output.splitlines()
    total_lines = len(lines)
    total_bytes = len(output.encode("utf-8", errors="replace"))

    size_label = (
        f"{total_bytes:,} bytes"
        if total_bytes < 10_000
        else f"{total_bytes / 1024:.1f} KB"
    )

    head_count = min(_HEAD_LINES, total_lines)
    tail_count_target = _TAIL_LINES_ON_FAILURE if not succeeded else _TAIL_LINES_DEFAULT
    tail_count = min(tail_count_target, max(0, total_lines - head_count))

    head_block = "\n".join(lines[:head_count]) if head_count else ""
    tail_block = "\n".join(lines[-tail_count:]) if tail_count else ""

    parts = [
        f"[shell-output-saved] $ {command}",
        f"full: {total_lines} lines, {size_label} → {artifact_path}",
    ]
    if head_count:
        parts.append(f"\n--- head ({head_count} lines) ---\n{head_block}")
    skipped = total_lines - head_count - tail_count
    if skipped > 0:
        parts.append(f"\n[... {skipped} lines omitted ...]")
    if tail_count:
        parts.append(f"\n--- tail ({tail_count} lines) ---\n{tail_block}")
    parts.append(
        "\n[To dig into the full log:\n"
        f'  - read_file(path="{artifact_path}", search="<keyword>")       '
        "← targeted\n"
        f'  - read_file(path="{artifact_path}", line_start=N, line_end=M) '
        "← specific region\n"
        f'  - read_file(path="{artifact_path}", full=true)                '
        "← full log if genuinely needed]"
    )
    return "\n".join(parts)


def is_session_shell_artifact(read_path: str, session_dir: Path | None) -> bool:
    """Strict check: is `read_path` inside this session's shell/ subdir?

    Used by the loop-level post-process after a successful read_file to
    decide whether to touch the artifact's mtime (LRU read-awareness).
    Uses resolve() so symlinks and ``..`` components don't fool the check,
    and pairs it with is_relative_to so user paths like ./shell/foo.log
    in an unrelated directory don't accidentally match.
    """
    if not session_dir or not isinstance(read_path, str) or not read_path:
        return False
    try:
        target = Path(read_path).resolve()
        root = (session_dir / "shell").resolve()
    except OSError:
        return False
    try:
        return target.is_relative_to(root)
    except (ValueError, AttributeError):
        return False


def touch_if_artifact(read_path: str, session_dir: Path | None) -> None:
    """Best-effort mtime bump for shell artifacts — keeps recently-read
    files out of the LRU's eviction queue. Silent on any failure."""
    if not is_session_shell_artifact(read_path, session_dir):
        return
    try:
        Path(read_path).touch()
    except OSError:
        pass
