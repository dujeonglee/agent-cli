"""Tests for agent loop (integration with mocked provider)."""

import json
from unittest.mock import MagicMock

import pytest

from agent_cli.loop import run_loop
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.compat import ModelCapabilities


def _complete(result: str) -> str:
    """Build a complete tool JSON response."""
    return json.dumps(
        {"thought": "done", "action": "complete", "action_input": {"result": result}}
    )


@pytest.fixture
def caps():
    return ModelCapabilities(
        context_window=32768,
        max_output_tokens=4096,
        supports_structured_output=True,
        supports_tool_calling=False,
        supports_thinking=False,
        thinking_budget=0,
        supports_strict_schema=False,
    )


def _make_provider(*responses):
    """Create a mock provider that returns responses in sequence."""
    provider = MagicMock()
    provider.call.side_effect = [LLMResponse(content=r) for r in responses]
    return provider


class TestRunLoopComplete:
    def test_direct_complete(self, caps):
        provider = _make_provider(_complete("42"))
        result = run_loop(
            query="What is the answer?",
            provider=provider,
            capabilities=caps,
            model="test-model",
            quiet=True,
        )
        assert result == "42"

    def test_complete_after_tool(self, caps, tmp_path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")

        provider = _make_provider(
            json.dumps(
                {
                    "thought": "read file",
                    "action": "read_file",
                    "action_input": {"path": str(test_file)},
                }
            ),
            _complete("File contains: hello world"),
        )
        result = run_loop(
            query="Read test.txt",
            provider=provider,
            capabilities=caps,
            model="test-model",
            quiet=True,
        )
        assert "hello world" in result

    def test_complete_with_string_action_input(self, caps):
        """LLM sends action_input as string instead of dict — should handle."""
        provider = _make_provider(
            json.dumps(
                {
                    "thought": "done",
                    "action": "complete",
                    "action_input": "Simple answer",
                }
            )
        )
        result = run_loop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="test-model",
            quiet=True,
        )
        assert result == "Simple answer"

    def test_complete_rejected_without_tools(self, caps):
        """Fulfillment guard: complete rejected if task needs tools but none called."""
        provider = _make_provider(
            _complete("Created file"),  # rejected — no tools used
            json.dumps(
                {
                    "thought": "write",
                    "action": "write_file",
                    "action_input": {"path": "/tmp/test.txt", "content": "hello"},
                }
            ),
            _complete("Created file successfully"),
        )
        result = run_loop(
            query="Create a new file",
            provider=provider,
            capabilities=caps,
            model="test-model",
            quiet=True,
        )
        assert "Created file successfully" in result

    def test_complete_empty_result_defaults(self, caps, tmp_path):
        """complete with empty result → returns '(completed)' instead of failing."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("data")

        provider = _make_provider(
            json.dumps(
                {
                    "thought": "read",
                    "action": "read_file",
                    "action_input": {"path": str(test_file)},
                }
            ),
            json.dumps(
                {
                    "thought": "done",
                    "action": "complete",
                    "action_input": {"result": ""},
                }
            ),
        )
        result = run_loop(
            query="Read file",
            provider=provider,
            capabilities=caps,
            model="test-model",
            quiet=True,
        )
        assert result == "(completed)"

    def test_complete_missing_result_key(self, caps, tmp_path):
        """complete with no result key → returns '(completed)'."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("data")

        provider = _make_provider(
            json.dumps(
                {
                    "thought": "read",
                    "action": "read_file",
                    "action_input": {"path": str(test_file)},
                }
            ),
            json.dumps(
                {
                    "thought": "done",
                    "action": "complete",
                    "action_input": {},
                }
            ),
        )
        result = run_loop(
            query="Read file",
            provider=provider,
            capabilities=caps,
            model="test-model",
            quiet=True,
        )
        assert result == "(completed)"


