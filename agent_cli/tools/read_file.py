"""Read file tool with hashline formatting.

Hashline system: each line is tagged as LINE#HASH:content for precise editing.
"""

from __future__ import annotations

import re
import zlib
from pathlib import Path

from agent_cli.tools.base import Tool
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
    """Parse a hashline ref like '5#VR' -> (5, 'VR').

    Contract: always raises ``RuntimeError`` on bad input — never lets
    a ``TypeError`` from ``re.match`` propagate. Callers catch
    ``RuntimeError`` and translate it into a ToolResult error so the
    LLM sees a clean Observation it can retry from. Without this guard
    a malformed payload (e.g. ``pos: 5`` instead of ``pos: "5#VR"``)
    would crash the worker thread inside ``re.py`` instead.
    """
    if not isinstance(ref, str):
        raise RuntimeError(
            f"Invalid hashline ref: {ref!r} (expected string, got "
            f"{type(ref).__name__}). Expected format: LINE#HASH "
            f"(e.g. 5#VR). Re-read the file with read_file to get "
            f"fresh hashline tags."
        )
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


_STAT_HEAD_LINES = 20  # lines shown alongside metadata in stat mode
_DEFAULT_SEARCH_CONTEXT = 5  # lines before/after each match


def format_hashlines_range(all_lines: list[str], start_idx: int, end_idx: int) -> str:
    """Format ``all_lines[start_idx:end_idx]`` with hashline tags.

    Companion to the full-text :func:`format_hashlines`; takes a pre-split
    line list and a 0-based half-open range so callers that already have
    line counts (partial reads, search context windows, structural fetches)
    do not pay the cost of re-splitting the file.

    Public API: also imported by :mod:`agent_cli.tools.code_index` so
    that ``code_index`` fetch output uses the same hashline format —
    callers can pipe a fetched body straight into ``edit_file`` without
    re-reading the file. Keep the signature stable.
    """
    out = []
    for i, line in enumerate(all_lines[start_idx:end_idx], start_idx + 1):
        tag = compute_line_hash(i, line)
        out.append(f"{i}#{tag}:{line}")
    return "\n".join(out)


