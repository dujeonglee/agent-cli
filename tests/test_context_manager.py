"""Tests for context/manager."""

import json
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
            m
            for m in msgs
            if "compressed" in m.get("content", "")
            and "Previous summary here" in m.get("content", "")
        ]
        assert len(summary_msgs) == 1
        # Verify resume instruction is included
        assert "Resume directly" in summary_msgs[0]["content"]
        # Verify assistant acknowledges without recap
        summary_idx = msgs.index(summary_msgs[0])
        assistant_reply = msgs[summary_idx + 1]
        assert assistant_reply["role"] == "assistant"
        assert "Resuming" in assistant_reply["content"]

    def test_no_summary_no_resume_injection(self, mock_provider, caps, tmp_path):
        """When there is no summary, no resume messages should be injected."""
        ctx = ContextManager(mock_provider, "test-model", caps, scratchpad_dir=tmp_path)
        ctx.add("user", "hello")
        ctx.add("assistant", "hi")
        msgs = ctx.get_messages()
        resume_msgs = [
            m for m in msgs if "Resuming where we left off" in m.get("content", "")
        ]
        assert len(resume_msgs) == 0
        compressed_msgs = [m for m in msgs if "compressed" in m.get("content", "")]
        assert len(compressed_msgs) == 0

    def test_summary_and_scratchpad_order(self, mock_provider, caps, tmp_path):
        """Scratchpad block should come before summary in message order."""
        # Create a scratchpad file so it gets injected
        scratchpad_file = tmp_path / "scratchpad.md"
        scratchpad_file.write_text("# Goal\nTest task")

        ctx = ContextManager(mock_provider, "test-model", caps, scratchpad_dir=tmp_path)
        ctx._summary = "Previous summary here"
        ctx.add("user", "continue")
        msgs = ctx.get_messages()

        # Find indices
        scratchpad_idx = None
        summary_idx = None
        for i, m in enumerate(msgs):
            if "Scratchpad" in m.get("content", ""):
                scratchpad_idx = i
            if "compressed" in m.get("content", ""):
                summary_idx = i

        assert scratchpad_idx is not None, "Scratchpad block should be present"
        assert summary_idx is not None, "Summary block should be present"
        assert scratchpad_idx < summary_idx, "Scratchpad should come before summary"

    def test_summary_injected_during_skill(self, mock_provider, caps, tmp_path):
        """Summary should still be injected even inside a skill context."""
        ctx = ContextManager(mock_provider, "test-model", caps, scratchpad_dir=tmp_path)
        ctx._summary = "Previous summary here"
        ctx.set_skill_context(skill_name="test_skill", parent_turn=1)
        ctx.add("user", "skill input")
        msgs = ctx.get_messages()

        # Summary should still be present
        summary_msgs = [m for m in msgs if "compressed" in m.get("content", "")]
        assert len(summary_msgs) == 1

        # But scratchpad should NOT be present (skill context skips it)
        scratchpad_msgs = [m for m in msgs if "Scratchpad" in m.get("content", "")]
        assert len(scratchpad_msgs) == 0

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
        call_args = mock_provider.call.call_args
        assert call_args.kwargs.get("skip_json_format") is True

    def test_force_compress_with_user_instruction(self, mock_provider, caps, tmp_path):
        ctx = ContextManager(
            mock_provider, "test-model", caps, keep_recent=1, scratchpad_dir=tmp_path
        )
        for i in range(5):
            ctx.messages.append({"role": "user", "content": f"msg{i}"})
            ctx.messages.append({"role": "assistant", "content": f"reply{i}"})

        ctx.force_compress(user_instruction="Focus on error analysis")
        assert mock_provider.call.called
        call_args = mock_provider.call.call_args
        system_arg = call_args.kwargs.get("system") or call_args[1].get("system")
        assert "Focus on error analysis" in system_arg

    def test_force_compress_no_instruction_no_extra(
        self, mock_provider, caps, tmp_path
    ):
        ctx = ContextManager(
            mock_provider, "test-model", caps, keep_recent=1, scratchpad_dir=tmp_path
        )
        for i in range(5):
            ctx.messages.append({"role": "user", "content": f"msg{i}"})
            ctx.messages.append({"role": "assistant", "content": f"reply{i}"})

        ctx.force_compress()
        call_args = mock_provider.call.call_args
        system_arg = call_args.kwargs.get("system") or call_args[1].get("system")
        assert "Additional Instruction" not in system_arg

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


