"""Tests for delegate output improvements (DO-01 ~ DO-40).

Covers: activity log, action summary, error detail, duration,
output format, persistence, iterations, and integration.
"""

from __future__ import annotations


import json

from agent_cli.tools.delegate import (
    DelegateResult,
    _extract_activity_log,
    _extract_last_actions,
    _format_delegate_output,
    _summarize_action,
)


# ── Test helpers ─────────────────────────────────────────────


def _make_action_msg(action: str, action_input: dict) -> dict:
    """Create a mock assistant message with ReAct JSON."""
    return {
        "role": "assistant",
        "content": json.dumps(
            {
                "thought": "test thought",
                "action": action,
                "action_input": action_input,
            }
        ),
    }


def _make_obs_msg(content: str) -> dict:
    """Create a mock user/observation message."""
    return {"role": "user", "content": content}


# ── DO-01 ~ DO-06: Activity Log Extraction ───────────────────


class TestExtractActivityLog:
    def test_basic(self):
        """DO-01: Extract actions from assistant messages."""
        messages = [
            _make_action_msg("read_file", {"path": "/src/auth.py"}),
            _make_obs_msg("file content here"),
            _make_action_msg("shell", {"command": "pytest tests/"}),
            _make_obs_msg("3 passed"),
        ]
        log = _extract_activity_log(messages)
        assert len(log) == 2
        assert log[0] == "iter 1: read_file auth.py"
        assert log[1] == "iter 2: shell pytest tests/"

    def test_empty_messages(self):
        """DO-02: Empty message list returns empty list."""
        assert _extract_activity_log([]) == []

    def test_no_actions(self):
        """DO-03: Assistant messages without actions return empty list."""
        messages = [
            {"role": "assistant", "content": "Just thinking out loud"},
            {"role": "user", "content": "ok"},
        ]
        assert _extract_activity_log(messages) == []

    def test_max_entries(self):
        """DO-04: Truncate to max_entries with ellipsis."""
        messages = []
        for i in range(25):
            messages.append(
                _make_action_msg("read_file", {"path": f"/src/file_{i}.py"})
            )
            messages.append(_make_obs_msg("content"))

        log = _extract_activity_log(messages)
        assert len(log) == 21  # 20 entries + "... and 5 more"
        assert log[-1] == "... and 5 more"

    def test_mixed_roles(self):
        """DO-05: Only extract from assistant messages."""
        messages = [
            _make_obs_msg("user message"),
            _make_action_msg("read_file", {"path": "/src/auth.py"}),
            _make_obs_msg("observation"),
            {"role": "system", "content": "system message"},
            _make_action_msg("shell", {"command": "ls"}),
        ]
        log = _extract_activity_log(messages)
        assert len(log) == 2
        assert "iter 1:" in log[0]
        assert "iter 2:" in log[1]

    def test_malformed_json(self):
        """DO-06: Malformed JSON assistant messages are skipped."""
        messages = [
            {"role": "assistant", "content": "not json at all"},
            _make_action_msg("read_file", {"path": "/src/auth.py"}),
            {"role": "assistant", "content": "{invalid json"},
        ]
        log = _extract_activity_log(messages)
        assert len(log) == 1
        assert log[0] == "iter 1: read_file auth.py"


# ── DO-07 ~ DO-13: Action Summary ────────────────────────────


class TestSummarizeAction:
    def test_read_file(self):
        """DO-07: read_file shows basename."""
        assert (
            _summarize_action("read_file", {"path": "/src/auth.py"})
            == "read_file auth.py"
        )

    def test_write_file(self):
        """DO-08: write_file shows basename."""
        assert (
            _summarize_action("write_file", {"path": "/src/config.py"})
            == "write_file config.py"
        )

    def test_edit_file(self):
        """DO-09: edit_file shows basename."""
        assert (
            _summarize_action("edit_file", {"path": "/src/main.py"})
            == "edit_file main.py"
        )

    def test_shell(self):
        """DO-10: shell shows command truncated to 60 chars."""
        short_cmd = "pytest tests/"
        assert (
            _summarize_action("shell", {"command": short_cmd}) == "shell pytest tests/"
        )

        long_cmd = "a" * 100
        result = _summarize_action("shell", {"command": long_cmd})
        assert result == f"shell {'a' * 60}"

    def test_delegate(self):
        """DO-11: delegate shows task truncated to 40 chars."""
        assert (
            _summarize_action("delegate", {"task": "Fix the bug"})
            == 'delegate "Fix the bug"'
        )

        long_task = "b" * 60
        result = _summarize_action("delegate", {"task": long_task})
        assert result == f'delegate "{"b" * 40}"'

    def test_unknown_action(self):
        """DO-12: Unknown action returns action name only."""
        assert _summarize_action("custom_tool", {"some": "arg"}) == "custom_tool"

    def test_non_dict_input(self):
        """DO-13: Non-dict action_input returns action name only."""
        assert _summarize_action("read_file", "not a dict") == "read_file"
        assert _summarize_action("shell", None) == "shell"


# ── DO-14 ~ DO-18: Error Detail ──────────────────────────────


