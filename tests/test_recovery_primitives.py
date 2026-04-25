"""Tests for recovery primitives (agent_cli.recovery.primitives).

Primitives are pure functions used as composition blocks for retry/
intervention messages. The contract: take harness-level inputs only,
return a text fragment, never reference provider/model/channel names.

See docs/robust-harness/DESIGN.md §2.2.
"""

from agent_cli.recovery.primitives import (
    ECHO_HEAD,
    constrain_action_required,
    constrain_format_json,
    echo_prior_output,
)


class TestEchoPriorOutput:
    def test_empty_returns_empty(self):
        assert echo_prior_output("") == ""

    def test_whitespace_only_returns_empty(self):
        assert echo_prior_output("   \n\t  ") == ""

    def test_default_no_args_returns_empty(self):
        assert echo_prior_output() == ""

    def test_short_content_quoted_verbatim(self):
        out = echo_prior_output("hello world")
        assert "hello world" in out
        assert "Your prior output:" in out
        assert "---" in out

    def test_block_structure_has_delimiters(self):
        out = echo_prior_output("payload")
        # Header, opening fence, payload, closing fence, trailing newline
        lines = out.split("\n")
        assert lines[0] == "Your prior output:"
        assert lines[1] == "---"
        assert lines[2] == "payload"
        assert lines[3] == "---"

    def test_long_content_head_truncated(self):
        long = "HEAD" + "x" * (ECHO_HEAD * 2)
        out = echo_prior_output(long)
        assert "HEAD" in out
        assert "..." in out
        # Output should not contain the full padding
        assert out.count("x") < long.count("x")

    def test_truncation_threshold_at_echo_head(self):
        exactly_n = "a" * ECHO_HEAD
        over_n = "a" * (ECHO_HEAD + 1)
        out_exact = echo_prior_output(exactly_n)
        out_over = echo_prior_output(over_n)
        # Exactly N chars: no ellipsis
        assert "..." not in out_exact
        # Over N chars: ellipsis appended
        assert "..." in out_over

    def test_strips_leading_trailing_whitespace(self):
        out = echo_prior_output("  payload  \n\n")
        # Quoted content should be trimmed
        assert "payload" in out
        # Should not have raw whitespace lines around it
        assert "\n\n  payload" not in out

    def test_does_not_reference_provider_or_channel_names(self):
        # Contract invariant: primitive output must not leak runtime concepts.
        out = echo_prior_output("anything")
        forbidden = ["ollama", "anthropic", "openai", "vllm", "thinking", "reasoning"]
        lowered = out.lower()
        for word in forbidden:
            assert word not in lowered, f"primitive leaked '{word}'"


class TestConstrainFormatJson:
    def test_returns_nonempty_string(self):
        out = constrain_format_json()
        assert isinstance(out, str)
        assert len(out) > 0

    def test_mentions_required_envelope_fields(self):
        out = constrain_format_json()
        assert "thought" in out
        assert "action" in out
        assert "action_input" in out

    def test_forbids_markdown_fences(self):
        out = constrain_format_json()
        assert "markdown" in out.lower() or "fences" in out.lower()


class TestConstrainActionRequired:
    def test_returns_nonempty_string(self):
        out = constrain_action_required()
        assert isinstance(out, str)
        assert len(out) > 0

    def test_presents_both_action_paths(self):
        out = constrain_action_required()
        assert '"action": "tool_name"' in out
        assert '"action": "complete"' in out
