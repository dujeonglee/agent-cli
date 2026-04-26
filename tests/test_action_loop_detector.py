"""Tests for ActionLoopDetector — the B1 (action loop) failure detector.

The detector is stateful across turns. Tests cover:
- Threshold semantics (default 2: fires on 2nd consecutive same call)
- Escalation level monotonicity (1, 2, 3, ... per consecutive fire)
- Counter reset on different action / different args
- Counter reset on prev_was_error=True (legitimate retry)
- Args canonicalization (dict key order, non-JSON values)

See docs/robust-harness/DESIGN.md §1, §3.1.
"""

import pytest

from agent_cli.recovery.detectors import ActionLoopDetector


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