class TestExtractLastActions:
    def test_basic(self):
        """DO-14: Extract last 5 actions from 10."""
        messages = []
        for i in range(10):
            messages.append(
                _make_action_msg("read_file", {"path": f"/src/file_{i}.py"})
            )
            messages.append(_make_obs_msg(f"content of file_{i}"))

        result = _extract_last_actions(messages)
        assert len(result) == 5
        assert "iter 6:" in result[0]
        assert "iter 10:" in result[4]

    def test_with_error_hint(self):
        """DO-15: Error keyword in observation adds hint."""
        messages = [
            _make_action_msg("shell", {"command": "pytest"}),
            _make_obs_msg("ERROR: 3 tests failed\nsome details"),
            _make_action_msg("edit_file", {"path": "/src/auth.py"}),
            _make_obs_msg("ok"),
        ]
        result = _extract_last_actions(messages)
        assert len(result) == 2
        assert "ERROR: 3 tests failed" in result[0]
        assert "→" not in result[1]  # no error in second observation

    def test_fewer_than_n(self):
        """DO-16: Fewer actions than n returns all."""
        messages = [
            _make_action_msg("read_file", {"path": "/src/a.py"}),
            _make_obs_msg("content"),
            _make_action_msg("read_file", {"path": "/src/b.py"}),
            _make_obs_msg("content"),
            _make_action_msg("read_file", {"path": "/src/c.py"}),
            _make_obs_msg("content"),
        ]
        result = _extract_last_actions(messages)
        assert len(result) == 3

    def test_no_observation(self):
        """DO-17: No user message after last action means no hint."""
        messages = [
            _make_action_msg("shell", {"command": "pytest"}),
            # No observation follows
        ]
        result = _extract_last_actions(messages)
        assert len(result) == 1
        assert "→" not in result[0]

    def test_empty(self):
        """DO-18: Empty messages returns empty list."""
        assert _extract_last_actions([]) == []


# ── DO-19 ~ DO-21: Duration ──────────────────────────────────


class TestDuration:
    def test_delegate_result_duration_field(self):
        """DO-19: DelegateResult accepts duration_secs, default 0.0."""
        dr = DelegateResult(duration_secs=45.2)
        assert dr.duration_secs == 45.2

        dr_default = DelegateResult()
        assert dr_default.duration_secs == 0.0

    def test_duration_zero_not_shown(self):
        """DO-21: duration_secs=0.0 does not show [Duration:] in output."""
        dr = DelegateResult(output="result", duration_secs=0.0)
        formatted = _format_delegate_output(dr)
        assert "[Duration:" not in formatted


# ── DO-22 ~ DO-27: Output Format ─────────────────────────────


class TestFormatOutput:
    def test_with_activity_log(self):
        """DO-22: Activity log present shows [Subagent activity] section."""
        dr = DelegateResult(
            output="done",
            activity_log=["iter 1: read_file auth.py", "iter 2: shell pytest"],
        )
        formatted = _format_delegate_output(dr)
        assert "[Subagent activity]" in formatted
        assert "- iter 1: read_file auth.py" in formatted
        assert "- iter 2: shell pytest" in formatted

    def test_without_activity_log(self):
        """DO-23: Empty activity log omits section."""
        dr = DelegateResult(output="done", activity_log=[])
        formatted = _format_delegate_output(dr)
        assert "[Subagent activity]" not in formatted

    def test_with_last_actions(self):
        """DO-24: last_actions present shows [Last actions before failure]."""
        dr = DelegateResult(
            output=None,
            last_actions=["iter 4: shell pytest → ERROR: 3 failed"],
        )
        formatted = _format_delegate_output(dr)
        assert "[Last actions before failure]" in formatted
        assert "- iter 4: shell pytest" in formatted

    def test_success_no_last_actions(self):
        """DO-25: Success result has no [Last actions before failure]."""
        dr = DelegateResult(output="done", last_actions=[])
        formatted = _format_delegate_output(dr)
        assert "[Last actions before failure]" not in formatted

    def test_duration_and_iterations(self):
        """DO-26: Duration and iterations on same footer line."""
        dr = DelegateResult(
            output="done",
            duration_secs=45.2,
            iterations=5,
        )
        formatted = _format_delegate_output(dr)
        assert "[Duration: 45.2s] [Subagent used 5 iterations]" in formatted


# ── DO-28 ~ DO-32: Persistence ───────────────────────────────


# ── DO-33 ~ DO-34: Iterations Count ──────────────────────────


class TestIterationsCount:
    def test_iterations_from_activity_log(self):
        """DO-33: iterations equals activity_log length."""
        messages = [
            _make_action_msg("read_file", {"path": "/src/a.py"}),
            _make_obs_msg("content"),
            _make_action_msg("read_file", {"path": "/src/b.py"}),
            _make_obs_msg("content"),
            _make_action_msg("shell", {"command": "pytest"}),
            _make_obs_msg("passed"),
            _make_action_msg("read_file", {"path": "/src/c.py"}),
            _make_obs_msg("content"),
            _make_action_msg("read_file", {"path": "/src/d.py"}),
            _make_obs_msg("content"),
        ]
        log = _extract_activity_log(messages)
        real_entries = [e for e in log if not e.startswith("...")]
        assert len(real_entries) == 5

    def test_iterations_excludes_ellipsis(self):
        """DO-34: Ellipsis entry excluded from count."""
        messages = []
        for i in range(25):
            messages.append(
                _make_action_msg("read_file", {"path": f"/src/file_{i}.py"})
            )
            messages.append(_make_obs_msg("content"))

        log = _extract_activity_log(messages)
        real_entries = [e for e in log if not e.startswith("...")]
        assert len(real_entries) == 20
        assert len(log) == 21  # 20 + "... and 5 more"


# ── DO-35 ~ DO-36: Regression ────────────────────────────────


class TestRegression:
    def test_delegate_result_default_fields(self):
        """DO-36: Default DelegateResult has correct defaults for new fields."""
        dr = DelegateResult()
        assert dr.output is None
        assert dr.files_read == []
        assert dr.files_modified == []
        assert dr.iterations == 0
        assert dr.duration_secs == 0.0
        assert dr.activity_log == []
        assert dr.last_actions == []
