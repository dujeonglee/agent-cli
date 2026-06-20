"""Tests for context/token_estimator."""

from agent_cli.context.token_estimator import estimate_tokens


class TestEstimateTokens:
    def test_basic(self):
        assert estimate_tokens("abcd") == 1
        assert estimate_tokens("a" * 100) == 25

    def test_empty(self):
        assert estimate_tokens("") == 0
