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
            suppress_output=True,
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
            suppress_output=True,
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
            suppress_output=True,
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
            suppress_output=True,
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
            suppress_output=True,
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
            suppress_output=True,
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
            suppress_output=True,
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
            suppress_output=True,
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
            suppress_output=True,
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
            suppress_output=True,
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
            suppress_output=True,
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
            suppress_output=True,
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
            suppress_output=True,
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
            suppress_output=True,
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
            suppress_output=True,
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
            suppress_output=True,
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
            suppress_output=True,
        )
        assert result == "ok"


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
        assert result == "answer"


class TestAskToolAvailability:
    """Verify ask tool inclusion/exclusion based on ctx and suppress_output."""

    def test_ask_available_with_ctx_not_headless(self, caps):
        """ctx present + suppress_output=False → ask included."""
        from agent_cli.loop import AgentLoop

        ctx = MagicMock()
        loop = AgentLoop(
            query="Q",
            provider=MagicMock(),
            capabilities=caps,
            model="m",
            ctx=ctx,
            suppress_output=False,
        )
        assert "ask" in loop.tools_list

    def test_ask_hidden_when_headless(self, caps):
        """suppress_output=True → ask removed even with ctx."""
        from agent_cli.loop import AgentLoop

        ctx = MagicMock()
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
        assert result is None
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
        assert result is None
        # LLM was called once (first iteration)
        assert provider.call.call_count == 1

    def test_interrupt_records_in_ctx(self, caps, tmp_path):
        """Interrupt adds message to ctx."""
        from agent_cli.loop import AgentLoop
        from agent_cli.context.manager import ContextManager

        provider = MagicMock()
        provider.call.side_effect = [LLMResponse(content=_complete("done"))]

        ctx = ContextManager(
            provider=provider,
            model="m",
            capabilities=caps,
            scratchpad_dir=tmp_path,
        )

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

    def test_interrupt_records_in_scratchpad(self, caps, tmp_path):
        """Interrupt adds progress entry to scratchpad."""
        from agent_cli.loop import AgentLoop
        from agent_cli.context.manager import ContextManager
        from agent_cli.context.scratchpad import load_scratchpad

        provider = MagicMock()
        provider.call.side_effect = [LLMResponse(content=_complete("done"))]

        ctx = ContextManager(
            provider=provider,
            model="m",
            capabilities=caps,
            scratchpad_dir=tmp_path,
        )

        loop = AgentLoop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            suppress_output=True,
        )
        loop._interrupted = True
        loop.run()

        scratchpad = load_scratchpad(tmp_path)
        assert "Interrupted" in scratchpad

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
        assert result is None

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
        assert result is None
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

        ctx = ContextManager(
            provider=provider,
            model="m",
            capabilities=caps,
            scratchpad_dir=tmp_path,
        )

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
            suppress_output=True,
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
            suppress_output=True,
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
            suppress_output=True,
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
            suppress_output=True,
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
            suppress_output=True,
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
            suppress_output=True,
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
            suppress_output=True,
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
        assert loop.include_delegate is True
        assert "complete" in loop.tools_list

    def test_should_continue(self, caps):
        from agent_cli.loop import AgentLoop

        loop = AgentLoop(
            query="q",
            provider=MagicMock(),
            capabilities=caps,
            model="m",
            max_iter=5,
        )
        loop.iteration = 4
        assert loop._should_continue() is True
        loop.iteration = 5
        assert loop._should_continue() is False

    def test_should_continue_unlimited(self, caps):
        from agent_cli.loop import AgentLoop

        loop = AgentLoop(
            query="q",
            provider=MagicMock(),
            capabilities=caps,
            model="m",
            max_iter=0,
        )
        loop.iteration = 999
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
        assert result == "42"