def _stat(path: str, text: str, all_lines: list[str]) -> ToolResult:
    """Return file metadata + first N lines + follow-up guidance.

    stat is a metadata query (like Unix `stat`), not a read — the caller
    is expected to pick one of the real read modes (full /
    line_start+line_end / search) as a follow-up.
    """
    total = len(all_lines)
    size_bytes = len(text.encode("utf-8"))
    size_label = (
        f"{size_bytes:,} bytes"
        if size_bytes < 10_000
        else f"{size_bytes / 1024:.1f} KB"
    )

    head_end = min(_STAT_HEAD_LINES, total)
    head = format_hashlines_range(all_lines, 0, head_end)

    hint = (
        f"\n\n[File has {total} total lines. stat returned metadata + the "
        f"first {head_end} lines only — you have NOT read the file yet. "
        f"Pick a follow-up read mode:\n"
        f"  - read_file(path) for a full read (if the file is small or central to the task)\n"
        f"  - read_file(path, line_start=N, line_end=M) for a specific range\n"
        f'  - read_file(path, search="keyword") to hunt for specific content]'
    )
    return ToolResult(
        True,
        output=f"[stat] {path}: {total} lines, {size_label}\n{head}{hint}",
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
        parts.append(format_hashlines_range(all_lines, lo, hi))
    return ToolResult(True, output="\n".join(parts))


def _read_one(spec: dict) -> ToolResult:
    """Read a single file with optional stat, search, or partial read modes.

    Modes (mutually exclusive, picked by keys present):
    - stat=True: metadata + first 20 lines + guidance (metadata query, NOT a read)
    - search="pattern", context=N: grep-style matches with surrounding lines
    - line_start/line_end: partial read (1-based inclusive)
    - no mode: full file
    """
    if not isinstance(spec, dict):
        return ToolResult(
            False,
            error=f"read_file: each read must be an object, got {type(spec).__name__}",
        )

    path = spec.get("path", "")
    line_start = spec.get("line_start", 0)
    line_end = spec.get("line_end", 0)
    stat = bool(spec.get("stat", False))
    search = spec.get("search", "") or ""
    context = spec.get("context", _DEFAULT_SEARCH_CONTEXT)

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

        if stat:
            return _stat(path, text, all_lines)

        if search:
            return _search(path, all_lines, search, context)

        if line_start > 0:
            start = max(0, line_start - 1)
            end = min(total, line_end) if line_end > 0 else total
            return ToolResult(
                True, output=format_hashlines_range(all_lines, start, end)
            )

        # Bare full read — return the whole file.
        return ToolResult(True, output=format_hashlines(text))
    except Exception as e:
        return ToolResult(False, error=f"read_file failed: {e}")


def _format_batch(reads: list, results: list[ToolResult]) -> ToolResult:
    """Combine multiple single-file reads into one observation.

    Mirrors :func:`delegate._format_parallel_results`: per-read header +
    body (or ERROR), then a summary line. ``success`` is False only when
    *every* read failed — a partial success stays True so the model still
    gets whatever it could read (reads are non-destructive).
    """
    parts: list[str] = []
    ok = 0
    for i, (spec, res) in enumerate(zip(reads, results), 1):
        path = spec.get("path", "?") if isinstance(spec, dict) else "?"
        parts.append(f"─── [{i}] {path} ───")
        if res.success:
            parts.append(res.output or "(empty)")
            ok += 1
        else:
            parts.append(f"ERROR: {res.error}")
        parts.append("")

    failed = len(reads) - ok
    parts.append(f"[read_file batch: {len(reads)} reads, {ok} ok, {failed} failed]")
    combined = "\n".join(parts)
    if ok == 0:
        return ToolResult(False, error=combined)
    return ToolResult(True, output=combined)


def tool_read_file(args: dict) -> ToolResult:
    """Read one or more files. ``args["reads"]`` is a list of read specs,
    each consumed by :func:`_read_one`.

    A single-element list returns that read's result verbatim (no batch
    header, so single reads are byte-identical to the old behavior);
    multiple reads are combined by :func:`_format_batch`.
    """
    reads = args.get("reads")
    if not reads or not isinstance(reads, list):
        return ToolResult(
            False,
            error=(
                "read_file requires a non-empty 'read_file_reads' list "
                "(each item: {path, ...optional line_start/line_end/search/stat})."
            ),
        )

    results = [_read_one(spec) for spec in reads]
    if len(results) == 1:
        return results[0]
    return _format_batch(reads, results)


class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "Read one or more files in a single call. Provide read_file_reads as a "
        "list; each item reads one file with an optional mode. Lines are tagged "
        "as LINE#HASH:content for editing. For a single file, pass a one-element "
        "list. Per-item modes: stat=true (metadata + first 20 lines, NOT a read — "
        "follow up with a real read), search='regex' (matching regions with "
        "context, efficient for targeted lookups), line_start/line_end (partial "
        "read, 1-based inclusive), or none (full file)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "read_file_reads": {
                "type": "array",
                "description": (
                    "List of reads (one or many). Each item reads one file. For a "
                    "single file, pass a one-element list."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path to read"},
                        "line_start": {
                            "type": "integer",
                            "description": "Start line (1-based). Omit to read from beginning.",
                        },
                        "line_end": {
                            "type": "integer",
                            "description": "End line (1-based, inclusive). Omit to read to end.",
                        },
                        "search": {
                            "type": "string",
                            "description": "Regex pattern. Returns only matching lines with surrounding context.",
                        },
                        "context": {
                            "type": "integer",
                            "description": "Lines of context before/after each search match (default 5).",
                        },
                        "stat": {
                            "type": "boolean",
                            "description": "Metadata query only — line count, size, first 20 lines. Not a read.",
                        },
                    },
                    "required": ["path"],
                },
            },
        },
        "required": ["read_file_reads"],
    }

    def touched_paths(self, action_input: dict) -> list[str]:
        reads = self.strip_prefix(action_input).get("reads") or []
        return [
            r["path"]
            for r in reads
            if isinstance(r, dict) and isinstance(r.get("path"), str)
        ]

    def _run(self, args: dict, *, session_dir=None) -> ToolResult:
        return tool_read_file(args)
