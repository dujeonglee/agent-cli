"""Tests for fuzzy edit matching in tools/edit_file.

edit_file is flat-native (consolidation roadmap Step 3): one op = one edit,
no `edits` batch array. The former multi-edit ambiguity machinery
(duplicate-ref / range-overlap pre-validation, bottom-up sort) was removed
with the array — several edits to one file are now several edit_file ops
across turns (a ref goes stale once an earlier edit shifts its line).
"""

import pytest

from agent_cli.tools.edit_file import (
    _normalize_for_fuzzy,
    fuzzy_verify_ref,
    tool_edit_file,
)
from agent_cli.tools.read_file import _parse_ref, compute_line_hash


class TestNormalize:
    def test_tabs_to_spaces(self):
        assert _normalize_for_fuzzy("\thello") == "hello"

    def test_collapse_spaces(self):
        assert _normalize_for_fuzzy("a   b") == "a b"

    def test_smart_quotes(self):
        assert _normalize_for_fuzzy("“hello”") == '"hello"'

    def test_em_dash(self):
        assert _normalize_for_fuzzy("a—b") == "a-b"


class TestFuzzyVerifyRef:
    def test_exact_match(self):
        lines = ["def hello():", "    pass"]
        h = compute_line_hash(1, lines[0])
        idx, was_fuzzy = fuzzy_verify_ref(lines, f"1#{h}")
        assert idx == 0
        assert was_fuzzy is False

    def test_fuzzy_match_stale_hash_raises(self):
        """When hash doesn't match, fuzzy raises with re-read guidance."""
        lines = ["def hello():"]
        # Use a wrong hash — should raise with guidance to re-read
        with pytest.raises(RuntimeError, match="Re-read the file"):
            fuzzy_verify_ref(lines, "1#ZZ")

    def test_out_of_range_raises(self):
        lines = ["only one line"]
        with pytest.raises(RuntimeError):
            fuzzy_verify_ref(lines, "5#ZZ")


class TestHashMismatchMessage:
    """fuzzy_verify_ref's error must point the caller at the recovery
    recipe (re-read to get fresh hashline tags)."""

    def test_message_mentions_reread(self):
        with pytest.raises(RuntimeError, match="Re-read"):
            fuzzy_verify_ref(["hello"], "1#ZZ")

    def test_message_mentions_read_file_recipe(self):
        with pytest.raises(RuntimeError, match="read_file"):
            fuzzy_verify_ref(["hello"], "1#ZZ")


class TestParseRefTypeGuard:
    """``_parse_ref`` was previously letting non-string ``ref`` reach
    ``re.match`` and crash with ``TypeError: expected string or
    bytes-like object`` straight out of ``re.py``. That escaped the
    worker thread instead of surfacing as an Observation the LLM
    could retry from. Contract: any non-string input raises a
    ``RuntimeError`` with the standard ``LINE#HASH`` hint, so callers
    catch it the same way as a malformed string.

    Each parametrize entry pins one ``type``-flavour the LLM has been
    observed to produce. ``None`` is exempt: callers gate on
    ``if pos:`` before reaching ``_parse_ref``, and the falsy guard
    means ``None`` never gets here. The list / dict / bool cases
    matter because ``[None]`` and ``True`` are *truthy* — they slip
    past the falsy guard and would otherwise hit ``re.match``.
    """

    @pytest.mark.parametrize(
        "bad_ref",
        [
            5,
            [],
            ["5#VR"],
            {"line": 5, "hash": "VR"},
            True,
            False,
        ],
    )
    def test_non_string_raises_runtime_error(self, bad_ref):
        with pytest.raises(RuntimeError) as exc:
            _parse_ref(bad_ref)
        # Message must point the LLM at the right shape AND the
        # retry recipe (re-read the file). Without "read_file" in
        # the text the model wouldn't know how to recover.
        msg = str(exc.value)
        assert "Expected format" in msg or "expected string" in msg
        assert "read_file" in msg

    def test_valid_string_still_works(self):
        # Sanity — the guard must not regress the happy path.
        line, h = _parse_ref("5#VR")
        assert line == 5 and h == "VR"


