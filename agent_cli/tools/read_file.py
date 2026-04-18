"""Read file tool with hashline formatting.

Hashline system: each line is tagged as LINE#HASH:content for precise editing.
"""

from __future__ import annotations

import re
import zlib
from pathlib import Path

from agent_cli.tools.result import ToolResult

# Hashline constants: 16-char alphabet for CRC32-to-2-char hash encoding
_NIBBLE = "ZPMQVRWSNKTXJBYH"
# Lookup table: maps byte 0-255 to 2-char hash tag (e.g. "VR", "KT")
_DICT = [f"{_NIBBLE[i >> 4]}{_NIBBLE[i & 0x0F]}" for i in range(256)]
_RE_SIGNIFICANT = re.compile(r"[\w\d]", re.UNICODE)


def compute_line_hash(idx: int, line: str) -> str:
    """Return a 2-char hash tag for *line* at 1-based *idx*."""
    line = line.rstrip("\r\n").rstrip()
    seed = 0 if _RE_SIGNIFICANT.search(line) else idx
    data = line.encode("utf-8")
    h = zlib.crc32(data, seed) & 0xFF
    return _DICT[h]


def format_hashlines(text: str) -> str:
    """Format file content with hashline tags: LINE#HASH:content"""
    lines = text.split("\n")
    out = []
    for i, line in enumerate(lines, 1):
        tag = compute_line_hash(i, line)
        out.append(f"{i}#{tag}:{line}")
    return "\n".join(out)


def _parse_ref(ref: str) -> tuple[int, str]:
    """Parse a hashline ref like '5#VR' -> (5, 'VR')."""
    m = re.match(r"^(\d+)#([A-Z]{2})$", ref)
    if not m:
        raise RuntimeError(
            f"Invalid hashline ref: '{ref}'. Expected format: LINE#HASH (e.g. 5#VR)"
        )
    return int(m.group(1)), m.group(2)


def _verify_ref(lines: list[str], ref: str) -> int:
    """Verify a hashline ref against actual content. Return 0-based index."""
    line_num, expected_hash = _parse_ref(ref)
    if line_num < 1 or line_num > len(lines):
        raise RuntimeError(
            f"Line {line_num} out of range (file has {len(lines)} lines)"
        )
    actual_hash = compute_line_hash(line_num, lines[line_num - 1])
    if actual_hash != expected_hash:
        raise RuntimeError(
            f"Hash mismatch at line {line_num}: expected {expected_hash}, "
            f"got {actual_hash}. The file may have changed. "
            f"Re-read the file to get current hashline tags."
        )
    return line_num - 1  # 0-based


_PEEK_LINES = 20  # lines shown in peek mode
_DEFAULT_SEARCH_CONTEXT = 5  # lines before/after each match


def _format_lines(all_lines: list[str], start_idx: int, end_idx: int) -> str:
    """Format lines[start_idx:end_idx] with hashline tags (start_idx is 0-based)."""
    out = []
    for i, line in enumerate(all_lines[start_idx:end_idx], start_idx + 1):
        tag = compute_line_hash(i, line)
        out.append(f"{i}#{tag}:{line}")
    return "\n".join(out)


def _peek(path: str, text: str, all_lines: list[str]) -> ToolResult:
    """Return file metadata + first N lines + guidance.

    peek is a sizing check, not a read — the caller is expected to pick
    one of the read modes (full / line_start+line_end / search) as a
    follow-up.
    """
    total = len(all_lines)
    size_bytes = len(text.encode("utf-8"))
    size_label = (
        f"{size_bytes:,} bytes"
        if size_bytes < 10_000
        else f"{size_bytes / 1024:.1f} KB"
    )

    head_end = min(_PEEK_LINES, total)
    head = _format_lines(all_lines, 0, head_end)

    hint = (
        f"\n\n[File has {total} total lines. This is a peek — you have NOT read "
        f"the file yet. Pick a follow-up read mode:\n"
        f"  - read_file(path) for a full read (if the file is small or central to the task)\n"
        f"  - read_file(path, line_start=N, line_end=M) for a specific range\n"
        f'  - read_file(path, search="keyword") to hunt for specific content]'
    )
    return ToolResult(
        True,
        output=f"[peek] {path}: {total} lines, {size_label}\n{head}{hint}",
    )


def _search(path: str, all_lines: list[str], pattern: str, context: int) -> ToolResult:
    """Return matches for pattern with surrounding context."""
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return ToolResult(False, error=f"Invalid search pattern '{pattern}': {e}")

    matches = [i for i, line in enumerate(all_lines) if regex.search(line)]
    if not matches:
        return ToolResult(
            True,
            output=f"[search] {path}: no matches for {pattern!r} (in {len(all_lines)} lines)",
        )

    # Merge overlapping context ranges
    ranges: list[tuple[int, int]] = []
    for m in matches:
        lo = max(0, m - context)
        hi = min(len(all_lines), m + context + 1)
        if ranges and lo <= ranges[-1][1]:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], hi))
        else:
            ranges.append((lo, hi))

    parts = [f"[search] {path}: {len(matches)} matches for {pattern!r}"]
    for lo, hi in ranges:
        parts.append(f"\n─── lines {lo + 1}-{hi} ───")
        parts.append(_format_lines(all_lines, lo, hi))
    return ToolResult(True, output="\n".join(parts))


def tool_read_file(args: dict) -> ToolResult:
    """Read a file with optional peek, search, or partial read modes.

    Modes (mutually exclusive, picked by args present):
    - peek=True: metadata + first 20 lines + guidance (sizing check, NOT a read)
    - search="pattern", context=N: grep-style matches with surrounding lines
    - line_start/line_end: partial read (1-based inclusive)
    - no mode: full file
    """

    path = args.get("path", "")
    line_start = args.get("line_start", 0)
    line_end = args.get("line_end", 0)
    peek = bool(args.get("peek", False))
    search = args.get("search", "") or ""
    context = args.get("context", _DEFAULT_SEARCH_CONTEXT)

    # Coerce to int (LLMs sometimes send strings)
    try:
        line_start = int(line_start) if line_start else 0
        line_end = int(line_end) if line_end else 0
        context = int(context) if context else _DEFAULT_SEARCH_CONTEXT
    except (ValueError, TypeError):
        line_start, line_end, context = 0, 0, _DEFAULT_SEARCH_CONTEXT

    try:
        text = Path(path).read_text(encoding="utf-8")
        all_lines = text.split("\n")
        total = len(all_lines)

        if peek:
            return _peek(path, text, all_lines)

        if search:
            return _search(path, all_lines, search, context)

        if line_start > 0:
            start = max(0, line_start - 1)
            end = min(total, line_end) if line_end > 0 else total
            return ToolResult(True, output=_format_lines(all_lines, start, end))

        return ToolResult(True, output=format_hashlines(text))
    except Exception as e:
        return ToolResult(False, error=f"read_file failed: {e}")
