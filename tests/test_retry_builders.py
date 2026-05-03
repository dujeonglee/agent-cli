"""Tests for the failure-grounding retry message builders.

These functions live in ``agent_cli.recovery.builders``. They produce
the :class:`Intervention` injected into the conversation when an LLM
response failed to parse / lacked an action.

v1 design: content-only echo (see docs/robust-harness/DESIGN.md §2.2).
The thinking channel is intentionally excluded from recovery — Step 2
observability data will validate or refute the need to add it back as a
separate primitive.

Falls back to the static template when ``prior_content`` is empty (the
returned Intervention has the static message and no primitives).
"""

from agent_cli.constants import (
    RETRY_HINT_NO_ACTION,
    RETRY_HINT_NO_JSON,
    SYSTEM_USER_PREFIXES,
)
from agent_cli.recovery.builders import (
    format_action_loop_intervention,
    format_no_action_retry,
    format_no_json_retry,
    format_no_thought_retry,
)
from agent_cli.recovery.intervention import Intervention


class TestFormatNoJsonRetry:
    def test_returns_intervention(self):
        result = format_no_json_retry()
        assert isinstance(result, Intervention)

    def test_empty_falls_back_to_static_template(self):
        intv = format_no_json_retry()
        assert intv.message == RETRY_HINT_NO_JSON
        assert intv.primitives == []  # no primitives composed in fallback

    def test_explicit_empty_string_falls_back(self):
        intv = format_no_json_retry(prior_content="")
        assert intv.message == RETRY_HINT_NO_JSON
        assert intv.primitives == []

    def test_whitespace_only_falls_back(self):
        intv = format_no_json_retry(prior_content="   \n  \t")
        assert intv.message == RETRY_HINT_NO_JSON
        assert intv.primitives == []

    def test_content_is_echoed_in_block(self):
        content = "thought: foo\naction: complete\naction_input: {result: 'x'}"
        intv = format_no_json_retry(prior_content=content)
        assert content in intv.message
        assert "Your prior output:" in intv.message
        assert intv.message.startswith("Your response was not valid JSON.")
        assert "Honor that" in intv.message
        assert '"action": "tool_name"' in intv.message

    def test_content_path_records_composed_primitives(self):
        intv = format_no_json_retry(prior_content="something")
        assert intv.primitives == ["echo_prior_output", "constrain_format_json"]

    def test_long_content_is_head_truncated(self):
        # Structural drift markers ('thought:', 'action:') sit at the head
        long_content = "thought: HEAD MARKER " + ("noise " * 200)
        intv = format_no_json_retry(prior_content=long_content)
        assert "thought: HEAD MARKER" in intv.message
        assert "..." in intv.message
        # Tail should be dropped — count of "noise" must drop
        assert intv.message.count("noise") < long_content.count("noise")

    def test_quotes_in_content_do_not_break_message(self):
        content = "thought: \"quoted\" with 'mixed' delimiters"
        intv = format_no_json_retry(prior_content=content)
        assert content in intv.message
        # Triple-dash delimiter survives any inner quoting
        assert "---" in intv.message

    def test_prefix_matches_system_user_prefixes(self):
        intv = format_no_json_retry(prior_content="some output")
        assert any(intv.message.startswith(p) for p in SYSTEM_USER_PREFIXES)

    def test_keyword_only_no_positional(self):
        # Prevent positional misuse.
        import pytest

        with pytest.raises(TypeError):
            format_no_json_retry("positional arg")  # type: ignore[misc]


class TestFormatNoActionRetry:
    def test_returns_intervention(self):
        result = format_no_action_retry()
        assert isinstance(result, Intervention)

    def test_empty_falls_back_to_static_template(self):
        intv = format_no_action_retry()
        assert intv.message == RETRY_HINT_NO_ACTION
        assert intv.primitives == []

    def test_content_is_echoed(self):
        content = '{"thought": "...", "args": {}}'  # parsed but action missing
        intv = format_no_action_retry(prior_content=content)
        assert content in intv.message
        assert "Your prior output:" in intv.message
        assert intv.message.startswith("Your JSON was parsed but has no action.")
        # Both action paths still presented
        assert '"action": "tool_name"' in intv.message
        assert '"action": "complete"' in intv.message

    def test_content_path_records_composed_primitives(self):
        intv = format_no_action_retry(prior_content="something")
        assert intv.primitives == ["echo_prior_output", "constrain_action_required"]

    def test_prefix_matches_system_user_prefixes(self):
        intv = format_no_action_retry(prior_content="some text")
        assert any(intv.message.startswith(p) for p in SYSTEM_USER_PREFIXES)

    def test_keyword_only_no_positional(self):
        import pytest

        with pytest.raises(TypeError):
            format_no_action_retry("positional")  # type: ignore[misc]


