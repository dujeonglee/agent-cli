"""Tests for context/overflow."""

from agent_cli.context.overflow import is_context_overflow, check_preemptive_overflow
from agent_cli.providers.compat import ModelCapabilities


class TestIsContextOverflow:
    def test_anthropic_pattern(self):
        assert is_context_overflow("Error: prompt is too long") is True

    def test_openai_pattern(self):
        assert (
            is_context_overflow("This model's maximum context length is 4096 tokens")
            is True
        )

    def test_ollama_pattern(self):
        assert is_context_overflow("context length exceeded") is True

    def test_generic_pattern(self):
        assert is_context_overflow("too many tokens in request") is True

    def test_unrelated_error(self):
        assert is_context_overflow("Connection refused") is False
        assert is_context_overflow("404 Not Found") is False

    def test_empty(self):
        assert is_context_overflow("") is False


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