class TestEditFileFieldTypeValidation:
    """The user-reported crash: an LLM (typically a smaller model that's
    not careful with JSON typing) sent ``pos: 5`` instead of ``pos: "5#VR"``
    to ``edit_file``. The ``if pos:`` guard passed (``5`` is truthy), the
    value reached ``_parse_ref``, ``re.match`` raised ``TypeError`` from
    inside ``re.py``, and the worker thread died with a stack trace the
    model couldn't see, never mind retry from.

    The fix: validate ``pos`` / ``end`` types up front and return a
    ``ToolResult`` error. That surfaces as an Observation, the LLM re-reads
    the file, and the next attempt has the right hashline shape.
    """

    def test_pos_as_int_returns_tool_result_error_not_crash(self, tmp_path):
        # The exact shape from the user's traceback.
        f = tmp_path / "x.py"
        f.write_text("line1\nline2\n")
        result = tool_edit_file(
            {"path": str(f), "op": "replace", "pos": 5, "lines": ["new"]}
        )
        assert result.success is False
        assert "pos" in result.error
        assert "read_file" in result.error

    def test_end_as_int_also_caught(self, tmp_path):
        f = tmp_path / "x.py"
        f.write_text("line1\nline2\n")
        result = tool_edit_file(
            {
                "path": str(f),
                "op": "replace",
                "pos": "1#AA",  # valid shape, ignored — end caught first
                "end": 7,
                "lines": ["new"],
            }
        )
        assert result.success is False
        assert "end" in result.error
        assert "read_file" in result.error

    def test_pos_as_list_caught(self, tmp_path):
        # ``[None]`` is truthy so it slips past the ``if pos:`` guard.
        f = tmp_path / "x.py"
        f.write_text("line1\n")
        result = tool_edit_file(
            {"path": str(f), "op": "replace", "pos": [None], "lines": ["x"]}
        )
        assert result.success is False
        assert "pos" in result.error

    def test_pos_as_dict_caught(self, tmp_path):
        f = tmp_path / "x.py"
        f.write_text("line1\n")
        result = tool_edit_file(
            {
                "path": str(f),
                "op": "replace",
                "pos": {"line": 1, "hash": "AA"},
                "lines": ["x"],
            }
        )
        assert result.success is False
        assert "pos" in result.error

    def test_pos_as_bool_caught(self, tmp_path):
        # ``True`` is truthy. Pre-validation catches it explicitly.
        f = tmp_path / "x.py"
        f.write_text("line1\n")
        result = tool_edit_file(
            {"path": str(f), "op": "replace", "pos": True, "lines": ["x"]}
        )
        assert result.success is False

    def test_none_pos_still_allowed_for_append_to_eof(self, tmp_path):
        # ``op=append`` without ``pos`` means "append to end of file".
        # The type guard must skip the check when the field is missing.
        f = tmp_path / "x.py"
        f.write_text("line1\n")
        result = tool_edit_file({"path": str(f), "op": "append", "lines": ["appended"]})
        assert result.success is True
        assert "appended" in f.read_text()

    def test_error_message_is_observation_friendly(self, tmp_path):
        # The full LLM-facing chain: tool returns ToolResult(False,
        # error=msg) → loop wraps it as ``Observation: <msg>`` → LLM
        # sees it next turn. Pin the vocabulary the prompt teaches.
        f = tmp_path / "x.py"
        f.write_text("line1\n")
        result = tool_edit_file(
            {"path": str(f), "op": "replace", "pos": 1, "lines": ["x"]}
        )
        assert result.success is False
        msg = result.error
        # The fix-recipe ("re-read with read_file") must be there.
        assert "read_file" in msg
        # And the expected shape, so a careful model can self-correct.
        assert "5#VR" in msg or "LINE#HASH" in msg or "hashline" in msg