class TestContextContinuity:
    """Verify context is properly maintained across turns and tools."""

    def test_tool_observation_in_ctx(self, caps, tmp_path):
        """Tool result is saved to ctx via _append_text_observation."""
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
        ctx = ContextManager(
            provider=provider, model="test", capabilities=caps, scratchpad_dir=tmp_path
        )
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
        ctx = ContextManager(
            provider=provider, model="test", capabilities=caps, scratchpad_dir=tmp_path
        )
        result = run_loop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="test",
            suppress_output=True,
            ctx=ctx,
        )
        assert result == "final answer"

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
        ctx = ContextManager(
            provider=provider, model="test", capabilities=caps, scratchpad_dir=tmp_path
        )
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

    def test_ctx_messages_grow_with_iterations(self, caps, tmp_path):
        """Each iteration adds messages to ctx."""
        from agent_cli.context.manager import ContextManager

        test_file = tmp_path / "a.txt"
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
                    "thought": "run",
                    "action": "shell",
                    "action_input": {"command": "echo ok"},
                }
            ),
            _complete("done"),
        )
        ctx = ContextManager(
            provider=provider, model="test", capabilities=caps, scratchpad_dir=tmp_path
        )
        run_loop(
            query="Do",
            provider=provider,
            capabilities=caps,
            model="test",
            suppress_output=True,
            ctx=ctx,
        )

        msgs = ctx.get_messages()
        # Should have: scratchpad? + user query + (read_file pair) + (shell pair) + more
        assert len(msgs) >= 5


class TestAppendObservationHelpers:
    """Test _append_native_observation and _append_text_observation."""

    def test_append_text_observation_basic(self):
        """Appends assistant + user messages and syncs ctx."""
        from agent_cli.loop import _append_text_observation

        messages = [{"role": "user", "content": "hello"}]
        ctx = MagicMock()

        _append_text_observation(messages, ctx, "llm response", "Observation: result")

        assert len(messages) == 3
        assert messages[1]["role"] == "assistant"
        assert messages[1]["content"] == "llm response"
        assert messages[2]["role"] == "user"
        assert messages[2]["content"] == "Observation: result"
        assert ctx.add.call_count == 2
        ctx.add.assert_any_call("assistant", "llm response")
        ctx.add.assert_any_call("user", "Observation: result")

    def test_append_text_observation_no_ctx(self):
        """Works without ctx (no crash)."""
        from agent_cli.loop import _append_text_observation

        messages = []
        _append_text_observation(messages, None, "llm", "obs")
        assert len(messages) == 2

    def test_append_native_observation_basic(self):
        """Extends messages with formatted tool call messages and syncs ctx."""
        from agent_cli.loop import _append_native_observation

        messages = []
        ctx = MagicMock()
        response = MagicMock()
        response.content = "thinking..."
        response.tool_calls = [
            {"id": "t1", "name": "shell", "input": {"command": "ls"}}
        ]

        observations = [{"tool_call": {"id": "t1"}, "output": "file.txt"}]

        # Use fallback provider (not anthropic/openai) for simple format
        _append_native_observation(messages, ctx, "ollama", response, observations)

        assert len(messages) == 2  # assistant + user (fallback format)
        assert ctx.add.call_count == 2

    def test_append_native_observation_no_ctx(self):
        """Works without ctx (no crash)."""
        from agent_cli.loop import _append_native_observation

        messages = []
        response = MagicMock()
        response.content = "ok"
        response.tool_calls = []

        _append_native_observation(messages, None, "ollama", response, [])
        assert len(messages) == 2  # fallback still appends assistant + user


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
            suppress_output=True,
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
            suppress_output=True,
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
            suppress_output=True,
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
            suppress_output=True,
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
            suppress_output=True,
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
            suppress_output=True,
            ctx=None,
        )
        assert result == "ok"

    def test_skill_internal_loop_skips_scratchpad(self, caps, tmp_path):
        """Scratchpad NOT injected when inside a skill (skill_name set)."""
        from agent_cli.context.manager import ContextManager

        provider = _make_provider(_complete("done"))
        ctx = ContextManager(
            provider=provider, model="test", capabilities=caps, scratchpad_dir=tmp_path
        )
        ctx.init_task()

        # Set skill context (simulating inside a skill)
        ctx.set_skill_context(skill_name="summarize", parent_turn=1)

        msgs = ctx.get_messages()
        # Scratchpad should NOT be in messages
        all_content = " ".join(m.get("content", "") for m in msgs)
        assert "[Scratchpad" not in all_content

        # Reset — scratchpad should appear again
        ctx.set_skill_context()
        msgs = ctx.get_messages()
        all_content = " ".join(m.get("content", "") for m in msgs)
        assert "[Scratchpad" in all_content

    def test_debug_log_to_stderr_when_verbose(self, caps, tmp_path, capsys):
        """debug messages go to stderr only when verbose is on."""
        from agent_cli.loop import _debug_log, _set_debug_verbose

        # Off by default
        _set_debug_verbose(False)
        _debug_log("should_not_appear")
        captured = capsys.readouterr()
        assert "should_not_appear" not in captured.err

        # On when verbose
        _set_debug_verbose(True)
        _debug_log("should_appear")
        captured = capsys.readouterr()
        assert "should_appear" in captured.err

        # Cleanup
        _set_debug_verbose(False)