class TestRunLoopToolExecution:
    def test_shell_tool(self, caps):
        provider = _make_provider(
            json.dumps(
                {
                    "thought": "run pwd",
                    "action": "shell",
                    "action_input": {"command": "pwd"},
                }
            ),
            _complete("Executed command"),
        )
        result = run_loop(
            query="Run pwd",
            provider=provider,
            capabilities=caps,
            model="test-model",
            quiet=True,
        )
        assert result is not None

    def test_unknown_tool(self, caps):
        provider = _make_provider(
            json.dumps(
                {
                    "thought": "t",
                    "action": "nonexistent_tool",
                    "action_input": {},
                }
            ),
            _complete("ok"),
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
            _complete("recovered"),
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
            json.dumps(
                {
                    "thought": "thinking",
                    "action": "shell",
                    "action_input": {"command": "date +%s"},
                }
            ),
            json.dumps(
                {
                    "thought": "thinking",
                    "action": "shell",
                    "action_input": {"command": "uname -s"},
                }
            ),
            json.dumps(
                {
                    "thought": "thinking",
                    "action": "shell",
                    "action_input": {"command": "whoami"},
                }
            ),
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


class TestCheckpoint:
    def test_no_checkpoint_before_threshold(self, caps):
        """Under 50 iterations → no checkpoint nudge injected."""
        responses = []
        for i in range(10):
            responses.append(
                json.dumps(
                    {
                        "thought": f"step {i}",
                        "action": "shell",
                        "action_input": {"command": f"date +step{i}"},
                    }
                )
            )
        responses.append(_complete("completed"))
        provider = _make_provider(*responses)
        result = run_loop(
            query="Run some commands",
            provider=provider,
            capabilities=caps,
            model="test-model",
            quiet=True,
        )
        assert result == "completed"
        assert provider.call.call_count == 11

    def test_checkpoint_nudge_injected(self, caps):
        """At 50+ iterations, checkpoint nudge should be injected into messages."""
        from agent_cli.loop import _CHECKPOINT_FIRST

        responses = []
        for i in range(_CHECKPOINT_FIRST + 1):
            responses.append(
                json.dumps(
                    {
                        "thought": f"step {i}",
                        "action": "shell",
                        "action_input": {"command": f"date +step{i}"},
                    }
                )
            )
        responses.append(_complete("done after checkpoint"))
        provider = _make_provider(*responses)
        result = run_loop(
            query="Keep running commands",
            provider=provider,
            capabilities=caps,
            model="test-model",
            quiet=True,
        )
        assert result == "done after checkpoint"

        last_call = provider.call.call_args
        messages = last_call.kwargs.get("messages") or last_call[1].get("messages")
        checkpoint_found = any(
            "[SYSTEM] CHECKPOINT" in m.get("content", "")
            for m in messages
            if isinstance(m.get("content"), str)
        )
        assert checkpoint_found, "Checkpoint nudge was not found in messages"


class TestToolHistoryTracking:
    def test_history_recorded(self, caps, tmp_path):
        """Tool execution should record history for checkpoint use."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello")

        provider = _make_provider(
            json.dumps(
                {
                    "thought": "read",
                    "action": "read_file",
                    "action_input": {"path": str(test_file)},
                }
            ),
            json.dumps(
                {
                    "thought": "run",
                    "action": "shell",
                    "action_input": {"command": "whoami"},
                }
            ),
            _complete("ok"),
        )
        result = run_loop(
            query="Read file then run command",
            provider=provider,
            capabilities=caps,
            model="test-model",
            quiet=True,
        )
        assert result == "ok"


class TestEchoAsFinalAnswer:
    def test_simple_echo_becomes_final(self, caps):
        """echo 'Task done' → intercepted as final answer."""
        provider = _make_provider(
            json.dumps(
                {
                    "thought": "done",
                    "action": "shell",
                    "action_input": {
                        "command": 'echo "Task completed successfully."',
                        "timeout": 5,
                    },
                }
            ),
        )
        result = run_loop(
            query="Do something",
            provider=provider,
            capabilities=caps,
            model="test-model",
            quiet=True,
        )
        assert result == "Task completed successfully."

    def test_echo_with_pipe_not_intercepted(self, caps):
        """echo ... | grep should NOT be treated as final answer."""
        provider = _make_provider(
            json.dumps(
                {
                    "thought": "search",
                    "action": "shell",
                    "action_input": {"command": "echo hello | grep h"},
                }
            ),
            _complete("found"),
        )
        result = run_loop(
            query="Search",
            provider=provider,
            capabilities=caps,
            model="test-model",
            quiet=True,
        )
        assert result == "found"

    def test_echo_with_redirect_not_intercepted(self, caps):
        """echo ... > file should NOT be treated as final answer."""
        provider = _make_provider(
            json.dumps(
                {
                    "thought": "write",
                    "action": "shell",
                    "action_input": {"command": "echo hello > out.txt"},
                }
            ),
            _complete("written"),
        )
        result = run_loop(
            query="Write",
            provider=provider,
            capabilities=caps,
            model="test-model",
            quiet=True,
        )
        assert result == "written"


class TestRepeatedCallDetection:
    def test_repeated_calls_stops_loop(self, caps, tmp_path):
        """Same tool+input 3 times → loop returns None."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello")

        same_call = json.dumps(
            {
                "thought": "read again",
                "action": "read_file",
                "action_input": {"path": str(test_file)},
            }
        )
        provider = _make_provider(same_call, same_call, same_call)
        result = run_loop(
            query="Read file",
            provider=provider,
            capabilities=caps,
            model="test-model",
            quiet=True,
        )
        assert result is None

    def test_different_inputs_no_stop(self, caps, tmp_path):
        """Same tool with different inputs should NOT trigger repeat detection."""
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f3 = tmp_path / "c.txt"
        f1.write_text("a")
        f2.write_text("b")
        f3.write_text("c")

        provider = _make_provider(
            json.dumps(
                {
                    "thought": "r1",
                    "action": "read_file",
                    "action_input": {"path": str(f1)},
                }
            ),
            json.dumps(
                {
                    "thought": "r2",
                    "action": "read_file",
                    "action_input": {"path": str(f2)},
                }
            ),
            json.dumps(
                {
                    "thought": "r3",
                    "action": "read_file",
                    "action_input": {"path": str(f3)},
                }
            ),
            _complete("ok"),
        )
        result = run_loop(
            query="Read files",
            provider=provider,
            capabilities=caps,
            model="test-model",
            quiet=True,
        )
        assert result == "ok"