class TestEditOps:
    """One op = one edit. replace / append / prepend / delete against the
    file's last-read state."""

    def _write(self, tmp_path, lines):
        path = tmp_path / "f.c"
        path.write_text("\n".join(lines))
        return path

    def test_replace_single_line(self, tmp_path):
        lines = ["l1", "l2", "l3"]
        path = self._write(tmp_path, lines)
        h = compute_line_hash(2, lines[1])
        result = tool_edit_file(
            {"path": str(path), "op": "replace", "pos": f"2#{h}", "lines": ["X"]}
        )
        assert result.success, result.error
        assert path.read_text().splitlines() == ["l1", "X", "l3"]

    def test_replace_range(self, tmp_path):
        lines = ["a", "b", "c", "d"]
        path = self._write(tmp_path, lines)
        h2 = compute_line_hash(2, lines[1])
        h3 = compute_line_hash(3, lines[2])
        result = tool_edit_file(
            {
                "path": str(path),
                "op": "replace",
                "pos": f"2#{h2}",
                "end": f"3#{h3}",
                "lines": ["X"],
            }
        )
        assert result.success, result.error
        assert path.read_text().splitlines() == ["a", "X", "d"]

    def test_single_edit_pos_equals_end_allowed(self, tmp_path):
        """pos==end is a degenerate single-line range — must work."""
        lines = ["l1", "l2", "l3"]
        path = self._write(tmp_path, lines)
        h = compute_line_hash(2, lines[1])
        result = tool_edit_file(
            {
                "path": str(path),
                "op": "replace",
                "pos": f"2#{h}",
                "end": f"2#{h}",
                "lines": ["X"],
            }
        )
        assert result.success, result.error
        assert path.read_text().splitlines() == ["l1", "X", "l3"]


class TestDeleteOp:
    """``op=delete`` removes the pos..end range. ``lines`` is not part of
    delete's schema, so any value it carries is ignored; the result equals
    the legacy ``replace`` + ``lines=[]`` form."""

    def _write(self, tmp_path, lines):
        path = tmp_path / "f.c"
        path.write_text("\n".join(lines))
        return path

    def test_delete_single_line(self, tmp_path):
        lines = ["a", "b", "c"]
        path = self._write(tmp_path, lines)
        h = compute_line_hash(2, lines[1])
        result = tool_edit_file({"path": str(path), "op": "delete", "pos": f"2#{h}"})
        assert result.success
        assert path.read_text() == "a\nc"

    def test_delete_range(self, tmp_path):
        lines = ["a", "b", "c", "d"]
        path = self._write(tmp_path, lines)
        h2 = compute_line_hash(2, lines[1])
        h3 = compute_line_hash(3, lines[2])
        result = tool_edit_file(
            {
                "path": str(path),
                "op": "delete",
                "pos": f"2#{h2}",
                "end": f"3#{h3}",
            }
        )
        assert result.success
        assert path.read_text() == "a\nd"

    def test_delete_without_pos_errors(self, tmp_path):
        path = self._write(tmp_path, ["a", "b"])
        result = tool_edit_file({"path": str(path), "op": "delete"})
        assert not result.success
        assert "pos" in result.error

    def test_delete_equals_legacy_replace_empty(self, tmp_path):
        # delete and replace+lines=[] must produce identical output.
        lines = ["a", "b", "c"]
        h = compute_line_hash(2, lines[1])
        p_del = tmp_path / "del.c"
        p_del.write_text("\n".join(lines))
        p_rep = tmp_path / "rep.c"
        p_rep.write_text("\n".join(lines))
        r_del = tool_edit_file({"path": str(p_del), "op": "delete", "pos": f"2#{h}"})
        r_rep = tool_edit_file(
            {"path": str(p_rep), "op": "replace", "pos": f"2#{h}", "lines": []}
        )
        assert r_del.success and r_rep.success
        assert p_del.read_text() == p_rep.read_text() == "a\nc"

    def test_delete_ignores_lines(self, tmp_path):
        # lines is not in delete's schema — supplying it must not insert text.
        lines = ["a", "b", "c"]
        path = self._write(tmp_path, lines)
        h = compute_line_hash(2, lines[1])
        result = tool_edit_file(
            {"path": str(path), "op": "delete", "pos": f"2#{h}", "lines": ["IGNORED"]}
        )
        assert result.success
        assert path.read_text() == "a\nc"

    def test_op_key_order_independent(self, tmp_path):
        # op may appear after other params — looked up by key, not position.
        lines = ["a", "b", "c"]
        path = self._write(tmp_path, lines)
        h = compute_line_hash(2, lines[1])
        result = tool_edit_file({"path": str(path), "pos": f"2#{h}", "op": "delete"})
        assert result.success
        assert path.read_text() == "a\nc"

    def test_unknown_op_error_lists_delete(self, tmp_path):
        path = self._write(tmp_path, ["a", "b"])
        h = compute_line_hash(1, "a")
        result = tool_edit_file({"path": str(path), "op": "destroy", "pos": f"1#{h}"})
        assert not result.success
        assert "destroy" in result.error
        assert "delete" in result.error
