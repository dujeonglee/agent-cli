"""Tests for the recovery-layer detectors.

Stateful detector (``ActionLoopDetector``) — class with state across
turns. Stateless detectors (``detect_unknown_tool``,
``detect_schema_mismatch``) — pure functions checking a single
attempt without remembering anything. The split is documented in
``recovery/detectors.py`` module docstring.

See docs/robust-harness/DESIGN.md §1, §3.1.
"""

import pytest

from agent_cli.recovery.detectors import (
    ActionLoopDetector,
    detect_nested_envelope,
    detect_schema_mismatch,
    detect_thought_missing,
    detect_unknown_tool,
)


class TestThreshold:
    def test_default_threshold_is_two(self):
        d = ActionLoopDetector()
        assert d.threshold == 2

    def test_threshold_below_two_rejected(self):
        with pytest.raises(ValueError):
            ActionLoopDetector(threshold=1)

    def test_first_observation_does_not_fire(self):
        d = ActionLoopDetector()
        assert d.observe("read_file", {"path": "x"}) == 0

    def test_second_consecutive_fires_level_one(self):
        d = ActionLoopDetector()
        d.observe("read_file", {"path": "x"})
        assert d.observe("read_file", {"path": "x"}) == 1

    def test_third_consecutive_fires_level_two(self):
        d = ActionLoopDetector()
        d.observe("read_file", {"path": "x"})
        d.observe("read_file", {"path": "x"})
        assert d.observe("read_file", {"path": "x"}) == 2

    def test_fourth_consecutive_fires_level_three(self):
        d = ActionLoopDetector()
        for _ in range(3):
            d.observe("read_file", {"path": "x"})
        assert d.observe("read_file", {"path": "x"}) == 3

    def test_higher_threshold_delays_fire(self):
        d = ActionLoopDetector(threshold=3)
        assert d.observe("a", {}) == 0
        assert d.observe("a", {}) == 0  # 2nd call: still no fire
        assert d.observe("a", {}) == 1  # 3rd call: first fire


class TestCounterReset:
    def test_different_action_resets(self):
        d = ActionLoopDetector()
        d.observe("read_file", {"path": "x"})
        # Different action → counter resets, no fire even on second same call
        assert d.observe("shell", {"cmd": "ls"}) == 0
        assert d.observe("shell", {"cmd": "ls"}) == 1  # now this fires

    def test_different_args_resets(self):
        d = ActionLoopDetector()
        d.observe("read_file", {"path": "x"})
        assert d.observe("read_file", {"path": "y"}) == 0  # different path

    def test_dict_key_order_irrelevant(self):
        d = ActionLoopDetector()
        d.observe("tool", {"a": 1, "b": 2})
        # Same args, different key order → same signature → fires
        assert d.observe("tool", {"b": 2, "a": 1}) == 1


class TestErrorRetryReset:
    def test_prev_was_error_resets_counter_no_fire(self):
        d = ActionLoopDetector()
        d.observe("shell", {"cmd": "ls /foo"})
        # Tool errored last turn — retry is legitimate, no fire
        assert d.observe("shell", {"cmd": "ls /foo"}, prev_was_error=True) == 0

    def test_prev_error_does_not_block_future_loop(self):
        # After an error retry, if model loops on success, B1 should still fire
        d = ActionLoopDetector()
        d.observe("shell", {"cmd": "x"})
        d.observe("shell", {"cmd": "x"}, prev_was_error=True)  # legitimate retry
        # Now same call, no error this time → loop detected on next observation
        assert d.observe("shell", {"cmd": "x"}) == 1

    def test_prev_error_resets_fire_count(self):
        d = ActionLoopDetector()
        d.observe("a", {})
        d.observe("a", {})  # fire level 1
        # Error happened — counter resets
        d.observe("a", {}, prev_was_error=True)
        # Next same call: only 2nd in a row, fire level 1 again, not 2
        assert d.observe("a", {}) == 1


class TestEscalationCounters:
    def test_fire_count_property_tracks_fires(self):
        d = ActionLoopDetector()
        assert d.fire_count == 0
        d.observe("a", {})
        assert d.fire_count == 0
        d.observe("a", {})
        assert d.fire_count == 1
        d.observe("a", {})
        assert d.fire_count == 2

    def test_consecutive_count_tracks_repeats(self):
        d = ActionLoopDetector()
        assert d.consecutive_count == 0
        d.observe("a", {})
        assert d.consecutive_count == 1
        d.observe("a", {})
        assert d.consecutive_count == 2
        d.observe("a", {})
        assert d.consecutive_count == 3

    def test_consecutive_count_resets_on_different_action(self):
        d = ActionLoopDetector()
        d.observe("a", {})
        d.observe("a", {})
        d.observe("b", {})
        assert d.consecutive_count == 1