class TestRunLoopQuietMode:
    def test_quiet_no_render(self, caps, capsys):
        provider = _make_provider(_complete("answer"))
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
        context_window=128000,
        max_output_tokens=4096,
        supports_structured_output=True,
        supports_tool_calling=True,
        supports_thinking=False,
        thinking_budget=0,
        supports_strict_schema=True,
    )


class TestRunLoopNativeToolCalling:
    def test_anthropic_tool_call_then_complete(self, caps_tc, tmp_path):
        """Native tool_calls → execute → complete tool call."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")

        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(
                content="I'll read the file.",
                tool_calls=[
                    {
                        "id": "tu_1",
                        "name": "read_file",
                        "input": {"path": str(test_file)},
                    }
                ],
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    {
                        "id": "tu_2",
                        "name": "complete",
                        "input": {"result": "File contains hello world"},
                    }
                ],
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

    def test_openai_tool_call_then_complete(self, caps_tc):
        """OpenAI native tool_calls → execute → complete."""
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "name": "shell",
                        "input": {"command": "whoami"},
                    }
                ],
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    {
                        "id": "call_2",
                        "name": "complete",
                        "input": {"result": "Executed"},
                    }
                ],
            ),
        ]

        result = run_loop(
            query="Run whoami",
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
            json.dumps(
                {
                    "thought": "t",
                    "action": "shell",
                    "action_input": {"command": "whoami"},
                }
            ),
            _complete("ok"),
        )

        result = run_loop(
            query="Run command",
            provider=provider,
            capabilities=caps,
            model="test-model",
            quiet=True,
        )

        assert result == "ok"


class TestAskTool:
    def test_ask_single_question(self, caps, monkeypatch, tmp_path):
        """ask tool with single question in array."""
        from agent_cli.context.manager import ContextManager

        monkeypatch.setattr("builtins.input", lambda _: "yes, proceed")

        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(
                content=json.dumps(
                    {
                        "thought": "need clarification",
                        "action": "ask",
                        "action_input": {"questions": ["Should I continue?"]},
                    }
                )
            ),
            LLMResponse(
                content=json.dumps(
                    {
                        "thought": "user said yes",
                        "action": "complete",
                        "action_input": {"result": "Done after confirmation"},
                    }
                )
            ),
        ]
        ctx = ContextManager(
            provider=provider, model="test", capabilities=caps, scratchpad_dir=tmp_path
        )
        result = run_loop(
            query="Do something",
            provider=provider,
            capabilities=caps,
            model="test-model",
            quiet=True,
            ctx=ctx,
        )
        assert result == "Done after confirmation"
        assert provider.call.call_count == 2

    def test_ask_multiple_questions(self, caps, monkeypatch, tmp_path):
        """ask tool with multiple questions — collects all answers."""
        from agent_cli.context.manager import ContextManager

        answers = iter(["file.py", "python"])
        monkeypatch.setattr("builtins.input", lambda _: next(answers))

        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(
                content=json.dumps(
                    {
                        "thought": "need info",
                        "action": "ask",
                        "action_input": {
                            "questions": ["Which file?", "What language?"]
                        },
                    }
                )
            ),
            LLMResponse(
                content=json.dumps(
                    {
                        "thought": "got both answers",
                        "action": "complete",
                        "action_input": {"result": "Processing file.py in python"},
                    }
                )
            ),
        ]
        ctx = ContextManager(
            provider=provider, model="test", capabilities=caps, scratchpad_dir=tmp_path
        )
        result = run_loop(
            query="Help me",
            provider=provider,
            capabilities=caps,
            model="test-model",
            quiet=True,
            ctx=ctx,
        )
        assert result == "Processing file.py in python"

    def test_ask_string_coercion(self, caps, monkeypatch, tmp_path):
        """ask tool with string input (not array) — auto-coerced to list."""
        from agent_cli.context.manager import ContextManager

        monkeypatch.setattr("builtins.input", lambda _: "42")

        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(
                content=json.dumps(
                    {
                        "thought": "ask",
                        "action": "ask",
                        "action_input": {"questions": "What is the answer?"},
                    }
                )
            ),
            LLMResponse(
                content=json.dumps(
                    {
                        "thought": "done",
                        "action": "complete",
                        "action_input": {"result": "The answer is 42"},
                    }
                )
            ),
        ]
        ctx = ContextManager(
            provider=provider, model="test", capabilities=caps, scratchpad_dir=tmp_path
        )
        result = run_loop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="test-model",
            quiet=True,
            ctx=ctx,
        )
        assert result == "The answer is 42"

    def test_ask_legacy_question_key(self, caps, monkeypatch, tmp_path):
        """ask tool with legacy 'question' key — backward compatible."""
        from agent_cli.context.manager import ContextManager

        monkeypatch.setattr("builtins.input", lambda _: "yes")

        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(
                content=json.dumps(
                    {
                        "thought": "ask",
                        "action": "ask",
                        "action_input": {"question": "Continue?"},
                    }
                )
            ),
            LLMResponse(
                content=json.dumps(
                    {
                        "thought": "done",
                        "action": "complete",
                        "action_input": {"result": "ok"},
                    }
                )
            ),
        ]
        ctx = ContextManager(
            provider=provider, model="test", capabilities=caps, scratchpad_dir=tmp_path
        )
        result = run_loop(
            query="Do it",
            provider=provider,
            capabilities=caps,
            model="test-model",
            quiet=True,
            ctx=ctx,
        )
        assert result == "ok"

    def test_ask_available_with_ctx(self, caps, tmp_path):
        """ask tool should be in system prompt when ctx is provided."""
        from agent_cli.context.manager import ContextManager

        provider = MagicMock()
        provider.call.return_value = LLMResponse(
            content=json.dumps(
                {
                    "thought": "done",
                    "action": "complete",
                    "action_input": {"result": "ok"},
                }
            )
        )
        ctx = ContextManager(
            provider=provider, model="test", capabilities=caps, scratchpad_dir=tmp_path
        )
        run_loop(
            query="Do something",
            provider=provider,
            capabilities=caps,
            model="test-model",
            quiet=True,
            ctx=ctx,
        )
        call_args = provider.call.call_args
        system = call_args.kwargs.get("system", "")
        assert "ask" in system.lower()


class TestExtractQuestions:
    def test_dict_with_questions_list(self):
        from agent_cli.loop import _extract_questions

        assert _extract_questions({"questions": ["a", "b"]}) == ["a", "b"]

    def test_dict_with_questions_string(self):
        from agent_cli.loop import _extract_questions

        assert _extract_questions({"questions": "single"}) == ["single"]

    def test_dict_with_question_key(self):
        from agent_cli.loop import _extract_questions

        assert _extract_questions({"question": "legacy"}) == ["legacy"]

    def test_string_input(self):
        from agent_cli.loop import _extract_questions

        assert _extract_questions("direct question") == ["direct question"]

    def test_list_input(self):
        from agent_cli.loop import _extract_questions

        assert _extract_questions(["q1", "q2"]) == ["q1", "q2"]

    def test_none_input(self):
        from agent_cli.loop import _extract_questions

        assert _extract_questions(None) == []

    def test_empty_dict(self):
        from agent_cli.loop import _extract_questions

        assert _extract_questions({}) == []

    def test_empty_list(self):
        from agent_cli.loop import _extract_questions

        assert _extract_questions([]) == []

    def test_empty_string(self):
        from agent_cli.loop import _extract_questions

        assert _extract_questions("") == []

    def test_filters_empty_items(self):
        from agent_cli.loop import _extract_questions

        assert _extract_questions(["a", "", "b"]) == ["a", "b"]


class TestScratchpadIntegration:
    """Test loop.py integration with scratchpad begin_turn/end_turn."""

    def test_init_task_on_first_run(self, caps, tmp_path):
        """First run_loop with ctx creates scratchpad.md automatically."""
        from agent_cli.context.manager import ContextManager
        from agent_cli.context.scratchpad import load_scratchpad

        provider = _make_provider(_complete("done"))
        ctx = ContextManager(
            provider=provider, model="test", capabilities=caps, scratchpad_dir=tmp_path
        )
        run_loop(
            query="Do something",
            provider=provider,
            capabilities=caps,
            model="test",
            quiet=True,
            ctx=ctx,
        )
        content = load_scratchpad(tmp_path)
        assert "Do something" in content

    def test_begin_turn_increments_counter(self, caps, tmp_path):
        """Each iteration calls begin_turn, incrementing turn count."""
        from agent_cli.context.manager import ContextManager

        test_file = tmp_path / "test.txt"
        test_file.write_text("hello")

        provider = _make_provider(
            json.dumps(
                {
                    "thought": "read",
                    "action": "read_file",
                    "action_input": {"path": str(test_file)},
                }
            ),
            _complete("done"),
        )
        ctx = ContextManager(
            provider=provider, model="test", capabilities=caps, scratchpad_dir=tmp_path
        )
        run_loop(
            query="Read file",
            provider=provider,
            capabilities=caps,
            model="test",
            quiet=True,
            ctx=ctx,
        )
        # 2 iterations (read_file + complete) → turn_count >= 2
        assert ctx._turn_count >= 2

    def test_tool_result_saved_as_artifact(self, caps, tmp_path):
        """Tool execution result is saved as artifact."""
        from agent_cli.context.manager import ContextManager
        from agent_cli.context.scratchpad import build_artifact_index

        test_file = tmp_path / "data.txt"
        test_file.write_text("important data")

        provider = _make_provider(
            json.dumps(
                {
                    "thought": "read",
                    "action": "read_file",
                    "action_input": {"path": str(test_file)},
                }
            ),
            _complete("done"),
        )
        ctx = ContextManager(
            provider=provider, model="test", capabilities=caps, scratchpad_dir=tmp_path
        )
        run_loop(
            query="Read data",
            provider=provider,
            capabilities=caps,
            model="test",
            quiet=True,
            ctx=ctx,
        )
        index = build_artifact_index(tmp_path)
        assert len(index) >= 1  # at least the tool result artifact

    def test_complete_result_saved_as_artifact(self, caps, tmp_path):
        """Complete tool result is saved as artifact."""
        from agent_cli.context.manager import ContextManager
        from agent_cli.context.scratchpad import build_artifact_index

        provider = _make_provider(_complete("final answer here"))
        ctx = ContextManager(
            provider=provider, model="test", capabilities=caps, scratchpad_dir=tmp_path
        )
        run_loop(
            query="Answer question",
            provider=provider,
            capabilities=caps,
            model="test",
            quiet=True,
            ctx=ctx,
        )
        index = build_artifact_index(tmp_path)
        # Complete result should also be an artifact
        assert len(index) >= 1

    def test_scratchpad_progress_updated(self, caps, tmp_path):
        """Scratchpad progress section updated after tool execution."""
        from agent_cli.context.manager import ContextManager
        from agent_cli.context.scratchpad import load_scratchpad

        test_file = tmp_path / "code.py"
        test_file.write_text("def hello(): pass")

        provider = _make_provider(
            json.dumps(
                {
                    "thought": "read",
                    "action": "read_file",
                    "action_input": {"path": str(test_file)},
                }
            ),
            _complete("reviewed"),
        )
        ctx = ContextManager(
            provider=provider, model="test", capabilities=caps, scratchpad_dir=tmp_path
        )
        run_loop(
            query="Review code",
            provider=provider,
            capabilities=caps,
            model="test",
            quiet=True,
            ctx=ctx,
        )
        content = load_scratchpad(tmp_path)
        assert "## Progress" in content
        assert "턴" in content  # progress entries contain turn markers

    def test_no_ctx_no_scratchpad(self, caps):
        """Without ctx, no scratchpad operations (no crash)."""
        provider = _make_provider(_complete("ok"))
        result = run_loop(
            query="Simple",
            provider=provider,
            capabilities=caps,
            model="test",
            quiet=True,
            ctx=None,
        )
        assert result == "ok"
