"""Tests for context/manager."""

from unittest.mock import MagicMock

import pytest

from agent_cli.context.manager import ContextManager
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.compat import ModelCapabilities


@pytest.fixture
def caps():
    return ModelCapabilities(
        context_window=1000,  # Small window to trigger compression easily
        max_output_tokens=200,
        supports_structured_output=False,
        supports_tool_calling=False,
        supports_thinking=False,
        thinking_budget=0,
        supports_strict_schema=False,
    )


@pytest.fixture
def mock_provider():
    provider = MagicMock()
    provider.call.return_value = LLMResponse(
        content="## Goal\nTest goal\n## Progress\nDone step 1"
    )
    return provider


class TestContextManager:
    def test_add_and_get(self, mock_provider, caps, tmp_path):
        ctx = ContextManager(mock_provider, "test-model", caps, scratchpad_dir=tmp_path)
        ctx.add("user", "hello")
        ctx.add("assistant", "hi")
        msgs = ctx.get_messages()
        # Messages include user+assistant (no scratchpad since no scratchpad.md exists)
        user_msgs = [m for m in msgs if m["content"] == "hello"]
        assert len(user_msgs) == 1

    def test_summary_prepended(self, mock_provider, caps, tmp_path):
        ctx = ContextManager(mock_provider, "test-model", caps, scratchpad_dir=tmp_path)
        ctx._summary = "Previous summary here"
        ctx.add("user", "new message")
        msgs = ctx.get_messages()
        summary_msgs = [
            m for m in msgs if "[Previous conversation summary]" in m.get("content", "")
        ]
        assert len(summary_msgs) == 1

    def test_compression_triggered(self, mock_provider, caps, tmp_path):
        ctx = ContextManager(
            mock_provider, "test-model", caps, keep_recent=1, scratchpad_dir=tmp_path
        )
        # Add enough messages to exceed max_context_chars
        for i in range(10):
            ctx.add("user", "x" * 500)
            ctx.add("assistant", "y" * 500)

        # Provider should have been called for compression
        assert mock_provider.call.called
        assert ctx._summary is not None

    def test_incremental_update(self, mock_provider, caps, tmp_path):
        ctx = ContextManager(
            mock_provider, "test-model", caps, keep_recent=1, scratchpad_dir=tmp_path
        )
        ctx._summary = "Existing summary"

        # Add messages to trigger compression
        for i in range(10):
            ctx.add("user", "x" * 500)
            ctx.add("assistant", "y" * 500)

        # Check that incremental prompt was used (contains "Existing Summary" section header)
        call_args = mock_provider.call.call_args
        messages_arg = call_args.kwargs.get("messages") or call_args[1].get("messages")
        prompt_text = messages_arg[0]["content"]
        assert "## Existing Summary" in prompt_text
        assert "## New Conversation to Incorporate" in prompt_text

    def test_force_compress(self, mock_provider, caps, tmp_path):
        ctx = ContextManager(
            mock_provider, "test-model", caps, keep_recent=1, scratchpad_dir=tmp_path
        )
        for i in range(5):
            ctx.messages.append({"role": "user", "content": f"msg{i}"})
            ctx.messages.append({"role": "assistant", "content": f"reply{i}"})

        ctx.force_compress()
        assert mock_provider.call.called

    def test_get_estimated_tokens(self, mock_provider, caps, tmp_path):
        ctx = ContextManager(mock_provider, "test-model", caps, scratchpad_dir=tmp_path)
        ctx.add("user", "a" * 100)
        tokens = ctx.get_estimated_tokens()
        assert tokens > 0

    def test_serialize_truncates_long_content(self, mock_provider, caps, tmp_path):
        ctx = ContextManager(mock_provider, "test-model", caps, scratchpad_dir=tmp_path)
        msgs = [{"role": "user", "content": "x" * 5000}]
        serialized = ctx._serialize_messages(msgs)
        assert "truncated" in serialized
        assert len(serialized) < 5000


class TestSerializationTruncation:
    def test_under_limit_not_truncated(self, mock_provider, caps, tmp_path):
        ctx = ContextManager(mock_provider, "test-model", caps, scratchpad_dir=tmp_path)
        msgs = [{"role": "user", "content": "a" * 1999}]
        serialized = ctx._serialize_messages(msgs)
        assert "truncated" not in serialized

    def test_exact_limit_not_truncated(self, mock_provider, caps, tmp_path):
        ctx = ContextManager(mock_provider, "test-model", caps, scratchpad_dir=tmp_path)
        msgs = [{"role": "user", "content": "a" * 2000}]
        serialized = ctx._serialize_messages(msgs)
        assert "truncated" not in serialized

    def test_over_limit_truncated(self, mock_provider, caps, tmp_path):
        ctx = ContextManager(mock_provider, "test-model", caps, scratchpad_dir=tmp_path)
        msgs = [{"role": "user", "content": "a" * 2001}]
        serialized = ctx._serialize_messages(msgs)
        assert "1 more characters truncated" in serialized

    def test_truncation_in_compression_prompt(self, mock_provider, caps, tmp_path):
        """Verify truncated content reaches the LLM during compression."""
        ctx = ContextManager(
            mock_provider, "test-model", caps, keep_recent=1, scratchpad_dir=tmp_path
        )
        # Add messages with long tool result to trigger compression
        for _ in range(10):
            ctx.add("user", "short query")
            ctx.add("assistant", "z" * 5000)  # long tool result

        # Check the prompt sent to LLM for compression
        if mock_provider.call.called:
            call_args = mock_provider.call.call_args
            messages_arg = call_args.kwargs.get("messages") or call_args[1].get(
                "messages"
            )
            prompt_text = messages_arg[0]["content"]
            assert "truncated" in prompt_text
