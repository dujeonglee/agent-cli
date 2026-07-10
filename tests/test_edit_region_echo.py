"""Post-edit windowed hashline echo (Option D: diff + fresh refs).

edit_file historically returned only a unified diff — the diff shows WHAT
changed but carries no ``LINE#HASH`` tags, so the model could not chain a
follow-up edit on the region it just touched without a ``read_file`` round-trip
(write_file already emits hashlines for exactly this reason). These tests pin
the fix: every successful edit appends an ``Updated region`` block of fresh
hashline refs at correct POST-edit line numbers, alongside the diff, capped so
an "edited the whole file" case does not dump every line back into context.
"""

from __future__ import annotations

import re

from agent_cli.tools.edit_file import (
    _MAX_REGION_LINES,
    _REGION_HEADER,
    _REGION_TRUNCATION_PREFIX,
    _updated_region_echo,
    apply_edits_batch,
    tool_edit_file,
)
from agent_cli.tools.read_file import compute_line_hash

_REF_RE = re.compile(r"^(\d+)#([A-Z]{2}):", re.MULTILINE)


def _ref(n: int, line: str) -> str:
    return f"{n}#{compute_line_hash(n, line)}"


def _write(tmp_path, name, lines):
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n")
    return p


def _region_block(output: str) -> str:
    """Return the text of the Updated-region block, or '' if absent."""
    if _REGION_HEADER not in output:
        return ""
    return output.split(_REGION_HEADER, 1)[1]


class TestRegionEcho:
    def test_edit_output_has_diff_and_region(self, tmp_path):
        # Option D: BOTH a unified diff and a fresh-hashline region block.
        p = _write(
            tmp_path,
            "greet.py",
            ["def greet(name):", '    msg = "hi " + name', "    return msg"],
        )
        r = tool_edit_file(
            {
                "path": str(p),
                "op": "replace",
                "pos": _ref(2, '    msg = "hi " + name'),
                "lines": ['    msg = f"hello, {name}!"'],
            }
        )
        assert r.success
        assert "@@" in r.output  # the diff is still there
        assert _REGION_HEADER in r.output  # and the region block
        assert _REF_RE.search(_region_block(r.output))  # with parseable refs

    def test_region_ref_chains_a_followup_edit_without_reread(self, tmp_path):
        # The strongest guarantee: a ref taken straight from the region echo
        # resolves against the on-disk file, so a second edit needs no read.
        p = _write(tmp_path, "f.py", ["alpha", "beta", "gamma", "delta"])
        r1 = tool_edit_file(
            {"path": str(p), "op": "replace", "pos": _ref(2, "beta"), "lines": ["BETA"]}
        )
        assert r1.success
        block = _region_block(r1.output)
        # Find the fresh ref for the line we just wrote ("BETA").
        ref = None
        for m in _REF_RE.finditer(block):
            if f"{m.group(1)}#{m.group(2)}:BETA" in block:
                ref = f"{m.group(1)}#{m.group(2)}"
                break
        assert ref is not None, f"no fresh ref for edited line in:\n{block}"
        # Chain a second edit using ONLY that echoed ref — no re-read.
        r2 = tool_edit_file(
            {"path": str(p), "op": "replace", "pos": ref, "lines": ["BETA2"]}
        )
        assert r2.success, r2.error
        assert p.read_text().splitlines() == ["alpha", "BETA2", "gamma", "delta"]

    def test_region_line_numbers_are_absolute_after_shift(self, tmp_path):
        # Inserting lines shifts everything below; the echoed refs must carry
        # the true POST-edit line numbers, not pre-edit ones.
        p = _write(tmp_path, "f.txt", ["a", "b", "c", "d", "e", "f"])
        r = tool_edit_file(
            {"path": str(p), "op": "prepend", "pos": _ref(4, "d"), "lines": ["X", "Y"]}
        )
        assert r.success
        result_lines = p.read_text().split("\n")
        # Every echoed ref must verify against the actual resulting file.
        for m in _REF_RE.finditer(_region_block(r.output)):
            n = int(m.group(1))
            assert compute_line_hash(n, result_lines[n - 1]) == m.group(2)

    def test_batch_edit_emits_region(self, tmp_path):
        p = _write(tmp_path, "f.txt", ["a", "b", "c", "d", "e"])
        r = apply_edits_batch(
            str(p),
            [
                {"op": "replace", "pos": _ref(2, "b"), "lines": ["B"]},
                {"op": "replace", "pos": _ref(4, "d"), "lines": ["D"]},
            ],
        )
        assert r.success
        block = _region_block(r.output)
        assert block
        result_lines = p.read_text().split("\n")
        for m in _REF_RE.finditer(block):
            n = int(m.group(1))
            assert compute_line_hash(n, result_lines[n - 1]) == m.group(2)

    def test_no_op_edit_emits_no_region(self, tmp_path):
        # Replacing a line with itself changes nothing → no diff, no region.
        p = _write(tmp_path, "f.txt", ["a", "b", "c"])
        r = tool_edit_file(
            {"path": str(p), "op": "replace", "pos": _ref(2, "b"), "lines": ["b"]}
        )
        assert r.success
        assert _REGION_HEADER not in r.output


class TestRegionEchoHelper:
    def test_merges_adjacent_windows(self):
        # Two changes 2 lines apart fall within one another's context (±3),
        # so they collapse into a single contiguous block (no blank gap).
        orig = "\n".join(str(i) for i in range(20))
        new = orig.split("\n")
        new[8] = "EIGHT"
        new[10] = "TEN"
        echo = _updated_region_echo(orig, "\n".join(new))
        assert echo.count("\n\n") == 0  # single merged block, not two
        assert "EIGHT" in echo and "TEN" in echo

    def test_large_edit_is_truncated(self):
        # Rewriting far more than the cap must not dump every line back.
        orig = "\n".join(f"line{i}" for i in range(_MAX_REGION_LINES * 3))
        new = "\n".join(f"CHANGED{i}" for i in range(_MAX_REGION_LINES * 3))
        echo = _updated_region_echo(orig, new)
        emitted = len(_REF_RE.findall(echo))
        assert emitted <= _MAX_REGION_LINES
        assert _REGION_TRUNCATION_PREFIX in echo

    def test_empty_when_identical(self):
        assert _updated_region_echo("a\nb\nc", "a\nb\nc") == ""