class TestArgsCanonicalization:
    def test_string_args_supported(self):
        d = ActionLoopDetector()
        d.observe("tool", "raw string")
        assert d.observe("tool", "raw string") == 1

    def test_list_args_supported(self):
        d = ActionLoopDetector()
        d.observe("tool", [1, 2, 3])
        assert d.observe("tool", [1, 2, 3]) == 1

    def test_non_json_serializable_does_not_crash(self):
        # Anything that json.dumps cannot handle (object refs, sets, etc.)
        # must fall back to repr, not raise
        class NotJsonable:
            def __repr__(self):
                return "<NJ>"

        d = ActionLoopDetector()
        obj = NotJsonable()
        # Should not raise
        d.observe("tool", obj)
        assert d.observe("tool", obj) == 1

    def test_nested_dict_canonicalized(self):
        d = ActionLoopDetector()
        d.observe("t", {"x": {"a": 1, "b": 2}})
        assert d.observe("t", {"x": {"b": 2, "a": 1}}) == 1


class TestDetectUnknownTool:
    """Stateless A4 detector — pure ``in`` membership check."""

    def test_known_tool_returns_false(self):
        assert detect_unknown_tool("read_file", ["read_file", "shell"]) is False

    def test_unknown_tool_returns_true(self):
        assert detect_unknown_tool("bogus_tool", ["read_file", "shell"]) is True

    def test_empty_tools_list_anything_unknown(self):
        assert detect_unknown_tool("read_file", []) is True

    def test_empty_action_returns_false(self):
        # No action emitted at all is not an "unknown tool" — it's
        # captured by FAILURE_NO_ACTION (A3) at the parser layer.
        assert detect_unknown_tool("", ["read_file"]) is False

    def test_case_sensitive(self):
        assert detect_unknown_tool("Read_File", ["read_file"]) is True
        assert detect_unknown_tool("read_file", ["read_file"]) is False

    def test_does_not_mutate_tools_list(self):
        tools = ["read_file", "shell"]
        snapshot = list(tools)
        detect_unknown_tool("bogus", tools)
        assert tools == snapshot


class TestDetectSchemaMismatch:
    """Stateless A5 detector — wraps ``validate_tool_input``.

    Tests focus on the *contract* exposed by the recovery layer:
    returned tuple shape, normalized output use, and that it surfaces
    the same errors the underlying validator produces. We do not
    exhaustively test the underlying schema rules (those have their
    own tests in this file's TestValidateToolInput).
    """

    def test_valid_input_returns_no_mismatch(self):
        # Flat-native (Step 3): read_file takes flat {path, ...mode}.
        mismatched, err, normalized = detect_schema_mismatch(
            "read_file", {"path": "x.py"}
        )
        assert mismatched is False
        assert err is None
        assert normalized == {"path": "x.py"}

    def test_missing_required_field(self):
        mismatched, err, _ = detect_schema_mismatch("read_file", {})
        assert mismatched is True
        assert err is not None
        assert "path" in err  # error mentions the missing field

    def test_string_input_auto_promoted_to_dict(self):
        # validate_tool_input promotes strings to {required[0]: value}
        mismatched, err, normalized = detect_schema_mismatch("shell", "echo hi")
        assert mismatched is False
        assert normalized == {"command": "echo hi"}

    def test_unknown_tool_treated_as_mismatch(self):
        # validate_tool_input also rejects unknown tools, so the schema
        # detector flags them too. The loop runs detect_unknown_tool first
        # to give A4 priority over A5 — this test only confirms the
        # detector does not silently accept unknown tools.
        mismatched, err, _ = detect_schema_mismatch("bogus", {})
        assert mismatched is True
        assert err is not None

    def test_returned_tuple_shape(self):
        result = detect_schema_mismatch("read_file", {"path": "x"})
        assert isinstance(result, tuple)
        assert len(result) == 3


