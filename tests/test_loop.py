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
            suppress_output=True,
        )
        assert result.output == "42"

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
            suppress_output=True,
        )
        assert "hello world" in result.output

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
            suppress_output=True,
        )
        assert result.output == "Simple answer"

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
            suppress_output=True,
        )
        assert "Created file successfully" in result.output

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
            suppress_output=True,
        )
        assert (
            result.output
            == "(Completed without result — model may lack capability for this task)"
        )

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
            suppress_output=True,
        )
        assert (
            result.output
            == "(Completed without result — model may lack capability for this task)"
        )


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
            suppress_output=True,
        )
        assert result.success

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
            suppress_output=True,
        )
        assert result.output == "ok"


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
            suppress_output=True,
            max_turns=5,
        )
        assert result.output == "recovered"


class TestRunLoopMaxIter:
    def test_returns_none_on_max_turns(self, caps):
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
            suppress_output=True,
            max_turns=2,
        )
        assert not result.success


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
            suppress_output=True,
        )
        assert result.output == "ok"


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
            suppress_output=True,
        )
        assert result.output == "Task completed successfully."

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
            suppress_output=True,
        )
        assert result.output == "found"

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
            suppress_output=True,
        )
        assert result.output == "written"


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
            suppress_output=True,
        )
        assert not result.success

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
            suppress_output=True,
        )
        assert result.output == "ok"


class TestRunLoopHeadlessMode:
    def test_headless_no_render(self, caps, capsys):
        provider = _make_provider(_complete("answer"))
        result = run_loop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="test-model",
            suppress_output=True,
        )
        assert result.output == "answer"


class TestAskToolAvailability:
    """Verify ask tool inclusion/exclusion based on ctx and suppress_output."""

    def test_ask_available_with_ctx_not_headless(self, caps, tmp_path):
        """ctx present + suppress_output=False → ask included."""
        from agent_cli.loop import AgentLoop

        ctx = MagicMock()
        ctx.session_dir = tmp_path
        loop = AgentLoop(
            query="Q",
            provider=MagicMock(),
            capabilities=caps,
            model="m",
            ctx=ctx,
            suppress_output=False,
        )
        assert "ask" in loop.tools_list

    def test_ask_hidden_when_headless(self, caps, tmp_path):
        """suppress_output=True → ask removed even with ctx."""
        from agent_cli.loop import AgentLoop

        ctx = MagicMock()
        ctx.session_dir = tmp_path
        loop = AgentLoop(
            query="Q",
            provider=MagicMock(),
            capabilities=caps,
            model="m",
            ctx=ctx,
            suppress_output=True,
        )
        assert "ask" not in loop.tools_list

    def test_ask_hidden_without_ctx(self, caps):
        """ctx=None → ask removed regardless of suppress_output."""
        from agent_cli.loop import AgentLoop

        loop = AgentLoop(
            query="Q",
            provider=MagicMock(),
            capabilities=caps,
            model="m",
            ctx=None,
            suppress_output=False,
        )
        assert "ask" not in loop.tools_list


