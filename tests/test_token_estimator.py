"""Tests for context/token_estimator."""

from agent_cli.context.token_estimator import (
    estimate_tokens,
    estimate_tokens_from_messages,
)


class TestEstimateTokens:
    def test_basic(self):
        assert estimate_tokens("abcd") == 1
        assert estimate_tokens("a" * 100) == 25

    def test_empty(self):
        assert estimate_tokens("") == 0


class TestEstimateFromMessages:
    def test_single_message(self):
        msgs = [{"role": "user", "content": "a" * 40}]
        assert estimate_tokens_from_messages(msgs) == 10 + 4  # 40/4 + overhead

    def test_multiple(self):
        msgs = [
            {"role": "user", "content": "a" * 40},
            {"role": "assistant", "content": "b" * 80},
        ]
        assert estimate_tokens_from_messages(msgs) == (10 + 4) + (20 + 4)

    def test_empty_list(self):
        assert estimate_tokens_from_messages([]) == 0
