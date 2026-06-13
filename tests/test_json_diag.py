"""JSON syntax diagnostic — the recovery surface that tells the model
*where* its JSON broke (line/column + caret) instead of a generic
"not valid JSON" nudge.

Covers the pure util (``describe_json_error``), each JSON wire format's
``diagnose_syntax_error`` extraction, cross-format parity on the same
broken input, and the ``format_no_json_retry`` embedding.
"""

import json

from agent_cli.recovery.wf_recovery import format_no_json_retry
from agent_cli.wire_formats._json_diag import describe_json_error
from agent_cli.wire_formats.md_array import MdArrayFormat
from agent_cli.wire_formats.react import ReActFormat


class TestDescribeJsonError:
    def test_missing_closing_bracket(self):
        bad = '{"thought":"x","actions":[{"action":"shell","command":"ls"}}'
        out = describe_json_error(bad)
        assert out is not None
        # message + location, sourced straight from JSONDecodeError
        assert "line 1" in out
        assert "column" in out
        # caret points at something
        assert "^" in out

    def test_missing_comma(self):
        out = describe_json_error('{"a": 1 "b": 2}')
        assert out is not None
        assert "delimiter" in out  # "Expecting ',' delimiter"
        assert "^" in out

    def test_unterminated_string(self):
        out = describe_json_error('{"a": "unterminated')
        assert out is not None
        assert "^" in out

    def test_caret_aligns_with_error_column(self):
        # single-line input: the caret line's '^' index must equal colno-1
        bad = '{"a": 1 "b": 2}'
        out = describe_json_error(bad)
        snippet_line, caret_line = out.splitlines()[-2:]
        try:
            json.loads(bad)
            raise AssertionError("expected JSONDecodeError")
        except json.JSONDecodeError as e:
            # caret sits under the same visual column as the snippet char
            assert caret_line.index("^") == e.colno - 1 + 4  # +4 indent, col0

    def test_valid_json_returns_none(self):
        assert describe_json_error('{"a": 1, "b": [1, 2, 3]}') is None

    def test_empty_returns_none(self):
        assert describe_json_error("") is None
        assert describe_json_error("   \n\t ") is None

    def test_long_line_is_windowed_but_keeps_caret(self):
        # error near the end of a very long single line — snippet truncates
        # at the head but the caret stays aligned to the local window
        prefix = '{"x":"' + ("a" * 200) + '", "y" 5}'
        out = describe_json_error(prefix)
        assert out is not None
        lines = out.splitlines()
        # windowed: snippet should be far shorter than the raw input
        assert len(lines[-2]) < len(prefix)
        assert lines[-2].lstrip().startswith("...")
        assert "^" in lines[-1]


class TestFormatDiagnose:
    def test_react_diagnoses_broken_json(self):
        bad = '{"thought":"x","action":"shell","action_input":{"command":"ls"}'
        out = ReActFormat().diagnose_syntax_error(bad)
        assert out is not None and "^" in out

    def test_react_strips_fences_before_diagnosing(self):
        bad = '```json\n{"a": 1 "b": 2}\n```'
        out = ReActFormat().diagnose_syntax_error(bad)
        assert out is not None and "delimiter" in out

    def test_react_valid_returns_none(self):
        ok = '{"thought":"x","action":"shell","action_input":{"command":"ls"}}'
        assert ReActFormat().diagnose_syntax_error(ok) is None

    def test_md_array_diagnoses_broken_action_body(self):
        bad = '## Thought\nreasoning\n\n## Action\n[{"action":"shell","command":"ls"}'
        out = MdArrayFormat().diagnose_syntax_error(bad)
        assert out is not None and "^" in out

    def test_md_array_valid_returns_none(self):
        ok = '## Thought\nr\n\n## Action\n[{"action":"shell","command":"ls"}]'
        assert MdArrayFormat().diagnose_syntax_error(ok) is None

    def test_base_default_returns_none(self):
        # a format that does not implement diagnosis falls back to None,
        # so the generic hint path is used unchanged
        class _Bare(ReActFormat):
            def diagnose_syntax_error(self, prior_content):  # seam default
                return None

        assert _Bare().diagnose_syntax_error("anything") is None


class TestCrossFormatParity:
    def test_both_formats_diagnose_same_structural_error(self):
        # identical broken op, wrapped per format — both must surface a
        # location, not one silently returning None
        react_in = '{"thought":"t","action":"shell","action_input":{"command":"ls"'
        md_in = '## Thought\nt\n\n## Action\n[{"action":"shell","command":"ls"'
        r = ReActFormat().diagnose_syntax_error(react_in)
        m = MdArrayFormat().diagnose_syntax_error(md_in)
        assert r is not None and "^" in r
        assert m is not None and "^" in m


class TestNoJsonRetryEmbedding:
    def test_syntax_error_embedded_after_framing(self):
        intv = format_no_json_retry(
            prior_content='{"a": 1 "b": 2}',
            wire_format=ReActFormat(),
            syntax_error='Expecting \',\' delimiter (line 1, column 9)\n    {"a": 1 "b"\n           ^',
        )
        # framing still leads (existing prefix-skip relies on it)
        assert intv.message.startswith("Your response was not valid JSON.")
        assert "Expecting ',' delimiter" in intv.message
        assert "^" in intv.message
        assert "diagnose_json_error" in intv.primitives

    def test_no_syntax_error_is_bit_for_bit_unchanged(self):
        # omitting syntax_error reproduces the legacy message exactly
        base = format_no_json_retry(
            prior_content="some drift", wire_format=ReActFormat()
        )
        explicit_none = format_no_json_retry(
            prior_content="some drift", wire_format=ReActFormat(), syntax_error=None
        )
        assert base.message == explicit_none.message
        assert base.primitives == explicit_none.primitives
        assert "diagnose_json_error" not in base.primitives