class TestHybridCompaction:
    """Tests for rule-based file extraction + LLM summary hybrid."""

    def test_extract_files_from_tool_calls(self, mock_provider, caps, tmp_path):
        ctx = ContextManager(mock_provider, "test-model", caps, scratchpad_dir=tmp_path)
        msgs = [
            {
                "role": "assistant",
                "content": json.dumps(
                    {"action": "read_file", "action_input": {"path": "src/main.py"}}
                ),
            },
            {"role": "user", "content": "Observation: file contents"},
            {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "action": "edit_file",
                        "action_input": {"path": "src/main.py", "edits": []},
                    }
                ),
            },
            {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "action": "write_file",
                        "action_input": {"path": "tests/test.py", "content": "..."},
                    }
                ),
            },
        ]
        read, modified = ctx._extract_files_touched(msgs)
        assert read == {"src/main.py"}
        assert modified == {"src/main.py", "tests/test.py"}

    def test_extract_skips_non_json(self, mock_provider, caps, tmp_path):
        ctx = ContextManager(mock_provider, "test-model", caps, scratchpad_dir=tmp_path)
        msgs = [
            {"role": "assistant", "content": "plain text response"},
            {"role": "user", "content": "hello"},
        ]
        read, modified = ctx._extract_files_touched(msgs)
        assert read == set()
        assert modified == set()

    def test_extract_skips_shell_commands(self, mock_provider, caps, tmp_path):
        ctx = ContextManager(mock_provider, "test-model", caps, scratchpad_dir=tmp_path)
        msgs = [
            {
                "role": "assistant",
                "content": json.dumps(
                    {"action": "shell", "action_input": {"command": "ls -la"}}
                ),
            },
        ]
        read, modified = ctx._extract_files_touched(msgs)
        assert read == set()
        assert modified == set()

    def test_format_files_touched(self, mock_provider, caps, tmp_path):
        ctx = ContextManager(mock_provider, "test-model", caps, scratchpad_dir=tmp_path)
        result = ctx._format_files_touched({"a.py", "b.py"}, {"a.py"})
        assert "## Files Touched" in result
        assert "a.py" in result
        assert "b.py" in result
        assert "- Read:" in result
        assert "- Modified:" in result

    def test_format_files_touched_empty(self, mock_provider, caps, tmp_path):
        ctx = ContextManager(mock_provider, "test-model", caps, scratchpad_dir=tmp_path)
        result = ctx._format_files_touched(set(), set())
        assert "(none)" in result

    def test_parse_files_from_summary(self, mock_provider, caps, tmp_path):
        ctx = ContextManager(mock_provider, "test-model", caps, scratchpad_dir=tmp_path)
        summary = (
            "## Goal\nSome goal\n\n"
            "## Files Touched\n"
            "- Read: a.py, b.py\n"
            "- Modified: a.py\n\n"
            "## Other\nstuff"
        )
        read, modified = ctx._parse_files_from_summary(summary)
        assert read == {"a.py", "b.py"}
        assert modified == {"a.py"}

    def test_parse_files_no_section(self, mock_provider, caps, tmp_path):
        ctx = ContextManager(mock_provider, "test-model", caps, scratchpad_dir=tmp_path)
        summary = "## Goal\nSome goal\n\n## Working State\nAll good"
        read, modified = ctx._parse_files_from_summary(summary)
        assert read == set()
        assert modified == set()

    def test_compress_includes_files_section(self, mock_provider, caps, tmp_path):
        """After compression, summary should contain rule-based Files Touched."""
        ctx = ContextManager(
            mock_provider, "test-model", caps, keep_recent=1, scratchpad_dir=tmp_path
        )
        # Add messages with tool calls
        for _ in range(5):
            ctx.messages.append(
                {
                    "role": "user",
                    "content": "x" * 200,
                }
            )
            ctx.messages.append(
                {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "action": "read_file",
                            "action_input": {"path": "config.py"},
                        }
                    ),
                }
            )

        ctx.force_compress()
        assert "## Files Touched" in ctx._summary
        assert "config.py" in ctx._summary

    def test_incremental_compress_merges_files(self, mock_provider, caps, tmp_path):
        """Incremental compression should merge files from prior summary."""
        ctx = ContextManager(
            mock_provider, "test-model", caps, keep_recent=1, scratchpad_dir=tmp_path
        )
        # Set up existing summary with prior files
        ctx._summary = (
            "## Goal\nOld goal\n\n## Files Touched\n- Read: old.py\n- Modified: (none)"
        )
        # Add new messages with different file
        for _ in range(5):
            ctx.messages.append({"role": "user", "content": "x" * 200})
            ctx.messages.append(
                {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "action": "read_file",
                            "action_input": {"path": "new.py"},
                        }
                    ),
                }
            )

        ctx.force_compress()
        assert "old.py" in ctx._summary  # preserved from prior
        assert "new.py" in ctx._summary  # added from new messages


