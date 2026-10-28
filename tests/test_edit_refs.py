"""edit_file 의 ref 검증과 단건 편집 의미 (tools/edit_file).

edit_file is flat-native (consolidation roadmap Step 3): one op = one edit.
v10.48.0: a lone op is a batch of one through ``apply_edits_batch_parts`` (the
same core a same-file run uses), and fuzzy ref matching is gone — a ref is
either exact or a hash mismatch the model recovers from by re-reading.
"""

import pytest

from agent_cli.tools.edit_file import tool_edit_file
from agent_cli.tools.read_file import _parse_ref, compute_line_hash


def _file(tmp_path, text):
    f = tmp_path / "f.py"
    f.write_text(text)
    return f


class TestRefVerification:
    def test_exact_match_applies(self, tmp_path):
        f = _file(tmp_path, "def hello():\n    pass")
        h = compute_line_hash(1, "def hello():")
        r = tool_edit_file(
            {"path": str(f), "op": "replace", "pos": f"1#{h}", "lines": ["def hi():"]}
        )
        assert r.success and f.read_text() == "def hi():\n    pass"

    def test_stale_hash_is_rejected_with_the_current_lines(self, tmp_path):
        f = _file(tmp_path, "def hello():")
        r = tool_edit_file(
            {"path": str(f), "op": "replace", "pos": "1#ZZ", "lines": ["x"]}
        )
        assert not r.success and f.read_text() == "def hello():"
        assert r.error == (
            "edit_file apply failed: Hash mismatch at line 1: ref '1#ZZ' does "
            "not match current content.\n"
            "Current lines 1–1 (fresh refs — retry from these, no re-read "
            f"needed):\n1#{compute_line_hash(1, 'def hello():')}:def hello():"
        )
        assert "batch" not in r.error


class TestMismatchWindow:
    """v10.50.0: a hash mismatch shows ±3 current lines with fresh refs. Replay
    of recorded slips: the old re-read hint was obeyed 50/50 (a read turn per
    slip); ±3 got 41/50 direct retries, ±2 once edited a neighbour."""

    def _body(self, n):
        return "\n".join(f"v{i} = {i}" for i in range(1, n + 1))

    def _window(self, err):
        return err.split("needed):\n", 1)[1].splitlines()

    def test_three_lines_each_side_with_valid_refs(self, tmp_path):
        f = _file(tmp_path, self._body(20))
        r = tool_edit_file({"path": str(f), "op": "replace", "pos": "10#ZZ"})
        assert "Current lines 7–13 " in r.error
        shown = self._window(r.error)
        assert [s.split("#")[0] for s in shown] == [str(i) for i in range(7, 14)]
        for i, s in zip(range(7, 14), shown):  # every shown ref is usable as-is
            assert s == f"{i}#{compute_line_hash(i, f'v{i} = {i}')}:v{i} = {i}"

    @pytest.mark.parametrize("line,span", [(1, "1–4"), (2, "1–5"), (20, "17–20")])
    def test_clamped_at_the_file_edges(self, tmp_path, line, span):
        f = _file(tmp_path, self._body(20))
        r = tool_edit_file({"path": str(f), "op": "replace", "pos": f"{line}#ZZ"})
        assert f"Current lines {span} " in r.error

    def test_a_retry_from_the_window_applies(self, tmp_path):
        f = _file(tmp_path, self._body(9))
        r = tool_edit_file(
            {"path": str(f), "op": "replace", "pos": "5#ZZ", "lines": ["x"]}
        )
        ref5 = next(s for s in self._window(r.error) if s.startswith("5#"))
        ok = tool_edit_file(
            {"path": str(f), "op": "replace", "pos": ref5.split(":")[0], "lines": ["x"]}
        )
        assert ok.success and f.read_text().split("\n")[4] == "x"

    def test_no_did_you_mean_for_a_matching_tag_nearby(self, tmp_path):
        """The tag's line is the wrong guess as often as not (the model gets the
        number right and the tag wrong) — replay showed it misleads."""
        f = _file(tmp_path, self._body(9))
        tag4 = compute_line_hash(4, "v4 = 4")
        r = tool_edit_file({"path": str(f), "op": "replace", "pos": f"5#{tag4}"})
        assert "did you mean" not in r.error.lower()

    def test_range_and_format_errors_keep_their_wording(self, tmp_path):
        f = _file(tmp_path, self._body(3))
        r = tool_edit_file({"path": str(f), "op": "replace", "pos": "9#ZZ"})
        assert "out of range" in r.error and "Current lines" not in r.error
        r = tool_edit_file({"path": str(f), "op": "replace", "pos": "#ZZ"})
        assert "Invalid hashline ref" in r.error and "Current lines" not in r.error

    def test_out_of_range_is_rejected(self, tmp_path):
        f = _file(tmp_path, "only one line")
        r = tool_edit_file(
            {"path": str(f), "op": "replace", "pos": "5#ZZ", "lines": ["x"]}
        )
        assert not r.success and "out of range" in r.error

    def test_whitespace_drift_is_not_tolerated_anymore(self, tmp_path):
        """v10.48.0: 퍼지 매칭 제거 — 공백만 달라진 줄의 옛 ref 도 불일치다
        (종전엔 '정규화한 현재 줄' 이 옛 해시와 맞으면 통과했다, 실측 0회)."""
        f = _file(tmp_path, "a  b")  # 읽은 뒤 누가 공백을 늘렸다
        old_ref = f"1#{compute_line_hash(1, 'a b')}"
        r = tool_edit_file(
            {"path": str(f), "op": "replace", "pos": old_ref, "lines": ["c"]}
        )
        assert not r.success and f.read_text() == "a  b"

    def test_single_edit_message_has_no_batch_count(self, tmp_path):
        f = _file(tmp_path, "a\nb")
        h = compute_line_hash(2, "b")
        r = tool_edit_file(
            {"path": str(f), "op": "replace", "pos": f"2#{h}", "lines": ["B"]}
        )
        assert r.output.startswith(f"Edit complete: {f} (2 lines)")
        assert "edits," not in r.output and "Fuzzy" not in r.output


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