class TestArtifactLazyLoading:
    """Scratchpad progress + system prompt guide LLM to read artifacts on demand."""

    def test_system_prompt_contains_artifact_guidance(self, caps):
        """System prompt tells LLM about artifact recovery via read_file."""
        from agent_cli.prompts.system_prompt import build_system_prompt
        from agent_cli.tools import TOOLS

        prompt = build_system_prompt(capabilities=caps, active_tools=list(TOOLS.keys()))
        assert "artifact" in prompt.lower()
        assert "read_file" in prompt

    def test_compaction_includes_artifact_hint(self, caps, tmp_path):
        """After compaction, a hint about artifact recovery is injected."""
        from agent_cli.context.manager import ContextManager

        provider = MagicMock()
        provider.call.return_value = __import__(
            "agent_cli.providers.base", fromlist=["LLMResponse"]
        ).LLMResponse(content="Summary of conversation")
        # Use small context to trigger compaction easily
        small_caps = (
            caps._replace(context_window=1000) if hasattr(caps, "_replace") else caps
        )
        ctx = ContextManager(
            provider=provider,
            model="test",
            capabilities=small_caps,
            scratchpad_dir=tmp_path,
        )
        ctx.init_task()
        # Fill messages to trigger compaction
        for i in range(20):
            ctx.add("user", "x" * 200)
            ctx.add("assistant", "y" * 200)

        msgs = ctx.get_messages()
        all_content = " ".join(m.get("content", "") for m in msgs)
        # After compaction, hint should be present
        if ctx._summary:
            assert (
                "artifact" in all_content.lower() or "scratchpad" in all_content.lower()
            )

    def test_read_file_progress_includes_filename(self, caps, tmp_path):
        """read_file progress: 'read_file: README.md (N줄)'."""
        from agent_cli.context.manager import ContextManager
        from agent_cli.context.scratchpad import load_scratchpad

        test_file = tmp_path / "README.md"
        test_file.write_text("line1\nline2\nline3\n")

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
            query="Read",
            provider=provider,
            capabilities=caps,
            model="test",
            suppress_output=True,
            ctx=ctx,
        )

        content = load_scratchpad(tmp_path)
        assert "README.md" in content

    def test_shell_progress_includes_command(self, caps, tmp_path):
        """shell progress: 'shell: ls -la'."""
        from agent_cli.context.manager import ContextManager
        from agent_cli.context.scratchpad import load_scratchpad

        provider = _make_provider(
            json.dumps(
                {
                    "thought": "list",
                    "action": "shell",
                    "action_input": {"command": "ls -la /tmp"},
                }
            ),
            _complete("done"),
        )
        ctx = ContextManager(
            provider=provider, model="test", capabilities=caps, scratchpad_dir=tmp_path
        )
        run_loop(
            query="List",
            provider=provider,
            capabilities=caps,
            model="test",
            suppress_output=True,
            ctx=ctx,
        )

        content = load_scratchpad(tmp_path)
        assert "ls -la" in content

    def test_complete_progress_includes_preview(self, caps, tmp_path):
        """complete progress: 'Task completed: first 80 chars...'."""
        from agent_cli.context.manager import ContextManager
        from agent_cli.context.scratchpad import load_scratchpad

        provider = _make_provider(
            _complete("Agent-CLI optimization analysis is complete with 5 findings")
        )
        ctx = ContextManager(
            provider=provider, model="test", capabilities=caps, scratchpad_dir=tmp_path
        )
        run_loop(
            query="Do",
            provider=provider,
            capabilities=caps,
            model="test",
            suppress_output=True,
            ctx=ctx,
        )

        content = load_scratchpad(tmp_path)
        assert (
            "optimization analysis" in content.lower() or "complete" in content.lower()
        )


