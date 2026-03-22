"""Read file tool with hashline formatting.

Hashline system: each line is tagged as LINE#HASH:content for precise editing.
"""

from __future__ import annotations

import re
import zlib
from pathlib import Path

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


def tool_read_file(args: dict) -> str:
    """Read a file and return hashline-formatted content.

    Supports partial reads via line_start/line_end (1-based, inclusive).
    """
    path = args.get("path", "")
    line_start = args.get("line_start", 0)
    line_end = args.get("line_end", 0)

    # Coerce to int (LLMs sometimes send strings)
    try:
        line_start = int(line_start) if line_start else 0
        line_end = int(line_end) if line_end else 0
    except (ValueError, TypeError):
        line_start, line_end = 0, 0

    try:
        text = Path(path).read_text(encoding="utf-8")
        all_lines = text.split("\n")
        total = len(all_lines)

        if line_start > 0:
            start = max(0, line_start - 1)  # 1-based to 0-based
            end = min(total, line_end) if line_end > 0 else total
            selected = all_lines[start:end]
            # Re-join and format with correct line numbers
            out = []
            for i, line in enumerate(selected, start + 1):
                tag = compute_line_hash(i, line)
                out.append(f"{i}#{tag}:{line}")
            return "\n".join(out)

        return format_hashlines(text)
    except Exception as e:
        raise RuntimeError(f"read_file failed: {e}")
