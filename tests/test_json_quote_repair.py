"""Generalized missing-quote repair — a string value/key missing ONE quote
(open or close) anywhere in the JSON.

Covers the pure util (``repair_value_quotes``), md_array's recovery stage
(``_extract_op_json`` + ``parse_turn`` end-to-end), composition with the
bracket-close repair, and the bail-to-retry boundary (a wrong guess must NOT
force a bogus op — notably bare keywords like ``true`` are left alone).
"""

import json

from agent_cli.wire_formats._json_repair import repair_value_quotes
from agent_cli.wire_formats.md_array import _extract_op_json
import agent_cli.wire_formats as wf_mod


def _action(body: str) -> str:
    return f"## Thought\nx\n\n## Action\n{body}"


class TestRepairValueQuotesUtil:
    def test_missing_open_quote(self):
        # "path": mgt.c"  → "path": "mgt.c"
        fixed, changed = repair_value_quotes(
            '[{"action": "read_file", "path": mgt.c"}]'
        )
        assert changed
        assert json.loads(fixed) == [{"action": "read_file", "path": "mgt.c"}]

    def test_missing_close_quote(self):
        # "path": "mgt.c}  → "path": "mgt.c"}
        fixed, changed = repair_value_quotes(
            '[{"action": "read_file", "path": "mgt.c}]'
        )
        assert changed
        assert json.loads(fixed) == [{"action": "read_file", "path": "mgt.c"}]

    def test_multiple_broken_values_one_pass(self):
        # two ops, one front-missing + one back-missing → both fixed
        fixed, changed = repair_value_quotes(
            '[{"action":"read_file","path":a.c"},{"action":"read_file","path":"b.c}]'
        )
        assert changed
        assert json.loads(fixed) == [
            {"action": "read_file", "path": "a.c"},
            {"action": "read_file", "path": "b.c"},
        ]

    def test_valid_input_unchanged(self):
        text = '[{"action": "read_file", "path": "mgt.c"}]'
        fixed, changed = repair_value_quotes(text)
        assert not changed
        assert fixed == text

    def test_bare_keyword_not_misquoted(self):
        # A genuine bare token (typo of a keyword / number) carries NO stray
        # quote → must NOT be wrapped into a string. Bail → caller retries.
        _fixed, changed = repair_value_quotes('[{"action":"complete","ok": ture}]')
        assert not changed

    def test_no_json_start_unchanged(self):
        fixed, changed = repair_value_quotes("not json at all")
        assert not changed and fixed == "not json at all"

    def test_truncated_string_to_eof_bails(self):
        # An unterminated string running to EOF with NO delimiter is genuine
        # truncation (output cut mid-value), not a missing close quote — must
        # NOT be force-closed into a bogus op.
        _fixed, changed = repair_value_quotes(
            '[{"action": "read_file", "path": "/some/very/long/pa'
        )
        assert not changed


class TestMdArrayRecovery:
    def test_extract_op_json_recovers_missing_open(self):
        parsed, repaired = _extract_op_json('[{"action": "read_file", "path": mgt.c"}]')
        assert repaired
        assert parsed == [{"action": "read_file", "path": "mgt.c"}]

    def test_extract_op_json_recovers_missing_close(self):
        parsed, repaired = _extract_op_json('[{"action": "read_file", "path": "mgt.c}]')
        assert repaired
        assert parsed == [{"action": "read_file", "path": "mgt.c"}]

    def test_composes_with_bracket_close(self):
        # quote-broken AND unclosed array → both repairs compose
        parsed, repaired = _extract_op_json('[{"action": "read_file", "path": mgt.c"}')
        assert repaired
        assert parsed == [{"action": "read_file", "path": "mgt.c"}]

    def test_parse_turn_end_to_end(self):
        wf = wf_mod.get("md_array")
        turn = wf.parse_turn(_action('[{"action": "read_file", "path": mgt.c"}]'))
        assert turn.parse_stage == 2  # recovered via repair
        assert [(o.action, o.action_input) for o in turn.ops] == [
            ("read_file", {"path": "mgt.c"})
        ]

    def test_parse_turn_valid_stays_stage_1(self):
        wf = wf_mod.get("md_array")
        turn = wf.parse_turn(_action('[{"action": "read_file", "path": "mgt.c"}]'))
        assert turn.parse_stage == 1  # no repair needed

    def test_unrepairable_stays_no_json(self):
        # bare keyword typo isn't a missing-quote case → NO_JSON, model retries
        wf = wf_mod.get("md_array")
        turn = wf.parse_turn(_action('[{"action":"complete","ok": ture}]'))
        assert turn.parse_stage == 0