class TestFormatNoThoughtRetry:
    """A2 NO_THOUGHT — action present but the 'thought' field omitted.

    The retry's purpose is to break the mimicry-strengthening loop where
    one drift-shaped response (prose+JSON, or pure JSON without thought)
    enters the transcript and trains subsequent turns to also drop the
    field. The builder echoes the prior output (so the model sees its
    own omission) and restates the constraint inline.
    """

    def test_returns_intervention(self):
        result = format_no_thought_retry()
        assert isinstance(result, Intervention)

    def test_empty_falls_back_to_static_message(self):
        intv = format_no_thought_retry()
        assert "thought" in intv.message
        assert intv.primitives == []

    def test_explicit_empty_string_falls_back(self):
        intv = format_no_thought_retry(prior_content="")
        assert intv.primitives == []
        assert "thought" in intv.message

    def test_whitespace_only_falls_back(self):
        intv = format_no_thought_retry(prior_content="   \n\t")
        assert intv.primitives == []

    def test_content_is_echoed(self):
        content = '{"action": "read_file", "action_input": {"path": "x.py"}}'
        intv = format_no_thought_retry(prior_content=content)
        assert content in intv.message
        assert "Your prior output:" in intv.message
        assert intv.message.startswith("Your JSON was missing the 'thought' field.")
        assert "Honor that" in intv.message
        # Constraint asks for purpose + reason
        assert "purpose" in intv.message
        assert "reason" in intv.message

    def test_content_path_records_composed_primitives(self):
        # Constraint is inlined (not promoted to a primitive — anti-patchwork
        # invariant: only one caller in v1). Only echo is a primitive.
        intv = format_no_thought_retry(prior_content="something")
        assert intv.primitives == ["echo_prior_output"]

    def test_prefix_matches_system_user_prefixes(self):
        intv = format_no_thought_retry(prior_content="some text")
        assert any(intv.message.startswith(p) for p in SYSTEM_USER_PREFIXES)

    def test_keyword_only_no_positional(self):
        import pytest

        with pytest.raises(TypeError):
            format_no_thought_retry("positional")  # type: ignore[misc]


class TestFormatActionLoopIntervention:
    """B1 (action loop) Intervention composer.

    Level 1 → probe_progress; level 2 → restate_task; level ≥3 → None
    (caller hard-fails). Temperature-down level intentionally omitted —
    see DESIGN.md §2.3 and recovery.builders.format_action_loop_intervention
    docstring.
    """

    def _kwargs(self, **overrides):
        base = dict(
            level=1,
            action="read_file",
            args_repr='{"path": "x.py"}',
            repeat_count=2,
            task="Refactor the parser",
        )
        base.update(overrides)
        return base

    def test_returns_intervention(self):
        intv = format_action_loop_intervention(**self._kwargs())
        assert isinstance(intv, Intervention)

    def test_level_one_uses_probe_progress(self):
        intv = format_action_loop_intervention(**self._kwargs(level=1))
        assert intv.primitives == ["probe_progress"]
        # probe_progress message: NO task anchor
        assert "You were asked to:" not in intv.message
        # Has the loop fact + complete option
        assert "read_file" in intv.message
        assert "complete" in intv.message

    def test_level_two_uses_restate_task(self):
        intv = format_action_loop_intervention(**self._kwargs(level=2))
        assert intv.primitives == ["restate_task"]
        # restate_task: task anchor present
        assert "You were asked to:" in intv.message
        assert "Refactor the parser" in intv.message
        # Diagnostic questions present
        assert "Why does" in intv.message
        assert "NOT getting" in intv.message

    def test_level_three_returns_none(self):
        # Caller is responsible for hard-failing
        assert format_action_loop_intervention(**self._kwargs(level=3)) is None

    def test_level_four_returns_none(self):
        assert format_action_loop_intervention(**self._kwargs(level=4)) is None

    def test_level_zero_returns_none(self):
        # Level 0 means no fire — caller should never invoke the builder
        # at level 0, but defensively the builder returns None
        assert format_action_loop_intervention(**self._kwargs(level=0)) is None

    def test_keyword_only_signature(self):
        import pytest

        with pytest.raises(TypeError):
            format_action_loop_intervention(1, "read_file", "{}", 2, "task")  # type: ignore[misc]