class TestCompressionFailureTracking:
    def test_failure_increments_counter(self, caps, tmp_path):
        """Compression failure increments counter and raises threshold."""
        provider = MagicMock()
        provider.call.side_effect = RuntimeError("LLM unavailable")

        ctx = ContextManager(
            provider, "test-model", caps, keep_recent=1, scratchpad_dir=tmp_path
        )
        original = ctx.max_context_chars

        # Fill messages to trigger compression
        for _ in range(10):
            ctx.add("user", "x" * 200)
            ctx.add("assistant", "y" * 200)

        assert ctx._compress_failures >= 1
        assert ctx.max_context_chars > original

    def test_threshold_capped_at_2x(self, caps, tmp_path):
        """Threshold increase capped at 2x original."""
        provider = MagicMock()
        provider.call.side_effect = RuntimeError("fail")

        ctx = ContextManager(
            provider, "test-model", caps, keep_recent=1, scratchpad_dir=tmp_path
        )
        original = ctx._original_max_context_chars

        # Force many compressions
        for _ in range(50):
            ctx.add("user", "x" * 200)
            ctx.add("assistant", "y" * 200)

        assert ctx.max_context_chars <= original * 2

    def test_success_resets_counter(self, caps, tmp_path):
        """Successful compression resets failure counter and threshold."""
        provider = MagicMock()
        provider.call.return_value = LLMResponse(content="Summary")

        ctx = ContextManager(
            provider, "test-model", caps, keep_recent=1, scratchpad_dir=tmp_path
        )
        # Simulate previous failures
        ctx._compress_failures = 2

        # Add enough messages then directly call _compress
        for _ in range(10):
            ctx.messages.append({"role": "user", "content": "x" * 200})
            ctx.messages.append({"role": "assistant", "content": "y" * 200})
        ctx._compress()

        assert ctx._compress_failures == 0
        assert ctx.max_context_chars == ctx._original_max_context_chars

    def test_alerts_user_after_max_failures(self, caps, tmp_path):
        """After 3+ failures, Rich console warning is shown."""
        from unittest.mock import patch

        provider = MagicMock()
        provider.call.side_effect = RuntimeError("fail")

        ctx = ContextManager(
            provider, "test-model", caps, keep_recent=1, scratchpad_dir=tmp_path
        )
        ctx._compress_failures = 3  # already at max

        with patch("agent_cli.render.console") as mock_console:
            # Add messages and trigger compression
            for _ in range(10):
                ctx.messages.append({"role": "user", "content": "x" * 200})
                ctx.messages.append({"role": "assistant", "content": "y" * 200})
            ctx._compress()

            assert ctx._compress_failures == 4
            mock_console.print.assert_called()
            call_str = str(mock_console.print.call_args)
            assert "failed" in call_str.lower()
