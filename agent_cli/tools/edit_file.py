"""Edit file tool with hashline verification and fuzzy matching."""
from __future__ import annotations

import re
from pathlib import Path

from agent_cli.tools.read_file import _parse_ref, _verify_ref, compute_line_hash


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
        actual_norm = _normalize_for_fuzzy(actual_line)

        # We don't have the original line content, only the hash.
        # So fuzzy matching works by trusting the line number
        # if the hash is only off due to normalization differences.
        # Recompute hash on normalized content to see if it matches.
        norm_hash = compute_line_hash(line_num, _normalize_for_fuzzy(actual_line))
        if norm_hash == expected_hash:
            return line_num - 1, True

        # Also try: maybe the file was re-saved with different whitespace
        # but the line number is still correct. Accept with warning.
        # This is a lenient fallback for small models.
        if actual_norm and len(actual_norm) > 0:
            # Accept if line number is valid and content is non-empty
            # (the agent likely has the right line, just wrong hash)
            return line_num - 1, True

        raise exact_err


def tool_edit_file(args: dict) -> str:
    """Apply hashline-based edits to a file with fuzzy matching support."""
    path = args.get("path", "")
    edits = args.get("edits", [])
    if not edits:
        raise RuntimeError("No edits provided.")
    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception as e:
        raise RuntimeError(f"edit_file: cannot read '{path}': {e}")

    file_lines = text.split("\n")
    fuzzy_warnings: list[str] = []

    # Pre-validate all refs before mutating
    for edit in edits:
        op = edit.get("op", "")
        pos = edit.get("pos")
        end = edit.get("end")
        if op not in ("replace", "append", "prepend"):
            raise RuntimeError(
                f"Unknown edit op: '{op}'. Use replace|append|prepend."
            )
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

    # Sort edits bottom-up so earlier splices don't shift later indices
    def _sort_key(edit):
        pos = edit.get("pos")
        if pos:
            n, _ = _parse_ref(pos)
            return -n
        return 0

    sorted_edits = sorted(edits, key=_sort_key)

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
                raise RuntimeError("replace requires 'pos'.")
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

    result_text = "\n".join(file_lines)
    try:
        Path(path).write_text(result_text, encoding="utf-8")
    except Exception as e:
        raise RuntimeError(f"edit_file: cannot write '{path}': {e}")

    msg = f"Edit complete: {path} ({len(file_lines)} lines)"
    if fuzzy_warnings:
        msg += "\n" + "\n".join(fuzzy_warnings)
    return msg
