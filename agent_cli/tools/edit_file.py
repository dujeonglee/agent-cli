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
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    # Dashes: em/en/minus to hyphen
    text = text.replace("\u2014", "-").replace("\u2013", "-").replace("\u2212", "-")
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

        # Hash mismatch even after normalization. Two possible causes:
        # (1) external mutation — file changed since the last read_file,
        #     fix is to re-read and retry;
        # (2) same-call mutation — in a multi-edit call, an earlier edit
        #     mutated the lines this later ref points at, fix is to
        #     combine overlapping edits into a single 'replace' op.
        # The message covers both so the caller can diagnose.
        raise RuntimeError(
            f"Hash mismatch at line {line_num}: ref '{ref}' does not match "
            f"current content. Re-read the file with read_file to get fresh "
            f"hashline tags, then retry. If this is a multi-edit call, verify "
            f"that no earlier edit mutates lines your later refs target — "
            f"combine overlapping edits into a single 'replace' operation."
        )


def _edit_range(edit: dict) -> tuple[int, int] | None:
    """Return the [start_line, end_line] range this edit touches, or
    None if there is no ref to compare against (e.g., append-to-EOF
    with no pos). Used for overlap detection only — the line numbers
    are extracted from refs syntactically and do not verify hashes.

    For replace ops, the range is inclusive [pos_line, end_line] (or
    [pos_line, pos_line] when end is omitted). For append/prepend
    insertion points we use [pos_line, pos_line] — the edit depends
    on that line's existence and siblings at that position, so any
    other edit touching the same line is a conflict.
    """
    pos = edit.get("pos")
    if not pos:
        return None
    try:
        start_line, _ = _parse_ref(pos)
    except RuntimeError:
        # Malformed ref — pre-validate will catch it with a proper
        # error later; skip it for overlap purposes.
        return None
    end = edit.get("end")
    if end:
        try:
            end_line, _ = _parse_ref(end)
            return (start_line, max(start_line, end_line))
        except RuntimeError:
            return (start_line, start_line)
    return (start_line, start_line)