class TestArtifactTags:
    """A. Tag enrichment tests."""

    def test_read_file_tag_includes_filepath(self, caps, tmp_path):
        """A1: read_file artifact has filepath in tags."""
        from agent_cli.context.manager import ContextManager
        from agent_cli.context.scratchpad import build_artifact_index

        test_file = tmp_path / "myfile.py"
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
            query="Read",
            provider=provider,
            capabilities=caps,
            model="test",
            suppress_output=True,
            ctx=ctx,
        )

        index = build_artifact_index(tmp_path)
        read_artifacts = [a for a in index if "read_file" in a.tags]
        assert len(read_artifacts) >= 1
        assert any(
            str(test_file) in a.tags or "myfile.py" in str(a.tags)
            for a in read_artifacts
        )

    def test_shell_tag_tool_name_only(self, caps, tmp_path):
        """A2: shell artifact has tool name tag only."""
        from agent_cli.context.manager import ContextManager
        from agent_cli.context.scratchpad import build_artifact_index

        provider = _make_provider(
            json.dumps(
                {
                    "thought": "run",
                    "action": "shell",
                    "action_input": {"command": "ls -la /tmp"},
                }
            ),
            _complete("done"),
        )
        ctx = ContextManager(
            provider=provider, model="test", capabilities=caps, scratchpad_dir=tmp_path
        )
        run_loop(
            query="Run",
            provider=provider,
            capabilities=caps,
            model="test",
            suppress_output=True,
            ctx=ctx,
        )

        index = build_artifact_index(tmp_path)
        shell_artifacts = [a for a in index if "shell" in a.tags]
        assert len(shell_artifacts) >= 1

    def test_complete_tag(self, caps, tmp_path):
        """A4: complete artifact has 'complete' tag."""
        from agent_cli.context.manager import ContextManager
        from agent_cli.context.scratchpad import build_artifact_index

        provider = _make_provider(_complete("final"))
        ctx = ContextManager(
            provider=provider, model="test", capabilities=caps, scratchpad_dir=tmp_path
        )
        run_loop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="test",
            suppress_output=True,
            ctx=ctx,
        )

        index = build_artifact_index(tmp_path)
        complete_artifacts = [a for a in index if "complete" in a.tags]
        assert len(complete_artifacts) >= 1