class TestDetectNestedEnvelope:
    """Stateless A6 detector — flags double-wrapped complete payloads.

    The detector observes only — it does not auto-unwrap. Tests verify
    the structural rule (string starting with ``{"result"`` that parses
    as a JSON object containing a top-level ``result`` key) without
    asserting on remediation behavior.
    """

    def test_non_string_returns_false(self):
        assert detect_nested_envelope(None) is False
        assert detect_nested_envelope(42) is False
        assert detect_nested_envelope({"result": "x"}) is False
        assert detect_nested_envelope(["result"]) is False

    def test_plain_text_returns_false(self):
        assert detect_nested_envelope("hello world") is False
        assert detect_nested_envelope("") is False

    def test_string_not_starting_with_result_key_returns_false(self):
        # A JSON object that doesn't start with the result key is not
        # the double-wrap pattern we're targeting.
        assert detect_nested_envelope('{"answer": "x"}') is False
        assert detect_nested_envelope('{"data": {"result": "x"}}') is False

    def test_malformed_json_returns_false(self):
        # Strings that look like the envelope but fail to parse must
        # not be flagged — false positives would corrupt observability.
        assert detect_nested_envelope('{"result": ') is False
        assert detect_nested_envelope('{"result": "unterminated') is False

    def test_valid_nested_envelope_returns_true(self):
        assert detect_nested_envelope('{"result": "the actual answer"}') is True

    def test_envelope_with_leading_whitespace_returns_true(self):
        assert detect_nested_envelope('  \n{"result": "x"}') is True

    def test_envelope_with_extra_keys_still_flags(self):
        # If the parsed object has a top-level result key, it's the
        # nested-envelope pattern — extra siblings don't disqualify.
        assert detect_nested_envelope('{"result": "x", "trace": "y"}') is True

    def test_non_object_json_returns_false(self):
        # A JSON array or scalar cannot be the nested envelope.
        assert detect_nested_envelope('["result"]') is False


class TestUnwrapNestedEnvelope:
    """``unwrap_nested_envelope`` performs the user-facing fix the
    detector observes: when the model double-wraps the ``complete``
    payload, peel one layer so the final card shows plain text.
    """

    def test_unwraps_one_level(self):
        from agent_cli.recovery.detectors import unwrap_nested_envelope

        assert (
            unwrap_nested_envelope('{"result": "the actual story"}')
            == "the actual story"
        )

    def test_plain_text_returns_unchanged(self):
        from agent_cli.recovery.detectors import unwrap_nested_envelope

        assert unwrap_nested_envelope("hello world") == "hello world"
        assert unwrap_nested_envelope("") == ""

    def test_non_string_returns_unchanged(self):
        from agent_cli.recovery.detectors import unwrap_nested_envelope

        # Non-string inputs (None, dict, int) round-trip untouched —
        # the caller is responsible for normalising elsewhere.
        assert unwrap_nested_envelope(None) is None
        assert unwrap_nested_envelope({"result": "x"}) == {"result": "x"}
        assert unwrap_nested_envelope(42) == 42

    def test_malformed_json_returns_unchanged(self):
        from agent_cli.recovery.detectors import unwrap_nested_envelope

        # If parsing fails, leave the raw text visible — better to
        # surface the LLM's actual output than guess.
        bad = '{"result": "unterminated'
        assert unwrap_nested_envelope(bad) == bad

    def test_non_envelope_object_returns_unchanged(self):
        from agent_cli.recovery.detectors import unwrap_nested_envelope

        # Object without top-level ``result`` is not the envelope.
        assert unwrap_nested_envelope('{"answer": "x"}') == '{"answer": "x"}'

    def test_inner_non_string_returns_unchanged(self):
        """If ``result`` is a dict/list/number, peeling would change
        the answer's shape — keep the wrapper visible so the caller
        sees the model's actual output."""
        from agent_cli.recovery.detectors import unwrap_nested_envelope

        wrapped = '{"result": {"nested": "object"}}'
        # Detector flagged it (top-level result key present), but
        # unwrap declines because inner isn't a string.
        assert unwrap_nested_envelope(wrapped) == wrapped


