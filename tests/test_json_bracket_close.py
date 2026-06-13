"""Conservative EOF bracket-close repair — recovers the dominant real
NO_JSON shape (a multi-op array the model finished but forgot to close).

Covers the pure util (``close_unbalanced``), md_array's recovery stage
(incl. a real captured-failure fixture), the bail-to-retry boundary, and
react/md_array parity via the shared util.
"""

import json

from agent_cli.wire_formats._json_repair import close_unbalanced
from agent_cli.wire_formats.md_array import _extract_op_json
from agent_cli.wire_formats.react import _fix_missing_brackets

# The captured shape: a 6-op read_file batch the model emitted in full but
# never closed (session 1781336790, delegate_explorer_b763fb). Trimmed paths.
_REAL_UNCLOSED_ARRAY = (
    '[{"action": "read_file", "path": "a/__init__.py"}, '
    '{"action": "read_file", "path": "a/compaction.py", "stat": true}, '
    '{"action": "read_file", "path": "a/overflow.py", "stat": true}, '
    '{"action": "read_file", "path": "a/render.py"}, '
    '{"action": "read_file", "path": "a/models.py"}, '
    '{"action": "read_file", "path": "a/resource_loader.py"}'
)  # ← no trailing ]


class TestCloseUnbalanced:
    def test_appends_missing_array_close(self):
        fixed, changed = close_unbalanced('[{"a": 1}')
        assert changed
        assert json.loads(fixed) == [{"a": 1}]

    def test_appends_nested_closers_in_order(self):
        fixed, changed = close_unbalanced('[{"a": [1, 2')
        assert changed
        assert fixed.endswith("]}]")  # close ] then } then ]
        assert json.loads(fixed) == [{"a": [1, 2]}]

    def test_balanced_input_unchanged(self):
        fixed, changed = close_unbalanced('[{"a": 1}]')
        assert not changed
        assert fixed == '[{"a": 1}]'

    def test_brackets_inside_strings_are_not_counted(self):
        # the `[` / `{` live in a string value — must not be treated as opens
        src = '{"cmd": "arr[0] = {x}"}'
        fixed, changed = close_unbalanced(src)
        assert not changed
        assert fixed == src

    def test_escaped_quote_does_not_break_string_tracking(self):
        src = '[{"s": "he said \\"hi\\""}'  # unclosed array, escaped quotes
        fixed, changed = close_unbalanced(src)
        assert changed
        assert json.loads(fixed) == [{"s": 'he said "hi"'}]

    def test_extra_closer_is_not_removed(self):
        # only appends; never deletes — an extra ] stays (caller bails)
        fixed, changed = close_unbalanced('[{"a": 1}]]')
        assert not changed


class TestMdArrayRecovery:
    def test_real_captured_unclosed_array_recovers_all_ops(self):
        parsed, repaired = _extract_op_json(_REAL_UNCLOSED_ARRAY)
        assert repaired is True
        assert isinstance(parsed, list)
        assert len(parsed) == 6  # all six read_file ops preserved
        assert all(op["action"] == "read_file" for op in parsed)

    def test_closed_array_is_not_marked_repaired(self):
        parsed, repaired = _extract_op_json(_REAL_UNCLOSED_ARRAY + "]")
        assert repaired is False
        assert len(parsed) == 6

    def test_truncated_mid_string_bails_to_none(self):
        # a genuinely truncated op (unterminated string) cannot be fixed by
        # bracket-closing alone → stays None so the loop falls back to retry
        truncated = '[{"action": "read_file", "path": "/some/very/long/pa'
        parsed, repaired = _extract_op_json(truncated)
        assert parsed is None

    def test_garbage_with_no_brackets_still_none(self):
        parsed, _ = _extract_op_json("not json and no brackets at all")
        assert parsed is None


class TestParity:
    def test_react_alias_matches_shared_util(self):
        # react's _fix_missing_brackets is now a thin alias — identical output
        for src in ('[{"a": 1}', '{"x": [1, 2', "[]", '{"ok": true}'):
            assert _fix_missing_brackets(src) == close_unbalanced(src)
