"""Shared post-mutation change echo (``_change_echo.render_change_echo``).

A file-mutating tool's observation should carry the same two things whenever
it shows a change: (1) a unified **diff** of WHAT changed, and (2) fresh
``LINE#HASH`` refs for the changed region so the model can chain a follow-up
``edit_file`` without a ``read_file`` round-trip. ``edit_file`` always emitted
both; ``write_file``'s small-overwrite branch emitted only the diff — the
asymmetry these tests pin closed by routing both tools through one helper.
"""

from __future__ import annotations

import re

from agent_cli.tools._change_echo import (
    _REGION_HEADER,
    render_change_echo,
)
from agent_cli.tools.read_file import compute_line_hash
from agent_cli.tools.write_file import tool_write_file

_REF_RE = re.compile(r"^(\d+)#([A-Z]{2}):", re.MULTILINE)


def _region_block(output: str) -> str:
    if _REGION_HEADER not in output:
        return ""
    return output.split(_REGION_HEADER, 1)[1]


class TestRenderChangeEcho:
    def test_diff_and_region_present_on_change(self):
        old = "a\nb\nc\n"
        new = "a\nB\nc\n"
        echo = render_change_echo(old, new, "f.txt")
        assert "@@" in echo  # unified diff present
        assert _REGION_HEADER in echo  # region block present
        assert _REF_RE.search(_region_block(echo))  # with parseable refs

    def test_empty_when_identical(self):
        assert render_change_echo("a\nb\n", "a\nb\n", "f.txt") == ""

    def test_diff_precedes_region(self):
        echo = render_change_echo("a\nb\nc\n", "a\nB\nc\n", "f.txt")
        assert echo.index("@@") < echo.index(_REGION_HEADER)


class TestWriteFileUnification:
    """The fix: write_file's SMALL-overwrite branch now emits the changed-region
    hashlines alongside the diff, matching edit_file (previously diff-only)."""

    def _seed(self, tmp_path):
        # 10-line file; changing 1 line = 10% < 30% → small-overwrite branch.
        p = tmp_path / "mod.py"
        p.write_text("\n".join(f"line{i}" for i in range(10)) + "\n")
        return p

    def test_small_overwrite_emits_diff_and_region(self, tmp_path):
        p = self._seed(tmp_path)
        new = (
            "\n".join(["line0", "CHANGED", *[f"line{i}" for i in range(2, 10)]]) + "\n"
        )
        r = tool_write_file({"path": str(p), "content": new})
        assert r.success
        assert "@@" in r.output  # diff still there
        assert _REGION_HEADER in r.output  # region hashlines now present too
        assert _REF_RE.search(_region_block(r.output))

    def test_small_overwrite_region_ref_chains_edit_without_reread(self, tmp_path):
        # A ref pulled straight from write_file's region echo must resolve for a
        # follow-up edit_file — the whole point of the unification.
        from agent_cli.tools.edit_file import tool_edit_file

        p = self._seed(tmp_path)
        new = (
            "\n".join(["line0", "CHANGED", *[f"line{i}" for i in range(2, 10)]]) + "\n"
        )
        r = tool_write_file({"path": str(p), "content": new})
        block = _region_block(r.output)
        ref = None
        for m in _REF_RE.finditer(block):
            if f"{m.group(1)}#{m.group(2)}:CHANGED" in block:
                ref = f"{m.group(1)}#{m.group(2)}"
                break
        assert ref is not None, f"no fresh ref for changed line in:\n{block}"
        r2 = tool_edit_file(
            {"path": str(p), "op": "replace", "pos": ref, "lines": ["CHANGED2"]}
        )
        assert r2.success, r2.error
        assert "CHANGED2" in p.read_text()

    def test_region_refs_are_absolute_post_write(self, tmp_path):
        p = self._seed(tmp_path)
        new = (
            "\n".join(["line0", "CHANGED", *[f"line{i}" for i in range(2, 10)]]) + "\n"
        )
        r = tool_write_file({"path": str(p), "content": new})
        result_lines = p.read_text().split("\n")
        for m in _REF_RE.finditer(_region_block(r.output)):
            n = int(m.group(1))
            assert compute_line_hash(n, result_lines[n - 1]) == m.group(2)

    def test_full_rewrite_still_full_hashlines_no_diff(self, tmp_path):
        # Genuine rewrite (≥30% changed) keeps the full-file hashline echo and
        # shows NO diff — unchanged behaviour (nothing to diff for a rewrite).
        p = self._seed(tmp_path)
        new = "\n".join(f"NEW{i}" for i in range(10)) + "\n"
        r = tool_write_file({"path": str(p), "content": new})
        assert r.success
        assert "@@" not in r.output
        assert _REGION_HEADER not in r.output
        assert _REF_RE.search(r.output)  # full-file hashlines instead