class TestSkillNamePropagation:
    """B. skill_name propagation to tags."""

    def test_execute_skill_passes_skill_name(self, caps):
        """B1: execute_skill passes skill_name to run_loop."""
        import unittest.mock

        from agent_cli.skills.executor import execute_skill
        from agent_cli.skills.models import Skill

        skill = Skill(name="optimize", description="d", prompt_template="Do $ARGUMENTS")
        with unittest.mock.patch("agent_cli.skills.executor.run_loop") as mock_run_loop:
            mock_run_loop.return_value = "ok"
            execute_skill(
                skill=skill,
                arguments="./",
                provider=MagicMock(),
                capabilities=caps,
                model="m",
                suppress_output=True,
            )
            _, kwargs = mock_run_loop.call_args
            assert kwargs["skill_name"] == "optimize"

    def test_skill_internal_tool_has_skill_tag(self, caps, tmp_path):
        """B2: tool inside skill has 'skill:name' tag."""
        from agent_cli.context.manager import ContextManager
        from agent_cli.context.scratchpad import build_artifact_index

        test_file = tmp_path / "src.py"
        test_file.write_text("code")

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
            query="Analyze",
            provider=provider,
            capabilities=caps,
            model="test",
            suppress_output=True,
            ctx=ctx,
            skill_name="optimize",
        )

        index = build_artifact_index(tmp_path)
        skill_tagged = [a for a in index if "skill:optimize" in a.tags]
        assert len(skill_tagged) >= 1

    def test_skill_internal_complete_has_skill_tag(self, caps, tmp_path):
        """B3: complete inside skill has 'skill:name' tag."""
        from agent_cli.context.manager import ContextManager
        from agent_cli.context.scratchpad import build_artifact_index

        provider = _make_provider(_complete("done"))
        ctx = ContextManager(
            provider=provider, model="test", capabilities=caps, scratchpad_dir=tmp_path
        )
        run_loop(
            query="Do",
            provider=provider,
            capabilities=caps,
            model="test",
            suppress_output=True,
            ctx=ctx,
            skill_name="summarize",
        )

        index = build_artifact_index(tmp_path)
        skill_complete = [
            a for a in index if "complete" in a.tags and "skill:summarize" in a.tags
        ]
        assert len(skill_complete) >= 1

    def test_no_skill_name_no_skill_tag(self, caps, tmp_path):
        """B4: normal chat has no 'skill:' tag."""
        from agent_cli.context.manager import ContextManager
        from agent_cli.context.scratchpad import build_artifact_index

        provider = _make_provider(_complete("done"))
        ctx = ContextManager(
            provider=provider, model="test", capabilities=caps, scratchpad_dir=tmp_path
        )
        run_loop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="test",
            suppress_output=True,
            ctx=ctx,
        )

        index = build_artifact_index(tmp_path)
        for a in index:
            assert not any(t.startswith("skill:") for t in a.tags)


class TestSkillSubdirectory:
    """C+D. Skill artifacts in subdirectories + rglob index."""

    def test_skill_artifacts_in_subdirectory(self, caps, tmp_path):
        """C1: skill artifacts stored under turn_N_skillname/."""
        from agent_cli.context.manager import ContextManager

        provider = _make_provider(_complete("done"))
        ctx = ContextManager(
            provider=provider, model="test", capabilities=caps, scratchpad_dir=tmp_path
        )
        # Simulate outer turn 1 first
        ctx.begin_turn("outer query")

        run_loop(
            query="Analyze",
            provider=provider,
            capabilities=caps,
            model="test",
            suppress_output=True,
            ctx=ctx,
            skill_name="optimize",
        )

        # Check subdirectory exists
        artifacts_dir = tmp_path / "artifacts"
        subdirs = [d for d in artifacts_dir.iterdir() if d.is_dir()]
        assert any("optimize" in d.name for d in subdirs)

    def test_normal_artifacts_flat(self, caps, tmp_path):
        """C2: normal (non-skill) artifacts are flat files."""
        from agent_cli.context.manager import ContextManager

        provider = _make_provider(_complete("done"))
        ctx = ContextManager(
            provider=provider, model="test", capabilities=caps, scratchpad_dir=tmp_path
        )
        run_loop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="test",
            suppress_output=True,
            ctx=ctx,
        )

        artifacts_dir = tmp_path / "artifacts"
        if artifacts_dir.exists():
            md_files = list(artifacts_dir.glob("turn_*.md"))
            assert len(md_files) >= 1  # flat files exist

    def test_rglob_indexes_all(self, caps, tmp_path):
        """D1: build_artifact_index finds flat + subdirectory artifacts."""
        from agent_cli.context.manager import ContextManager
        from agent_cli.context.scratchpad import build_artifact_index

        test_file = tmp_path / "f.txt"
        test_file.write_text("data")

        # Run with skill (creates subdirectory artifacts)
        provider1 = _make_provider(
            json.dumps(
                {
                    "thought": "read",
                    "action": "read_file",
                    "action_input": {"path": str(test_file)},
                }
            ),
            _complete("skill done"),
        )
        ctx = ContextManager(
            provider=provider1, model="test", capabilities=caps, scratchpad_dir=tmp_path
        )
        run_loop(
            query="Skill work",
            provider=provider1,
            capabilities=caps,
            model="test",
            suppress_output=True,
            ctx=ctx,
            skill_name="review",
        )

        # Run without skill (creates flat artifacts)
        provider2 = _make_provider(_complete("normal done"))
        run_loop(
            query="Normal work",
            provider=provider2,
            capabilities=caps,
            model="test",
            suppress_output=True,
            ctx=ctx,
        )

        index = build_artifact_index(tmp_path)
        # Should find both flat and subdirectory artifacts
        assert (
            len(index) >= 3
        )  # at least: skill read_file + skill complete + normal complete

    def test_subdirectory_artifact_loadable(self, caps, tmp_path):
        """D2: artifacts in subdirectory can be loaded by path."""
        from agent_cli.context.manager import ContextManager
        from agent_cli.context.scratchpad import build_artifact_index, load_artifact

        provider = _make_provider(_complete("result"))
        ctx = ContextManager(
            provider=provider, model="test", capabilities=caps, scratchpad_dir=tmp_path
        )
        run_loop(
            query="Do",
            provider=provider,
            capabilities=caps,
            model="test",
            suppress_output=True,
            ctx=ctx,
            skill_name="test-skill",
        )

        index = build_artifact_index(tmp_path)
        for meta in index:
            loaded_meta, body = load_artifact(meta.path)
            assert loaded_meta.entry_id
            assert body  # non-empty