class TestGracefulInterrupt:
    """Test Ctrl+C graceful interrupt via _interrupted flag."""

    def test_interrupt_returns_none(self, caps):
        """Setting _interrupted flag causes run() to return None."""
        from agent_cli.loop import AgentLoop

        provider = _make_provider(
            _complete("should not reach"),
        )
        loop = AgentLoop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="m",
            suppress_output=True,
        )
        loop._interrupted = True
        result = loop.run()
        assert not result.success
        # LLM should never be called
        provider.call.assert_not_called()

    def test_interrupt_after_first_iteration(self, caps, tmp_path):
        """Interrupt flag set during iteration → exits at next checkpoint."""
        import json as _json
        from agent_cli.loop import AgentLoop

        # Two responses: first is a tool call, second is complete
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello")
        responses = [
            _json.dumps(
                {
                    "thought": "reading",
                    "action": "read_file",
                    "action_input": {"path": str(test_file)},
                }
            ),
            _complete("final"),
        ]
        provider = MagicMock()
        provider.call.side_effect = [LLMResponse(content=r) for r in responses]

        loop = AgentLoop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="m",
            suppress_output=True,
        )

        # Monkey-patch: set interrupted after first LLM call
        original_call_llm = loop._call_llm

        def _call_and_interrupt(*args, **kwargs):
            result = original_call_llm(*args, **kwargs)
            loop._interrupted = True
            return result

        loop._call_llm = _call_and_interrupt

        result = loop.run()
        # First iteration completes (tool executed), then exits at checkpoint
        assert not result.success
        # LLM was called once (first iteration)
        assert provider.call.call_count == 1

    def test_interrupt_records_in_ctx(self, caps, tmp_path):
        """Interrupt adds message to ctx."""
        from agent_cli.loop import AgentLoop
        from agent_cli.context.manager import ContextManager

        provider = MagicMock()
        provider.call.side_effect = [LLMResponse(content=_complete("done"))]

        ctx = ContextManager(session_dir=tmp_path)

        loop = AgentLoop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            suppress_output=True,
        )
        # Set interrupt before first iteration (after setup adds user query)
        loop._interrupted = True
        loop.run()

        # Check ctx has interrupt message
        msgs = ctx.get_messages()
        user_msgs = [m for m in msgs if m["role"] == "user"]
        interrupt_msgs = [
            m for m in user_msgs if m["content"].startswith("⚡ User interrupted")
        ]
        assert len(interrupt_msgs) == 1

    def test_signal_handler_installed_and_restored(self, caps):
        """Signal handler is installed during run() and restored after."""
        import signal

        from agent_cli.loop import AgentLoop

        provider = _make_provider(_complete("ok"))
        original_handler = signal.getsignal(signal.SIGINT)

        loop = AgentLoop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="m",
            suppress_output=True,
            graceful_interrupt=True,
        )
        loop.run()

        # After run(), the original handler should be restored
        assert signal.getsignal(signal.SIGINT) is original_handler

    def test_no_signal_handler_without_graceful(self, caps):
        """Without graceful_interrupt, signal handler is NOT installed."""
        import signal

        from agent_cli.loop import AgentLoop

        provider = _make_provider(_complete("ok"))
        original_handler = signal.getsignal(signal.SIGINT)

        loop = AgentLoop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="m",
            suppress_output=True,
            graceful_interrupt=False,
        )
        loop.run()

        # Handler was never changed
        assert signal.getsignal(signal.SIGINT) is original_handler

    def test_signal_handler_sets_flag(self, caps):
        """Simulated SIGINT sets _interrupted flag."""
        from agent_cli.loop import AgentLoop

        provider = _make_provider(_complete("ok"))
        loop = AgentLoop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="m",
            suppress_output=True,
        )
        loop._install_signal_handler()
        try:
            assert not loop._interrupted
            # Simulate SIGINT
            import os
            import signal

            os.kill(os.getpid(), signal.SIGINT)
            assert loop._interrupted
        finally:
            loop._restore_signal_handler()

    def test_second_sigint_raises(self, caps):
        """Second Ctrl+C raises KeyboardInterrupt."""
        from agent_cli.loop import AgentLoop

        provider = _make_provider(_complete("ok"))
        loop = AgentLoop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="m",
            suppress_output=True,
        )
        loop._install_signal_handler()
        try:
            loop._interrupted = True  # simulate first Ctrl+C already happened
            import os
            import signal

            with pytest.raises(KeyboardInterrupt):
                os.kill(os.getpid(), signal.SIGINT)
        finally:
            loop._restore_signal_handler()

    def test_interrupt_without_ctx(self, caps):
        """Interrupt without ctx (no crash)."""
        from agent_cli.loop import AgentLoop

        provider = _make_provider(_complete("should not reach"))
        loop = AgentLoop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=None,
            suppress_output=True,
        )
        loop._interrupted = True
        result = loop.run()
        assert not result.success

    def test_run_mode_keyboardinterrupt_propagates(self, caps):
        """Without graceful_interrupt, KeyboardInterrupt propagates up."""
        from agent_cli.loop import AgentLoop

        provider = MagicMock()
        provider.call.side_effect = KeyboardInterrupt()

        loop = AgentLoop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="m",
            suppress_output=True,
            graceful_interrupt=False,
        )
        with pytest.raises(KeyboardInterrupt):
            loop.run()

    def test_chat_mode_sigint_graceful(self, caps, tmp_path):
        """With graceful_interrupt, SIGINT sets flag instead of raising."""
        import json as _json

        from agent_cli.loop import AgentLoop

        test_file = tmp_path / "f.txt"
        test_file.write_text("data")
        test_responses = [
            _json.dumps(
                {
                    "thought": "working",
                    "action": "read_file",
                    "action_input": {"path": str(test_file)},
                }
            ),
            _complete("done"),
        ]
        provider = MagicMock()
        provider.call.side_effect = [LLMResponse(content=r) for r in test_responses]

        loop = AgentLoop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="m",
            suppress_output=True,
            graceful_interrupt=True,
        )

        # Simulate: after first LLM call, send SIGINT
        original_call_llm = loop._call_llm

        def _call_then_sigint(*args, **kwargs):
            result = original_call_llm(*args, **kwargs)
            import os
            import signal

            os.kill(os.getpid(), signal.SIGINT)
            return result

        loop._call_llm = _call_then_sigint

        # Should NOT raise — graceful handler catches it
        result = loop.run()
        assert not result.success
        assert loop._interrupted

    def test_chat_mode_ctx_preserved_after_interrupt(self, caps, tmp_path):
        """After graceful interrupt in chat mode, ctx has all prior work."""
        import json as _json

        from agent_cli.context.manager import ContextManager
        from agent_cli.loop import AgentLoop

        test_file = tmp_path / "data.txt"
        test_file.write_text("important data")

        responses = [
            _json.dumps(
                {
                    "thought": "reading file",
                    "action": "read_file",
                    "action_input": {"path": str(test_file)},
                }
            ),
            _complete("final"),
        ]
        provider = MagicMock()
        provider.call.side_effect = [LLMResponse(content=r) for r in responses]

        ctx = ContextManager(session_dir=tmp_path)

        loop = AgentLoop(
            query="Analyze data.txt",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            suppress_output=True,
            graceful_interrupt=True,
        )

        # Interrupt after first iteration
        original_call_llm = loop._call_llm

        def _call_and_interrupt(*args, **kwargs):
            result = original_call_llm(*args, **kwargs)
            loop._interrupted = True
            return result

        loop._call_llm = _call_and_interrupt
        loop.run()

        # Verify: ctx should have user query + tool observation + interrupt msg
        msgs = ctx.get_messages()
        contents = [m["content"] for m in msgs]
        # User query present
        assert any("Analyze data.txt" in c for c in contents)
        # Tool observation present (read_file result survived)
        assert any("important data" in c for c in contents)
        # Interrupt message present
        assert any("interrupted" in c.lower() for c in contents)


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
        ctx = ContextManager(session_dir=tmp_path)
        result = run_loop(
            query="Do something",
            provider=provider,
            capabilities=caps,
            model="test-model",
            suppress_output=True,
            ctx=ctx,
        )
        assert result.output == "Done after confirmation"
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
        ctx = ContextManager(session_dir=tmp_path)
        result = run_loop(
            query="Help me",
            provider=provider,
            capabilities=caps,
            model="test-model",
            suppress_output=True,
            ctx=ctx,
        )
        assert result.output == "Processing file.py in python"

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
        ctx = ContextManager(session_dir=tmp_path)
        result = run_loop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="test-model",
            suppress_output=True,
            ctx=ctx,
        )
        assert result.output == "The answer is 42"

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
        ctx = ContextManager(session_dir=tmp_path)
        result = run_loop(
            query="Do it",
            provider=provider,
            capabilities=caps,
            model="test-model",
            suppress_output=True,
            ctx=ctx,
        )
        assert result.output == "ok"

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
        ctx = ContextManager(session_dir=tmp_path)
        run_loop(
            query="Do something",
            provider=provider,
            capabilities=caps,
            model="test-model",
            suppress_output=True,
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


class TestAgentLoopClass:
    """Test AgentLoop class directly."""

    def test_init_stores_params(self, caps):
        from agent_cli.loop import AgentLoop

        loop = AgentLoop(
            query="test",
            provider=MagicMock(),
            capabilities=caps,
            model="m",
            verbose=True,
            skill_name="summarize",
        )
        assert loop.query == "test"
        assert loop.model == "m"
        assert loop.verbose is True
        assert loop.skill_name == "summarize"
        assert "summarize" in loop.skill_stack

    def test_derived_state(self, caps):
        from agent_cli.loop import AgentLoop

        loop = AgentLoop(
            query="q",
            provider=MagicMock(),
            capabilities=caps,
            model="m",
            depth=0,
            max_depth=2,
        )
        # include_delegate removed — delegate in tools_list
        assert "complete" in loop.tools_list

    def test_should_continue(self, caps):
        from agent_cli.loop import AgentLoop

        loop = AgentLoop(
            query="q",
            provider=MagicMock(),
            capabilities=caps,
            model="m",
            max_turns=5,
        )
        loop.turn = 4
        assert loop._should_continue() is True
        loop.turn = 5
        assert loop._should_continue() is False

    def test_should_continue_unlimited(self, caps):
        from agent_cli.loop import AgentLoop

        loop = AgentLoop(
            query="q",
            provider=MagicMock(),
            capabilities=caps,
            model="m",
            max_turns=0,
        )
        loop.turn = 999
        assert loop._should_continue() is True

    def test_run_returns_answer(self, caps):
        """AgentLoop.run() returns the complete answer."""
        from agent_cli.loop import AgentLoop
        from agent_cli.providers.base import LLMResponse

        provider = MagicMock()
        provider.call.return_value = LLMResponse(content=_complete("42"))

        loop = AgentLoop(
            query="what",
            provider=provider,
            capabilities=caps,
            model="m",
            suppress_output=True,
        )
        result = loop.run()
        assert result.output == "42"


class TestContextContinuity:
    """Verify context is properly maintained across turns and tools."""

    def test_tool_observation_in_ctx(self, caps, tmp_path):
        """Tool result is saved to ctx via _append_observation."""
        from agent_cli.context.manager import ContextManager

        test_file = tmp_path / "f.txt"
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
        ctx = ContextManager(session_dir=tmp_path)
        run_loop(
            query="Read file",
            provider=provider,
            capabilities=caps,
            model="test",
            suppress_output=True,
            ctx=ctx,
        )

        # ctx should have: user query + assistant (read_file) + user (observation) + assistant (complete)
        msgs = ctx.get_messages()
        roles = [m["role"] for m in msgs]
        assert roles.count("assistant") >= 2  # at least tool call + complete

    def test_complete_answer_in_ctx_after_run_loop(self, caps, tmp_path):
        """AgentLoop adds final answer to ctx before returning."""
        from agent_cli.context.manager import ContextManager

        provider = _make_provider(_complete("final answer"))
        ctx = ContextManager(session_dir=tmp_path)
        result = run_loop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="test",
            suppress_output=True,
            ctx=ctx,
        )
        assert result.output == "final answer"

        # AgentLoop now adds the final answer to ctx
        msgs = ctx.get_messages()
        assistant_msgs = [
            m.get("content", "") for m in msgs if m["role"] == "assistant"
        ]
        assert any("final answer" in c for c in assistant_msgs)

    def test_ask_response_in_ctx(self, caps, tmp_path, monkeypatch):
        """ask tool response is saved to ctx."""
        from agent_cli.context.manager import ContextManager

        provider = _make_provider(
            json.dumps(
                {
                    "thought": "need info",
                    "action": "ask",
                    "action_input": {"questions": ["What file?"]},
                }
            ),
            _complete("done"),
        )
        ctx = ContextManager(session_dir=tmp_path)
        monkeypatch.setattr("builtins.input", lambda _: "test.py")
        run_loop(
            query="Help",
            provider=provider,
            capabilities=caps,
            model="test",
            suppress_output=True,
            ctx=ctx,
        )

        msgs = ctx.get_messages()
        all_content = " ".join(m.get("content", "") for m in msgs)
        assert "test.py" in all_content  # user response saved