class TestParseRefPastedLine:
    """A whole ``read_file`` line pasted as the ref (Qwen3.6-35B, Harbor
    financial-document-processor: ``pos: "67#YB:def extract_total(text):"``,
    twice, each rejected with "Invalid hashline ref"). The tag before the
    colon is unambiguously the ref the model meant, so it is accepted;
    a multi-line paste is not (which line was meant?) and still errors.
    """

    @pytest.mark.parametrize(
        "ref",
        [
            "67#YB:def extract_total(text):",
            "103#WW:    if total_candidates:",
            "5#VR:",
            "5#VR:a:b: c",
        ],
    )
    def test_pasted_line_yields_tag(self, ref):
        line, h = _parse_ref(ref)
        assert (line, h) == (int(ref.split("#")[0]), ref.split("#")[1][:2])

    @pytest.mark.parametrize(
        "bad_ref",
        [
            "67#YB def extract_total(text):",  # no colon separator
            "67#YB:def a():\n68#MQ:    pass",  # two pasted lines
            "#YB:def a():",
            "67:def a():",
        ],
    )
    def test_other_shapes_still_rejected(self, bad_ref):
        with pytest.raises(RuntimeError, match="Expected format: LINE#HASH"):
            _parse_ref(bad_ref)

    def test_edit_file_applies_pasted_ref(self, tmp_path):
        f = tmp_path / "x.py"
        f.write_text("def a():\n    return 1\n")
        tag = compute_line_hash(2, "    return 1")
        result = tool_edit_file(
            {
                "path": str(f),
                "op": "replace",
                "pos": f"2#{tag}:    return 1",
                "lines": ["    return 2"],
            }
        )
        assert result.success is True, result.error
        assert f.read_text() == "def a():\n    return 2\n"


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
        # v10.77.0: 정수 줄 번호는 적용 단계까지 가서 "줄 번호만" 오류가 된다
        assert "pos '5' is a bare line number" in result.error
        assert "has 3 lines" in result.error  # 끝을 넘은 줄

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
        assert "end '7' is a bare line number" in result.error

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
        # v10.77.0: the fix is in the message itself — the line's current ref.
        assert "LINE#HASH" in msg
        assert "That line is now:\n1#" in msg and "Use '1#" in msg


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


class TestBareLineNumberRefs:
    """v10.77.0 — 줄 번호만 온 ref(35B 최근 런 편집의 18%)는 구제하지 않고, 그 줄의
    지금 ref 를 보여 준다. 해시 검증은 그대로다."""

    def _file(self, tmp_path):
        f = tmp_path / "app.py"
        f.write_text("a\nb\nc\nd\n")
        return f

    def test_end_bare_shows_that_lines_ref(self, tmp_path):
        from agent_cli.tools.read_file import format_hashlines

        f = self._file(tmp_path)
        tags = format_hashlines(f.read_text()).split("\n")
        r = tool_edit_file(
            {
                "path": str(f),
                "op": "replace",
                "pos": tags[0].split(":")[0],
                "end": "3",
                "lines": ["x"],
            }
        )
        assert r.error == (
            "edit_file apply failed: end '3' is a bare line number — a ref is "
            f"LINE#HASH. That line is now:\n{tags[2]}\nUse '{tags[2].split(':')[0]}'."
        )
        assert f.read_text() == "a\nb\nc\nd\n"  # 아무것도 쓰지 않았다

    def test_both_bare_and_past_the_end(self, tmp_path):
        f = self._file(tmp_path)
        r = tool_edit_file({"path": str(f), "op": "delete", "pos": "2", "end": "30"})
        assert "pos '2' and end '30' are bare line numbers" in r.error
        assert "That line is now:\n2#" in r.error
        assert f"Line 30 is past the end — {f} has 5 lines." in r.error

    def test_unknown_op_comes_first(self, tmp_path):
        f = self._file(tmp_path)
        r = tool_edit_file({"path": str(f), "op": "bogus", "pos": "2", "lines": []})
        assert "Unknown edit op" in r.error
