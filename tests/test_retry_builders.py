"""Tests for the failure-grounding retry message builders.

These functions live in ``agent_cli.constants``. They produce the user-role
message injected into the conversation when an LLM response failed to
parse / lacked an action.

v1 design: content-only echo (see docs/robust-harness/DESIGN.md §2.2).
The thinking channel is intentionally excluded from recovery — Step 2
observability data will validate or refute the need to add it back as a
separate primitive.

Falls back to the static template when ``prior_content`` is empty.
"""

from agent_cli.constants import (
    RETRY_HINT_NO_ACTION,
    RETRY_HINT_NO_JSON,
    SYSTEM_USER_PREFIXES,
    format_no_action_retry,
    format_no_json_retry,
)


class TestFormatNoJsonRetry:
    def test_empty_falls_back_to_static_template(self):
        assert format_no_json_retry() == RETRY_HINT_NO_JSON

    def test_explicit_empty_string_falls_back(self):
        assert format_no_json_retry(prior_content="") == RETRY_HINT_NO_JSON

    def test_whitespace_only_falls_back(self):
        assert format_no_json_retry(prior_content="   \n  \t") == RETRY_HINT_NO_JSON

    def test_content_is_echoed_in_block(self):
        content = "thought: foo\naction: complete\naction_input: {result: 'x'}"
        out = format_no_json_retry(prior_content=content)
        assert content in out
        assert "Your prior output:" in out
        assert out.startswith("Your response was not valid JSON.")
        assert "Honor that" in out
        assert '"action": "tool_name"' in out

    def test_long_content_is_head_truncated(self):
        # Structural drift markers ('thought:', 'action:') sit at the head
        long_content = "thought: HEAD MARKER " + ("noise " * 200)
        out = format_no_json_retry(prior_content=long_content)
        assert "thought: HEAD MARKER" in out
        assert "..." in out
        # Tail should be dropped — count of "noise" must drop
        assert out.count("noise") < long_content.count("noise")

    def test_quotes_in_content_do_not_break_message(self):
        content = "thought: \"quoted\" with 'mixed' delimiters"
        out = format_no_json_retry(prior_content=content)
        assert content in out
        # Triple-dash delimiter survives any inner quoting
        assert "---" in out

    def test_prefix_matches_system_user_prefixes(self):
        out = format_no_json_retry(prior_content="some output")
        assert any(out.startswith(p) for p in SYSTEM_USER_PREFIXES)

    def test_keyword_only_no_positional(self):
        # Prevent positional misuse.
        import pytest

        with pytest.raises(TypeError):
            format_no_json_retry("positional arg")  # type: ignore[misc]


class TestFormatNoActionRetry:
    def test_empty_falls_back_to_static_template(self):
        assert format_no_action_retry() == RETRY_HINT_NO_ACTION

    def test_content_is_echoed(self):
        content = '{"thought": "...", "args": {}}'  # parsed but action missing
        out = format_no_action_retry(prior_content=content)
        assert content in out
        assert "Your prior output:" in out
        assert out.startswith("Your JSON was parsed but has no action.")
        # Both action paths still presented
        assert '"action": "tool_name"' in out
        assert '"action": "complete"' in out

    def test_prefix_matches_system_user_prefixes(self):
        out = format_no_action_retry(prior_content="some text")
        assert any(out.startswith(p) for p in SYSTEM_USER_PREFIXES)

    def test_keyword_only_no_positional(self):
        import pytest

        with pytest.raises(TypeError):
            format_no_action_retry("positional")  # type: ignore[misc]