class TestAppendObservationHelpers:
    """Test _append_native_observation and _append_observation."""

    def test_append_observation_no_ctx(self):
        """Works without ctx (no crash)."""
        from agent_cli.loop import _append_observation

        messages = []
        _append_observation(messages, None, "llm", "obs")
        assert len(messages) == 2


class TestSkillStack:
    """Skill stack prevents recursive calls (A→B ok, A→B→A blocked)."""

    def test_execution_context_shows_stack(self, caps, tmp_path):
        """System prompt shows execution context with call stack."""
        from agent_cli.prompts.system_prompt import _build_execution_context

        ctx = _build_execution_context(
            skill_stack=["summarize"], agent_stack=["reviewer"]
        )
        assert "Call stack: main → agent:reviewer → skill:summarize" in ctx
        assert "summarize" in ctx
        assert "reviewer" in ctx
        assert "Do not delegate" in ctx

    def test_skill_stack_blocks_recursive(self, caps, tmp_path):
        """Same skill in stack → blocked with error."""
        from agent_cli.loop import _handle_run_skill

        obs = _handle_run_skill(
            skill_input={"name": "optimize", "arguments": "./"},
            provider_name="ollama",
            base_url="http://localhost:11434",
            api_key="",
            capabilities=caps,
            model="test",
            ctx=None,
            session=None,
            parent_skill_name="",
            skill_stack=["optimize"],  # already in stack
        )
        error = obs.error if hasattr(obs, "error") else str(obs)
        assert "recursive" in error.lower() or "already" in error.lower()

    def test_skill_stack_allows_different(self, caps, tmp_path):
        """Different skills in stack → allowed (A→B ok)."""
        from agent_cli.loop import _handle_run_skill

        # This will fail because we can't actually execute, but it should
        # NOT be blocked by the stack check
        obs = _handle_run_skill(
            skill_input={"name": "summarize", "arguments": "./"},
            provider_name="ollama",
            base_url="http://localhost:11434",
            api_key="",
            capabilities=caps,
            model="test",
            ctx=None,
            session=None,
            parent_skill_name="",
            skill_stack=["optimize"],  # different skill
        )
        # Should NOT contain "recursive" error
        output = obs.output if obs.success else obs.error
        assert "recursive" not in output.lower()