class TestRunSkillIntercept:
    """E. run_skill loop-level intercept."""

    def test_run_skill_with_ctx(self, caps, tmp_path):
        """E1: run_skill passes ctx to inner run_loop."""
        from agent_cli.context.manager import ContextManager
        from agent_cli.context.scratchpad import load_scratchpad
        from unittest.mock import patch

        from agent_cli.skills.models import Skill

        mock_skills = {
            "summarize": Skill(
                name="summarize",
                description="Summarize",
                prompt_template="Summarize $ARGUMENTS. Reply with one sentence.",
                max_iter=3,
            )
        }

        # Outer provider: calls run_skill then complete
        outer_provider = _make_provider(
            json.dumps(
                {
                    "thought": "use skill",
                    "action": "run_skill",
                    "action_input": {"name": "summarize", "arguments": "test"},
                }
            ),
            _complete("all done"),
        )
        ctx = ContextManager(
            provider=outer_provider,
            model="test",
            capabilities=caps,
            scratchpad_dir=tmp_path,
        )

        with patch("agent_cli.skills.loader.load_skills", return_value=mock_skills):
            run_loop(
                query="Summarize something",
                provider=outer_provider,
                capabilities=caps,
                model="test",
                suppress_output=True,
                ctx=ctx,
                provider_name="ollama",
                base_url="http://localhost:11434",
            )

        # Scratchpad should exist (ctx was passed to inner loop)
        content = load_scratchpad(tmp_path)
        assert content  # non-empty

    def test_run_skill_unknown_returns_error(self, caps, tmp_path):
        """E3: unknown skill → error in observation, loop continues."""
        from agent_cli.context.manager import ContextManager
        from unittest.mock import patch

        provider = _make_provider(
            json.dumps(
                {
                    "thought": "try skill",
                    "action": "run_skill",
                    "action_input": {"name": "nonexistent", "arguments": ""},
                }
            ),
            _complete("ok"),
        )
        ctx = ContextManager(
            provider=provider, model="test", capabilities=caps, scratchpad_dir=tmp_path
        )

        with patch("agent_cli.skills.loader.load_skills", return_value={}):
            result = run_loop(
                query="Try",
                provider=provider,
                capabilities=caps,
                model="test",
                suppress_output=True,
                ctx=ctx,
            )
        assert result == "ok"  # loop continued after error

    def test_run_skill_result_includes_skill_header(self, caps, tmp_path):
        """run_skill result includes SKILL: name(args) header."""
        from agent_cli.loop import _handle_run_skill
        from agent_cli.skills.models import Skill
        from unittest.mock import patch

        mock_skills = {
            "summarize": Skill(
                name="summarize",
                description="Sum",
                prompt_template="Sum $ARGUMENTS",
                max_iter=3,
            )
        }
        with patch("agent_cli.skills.loader.load_skills", return_value=mock_skills):
            with patch(
                "agent_cli.skills.executor.execute_skill", return_value="Summary done"
            ):
                obs = _handle_run_skill(
                    skill_input={"name": "summarize", "arguments": "./src"},
                    provider_name="ollama",
                    base_url="http://localhost:11434",
                    api_key="",
                    capabilities=caps,
                    model="test",
                    ctx=None,
                    session=None,
                    parent_skill_name="",
                )
        assert "SKILL: summarize" in obs
        assert "./src" in obs
        assert "Summary done" in obs

    def test_run_skill_result_includes_internal_calls(self, caps, tmp_path):
        """When inner skill calls another skill, result shows internal call history."""
        from agent_cli.context.manager import ContextManager
        from agent_cli.context.scratchpad import save_artifact

        ctx = ContextManager(
            provider=MagicMock(),
            model="test",
            capabilities=caps,
            scratchpad_dir=tmp_path,
        )
        ctx.init_task()

        # Simulate: turn_before=0, then internal skill created artifacts
        ctx.begin_turn("q")  # turn 1
        save_artifact(
            turn=2,
            content="shell output",
            tags=["shell", "skill:summarize"],
            summary="shell: ls",
            base=tmp_path,
        )
        save_artifact(
            turn=3,
            content="optimize result",
            tags=["complete", "skill:optimize"],
            summary="Task completed: Analysis done",
            base=tmp_path,
        )

        from agent_cli.loop import _build_internal_skill_summary

        summary = _build_internal_skill_summary(ctx, turn_before=0)
        assert "optimize" in summary
        assert "Analysis done" in summary