def tool_edit_file(args: dict) -> ToolResult:
    """Apply hashline-based edits to a file with fuzzy matching support."""

    path = args.get("path", "")
    edits = args.get("edits", [])
    if not edits:
        return ToolResult(False, error="No edits provided.")

    # Filter out non-dict items in edits (LLM sometimes inserts ints or strings)
    edits = [e for e in edits if isinstance(e, dict)]
    if not edits:
        return ToolResult(
            False,
            error="No valid edit operations found (each edit must be a JSON object).",
        )

    # Field-type pre-validation. ``pos`` / ``end`` MUST be hashline
    # strings like ``"5#VR"`` — but smaller models sometimes emit
    # them as bare integers (``pos: 5``) or null-wrapped values
    # (``pos: [null]``). Without this guard those propagate into
    # ``_parse_ref`` → ``re.match`` and raise a raw ``TypeError`` from
    # ``re.py`` that escapes the worker thread, killing the loop
    # instead of surfacing an Observation the LLM can recover from.
    # Catch the bad shape here and return a clear retry message.
    for i, edit in enumerate(edits):
        for field in ("pos", "end"):
            v = edit.get(field)
            if v is None:
                continue
            if not isinstance(v, str):
                return ToolResult(
                    False,
                    error=(
                        f"edit #{i + 1}: '{field}' must be a hashline "
                        f"string like '5#VR', got {type(v).__name__} "
                        f"({v!r}). Re-read the file with read_file to "
                        f"get fresh hashline tags, then retry."
                    ),
                )

    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception as e:
        return ToolResult(False, error=f"edit_file: cannot read '{path}': {e}")

    original_text = text
    file_lines = text.split("\n")
    fuzzy_warnings: list[str] = []

    # Ambiguity checks — two complementary layers:
    #
    # Layer 1: shared ref string across edits. When the same hashline
    # ref appears in multiple edits (same pos twice, or pos in one
    # edit equals end/pos in another), the first edit's mutation
    # invalidates the shared ref for later edits. A ref that is both
    # pos and end of the SAME edit (degenerate single-line range) is
    # fine and not flagged.
    ref_sources: dict[str, set[int]] = {}
    for i, edit in enumerate(edits):
        for ref in (edit.get("pos"), edit.get("end")):
            if not ref:
                continue
            ref_sources.setdefault(ref, set()).add(i)
    duplicates = sorted(r for r, idxs in ref_sources.items() if len(idxs) > 1)
    if duplicates:
        refs_str = ", ".join(f"'{r}'" for r in duplicates)
        return ToolResult(
            False,
            error=(
                f"Ambiguous edit: reference(s) {refs_str} appear in multiple "
                f"edits of this call. Earlier edits mutate the file and "
                f"invalidate the hash for later edits that target the same "
                f"line. Combine overlapping edits into a single 'replace' "
                f"operation with the final intended content."
            ),
        )

    # Layer 2: overlapping line-number ranges with *different* ref
    # strings. Layer 1 matches on ref string equality; if two edits
    # target the same line via different hashes (pathological), or
    # their replace-ranges span overlapping regions with distinct
    # endpoint refs, Layer 1 misses them and they'd only surface as
    # a cryptic apply-time hash mismatch. Compute each edit's
    # effective line range and reject on any pairwise intersection.
    edit_ranges: list[tuple[int, tuple[int, int]]] = []
    for i, edit in enumerate(edits):
        r = _edit_range(edit)
        if r is not None:
            edit_ranges.append((i, r))
    for a_i in range(len(edit_ranges)):
        idx_a, (a_start, a_end) = edit_ranges[a_i]
        for b_i in range(a_i + 1, len(edit_ranges)):
            idx_b, (b_start, b_end) = edit_ranges[b_i]
            if a_start <= b_end and b_start <= a_end:
                return ToolResult(
                    False,
                    error=(
                        f"Ambiguous edit: edit #{idx_a + 1} "
                        f"(lines {a_start}-{a_end}) and edit #{idx_b + 1} "
                        f"(lines {b_start}-{b_end}) touch overlapping line "
                        f"regions. Combine overlapping edits into a single "
                        f"'replace' operation with the final intended "
                        f"content, or split dependent changes into separate "
                        f"edit_file calls with read_file between them."
                    ),
                )

    # Pre-validate all refs before mutating
    for edit in edits:
        op = edit.get("op", "")
        pos = edit.get("pos")
        end = edit.get("end")
        if op not in ("replace", "append", "prepend", "delete"):
            return ToolResult(
                False,
                error=f"Unknown edit op: '{op}'. Use replace|append|prepend|delete.",
            )
        try:
            if pos:
                _, was_fuzzy = fuzzy_verify_ref(file_lines, pos)
                if was_fuzzy:
                    fuzzy_warnings.append(
                        f"[warn] Fuzzy match used for ref '{pos}' — hash mismatch tolerated"
                    )
            if end:
                _, was_fuzzy = fuzzy_verify_ref(file_lines, end)
                if was_fuzzy:
                    fuzzy_warnings.append(
                        f"[warn] Fuzzy match used for ref '{end}' — hash mismatch tolerated"
                    )
        except RuntimeError as e:
            return ToolResult(False, error=str(e))

    # Sort edits bottom-up so earlier splices don't shift later indices
    def _sort_key(edit):
        pos = edit.get("pos")
        if pos:
            n, _ = _parse_ref(pos)
            return -n
        return 0

    sorted_edits = sorted(edits, key=_sort_key)

    # Apply each edit against the mutating file_lines. The ambiguity
    # check above catches the common multi-edit interaction patterns
    # up-front, but `fuzzy_verify_ref` can still raise for genuine
    # edge cases (e.g. overlapping ranges that don't share a ref
    # string). Wrap the apply loop so those surface as a clean
    # ToolResult error instead of an unhandled exception.
    try:
        for edit in sorted_edits:
            op = edit["op"]
            pos = edit.get("pos")
            end = edit.get("end")
            new_lines = edit.get("lines")
            if isinstance(new_lines, str):
                new_lines = new_lines.split("\n")
            if new_lines is None:
                new_lines = []

            if op in ("replace", "delete"):
                if not pos:
                    return ToolResult(False, error=f"{op} requires 'pos'.")
                # delete = replace the pos..end range with nothing. ``lines``
                # is not part of delete's schema, so any value it carries is
                # ignored (replace+lines=[] remains the legacy delete form).
                repl = [] if op == "delete" else new_lines
                start_idx, _ = fuzzy_verify_ref(file_lines, pos)
                if end:
                    end_idx, _ = fuzzy_verify_ref(file_lines, end)
                    file_lines[start_idx : end_idx + 1] = repl
                else:
                    file_lines[start_idx : start_idx + 1] = repl

            elif op == "append":
                if pos:
                    idx, _ = fuzzy_verify_ref(file_lines, pos)
                    file_lines[idx + 1 : idx + 1] = new_lines
                else:
                    file_lines.extend(new_lines)

            elif op == "prepend":
                if pos:
                    idx, _ = fuzzy_verify_ref(file_lines, pos)
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
        "Edit a file using hashline refs from read_file. "
        "Ops: replace, append, prepend, delete. "
        "delete removes the pos..end range and takes no lines; "
        "replace with lines=[] also deletes (legacy form)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "edit_file_path": {"type": "string", "description": "File path"},
            "edit_file_edits": {
                "type": "array",
                "description": "List of edit operations",
                "items": {
                    "type": "object",
                    "properties": {
                        "op": {"type": "string"},
                        "pos": {"type": "string"},
                        "end": {"type": "string"},
                        "lines": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["op", "pos"],
                },
            },
        },
        "required": ["edit_file_path", "edit_file_edits"],
    }

    def touched_paths(self, action_input: dict) -> list[str]:
        p = self.strip_prefix(action_input).get("path")
        return [p] if isinstance(p, str) and p else []

    def summary_arg(self, action_input: dict) -> str:
        return self.strip_prefix(action_input).get("path", "")

    def _run(self, args: dict, *, session_dir=None) -> ToolResult:
        return tool_edit_file(args)
