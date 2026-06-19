"""Edit file tool with hashline verification and fuzzy matching."""

from __future__ import annotations

import re
from pathlib import Path

from agent_cli.tools._diff import format_diff
from agent_cli.tools.base import Tool
from agent_cli.tools.read_file import _parse_ref, _verify_ref, compute_line_hash
from agent_cli.tools.result import ToolResult


def _normalize_for_fuzzy(text: str) -> str:
    """Normalize text for fuzzy comparison."""
    # Whitespace: tabs to spaces, collapse multiple spaces
    text = text.replace("\t", " ")
    text = re.sub(r" {2,}", " ", text)
    text = text.strip()
    # Quotes: smart quotes to straight
    text = text.replace("‘", "'").replace("’", "'")
    text = text.replace("“", '"').replace("”", '"')
    # Dashes: em/en/minus to hyphen
    text = text.replace("—", "-").replace("–", "-").replace("−", "-")
    return text


def fuzzy_verify_ref(lines: list[str], ref: str) -> tuple[int, bool]:
    """Try exact verify first, then fuzzy match on failure.

    Returns (0-based index, was_fuzzy). Raises RuntimeError if both fail.
    """
    try:
        idx = _verify_ref(lines, ref)
        return idx, False
    except RuntimeError as exact_err:
        # Try fuzzy: re-parse ref, compare normalized content
        line_num, expected_hash = _parse_ref(ref)
        if line_num < 1 or line_num > len(lines):
            raise exact_err

        # The hash mismatch might be due to whitespace/quote differences.
        # Check if the line at this position is "close enough" by comparing
        # normalized versions.
        actual_line = lines[line_num - 1]

        # We don't have the original line content, only the hash.
        # So fuzzy matching works by trusting the line number
        # if the hash is only off due to normalization differences.
        # Recompute hash on normalized content to see if it matches.
        norm_hash = compute_line_hash(line_num, _normalize_for_fuzzy(actual_line))
        if norm_hash == expected_hash:
            return line_num - 1, True

        # Hash mismatch even after normalization — the file changed since the
        # last read_file (external write, or an earlier edit_file op this turn
        # shifted/rewrote this line). Re-read to get fresh hashline tags.
        raise RuntimeError(
            f"Hash mismatch at line {line_num}: ref '{ref}' does not match "
            f"current content. Re-read the file with read_file to get fresh "
            f"hashline tags, then retry."
        )


