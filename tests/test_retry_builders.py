"""Tests for the failure-grounding retry message builders.

These functions live in ``agent_cli.recovery.common_recovery`` (B1
action loop) and ``agent_cli.recovery.wf_recovery`` (A-class wf-aware
builders). They produce the :class:`Intervention` injected into the
conversation when an LLM response failed to parse / lacked an action.

v1 design: content-only echo (see docs/robust-harness/DESIGN.md §2.2).
The thinking channel is intentionally excluded from recovery — Step 2
observability data will validate or refute the need to add it back as a
separate primitive.

Falls back to the static template when ``prior_content`` is empty (the
returned Intervention has the static message and no primitives).
"""

from agent_cli.recovery.common_recovery import format_action_loop_intervention
from agent_cli.recovery.wf_recovery import (
    format_no_action_retry,
    format_no_json_retry,
)
from agent_cli.recovery.intervention import Intervention
from agent_cli.wire_formats import all_system_user_prefixes
from agent_cli.wire_formats.react import ReActFormat

# Static fallbacks live on the ReAct plugin now (Step 7 cleanup).
# Tests that compared the builder's empty-input fallback against the
# legacy ``RETRY_HINT_NO_*`` constants now compare against the plugin
# accessors — same string, single source of truth.
_REACT = ReActFormat()
RETRY_HINT_NO_JSON = _REACT.static_retry_hint_no_json()
RETRY_HINT_NO_ACTION = _REACT.static_retry_hint_no_action()
SYSTEM_USER_PREFIXES = all_system_user_prefixes()


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

    def test_long_content_is_echoed_in_full(self):
        # No head truncation any more — format-failure signals can
        # sit at either end (a JSON whose closing brace is missing
        # looks fine in the head; long-prose drift only shows its
        # error at the tail). Echo gives the model the whole thing.
        head_marker = "thought: HEAD MARKER"
        tail_marker = "TAIL MARKER closing }"
        long_content = head_marker + (" noise " * 200) + tail_marker
        intv = format_no_json_retry(prior_content=long_content)
        # Pull out only the quoted echo block; the surrounding
        # framing text contains its own placeholder "..." in JSON
        # example shapes (e.g. ``{"thought": "...", "action": ...}``)
        # so a whole-message check would be tripped by the framing.
        body = intv.message.split("---")
        # Structure: [pre, body, post] — middle is the echoed payload.
        assert len(body) >= 3
        echo_body = body[1]
        assert head_marker in echo_body
        assert tail_marker in echo_body
        # No truncation marker INSIDE the echo body specifically.
        assert "..." not in echo_body
        # All padding survives between the markers.
        assert echo_body.count("noise") == long_content.count("noise")

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

    Step 7 of the wire_format extraction moved this from
    ``recovery/builders.format_no_thought_retry`` to
    ``ReActFormat.format_no_thought_retry`` (instance method) because
    NO_THOUGHT only applies to plugins where ``thought_required=True``.
    The builder behavior is unchanged; the call shape is now
    ``plugin.format_no_thought_retry(prior_content=…)``.
    """

    def _retry(self, **kwargs):
        return _REACT.format_no_thought_retry(**kwargs)

    def test_returns_intervention(self):
        assert isinstance(self._retry(), Intervention)

    def test_empty_falls_back_to_static_message(self):
        intv = self._retry()
        assert "thought" in intv.message
        assert intv.primitives == []

    def test_explicit_empty_string_falls_back(self):
        intv = self._retry(prior_content="")
        assert intv.primitives == []
        assert "thought" in intv.message

    def test_whitespace_only_falls_back(self):
        intv = self._retry(prior_content="   \n\t")
        assert intv.primitives == []

    def test_content_is_echoed(self):
        content = '{"action": "read_file", "action_input": {"path": "x.py"}}'
        intv = self._retry(prior_content=content)
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
        intv = self._retry(prior_content="something")
        assert intv.primitives == ["echo_prior_output"]

    def test_prefix_matches_system_user_prefixes(self):
        intv = self._retry(prior_content="some text")
        assert any(intv.message.startswith(p) for p in SYSTEM_USER_PREFIXES)

    def test_keyword_only_no_positional(self):
        import pytest

        with pytest.raises(TypeError):
            _REACT.format_no_thought_retry("positional")  # type: ignore[misc]


class TestFormatActionLoopIntervention:
    """B1 (action loop) Intervention composer.

    Level 1 → probe_progress; level 2 → restate_task; level ≥3 → None
    (caller hard-fails). Temperature-down level intentionally omitted —
    see DESIGN.md §2.3 and recovery.common_recovery.format_action_loop_intervention
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
