"""Tests for agent loop (integration with mocked provider)."""
import json
from unittest.mock import MagicMock, patch

import pytest

from agent_cli.loop import run_loop
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.compat import ModelCapabilities


@pytest.fixture
def caps():
    return ModelCapabilities(
        context_window=32768, max_output_tokens=4096,
        supports_structured_output=True, supports_tool_calling=False,
        supports_thinking=False, thinking_budget=0, supports_strict_schema=False,
    )


def _make_provider(*responses):
    """Create a mock provider that returns responses in sequence."""
    provider = MagicMock()
    provider.call.side_effect = [
        LLMResponse(content=r) for r in responses
    ]
    return provider


class TestRunLoopFinalAnswer:
    def test_direct_final_answer(self, caps):
        provider = _make_provider(
            json.dumps({"thought": "simple question", "final_answer": "42"})
        )
        result = run_loop(
            query="What is the answer?",
            provider=provider,
            capabilities=caps,
            model="test-model",
            quiet=True,
        )
        assert result == "42"

    def test_final_answer_after_tool(self, caps, tmp_path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")

        provider = _make_provider(
            json.dumps({
                "thought": "read file",
                "action": "read_file",
                "action_input": {"path": str(test_file)},
            }),
            json.dumps({
                "thought": "got it",
                "final_answer": "File contains: hello world",
            }),
        )
        result = run_loop(
            query="Read test.txt",
            provider=provider,
            capabilities=caps,
            model="test-model",
            quiet=True,
        )
        assert "hello world" in result


class TestRunLoopToolExecution:
    def test_shell_tool(self, caps):
        provider = _make_provider(
            json.dumps({
                "thought": "run echo",
                "action": "shell",
                "action_input": {"command": "echo hello"},
            }),
            json.dumps({
                "thought": "done",
                "final_answer": "Executed echo",
            }),
        )
        result = run_loop(
            query="Run echo hello",
            provider=provider,
            capabilities=caps,
            model="test-model",
            quiet=True,
        )
        assert result is not None

    def test_unknown_tool(self, caps):
        provider = _make_provider(
            json.dumps({
                "thought": "t",
                "action": "nonexistent_tool",
                "action_input": {},
            }),
            json.dumps({
                "thought": "t",
                "final_answer": "ok",
            }),
        )
        result = run_loop(
            query="Do something",
            provider=provider,
            capabilities=caps,
            model="test-model",
            quiet=True,
        )
        assert result == "ok"


class TestRunLoopParseFailure:
    def test_retry_on_bad_json(self, caps):
        provider = _make_provider(
            "This is not JSON at all",  # Will fail parsing
            json.dumps({"thought": "ok", "final_answer": "recovered"}),
        )
        result = run_loop(
            query="What?",
            provider=provider,
            capabilities=caps,
            model="test-model",
            quiet=True,
            max_iter=5,
        )
        assert result == "recovered"


class TestRunLoopMaxIter:
    def test_returns_none_on_max_iter(self, caps):
        provider = _make_provider(
            json.dumps({"thought": "thinking", "action": "shell", "action_input": {"command": "echo 1"}}),
            json.dumps({"thought": "thinking", "action": "shell", "action_input": {"command": "echo 2"}}),
            json.dumps({"thought": "thinking", "action": "shell", "action_input": {"command": "echo 3"}}),
        )
        result = run_loop(
            query="Keep going",
            provider=provider,
            capabilities=caps,
            model="test-model",
            quiet=True,
            max_iter=2,
        )
        assert result is None


class TestRunLoopQuietMode:
    def test_quiet_no_render(self, caps, capsys):
        provider = _make_provider(
            json.dumps({"thought": "t", "final_answer": "answer"})
        )
        result = run_loop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="test-model",
            quiet=True,
        )
        assert result == "answer"


@pytest.fixture
def caps_tc():
    """Capabilities with tool calling enabled."""
    return ModelCapabilities(
        context_window=128000, max_output_tokens=4096,
        supports_structured_output=True, supports_tool_calling=True,
        supports_thinking=False, thinking_budget=0, supports_strict_schema=True,
    )


class TestRunLoopNativeToolCalling:
    def test_anthropic_tool_call_then_final(self, caps_tc, tmp_path):
        """Native tool_calls → execute → text final_answer."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")

        provider = MagicMock()
        provider.call.side_effect = [
            # First call: tool_use response
            LLMResponse(
                content="I'll read the file.",
                tool_calls=[{
                    "id": "tu_1",
                    "name": "read_file",
                    "input": {"path": str(test_file)},
                }],
            ),
            # Second call: final answer (text, no tool_calls)
            LLMResponse(
                content=json.dumps({"thought": "got it", "final_answer": "File contains hello world"}),
                tool_calls=None,
            ),
        ]

        result = run_loop(
            query="Read the file",
            provider=provider,
            capabilities=caps_tc,
            model="claude-sonnet-4-20250514",
            provider_name="anthropic",
            quiet=True,
        )

        assert result is not None
        assert "hello world" in result
        assert provider.call.call_count == 2

    def test_openai_tool_call(self, caps_tc):
        """OpenAI native tool_calls → execute → final."""
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(
                content="",
                tool_calls=[{
                    "id": "call_1",
                    "name": "shell",
                    "input": {"command": "echo hi"},
                }],
            ),
            LLMResponse(
                content=json.dumps({"thought": "done", "final_answer": "Executed"}),
                tool_calls=None,
            ),
        ]

        result = run_loop(
            query="Run echo",
            provider=provider,
            capabilities=caps_tc,
            model="gpt-4o",
            provider_name="openai",
            quiet=True,
        )

        assert result == "Executed"

    def test_text_parsing_regression(self, caps):
        """When tool_calls=None, should fall back to text parsing."""
        provider = _make_provider(
            json.dumps({"thought": "t", "action": "shell", "action_input": {"command": "echo hi"}}),
            json.dumps({"thought": "done", "final_answer": "ok"}),
        )

        result = run_loop(
            query="Run echo",
            provider=provider,
            capabilities=caps,
            model="test-model",
            quiet=True,
        )

        assert result == "ok"