class TestNestedEnvelopeLenientParse:
    """Real-world regression. Qwen3.6 produced parallel-delegate
    ``complete`` payloads where the inner JSON body contained
    literal newlines / tabs inside string fields — model wrote
    markdown bodies with actual line breaks instead of properly
    escaped ``\\n``. ``json.loads`` in strict mode rejects those
    as "Invalid control character", so the detector silently
    missed the wrap and the user saw the ``{"result": "..."}``
    artifact at the head of their answer.

    The fix is ``strict=False`` on the json.loads call inside both
    detector and unwrapper, accepting the messy bodies these
    models actually produce.
    """

    def test_detector_flags_envelope_with_literal_newlines(self):
        # Body has real newlines in the middle — strict json.loads
        # would reject this. Lenient mode parses cleanly.
        body = "# 시계탑의 마지막 초침\n\n비가 내리던 어느 밤, 한 사람이..."
        wrapped = '{"result": "' + body + '"}'
        assert detect_nested_envelope(wrapped) is True

    def test_unwrapper_returns_clean_body_for_literal_newlines(self):
        from agent_cli.recovery.detectors import unwrap_nested_envelope

        body = "# Title\n\nFirst paragraph.\nSecond line."
        wrapped = '{"result": "' + body + '"}'
        # Inner body must come back with its newlines intact (not
        # escaped, not stripped).
        assert unwrap_nested_envelope(wrapped) == body

    def test_detector_flags_envelope_with_literal_tabs(self):
        # Same shape, different control character. ``\t`` inside the
        # body would also fail strict mode but pass lenient.
        wrapped = '{"result": "a\tb\tc"}'
        assert detect_nested_envelope(wrapped) is True

    def test_unwrapper_preserves_markdown_body(self):
        from agent_cli.recovery.detectors import unwrap_nested_envelope

        # Real-world shape: markdown headers + paragraphs + escaped
        # double-quotes inside the body. The user-reported example.
        body = (
            "## 달빛을 훔친 고양이\n\n"
            '서울의 한 골목길에 살던 검은 고양이 \\"월암\\"은\n'
            "평범하지 않은 습관이 있었다."
        )
        wrapped = '{"result": "' + body + '"}'
        result = unwrap_nested_envelope(wrapped)
        # The body's literal newlines come back as actual newlines
        # in the unwrapped string.
        assert "달빛을 훔친 고양이" in result
        assert "골목길" in result
        # The leading wrapper artifact is gone.
        assert not result.startswith('{"result"')

    def test_lenient_does_not_widen_to_non_envelope_shapes(self):
        # The ``{"result"`` head guard still applies — a JSON object
        # with literal newlines but a different top-level key must
        # not be flagged as a nested envelope.
        wrapped = '{"answer": "line1\nline2"}'
        assert detect_nested_envelope(wrapped) is False

    def test_strict_unparseable_still_returns_false(self):
        # The body claims to be the envelope but is genuinely
        # malformed (unterminated string). Lenient mode doesn't
        # change that — still False, no silent recovery.
        wrapped = '{"result": "unterminated\nnewline\n'
        assert detect_nested_envelope(wrapped) is False

    def test_unwrap_does_not_recurse(self):
        """``{"result": "{\\"result\\": ...}"}`` (double-nested) only
        peels one level. Recursive nesting is rare enough that the
        single-level form keeps the second wrapper visible for
        debugging instead of silently flattening."""
        from agent_cli.recovery.detectors import unwrap_nested_envelope

        double_nested = '{"result": "{\\"result\\": \\"deep\\"}"}'
        assert unwrap_nested_envelope(double_nested) == '{"result": "deep"}'

    def test_unwraps_with_leading_whitespace(self):
        from agent_cli.recovery.detectors import unwrap_nested_envelope

        assert unwrap_nested_envelope('  \n{"result": "x"}') == "x"


class TestDetectThoughtMissing:
    """A2 NO_THOUGHT detector — fires only when an action is present
    but the thought field is missing/empty. NO_ACTION (A3) is a
    different label and must not be conflated.
    """

    def test_no_action_returns_false(self):
        # No action means we are in NO_ACTION territory (A3), not A2.
        assert detect_thought_missing("some thought", None) is False
        assert detect_thought_missing("", "") is False
        assert detect_thought_missing(None, None) is False

    def test_action_present_thought_none_returns_true(self):
        assert detect_thought_missing(None, "read_file") is True

    def test_action_present_empty_thought_returns_true(self):
        assert detect_thought_missing("", "read_file") is True

    def test_action_present_whitespace_thought_returns_true(self):
        assert detect_thought_missing("   \n\t", "read_file") is True

    def test_action_present_valid_thought_returns_false(self):
        assert detect_thought_missing("I want to read the file", "read_file") is False

    def test_complete_action_is_exempt(self):
        # ``complete`` is the final-answer action — the reasoning slot
        # carries no next-turn obligation since there is no further
        # turn to propagate to. Empty thought on complete is no longer
        # treated as a drift signal. Reverses an earlier design
        # decision after Phase 2 bakeoff (2026-05-18) measured a
        # systematic NO_THOUGHT recovery loop on qwen3.6:27b's
        # ``complete_direct`` (markdown wire format, 5/5 runs).
        assert detect_thought_missing(None, "complete") is False
        assert detect_thought_missing("", "complete") is False
        assert detect_thought_missing("   \n", "complete") is False
        # A populated thought on complete is still fine (not flagged).
        assert detect_thought_missing("done", "complete") is False

    def test_non_string_thought_returns_false(self):
        # If the parser produced a non-string thought (e.g. a dict that
        # was incorrectly placed there), don't flag — only None / empty
        # / whitespace strings count as "missing".
        assert detect_thought_missing({"nested": "x"}, "read_file") is False
        assert detect_thought_missing(["a", "b"], "read_file") is False
