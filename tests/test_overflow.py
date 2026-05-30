"""Tests for context/overflow."""

from agent_cli.context.overflow import (
    is_context_overflow,
    check_preemptive_overflow,
    parse_overflow_amounts,
)
from agent_cli.providers.compat import ModelCapabilities

# Verified live against an omlx server (Qwen3.6-27B-MLX-8bit, 2026-05-30):
# a prompt over the configured context returns HTTP 400 with this body.
OMLX_400_MESSAGE = (
    "Prompt too long: 360012 tokens exceeds max context window of 262144 tokens"
)


class TestIsContextOverflow:
    def test_anthropic_pattern(self):
        assert is_context_overflow("Error: prompt is too long") is True

    def test_openai_pattern(self):
        assert (
            is_context_overflow("This model's maximum context length is 4096 tokens")
            is True
        )

    def test_context_length_exceeded_pattern(self):
        assert is_context_overflow("context length exceeded") is True

    def test_generic_pattern(self):
        assert is_context_overflow("too many tokens in request") is True

    def test_omlx_pattern(self):
        """The real omlx 400 body must classify as overflow."""
        assert is_context_overflow(OMLX_400_MESSAGE) is True

    def test_unrelated_error(self):
        assert is_context_overflow("Connection refused") is False
        assert is_context_overflow("404 Not Found") is False

    def test_unrelated_400_not_overflow(self):
        """A non-overflow 400 must NOT be treated as overflow — otherwise
        the recovery layer would shed history to 'fix' an unrelated bug."""
        assert (
            is_context_overflow("invalid_request_error: unknown field 'foo'") is False
        )

    def test_empty(self):
        assert is_context_overflow("") is False


class TestParseOverflowAmounts:
    def test_omlx_real_message(self):
        assert parse_overflow_amounts(OMLX_400_MESSAGE) == (360012, 262144)

    def test_anthropic_shape(self):
        msg = "prompt is too long: 270000 tokens > 262144 maximum"
        assert parse_overflow_amounts(msg) == (270000, 262144)

    def test_openai_shape_limit_first(self):
        msg = (
            "This model's maximum context length is 8192 tokens. However, "
            "your messages resulted in 10000 tokens"
        )
        # actual=10000, limit=8192 — order is reversed vs omlx/anthropic
        assert parse_overflow_amounts(msg) == (10000, 8192)

    def test_overflow_without_numbers_returns_none(self):
        # Classified as overflow, but no amounts to extract.
        assert parse_overflow_amounts("context length exceeded") == (None, None)

    def test_empty_returns_none(self):
        assert parse_overflow_amounts("") == (None, None)

    def test_unrelated_message_returns_none(self):
        assert parse_overflow_amounts("Connection refused") == (None, None)


class TestCheckPreemptiveOverflow:
    def test_within_limit(self):
        caps = ModelCapabilities(
            context_window=4096,
            max_output_tokens=512,
            supports_structured_output=False,
            supports_thinking=False,
            thinking_budget=0,
            supports_strict_schema=False,
        )
        msgs = [{"role": "user", "content": "a" * 100}]  # ~25 tokens
        assert check_preemptive_overflow(msgs, caps) is False

    def test_over_limit(self):
        caps = ModelCapabilities(
            context_window=100,
            max_output_tokens=50,
            supports_structured_output=False,
            supports_thinking=False,
            thinking_budget=0,
            supports_strict_schema=False,
        )
        # 4000 chars ≈ 1000 tokens, context_window=100, reserve=2048
        msgs = [{"role": "user", "content": "a" * 4000}]
        assert check_preemptive_overflow(msgs, caps) is True
