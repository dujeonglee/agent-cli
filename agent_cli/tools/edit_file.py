"""Edit file tool with hashline verification and fuzzy matching."""

from __future__ import annotations

import re
from pathlib import Path

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

    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception as e:
        return ToolResult(False, error=f"edit_file: cannot read '{path}': {e}")

    file_lines = text.split("\n")
    fuzzy_warnings: list[str] = []

    # Ambiguity check: when the same hashline ref appears in multiple
    # edits (same pos twice, or pos in one edit equals end/pos in
    # another), the first edit's mutation invalidates the shared ref
    # for later edits. Historically this manifested as a mid-apply
    # "Hash mismatch" RuntimeError that blamed "a previous turn" —
    # misleading because the mutation happened in the same call.
    # Catching it here gives the model an actionable message. A ref
    # that is both pos and end of the SAME edit (degenerate single-line
    # range) is fine and not flagged.
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

    # Pre-validate all refs before mutating
    for edit in edits:
        op = edit.get("op", "")
        pos = edit.get("pos")
        end = edit.get("end")
        if op not in ("replace", "append", "prepend"):
            return ToolResult(
                False, error=f"Unknown edit op: '{op}'. Use replace|append|prepend."
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

            if op == "replace":
                if not pos:
                    return ToolResult(False, error="replace requires 'pos'.")
                start_idx, _ = fuzzy_verify_ref(file_lines, pos)
                if end:
                    end_idx, _ = fuzzy_verify_ref(file_lines, end)
                    file_lines[start_idx : end_idx + 1] = new_lines
                else:
                    file_lines[start_idx : start_idx + 1] = new_lines

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
    return ToolResult(True, output=msg)