class TestReadyForReviewTextPath:
    """Test ready_for_review tool via text parsing path."""

    def test_ready_for_review_then_complete(self, caps, tmp_path):
        """LLM calls ready_for_review, reviews, then completes."""
        provider = _make_provider(
            json.dumps(
                {
                    "thought": "I think I'm done",
                    "action": "ready_for_review",
                    "action_input": {"summary": "Analyzed all files"},
                }
            ),
            _complete("Analysis complete"),
        )
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(session_dir=tmp_path)
        result = run_loop(
            query="Analyze the code",
            provider=provider,
            capabilities=caps,
            model="test",
            suppress_output=True,
            ctx=ctx,
        )
        assert result.output == "Analysis complete"

    def test_ready_for_review_returns_query_in_observation(self, caps, tmp_path):
        """The observation from ready_for_review contains the original query."""
        query_text = "Find all bugs in the authentication module"
        provider = _make_provider(
            json.dumps(
                {
                    "thought": "reviewing",
                    "action": "ready_for_review",
                    "action_input": {"summary": "done"},
                }
            ),
            _complete("ok"),
        )
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(session_dir=tmp_path)
        run_loop(
            query=query_text,
            provider=provider,
            capabilities=caps,
            model="test",
            suppress_output=True,
            ctx=ctx,
        )
        # The query should have appeared in the messages (as review observation)
        messages = ctx.get_messages()
        obs_contents = [m["content"] for m in messages if m["role"] == "user"]
        assert any(query_text in c for c in obs_contents)

    def test_ready_for_review_renders_in_main_loop(self, caps, tmp_path):
        """ready_for_review should render observation in main loop (not skill)."""
        from unittest.mock import patch

        provider = _make_provider(
            json.dumps(
                {
                    "thought": "I think I'm done",
                    "action": "ready_for_review",
                    "action_input": {"summary": "Did everything"},
                }
            ),
            _complete("All done"),
        )
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(session_dir=tmp_path)
        with patch("agent_cli.loop.render_step") as mock_render:
            run_loop(
                query="Fix the bug",
                provider=provider,
                capabilities=caps,
                model="test",
                suppress_output=False,
                ctx=ctx,
            )
            # render_step should have been called for ready_for_review observation
            render_calls = [
                c
                for c in mock_render.call_args_list
                if c.args[0] == "observation"
                and c.kwargs.get("tool_name") == "ready_for_review"
            ]
            assert len(render_calls) >= 1

    def test_ready_for_review_not_rendered_in_skill(self, caps, tmp_path):
        """ready_for_review should NOT render observation inside a skill."""
        from unittest.mock import patch

        provider = _make_provider(
            json.dumps(
                {
                    "thought": "done",
                    "action": "ready_for_review",
                    "action_input": {"summary": "Done"},
                }
            ),
            _complete("ok"),
        )
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(session_dir=tmp_path)
        with patch("agent_cli.loop.render_step") as mock_render:
            run_loop(
                query="Greet Alice",
                provider=provider,
                capabilities=caps,
                model="test",
                suppress_output=False,
                skill_name="greet",
                ctx=ctx,
            )
            # render_step should NOT be called for ready_for_review in skill mode
            render_calls = [
                c
                for c in mock_render.call_args_list
                if c.args[0] == "observation"
                and c.kwargs.get("tool_name") == "ready_for_review"
            ]
            assert len(render_calls) == 0


class TestNoOutputTruncation:
    """Verify tool output is passed to LLM without truncation."""

    def test_large_file_not_truncated(self, caps, tmp_path):
        """A large file should be returned in full, not truncated."""
        large_content = "\n".join(f"line {i}: {'x' * 100}" for i in range(500))
        test_file = tmp_path / "large.txt"
        test_file.write_text(large_content)

        provider = _make_provider(
            json.dumps(
                {
                    "thought": "read large file",
                    "action": "read_file",
                    "action_input": {"path": str(test_file)},
                }
            ),
            _complete("Read 500 lines"),
        )
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(session_dir=tmp_path)
        run_loop(
            query="Read the file",
            provider=provider,
            capabilities=caps,
            model="test",
            suppress_output=True,
            ctx=ctx,
        )
        # Verify the observation contains all 500 lines (hashline format: "500#xx:")
        messages = ctx.get_messages()
        all_content = " ".join(m.get("content", "") for m in messages)
        assert "500#" in all_content, "Last line (500) should be in context"
        # Verify no truncation notice
        assert "[... truncated" not in all_content