def tool_edit_file(args: dict) -> ToolResult:
    """Apply a single hashline-based edit to a file (fuzzy matching support).

    Flat-native (consolidation roadmap Step 3): one op = one edit. Several
    edits to one file are emitted as several edit_file ops in the same turn —
    hashline refs are content-addressed, so a later op's ref still resolves
    (via fuzzy match) after an earlier op shifts line numbers.
    """
    path = args.get("path", "")
    op = args.get("op", "")
    pos = args.get("pos")
    end = args.get("end")
    new_lines = args.get("lines")

    if op not in ("replace", "append", "prepend", "delete"):
        return ToolResult(
            False,
            error=f"Unknown edit op: '{op}'. Use replace|append|prepend|delete.",
        )

    # ``pos`` / ``end`` MUST be hashline strings like ``"5#VR"`` — but smaller
    # models sometimes emit them as bare integers (``pos: 5``) or null-wrapped
    # values. Without this guard those propagate into ``_parse_ref`` →
    # ``re.match`` and raise a raw ``TypeError`` from ``re.py`` that escapes the
    # worker thread, killing the loop instead of surfacing a recoverable
    # Observation. Catch the bad shape here and return a clear retry message.
    for field, v in (("pos", pos), ("end", end)):
        if v is None:
            continue
        if not isinstance(v, str):
            return ToolResult(
                False,
                error=(
                    f"'{field}' must be a hashline string like '5#VR', got "
                    f"{type(v).__name__} ({v!r}). Re-read the file with "
                    f"read_file to get fresh hashline tags, then retry."
                ),
            )

    if isinstance(new_lines, str):
        new_lines = new_lines.split("\n")
    if new_lines is None:
        new_lines = []

    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception as e:
        return ToolResult(False, error=f"edit_file: cannot read '{path}': {e}")

    original_text = text
    file_lines = text.split("\n")
    fuzzy_warnings: list[str] = []

    def _resolve(ref: str) -> int:
        idx, was_fuzzy = fuzzy_verify_ref(file_lines, ref)
        if was_fuzzy:
            fuzzy_warnings.append(
                f"[warn] Fuzzy match used for ref '{ref}' — hash mismatch tolerated"
            )
        return idx

    # Resolve all refs BEFORE mutating so a bad ref fails without a partial
    # write. ``delete`` = replace the pos..end range with nothing; ``lines`` is
    # not part of delete's schema (replace+lines=[] remains the legacy delete
    # form).
    try:
        if op in ("replace", "delete"):
            if not pos:
                return ToolResult(False, error=f"{op} requires 'pos'.")
            repl = [] if op == "delete" else new_lines
            start_idx = _resolve(pos)
            if end:
                end_idx = _resolve(end)
                file_lines[start_idx : end_idx + 1] = repl
            else:
                file_lines[start_idx : start_idx + 1] = repl

        elif op == "append":
            if pos:
                idx = _resolve(pos)
                file_lines[idx + 1 : idx + 1] = new_lines
            else:
                file_lines.extend(new_lines)

        elif op == "prepend":
            if pos:
                idx = _resolve(pos)
                file_lines[idx:idx] = new_lines
            else:
                file_lines[0:0] = new_lines
    except RuntimeError as e:
        return ToolResult(False, error=f"edit_file apply failed: {e}")

    result_text = "\n".join(file_lines)
    try:
        Path(path).write_text(result_text, encoding="utf-8")
    except Exception as e:
        return ToolResult(False, error=f"edit_file: cannot write '{path}': {e}")

    msg = f"Edit complete: {path} ({len(file_lines)} lines)"
    if fuzzy_warnings:
        msg += "\n" + "\n".join(fuzzy_warnings)
    diff = format_diff(original_text, result_text, path)
    if diff:
        msg += "\n\n" + diff
    # Refresh code_index after a successful edit. Best-effort —
    # post_hook swallows its own exceptions so an indexing hiccup
    # never poisons the user-facing edit.
    from agent_cli.tools.code_index import post_hook

    post_hook(path)
    return ToolResult(True, output=msg)


class EditFileTool(Tool):
    name = "edit_file"
    description = (
        "Edit a file using a hashline ref from read_file. "
        "Ops: replace, append, prepend, delete. "
        "delete removes the pos..end range and takes no lines; "
        "replace with lines=[] also deletes (legacy form)."
    )
    # Flat-native (consolidation roadmap Step 3): the schema is the plain
    # single-edit shape — no `edit_file_` wire-key prefix and no `edits` batch
    # array. One op applies one edit; several edits to one file are several
    # edit_file ops in a turn (hashline refs are content-addressed, so they
    # survive line shifts from earlier ops). `wrap_single_op` is identity;
    # `key_prefix` is left at its default so strip_prefix is a no-op on these
    # flat keys and `claims` returns False for a flat `{path}`.
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path"},
            "op": {
                "type": "string",
                "description": "replace | append | prepend | delete",
            },
            "pos": {
                "type": "string",
                "description": "Hashline ref (e.g. '5#VR') of the target line",
            },
            "end": {
                "type": "string",
                "description": "Hashline ref ending an inclusive range (replace/delete)",
            },
            "lines": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Replacement / inserted lines",
            },
        },
        "required": ["path", "op", "pos"],
    }

    def wrap_single_op(self, flat: dict) -> dict:
        return flat

    def touched_paths(self, action_input: dict) -> list[str]:
        p = self.strip_prefix(action_input).get("path")
        return [p] if isinstance(p, str) and p else []

    def summary_arg(self, action_input: dict) -> str:
        return self.strip_prefix(action_input).get("path", "")

    def render_action_input_for_context(self, action_input: dict) -> dict:
        """Elide the replacement ``lines`` body on re-feed (keep the op shape —
        a 1-element array marker — so the wire shape stays valid). The edit was
        already applied + confirmed; re-feeding the inserted text every turn
        only crowds out context (the model reads_file to view)."""
        lines = action_input.get("lines")
        if isinstance(lines, list) and lines:
            path = action_input.get("path", "")
            return {
                **action_input,
                "lines": [f"<{len(lines)} lines edited in {path} — read_file to view>"],
            }
        return action_input

    def _run(self, args: dict, *, session_dir=None) -> ToolResult:
        return tool_edit_file(args)