class TestSkillStack:
    """Skill stack prevents recursive calls (A→B ok, A→B→A blocked)."""

    def test_skill_stack_passed_to_system_prompt(self, caps, tmp_path):
        """System prompt hides skills already in the stack."""
        from agent_cli.prompts.system_prompt import build_skill_descriptions
        from agent_cli.skills.models import Skill

        skills = {
            "summarize": Skill(
                name="summarize", description="Sum", prompt_template="$ARGUMENTS"
            ),
            "optimize": Skill(
                name="optimize", description="Opt", prompt_template="$ARGUMENTS"
            ),
        }
        desc = build_skill_descriptions(skills, exclude_names=["summarize"])
        assert "optimize" in desc
        assert "summarize" not in desc

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
        assert "recursive" in obs.lower() or "already" in obs.lower()

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
        assert "recursive" not in obs.lower()


class TestRunSkillNoDuplicateArtifact:
    """F. No duplicate artifact from outer loop for run_skill."""

    def test_no_outer_end_turn_for_run_skill(self, caps, tmp_path):
        """F1: outer loop does not call end_turn for run_skill result."""
        from agent_cli.context.manager import ContextManager
        from agent_cli.context.scratchpad import build_artifact_index
        from unittest.mock import patch

        from agent_cli.skills.models import Skill

        mock_skills = {
            "simple": Skill(
                name="simple",
                description="Simple",
                prompt_template="Say hello. Use complete to answer.",
                max_iter=2,
            )
        }

        provider = _make_provider(
            json.dumps(
                {
                    "thought": "use skill",
                    "action": "run_skill",
                    "action_input": {"name": "simple", "arguments": ""},
                }
            ),
            _complete("final"),
        )
        ctx = ContextManager(
            provider=provider, model="test", capabilities=caps, scratchpad_dir=tmp_path
        )

        with patch("agent_cli.skills.loader.load_skills", return_value=mock_skills):
            run_loop(
                query="Do",
                provider=provider,
                capabilities=caps,
                model="test",
                suppress_output=True,
                ctx=ctx,
                provider_name="ollama",
                base_url="http://localhost:11434",
            )

        index = build_artifact_index(tmp_path)
        # Check no duplicate — inner loop saves its own, outer should not duplicate
        entry_ids = [a.entry_id for a in index]
        assert len(entry_ids) == len(set(entry_ids)), (
            f"Duplicate artifacts: {entry_ids}"
        )
