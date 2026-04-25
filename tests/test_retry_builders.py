"""Tests for the failure-grounding retry message builders.

These functions live in agent_cli.constants. They produce the user-role
message injected into the conversation when an LLM response failed to
parse / lacked an action. Two echo channels:

- ``prior_content``: the actual emitted text (head-truncated). Primary
  failure-grounding signal — the model sees its own structural drift.
- ``prior_thinking``: provider-side reasoning channel (tail-truncated).
  Captures self-correction beats when the model produced any.

Falls back to the static template when both channels are empty.
"""

from agent_cli.constants import (
    RETRY_HINT_NO_ACTION,
    RETRY_HINT_NO_JSON,
    SYSTEM_USER_PREFIXES,
    format_no_action_retry,
    format_no_json_retry,
)


class TestFormatNoJsonRetry:
    def test_both_channels_empty_falls_back(self):
        assert format_no_json_retry() == RETRY_HINT_NO_JSON

    def test_explicit_empty_strings_fall_back(self):
        assert (
            format_no_json_retry(prior_content="", prior_thinking="")
            == RETRY_HINT_NO_JSON
        )

    def test_whitespace_only_both_falls_back(self):
        assert (
            format_no_json_retry(prior_content="   \n  ", prior_thinking="\t\n")
            == RETRY_HINT_NO_JSON
        )

    def test_thinking_only_quotes_thinking(self):
        thinking = "I keep failing to provide valid JSON. Let me try."
        out = format_no_json_retry(prior_thinking=thinking)
        assert thinking in out
        assert "Your prior reasoning:" in out
        assert "Your prior output:" not in out  # content channel absent
        assert out.startswith("Your response was not valid JSON.")
        assert "Honor that" in out
        assert '"action": "tool_name"' in out

    def test_content_only_quotes_content(self):
        content = "thought: foo\naction: complete\naction_input: {result: 'x'}"
        out = format_no_json_retry(prior_content=content)
        assert content in out
        assert "Your prior output:" in out
        assert "Your prior reasoning:" not in out
        assert out.startswith("Your response was not valid JSON.")

    def test_both_channels_quote_both_in_order(self):
        content = "YAML-style drift here"
        thinking = "Let me think about JSON next time."
        out = format_no_json_retry(prior_content=content, prior_thinking=thinking)
        # Both present
        assert content in out
        assert thinking in out
        # Order: content section before reasoning section (failure
        # grounding goes first, self-instruction second)
        assert out.index("Your prior output:") < out.index("Your prior reasoning:")

    def test_long_content_is_head_truncated(self):
        # Structural drift markers ('thought:', 'action:') sit at the head
        long_content = "thought: HEAD MARKER " + ("noise " * 200)
        out = format_no_json_retry(prior_content=long_content)
        assert "thought: HEAD MARKER" in out
        assert "..." in out
        # Tail should be dropped — count of "noise" must drop
        assert out.count("noise") < long_content.count("noise")

    def test_long_thinking_is_tail_truncated(self):
        # Self-correction beat sits at the tail
        long_thinking = ("padding " * 200) + " TAIL CORRECTION."
        out = format_no_json_retry(prior_thinking=long_thinking)
        assert "TAIL CORRECTION." in out
        assert "..." in out
        assert out.count("padding") < long_thinking.count("padding")

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
        # Prevent positional ambiguity between the two similar args.
        import pytest

        with pytest.raises(TypeError):
            format_no_json_retry("positional arg")  # type: ignore[misc]


class TestFormatNoActionRetry:
    def test_both_channels_empty_falls_back(self):
        assert format_no_action_retry() == RETRY_HINT_NO_ACTION

    def test_thinking_only(self):
        out = format_no_action_retry(prior_thinking="some reasoning")
        assert "some reasoning" in out
        assert "Your prior reasoning:" in out
        assert out.startswith("Your JSON was parsed but has no action.")
        # Both action paths still presented
        assert '"action": "tool_name"' in out
        assert '"action": "complete"' in out

    def test_content_only(self):
        content = '{"thought": "...", "args": {}}'  # parsed but action missing
        out = format_no_action_retry(prior_content=content)
        assert content in out
        assert "Your prior output:" in out

    def test_both_channels_quote_both(self):
        out = format_no_action_retry(
            prior_content="content here", prior_thinking="thinking here"
        )
        assert "content here" in out
        assert "thinking here" in out

    def test_prefix_matches_system_user_prefixes(self):
        out = format_no_action_retry(prior_thinking="r")
        assert any(out.startswith(p) for p in SYSTEM_USER_PREFIXES)

    def test_keyword_only_no_positional(self):
        import pytest

        with pytest.raises(TypeError):
            format_no_action_retry("positional")  # type: ignore[misc]
