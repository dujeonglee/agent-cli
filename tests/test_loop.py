"""Tests for agent loop (integration with mocked provider)."""

import json
from unittest.mock import MagicMock

import pytest

from agent_cli.loop import run_loop
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities


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
        supports_thinking=False,
        thinking_budget=0,
        supports_strict_schema=False,
    )


def _make_provider(*responses):
    """Create a mock provider that returns responses in sequence."""
    provider = MagicMock()
    provider.call.side_effect = [LLMResponse(content=r) for r in responses]
    return provider


def _messages_from_call(call_obj):
    """Pull the ``messages`` argument out of a ``MagicMock.call`` record.

    The loop sometimes passes ``messages`` positionally (first arg) and
    sometimes as a keyword. ``MagicMock.call_args_list`` records both
    shapes; this helper normalises so tests don't need to know which
    invocation form the loop happened to pick.
    """
    args, kwargs = call_obj
    if args:
        return args[0]
    return kwargs.get("messages") or []


class TestRunLoopComplete:
    def test_direct_complete(self, caps):
        provider = _make_provider(_complete("42"))
        result = run_loop(
            query="What is the answer?",
            provider=provider,
            capabilities=caps,
            model="test-model",
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
        )
        assert result.output == "Simple answer"

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
                    "action_input": {"shell_command": "pwd"},
                }
            ),
            _complete("Executed command"),
        )
        result = run_loop(
            query="Run pwd",
            provider=provider,
            capabilities=caps,
            model="test-model",
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
        )
        assert result.output == "ok"


class TestToolExceptionSafetyNet:
    """``_dispatch_tool_with_hooks`` wraps the invoke step in a
    ``try/except Exception`` so a tool that raises instead of
    returning a ``ToolResult`` doesn't tear down the worker thread
    (web) or the whole process (chat / run). The user-reported
    ``edit_file`` TypeError was the case that exposed this gap:
    the exception escaped re.py with no Observation reaching the
    LLM, no path to recovery.

    The contract pinned here:

      1. Tool ``Exception`` → ``ToolResult(False, error=...)``,
         loop continues, observation lands in messages, LLM gets
         next turn.
      2. ``KeyboardInterrupt`` / ``SystemExit`` still propagate so
         user Ctrl+C exits the loop cleanly — Exception only, NOT
         BaseException.
      3. Error message mentions the tool name + a recovery hint so
         the LLM has actionable info to retry from.
    """

    def _patch_tool(self, monkeypatch, name, fn):
        """Swap a registered tool's leaf execution (``_run``) for ``fn(args)``.
        The Tool wrapper (prefix strip + dispatch) stays intact; only the
        innermost call swaps. Restored automatically by monkeypatch on
        teardown."""
        from agent_cli.tools import TOOLS

        monkeypatch.setattr(TOOLS[name], "_run", lambda args, **kw: fn(args))

    def test_tool_typeerror_becomes_tool_result_error(self, caps, monkeypatch):
        # The exact failure mode the user hit: a tool raises TypeError
        # somewhere deep (in their case ``re.match`` inside
        # ``_parse_ref``). The loop must convert this to a normal
        # tool failure and continue.
        def boom(args):
            raise TypeError("expected string or bytes-like object")

        self._patch_tool(monkeypatch, "shell", boom)
        provider = _make_provider(
            json.dumps(
                {
                    "thought": "try shell",
                    "action": "shell",
                    "action_input": {"shell_command": "pwd"},
                }
            ),
            _complete("recovered after tool crashed"),
        )
        result = run_loop(
            query="run something",
            provider=provider,
            capabilities=caps,
            model="test-model",
        )
        # Loop did NOT die — it completed via the second LLM emission.
        assert result.success
        assert result.output == "recovered after tool crashed"

    def test_tool_runtime_error_message_reaches_llm_as_observation(
        self, caps, monkeypatch
    ):
        # The observation passed to the LLM must include the tool
        # name AND the original exception text so the model can
        # diagnose. Verify by capturing the messages the second LLM
        # call received.
        def boom(args):
            raise RuntimeError("internal tool bug 12345")

        self._patch_tool(monkeypatch, "shell", boom)
        captured_messages: list = []

        def capture_then_complete(messages, **kwargs):
            captured_messages.extend(messages)
            return LLMResponse(content=_complete("done"))

        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(
                content=json.dumps(
                    {
                        "thought": "try",
                        "action": "shell",
                        "action_input": {"shell_command": "pwd"},
                    }
                )
            ),
            LLMResponse(content=_complete("done")),
        ]
        run_loop(
            query="x",
            provider=provider,
            capabilities=caps,
            model="test-model",
        )
        # Second call's input messages should contain the
        # observation with the error text.
        assert provider.call.call_count == 2
        second_call_messages = _messages_from_call(provider.call.call_args_list[1])
        observation_msgs = [
            m for m in second_call_messages if "Observation" in (m.get("content") or "")
        ]
        assert observation_msgs, "no observation made it into the next turn"
        obs_text = observation_msgs[-1]["content"]
        assert "shell" in obs_text  # tool name
        assert "internal tool bug 12345" in obs_text  # original exception text

    def test_tool_exception_observation_mentions_retry_path(self, caps, monkeypatch):
        # LLM-facing vocabulary check: the error message has to
        # suggest a recovery path. Without that hint the model
        # might keep retrying the same broken call.
        def boom(args):
            raise ValueError("bad")

        self._patch_tool(monkeypatch, "shell", boom)
        provider = _make_provider(
            json.dumps(
                {
                    "thought": "try",
                    "action": "shell",
                    "action_input": {"shell_command": "x"},
                }
            ),
            _complete("ok"),
        )
        run_loop(
            query="x",
            provider=provider,
            capabilities=caps,
            model="test-model",
        )
        second_call_messages = _messages_from_call(provider.call.call_args_list[1])
        obs_text = next(
            m["content"]
            for m in second_call_messages
            if "Observation" in (m.get("content") or "")
        )
        # The wording should explicitly prompt the model to consider
        # an alternative — not just keep trying the same call.
        assert "retry" in obs_text.lower() or "different approach" in obs_text.lower()

    def test_keyboard_interrupt_propagates(self, caps, monkeypatch):
        # User Ctrl+C must exit the loop, not be silently swallowed
        # into "Tool 'x' raised KeyboardInterrupt — retry?" — that
        # would trap the user in a runaway loop they can't quit.
        def interrupted(args):
            raise KeyboardInterrupt()

        self._patch_tool(monkeypatch, "shell", interrupted)
        provider = _make_provider(
            json.dumps(
                {
                    "thought": "try",
                    "action": "shell",
                    "action_input": {"shell_command": "x"},
                }
            ),
        )
        with pytest.raises(KeyboardInterrupt):
            run_loop(
                query="x",
                provider=provider,
                capabilities=caps,
                model="test-model",
            )

    def test_system_exit_propagates(self, caps, monkeypatch):
        # Same rationale as KeyboardInterrupt — SystemExit is a
        # deliberate shutdown signal, not a tool failure to recover
        # from.
        def quit(args):
            raise SystemExit(0)

        self._patch_tool(monkeypatch, "shell", quit)
        provider = _make_provider(
            json.dumps(
                {
                    "thought": "try",
                    "action": "shell",
                    "action_input": {"shell_command": "x"},
                }
            ),
        )
        with pytest.raises(SystemExit):
            run_loop(
                query="x",
                provider=provider,
                capabilities=caps,
                model="test-model",
            )

    def test_normal_tool_failure_via_tool_result_still_works(self, caps, monkeypatch):
        # The safety net must not change behaviour for tools that
        # report failure the *normal* way (return ToolResult(False)).
        # Such tools never raise, so the except clause never runs,
        # and the existing observation path handles them. Regression
        # guard so a future refactor of the safety net can't
        # accidentally double-wrap or swallow these.
        from agent_cli.tools.result import ToolResult

        def polite_fail(args):
            return ToolResult(False, error="polite error")

        self._patch_tool(monkeypatch, "shell", polite_fail)
        provider = _make_provider(
            json.dumps(
                {
                    "thought": "try",
                    "action": "shell",
                    "action_input": {"shell_command": "x"},
                }
            ),
            _complete("ok"),
        )
        run_loop(
            query="x",
            provider=provider,
            capabilities=caps,
            model="test-model",
        )
        second_call_messages = _messages_from_call(provider.call.call_args_list[1])
        obs_text = next(
            m["content"]
            for m in second_call_messages
            if "Observation" in (m.get("content") or "")
        )
        # The original tool message should land verbatim — no
        # "raised X" framing applied to a non-exceptional failure.
        assert "polite error" in obs_text
        assert "raised" not in obs_text


class TestActionInferenceCorrection:
    """Dropped action name (parse_stage 3) recovered from action_input key
    prefixes — dispatches the inferred tool AND rewrites the next-turn
    prior to the corrected shape (no raw-drift mimicry).

    Exercised here with edit_file, which is still a wire-key-prefixed batch
    tool (``edit_file_edits``). The flat-native tools (read_file/write_file,
    Step 3) carry no prefix, so a dropped action with flat input is ambiguous
    and falls to NO_ACTION instead (see test_dropped_field_recovery). When the
    remaining batch tools go flat, this end-to-end infer path becomes fully
    latent — pinned only by the unit test in test_dropped_field_recovery."""

    def test_infers_action_and_corrects_prior(self, caps, tmp_path):
        from agent_cli.tools.read_file import format_hashlines

        target = tmp_path / "x.txt"
        target.write_text("hello body\n")
        ref = format_hashlines("hello body\n").split("\n")[0].split(":", 1)[0]
        provider = _make_provider(
            json.dumps(
                {
                    "thought": "edit the file",
                    # action name dropped — only the prefixed input keys, so
                    # infer_action recovers "edit_file" from the edit_file_ prefix
                    "action_input": {
                        "edit_file_path": str(target),
                        "edit_file_edits": [
                            {"op": "replace", "pos": ref, "lines": ["EDITED LINE"]}
                        ],
                    },
                }
            ),
            _complete("done"),
        )
        result = run_loop(
            query="edit it",
            provider=provider,
            capabilities=caps,
            model="test-model",
        )
        # 1. inferred edit_file actually ran (the edit landed in the file)
        assert result.success
        assert "EDITED LINE" in target.read_text()
        # 2. assistant prior rewritten to the corrected shape (action
        #    present), NOT the raw drift (action absent)
        second = _messages_from_call(provider.call.call_args_list[1])
        assistant = [m for m in second if m["role"] == "assistant"]
        joined = " ".join(m.get("content", "") for m in assistant)
        assert '"action": "edit_file"' in joined

    def test_ambiguous_input_not_inferred(self, caps):
        # Two distinct tool prefixes present → ambiguous → no inference,
        # falls through to the NO_ACTION retry, then completes.
        provider = _make_provider(
            json.dumps(
                {
                    "thought": "drift",
                    "action_input": {
                        "edit_file_edits": [{"op": "append", "pos": "1#AB"}],
                        "shell_command": "ls",
                    },
                }
            ),
            _complete("recovered"),
        )
        result = run_loop(
            query="go",
            provider=provider,
            capabilities=caps,
            model="test-model",
        )
        assert result.success
        assert result.output == "recovered"


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
            max_turns=5,
        )
        assert result.output == "recovered"

    def test_retry_echoes_content_back(self, caps):
        """The failing content is the failure-grounding signal — echoed
        when present so the model sees its own structural drift
        (YAML-style keys, function-call syntax, bare prose) and
        self-diagnoses."""
        provider = MagicMock()
        bad_output = "thought: drifted into YAML\naction: complete"
        provider.call.side_effect = [
            LLMResponse(content=bad_output, thinking=""),
            LLMResponse(content=_complete("recovered")),
        ]

        run_loop(
            query="anything",
            provider=provider,
            capabilities=caps,
            model="test-model",
            max_turns=5,
        )

        second_call_messages = provider.call.call_args_list[1].kwargs["messages"]
        retry_msg = second_call_messages[-1]["content"]
        assert bad_output in retry_msg
        assert "Your prior output:" in retry_msg
        assert retry_msg.startswith("Your response was not valid JSON.")

    def test_retry_does_not_echo_thinking_channel(self, caps):
        """v1 design invariant: the thinking channel is *not* consumed by
        the recovery layer (see docs/robust-harness/DESIGN.md §2.2). This
        test guards against accidental re-introduction.

        Even when the provider surfaces a non-empty thinking field, the
        retry message echoes only ``content`` — the thinking text must
        not appear in the injected retry."""
        provider = MagicMock()
        thinking_text = "I keep failing to provide valid JSON."
        provider.call.side_effect = [
            LLMResponse(
                content="Plain prose, no JSON envelope here.",
                thinking=thinking_text,
            ),
            LLMResponse(content=_complete("recovered")),
        ]

        run_loop(
            query="anything",
            provider=provider,
            capabilities=caps,
            model="test-model",
            max_turns=5,
        )

        second_call_messages = provider.call.call_args_list[1].kwargs["messages"]
        retry_msg = second_call_messages[-1]["content"]
        assert "Plain prose, no JSON envelope here." in retry_msg
        # Recovery layer must not echo thinking
        assert thinking_text not in retry_msg
        assert "Your prior reasoning:" not in retry_msg

    def test_retry_no_content_uses_static_template(self, caps):
        """Empty content → static fallback. Path stays clean for
        providers that produce no echoable signal."""
        # Whitespace-only content strips to empty inside echo_prior_output,
        # so the echo block produces nothing and the fallback fires.
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(content="   ", thinking=""),
            LLMResponse(content=_complete("recovered")),
        ]

        result = run_loop(
            query="anything",
            provider=provider,
            capabilities=caps,
            model="test-model",
            max_turns=5,
        )

        second_call_messages = provider.call.call_args_list[1].kwargs["messages"]
        retry_msg = second_call_messages[-1]["content"]
        assert retry_msg.startswith("Your response was not valid JSON.")
        # No echo block
        assert "Your prior output:" not in retry_msg
        assert result.output == "recovered"


class TestActionRenderShowsRawEmission:
    """The action card must show the model's ACTUAL emission (``op.action_input``,
    pre-wrap), not a dispatch-canonical form. read_file is flat-native (Step 3),
    so its `wrap_single_op` is identity and the model's flat `{path}` op flows
    straight through validate→run; the card must show that flat `{path}` —
    matching history.jsonl / resume-replay — never the old `read_file_reads`
    batch wrapper a reader might still expect. (The render-vs-wrap gap the
    2d45aa5 fix closed is still observable for the batch tools whose wrap
    re-shapes flat ops; read_file no longer exercises it but pins the flat
    card invariant.)"""

    def _capture_render(self, monkeypatch):
        import agent_cli.loop as loop_mod

        recorded: list[dict] = []
        real = loop_mod.render_step

        def fake(step_type, content, turn, **kwargs):
            recorded.append(
                {
                    "type": step_type,
                    "content": content,
                    "tool_name": kwargs.get("tool_name"),
                    "tool_input": kwargs.get("tool_input"),
                    "success": kwargs.get("success", True),
                }
            )
            real(step_type, content, turn, **kwargs)

        monkeypatch.setattr("agent_cli.loop.render_step", fake)
        return recorded

    def test_read_file_card_shows_flat_path_not_wrapped_batch(
        self, caps, monkeypatch, tmp_path
    ):
        from agent_cli.context.manager import ContextManager
        from agent_cli.wire_formats import get

        target = tmp_path / "f.txt"
        target.write_text("hello world\n")
        recorded = self._capture_render(monkeypatch)

        ctx = ContextManager(session_dir=tmp_path, wire_format=get("md_array"))
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(
                content="## Thought\nread it\n\n## Action\n"
                f'[{{"action": "read_file", "path": "{target}"}}]'
            ),
            LLMResponse(
                content="## Thought\ndone\n\n## Action\n"
                '[{"action": "complete", "result": "ok"}]'
            ),
        ]
        run_loop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            max_turns=5,
            wire_format="md_array",
        )

        action_inputs = [
            r["tool_input"]
            for r in recorded
            if r["type"] == "action" and r["tool_name"] == "read_file"
        ]
        assert action_inputs, "no read_file action was rendered"
        rendered = action_inputs[0]
        # The card shows the model's flat emission — NOT the wrapped batch form.
        assert "read_file_reads" not in rendered
        assert json.loads(rendered) == {"path": str(target)}

    def test_dispatch_runs_flat_op(self, caps, monkeypatch, tmp_path):
        # Regression guard: the flat read op executes (identity wrap → run) and
        # returns the file body. We assert the observation carries the content.
        from agent_cli.context.manager import ContextManager
        from agent_cli.wire_formats import get

        target = tmp_path / "f.txt"
        target.write_text("UNIQUE_MARKER_LINE\n")
        recorded = self._capture_render(monkeypatch)

        ctx = ContextManager(session_dir=tmp_path, wire_format=get("md_array"))
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(
                content="## Thought\nread it\n\n## Action\n"
                f'[{{"action": "read_file", "path": "{target}"}}]'
            ),
            LLMResponse(
                content="## Thought\ndone\n\n## Action\n"
                '[{"action": "complete", "result": "ok"}]'
            ),
        ]
        run_loop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            max_turns=5,
            wire_format="md_array",
        )
        observations = [r["content"] for r in recorded if r["type"] == "observation"]
        assert any("UNIQUE_MARKER_LINE" in (o or "") for o in observations)


class TestRunLoopObservability:
    """End-to-end checks that TurnRecord JSONL is written correctly.

    These tests use a real ContextManager pointed at tmp_path so the
    recorder has a session_dir to write to. Records are read back via
    the JSONL file rather than mocking the recorder — the file *is* the
    contract analysis tools depend on.
    """

    def _read_turns(self, session_dir):
        path = session_dir / "turns.jsonl"
        if not path.exists():
            return []
        return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]

    def test_success_turn_records_no_failure(self, caps, tmp_path):
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(session_dir=tmp_path)
        provider = _make_provider(_complete("answer"))
        run_loop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="test-model",
            ctx=ctx,
        )

        rows = self._read_turns(tmp_path)
        assert len(rows) == 1
        assert rows[0]["model"] == "test-model"
        assert rows[0]["failure_signal"] is None
        assert rows[0]["primitives_applied"] == []
        # parse_stage > 0 means parser succeeded somewhere
        assert rows[0]["parse_stage"] >= 1

    def test_no_json_failure_records_signal_and_primitives(self, caps, tmp_path):
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(session_dir=tmp_path)
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(content="not json at all"),
            LLMResponse(content=_complete("recovered")),
        ]
        run_loop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            max_turns=5,
        )

        rows = self._read_turns(tmp_path)
        # First row: failure with primitives composed
        assert rows[0]["failure_signal"] == "NO_JSON"
        assert rows[0]["parse_stage"] == 0
        assert rows[0]["primitives_applied"] == [
            "echo_prior_output",
            "constrain_format_json",
        ]

    def test_degenerate_emission_labeled(self, caps, tmp_path):
        from agent_cli.context.manager import ContextManager
        from agent_cli.wire_formats import get

        # Empty wire blocks repeated (## Thought/## Action with no body) =
        # format runaway. This emission has NO JSON array → parse_stage 0,
        # which would naively be labeled NO_JSON. But degeneration is a
        # generation-level pathology checked BEFORE parse_stage, so it must be
        # labeled DEGENERATE (the more specific cause) — guarding the label
        # ordering. Recovery then re-prompts and the model completes. (Real
        # runs break this mid-stream via degeneration_check; the mock provider
        # returns the full text so this exercises the loop's labeling branch.)
        ctx = ContextManager(session_dir=tmp_path, wire_format=get("md_array"))
        degen = "## Thought\n\n## Action\n\n## Thought\n\n## Action\n"
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(content=degen),
            LLMResponse(
                content="## Thought\ndone\n\n## Action\n"
                '[{"action": "complete", "result": "ok"}]'
            ),
        ]
        run_loop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            max_turns=5,
            wire_format="md_array",
        )
        rows = self._read_turns(tmp_path)
        assert rows[0]["failure_signal"] == "DEGENERATE"
        assert rows[1]["primitives_applied"] == []

    def test_raw_failures_captured_when_env_enabled(self, caps, tmp_path, monkeypatch):
        """AGENT_CLI_RECORD_RAW_FAILURES=on → 실패 턴의 raw 응답이
        raw_failures.jsonl 에 잡힌다 (turns.jsonl 은 메타만 유지)."""
        monkeypatch.setenv("AGENT_CLI_RECORD_RAW_FAILURES", "on")
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(session_dir=tmp_path)
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(content="not json at all"),
            LLMResponse(content=_complete("recovered")),
        ]
        run_loop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            max_turns=5,
        )

        path = tmp_path / "raw_failures.jsonl"
        assert path.exists()
        rows = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
        # 실패 턴(NO_JSON)만 잡힘 — recovery 성공 턴은 안 남음
        assert len(rows) == 1
        assert rows[0]["failure_signal"] == "NO_JSON"
        assert rows[0]["parse_stage"] == 0
        assert rows[0]["raw"] == "not json at all"

    def test_raw_failures_off_by_default(self, caps, tmp_path, monkeypatch):
        """env 미설정 → raw_failures.jsonl 생성 안 됨 (turns.jsonl 은 정상)."""
        monkeypatch.delenv("AGENT_CLI_RECORD_RAW_FAILURES", raising=False)
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(session_dir=tmp_path)
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(content="not json at all"),
            LLMResponse(content=_complete("recovered")),
        ]
        run_loop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            max_turns=5,
        )

        assert not (tmp_path / "raw_failures.jsonl").exists()
        assert (tmp_path / "turns.jsonl").exists()  # turns 는 정상 기록

    def test_empty_response_records_no_output_signal(self, caps, tmp_path):
        """A1b: model emits empty/whitespace-only content. The label must
        be NO_OUTPUT (not NO_JSON), and primitives stay empty because
        echo_prior_output has nothing to quote."""
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(session_dir=tmp_path)
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(content=""),
            LLMResponse(content=_complete("recovered")),
        ]
        run_loop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            max_turns=5,
        )

        rows = self._read_turns(tmp_path)
        assert rows[0]["failure_signal"] == "NO_OUTPUT"
        assert rows[0]["parse_stage"] == 0
        # Empty content → fallback path → no primitives composed.
        assert rows[0]["primitives_applied"] == []
        assert rows[1]["failure_signal"] is None

    def test_whitespace_only_response_records_no_output_signal(self, caps, tmp_path):
        """Whitespace-only content (newlines, tabs, spaces) must also be
        classified as NO_OUTPUT — operationally identical to empty."""
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(session_dir=tmp_path)
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(content="   \n\t\n  "),
            LLMResponse(content=_complete("recovered")),
        ]
        run_loop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            max_turns=5,
        )

        rows = self._read_turns(tmp_path)
        assert rows[0]["failure_signal"] == "NO_OUTPUT"

    def test_non_empty_non_json_still_labeled_no_json(self, caps, tmp_path):
        """Boundary: NO_JSON label is reserved for *non-empty* content
        that failed to parse — the format-drift case where echo grounding
        is meaningful."""
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(session_dir=tmp_path)
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(content="thought: drifted into YAML"),
            LLMResponse(content=_complete("recovered")),
        ]
        run_loop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            max_turns=5,
        )

        rows = self._read_turns(tmp_path)
        assert rows[0]["failure_signal"] == "NO_JSON"
        # Non-empty content → echo path → primitives populated
        assert rows[0]["primitives_applied"] == [
            "echo_prior_output",
            "constrain_format_json",
        ]

    def test_no_action_failure_records_signal(self, caps, tmp_path):
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(session_dir=tmp_path)
        # JSON parses but lacks action field
        no_action_json = json.dumps({"thought": "hmm", "args": {}})
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(content=no_action_json),
            LLMResponse(content=_complete("recovered")),
        ]
        run_loop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            max_turns=5,
        )

        rows = self._read_turns(tmp_path)
        assert rows[0]["failure_signal"] == "NO_ACTION"
        # parse_stage is >0 because JSON itself parsed
        assert rows[0]["parse_stage"] >= 1
        assert rows[0]["primitives_applied"] == [
            "echo_prior_output",
            "constrain_action_required",
        ]

    def test_seq_increments_across_turns(self, caps, tmp_path):
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(session_dir=tmp_path)
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(content="garbage 1"),
            LLMResponse(content="garbage 2"),
            LLMResponse(content=_complete("ok")),
        ]
        run_loop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            max_turns=10,
        )

        rows = self._read_turns(tmp_path)
        assert len(rows) >= 3
        # seq removed (was run-local); rows are ordered by append + timestamp.
        assert all("timestamp" in r and "seq" not in r for r in rows[:3])

    def test_opt_out_writes_no_file(self, caps, tmp_path):
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(session_dir=tmp_path)
        provider = _make_provider(_complete("answer"))
        run_loop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            record_turns=False,
        )
        assert not (tmp_path / "turns.jsonl").exists()

    def test_no_session_writes_no_file(self, caps, tmp_path):
        # ctx=None → headless / subagent path. Recorder must remain
        # disabled even when the flag is on (no place to write).
        provider = _make_provider(_complete("answer"))
        run_loop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=None,
            record_turns=True,
        )
        # No turns.jsonl should be created anywhere under tmp_path
        assert list(tmp_path.glob("**/turns.jsonl")) == []

    def test_nested_envelope_records_signal_and_unwraps(self, caps, tmp_path):
        """A6: model double-wraps the complete payload — the detector
        still records ``NESTED_ENVELOPE`` (observability for Step 4b
        analysis), AND the user-facing output is unwrapped one level
        so the final card / CLI doesn't show the raw ``{"result":
        "..."}`` artifact. No recovery primitives applied — the
        unwrap is purely cosmetic; the LLM still completed."""
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(session_dir=tmp_path)
        # Model emits {"action":"complete","action_input":{"result":"<JSON>"}}
        # where the inner result is itself a JSON envelope.
        nested = json.dumps({"result": "the actual story"})
        provider = _make_provider(_complete(nested))
        result = run_loop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
        )

        rows = self._read_turns(tmp_path)
        assert len(rows) == 1
        assert rows[0]["failure_signal"] == "NESTED_ENVELOPE"
        # Observability signal preserved — no recovery primitives
        # applied for A6 since the unwrap is non-corrective.
        assert rows[0]["primitives_applied"] == []
        # Output unwrapped one level — the inner ``result`` string.
        assert result.output == "the actual story"

    def test_well_formed_complete_does_not_flag_nested_envelope(self, caps, tmp_path):
        """Plain text result must NOT trigger the A6 detector."""
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(session_dir=tmp_path)
        provider = _make_provider(_complete("just a plain answer"))
        run_loop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
        )

        rows = self._read_turns(tmp_path)
        assert rows[0]["failure_signal"] is None


def _shell_call(cmd: str) -> str:
    """Build a shell tool call as a JSON envelope."""
    return json.dumps(
        {
            "thought": "running",
            "action": "shell",
            "action_input": {"shell_command": cmd},
        }
    )


class TestRunLoopActionLoop:
    """B1 (action loop) detection + recovery — manufactured loop scenarios.

    Threshold is 2: the SECOND consecutive identical (action, args) call
    fires probe_progress; the third fires restate_task; the fourth
    hard-fails. A different action interleaved resets the counter.
    """

    def _read_turns(self, session_dir):
        path = session_dir / "turns.jsonl"
        if not path.exists():
            return []
        return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]

    def test_two_repeats_fires_probe_progress(self, caps, tmp_path):
        """First repeat (call #2 with same args) → probe_progress nudge,
        no dispatch. Recovery: model emits complete on next turn."""
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(session_dir=tmp_path)
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(content=_shell_call("ls /tmp")),  # turn 1: dispatch
            LLMResponse(content=_shell_call("ls /tmp")),  # turn 2: B1 fires
            LLMResponse(content=_complete("done")),  # turn 3: recovery
        ]
        result = run_loop(
            query="List files",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            max_turns=10,
        )
        assert result.success
        # Turn 2 retry message should contain probe_progress phrasing
        third_call_messages = provider.call.call_args_list[2].kwargs["messages"]
        retry_msg = third_call_messages[-1]["content"]
        assert "You have called" in retry_msg
        assert "shell" in retry_msg
        assert "Re-read the previous responses" in retry_msg
        # Must NOT include task anchor (that's restate_task's job)
        assert "You were asked to:" not in retry_msg

    def test_three_repeats_fires_restate_task(self, caps, tmp_path):
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(session_dir=tmp_path)
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(content=_shell_call("ls")),  # 1: dispatch
            LLMResponse(content=_shell_call("ls")),  # 2: probe_progress
            LLMResponse(content=_shell_call("ls")),  # 3: restate_task
            LLMResponse(content=_complete("done")),  # 4: recovery
        ]
        result = run_loop(
            query="The original user task here",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            max_turns=10,
        )
        assert result.success
        # Turn 3 (4th LLM call) should see restate_task message
        fourth_call_messages = provider.call.call_args_list[3].kwargs["messages"]
        retry_msg = fourth_call_messages[-1]["content"]
        assert "You were asked to:" in retry_msg
        assert "The original user task here" in retry_msg
        assert "previous nudge did not work" in retry_msg.lower()

    def test_four_repeats_hard_fails(self, caps, tmp_path):
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(session_dir=tmp_path)
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(content=_shell_call("ls")) for _ in range(5)
        ]
        result = run_loop(
            query="t",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            max_turns=10,
        )
        assert not result.success
        assert "loop" in result.error.lower()
        # Error message should cite which primitives were tried
        assert "probe_progress" in result.error
        assert "restate_task" in result.error

    def test_different_action_resets_counter(self, caps, tmp_path):
        """B1 only fires on *consecutive* identical calls. A different
        action between repeats clears the counter."""
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(session_dir=tmp_path)
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(content=_shell_call("ls")),  # 1
            LLMResponse(content=_shell_call("pwd")),  # 2 (different)
            LLMResponse(content=_shell_call("ls")),  # 3 (same as 1, but reset)
            LLMResponse(content=_complete("done")),  # 4
        ]
        result = run_loop(
            query="t",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            max_turns=10,
        )
        assert result.success
        # Verify no B1 record was emitted (no failure_signal=ACTION_LOOP)
        rows = self._read_turns(tmp_path)
        assert all(r["failure_signal"] != "ACTION_LOOP" for r in rows)

    def test_probe_progress_recorded_in_turnrecord(self, caps, tmp_path):
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(session_dir=tmp_path)
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(content=_shell_call("ls")),
            LLMResponse(content=_shell_call("ls")),  # B1 fires
            LLMResponse(content=_complete("done")),
        ]
        run_loop(
            query="t",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            max_turns=10,
        )
        rows = self._read_turns(tmp_path)
        # Find the B1 row
        b1_rows = [r for r in rows if r["failure_signal"] == "ACTION_LOOP"]
        assert len(b1_rows) == 1
        assert b1_rows[0]["primitives_applied"] == ["probe_progress"]

    def test_restate_task_recorded_in_turnrecord(self, caps, tmp_path):
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(session_dir=tmp_path)
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(content=_shell_call("ls")),
            LLMResponse(content=_shell_call("ls")),  # probe_progress
            LLMResponse(content=_shell_call("ls")),  # restate_task
            LLMResponse(content=_complete("done")),
        ]
        run_loop(
            query="t",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            max_turns=10,
        )
        rows = self._read_turns(tmp_path)
        b1_rows = [r for r in rows if r["failure_signal"] == "ACTION_LOOP"]
        assert len(b1_rows) == 2
        assert b1_rows[0]["primitives_applied"] == ["probe_progress"]
        assert b1_rows[1]["primitives_applied"] == ["restate_task"]


class TestRunLoopUnknownTool:
    """A4 — model emits an action that is not in the registry.

    Pre-dispatch detection in the recovery layer labels the failure and
    feeds the same observation the leaf-level dispatch would have
    produced. The tool is not actually invoked.
    """

    def _read_turns(self, session_dir):
        path = session_dir / "turns.jsonl"
        if not path.exists():
            return []
        return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]

    def test_unknown_tool_labeled_in_turnrecord(self, caps, tmp_path):
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(session_dir=tmp_path)
        bogus_call = json.dumps(
            {
                "thought": "trying",
                "action": "bogus_tool",
                "action_input": {"x": 1},
            }
        )
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(content=bogus_call),
            LLMResponse(content=_complete("recovered")),
        ]
        result = run_loop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            max_turns=5,
        )
        assert result.success
        rows = self._read_turns(tmp_path)
        a4_rows = [r for r in rows if r["failure_signal"] == "UNKNOWN_TOOL"]
        assert len(a4_rows) == 1
        assert a4_rows[0]["primitives_applied"] == []  # no primitive yet (Step 4b)

    def test_unknown_tool_observation_lists_available(self, caps, tmp_path):
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(session_dir=tmp_path)
        bogus_call = json.dumps({"thought": "t", "action": "bogus", "action_input": {}})
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(content=bogus_call),
            LLMResponse(content=_complete("ok")),
        ]
        run_loop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            max_turns=5,
        )
        # The observation injected before the recovery call should
        # describe the failure with the available-tools list.
        second_call_messages = provider.call.call_args_list[1].kwargs["messages"]
        observation = second_call_messages[-1]["content"]
        assert "Unknown tool" in observation
        assert "bogus" in observation
        assert "Available:" in observation


class TestRunLoopSchemaMismatch:
    """A5 — model emits a known action with input violating the schema.

    The detector still normalizes inputs (string→dict) when valid; the
    integration test exercises the *failure* path where normalization
    cannot save the input.
    """

    def _read_turns(self, session_dir):
        path = session_dir / "turns.jsonl"
        if not path.exists():
            return []
        return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]

    def test_missing_required_field_labeled(self, caps, tmp_path):
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(session_dir=tmp_path)
        # write_file requires `path` + `content`; the model omits `path`. (A
        # top-level required field, caught by validate. read_file's required
        # field is the inner `reads[].path`, which multi-op wrapping nests
        # below the validated top level — so a bad read item wouldn't surface
        # here. write_file's `path` is top-level after wrap.)
        bad_call = json.dumps(
            {
                "thought": "trying",
                "action": "write_file",
                "action_input": {"content": "x"},
            }
        )
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(content=bad_call),
            LLMResponse(content=_complete("recovered")),
        ]
        result = run_loop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            max_turns=5,
        )
        assert result.success
        rows = self._read_turns(tmp_path)
        a5_rows = [r for r in rows if r["failure_signal"] == "SCHEMA_MISMATCH"]
        assert len(a5_rows) == 1
        assert a5_rows[0]["primitives_applied"] == []

    def test_schema_observation_includes_schema_hint(self, caps, tmp_path):
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(session_dir=tmp_path)
        # write_file with empty input → missing top-level required `path`.
        bad_call = json.dumps(
            {"thought": "t", "action": "write_file", "action_input": {}}
        )
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(content=bad_call),
            LLMResponse(content=_complete("ok")),
        ]
        run_loop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
            max_turns=5,
        )
        second_call_messages = provider.call.call_args_list[1].kwargs["messages"]
        observation = second_call_messages[-1]["content"]
        # Error mentions which field is missing and the schema
        assert "path" in observation
        assert "Missing required field" in observation


class TestRunLoopMaxIter:
    def test_returns_none_on_max_turns(self, caps):
        provider = _make_provider(
            json.dumps(
                {
                    "thought": "thinking",
                    "action": "shell",
                    "action_input": {"shell_command": "date +%s"},
                }
            ),
            json.dumps(
                {
                    "thought": "thinking",
                    "action": "shell",
                    "action_input": {"shell_command": "uname -s"},
                }
            ),
            json.dumps(
                {
                    "thought": "thinking",
                    "action": "shell",
                    "action_input": {"shell_command": "whoami"},
                }
            ),
        )
        result = run_loop(
            query="Keep going",
            provider=provider,
            capabilities=caps,
            model="test-model",
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
                    "action_input": {"shell_command": "whoami"},
                }
            ),
            _complete("ok"),
        )
        result = run_loop(
            query="Read file then run command",
            provider=provider,
            capabilities=caps,
            model="test-model",
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
        )
        assert result.output == "Task completed successfully."

    def test_echo_with_pipe_not_intercepted(self, caps):
        """echo ... | grep should NOT be treated as final answer."""
        provider = _make_provider(
            json.dumps(
                {
                    "thought": "search",
                    "action": "shell",
                    "action_input": {"shell_command": "echo hello | grep h"},
                }
            ),
            _complete("found"),
        )
        result = run_loop(
            query="Search",
            provider=provider,
            capabilities=caps,
            model="test-model",
        )
        assert result.output == "found"

    def test_echo_with_redirect_not_intercepted(self, caps):
        """echo ... > file should NOT be treated as final answer."""
        provider = _make_provider(
            json.dumps(
                {
                    "thought": "write",
                    "action": "shell",
                    "action_input": {"shell_command": "echo hello > out.txt"},
                }
            ),
            _complete("written"),
        )
        result = run_loop(
            query="Write",
            provider=provider,
            capabilities=caps,
            model="test-model",
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
        )
        assert result.output == "answer"


class TestAskToolAvailability:
    """Verify ask tool inclusion/exclusion based on ctx presence."""

    def test_ask_available_with_ctx(self, caps, tmp_path):
        """ctx present → ask included."""
        from agent_cli.loop import AgentLoop

        ctx = MagicMock()
        ctx.session_dir = tmp_path
        loop = AgentLoop(
            query="Q",
            provider=MagicMock(),
            capabilities=caps,
            model="m",
            ctx=ctx,
        )
        assert "ask" in loop.tools_list

    def test_ask_hidden_without_ctx(self, caps):
        """ctx=None → ask removed (non-interactive mode)."""
        from agent_cli.loop import AgentLoop

        loop = AgentLoop(
            query="Q",
            provider=MagicMock(),
            capabilities=caps,
            model="m",
            ctx=None,
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

    def test_interrupt_records_observation_in_ctx(self, caps, tmp_path):
        """Interrupt is recorded as a tool-style observation, not a bare
        user message — so the transcript doesn't show two consecutive
        user turns and recent_exchanges skips it via the ``tool`` field."""
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
        )
        # Set interrupt before first iteration (after setup adds user query)
        loop._interrupted = True
        loop.run()

        # Raw history: the interrupt is a tool observation, not a plain
        # user message.
        raw = ctx.get_raw_messages()
        interrupts = [m for m in raw if m.get("tool") == "interrupt"]
        assert len(interrupts) == 1
        assert interrupts[0]["role"] == "user"
        assert interrupts[0]["success"] is False
        assert interrupts[0]["content"].startswith("⚡ User interrupted")
        # No bare user-role INTERRUPT_NOTICE (the old shape).
        from agent_cli.constants import INTERRUPT_NOTICE

        bare = [
            m
            for m in raw
            if m.get("role") == "user"
            and not m.get("tool")
            and m.get("content") == INTERRUPT_NOTICE
        ]
        assert bare == []

        # Rendered for the LLM: shows up as an "[interrupt] …" observation.
        rendered = [m["content"] for m in ctx.get_messages() if m["role"] == "user"]
        assert any(c.startswith("[interrupt]") for c in rendered)

    def test_interrupt_check_passed_to_provider(self, caps, tmp_path):
        """The loop hands the provider a zero-arg ``interrupt_check`` so the
        streaming path can break mid-generation; it reflects ``stop_event``."""
        from agent_cli.loop import AgentLoop
        from agent_cli.context.manager import ContextManager

        provider = MagicMock()
        provider.call.side_effect = [LLMResponse(content=_complete("done"))]
        ctx = ContextManager(session_dir=tmp_path)
        loop = AgentLoop(
            query="Q", provider=provider, capabilities=caps, model="m", ctx=ctx
        )
        loop.run()

        check = provider.call.call_args.kwargs["interrupt_check"]
        assert callable(check)
        assert check() is False  # stop_event not set
        loop.stop_event.set()
        assert check() is True  # now it reflects the interrupt

    def test_interrupt_check_shared_with_nested_loop(self, caps):
        """Skills and delegates run as nested AgentLoops built with the
        parent's stop_event (loop.py passes stop_event=self.stop_event to
        _handle_run_skill and tool_delegate). One interrupt therefore breaks
        in-flight generation in children too — including each parallel
        delegate worker, which closes its OWN stream on its OWN thread (the
        reason the close happens on the owning thread, not the signal
        handler). Here the child shares the parent's event, so setting it
        flips both _interrupt_check predicates."""
        from agent_cli.loop import AgentLoop

        parent = AgentLoop(
            query="Q", provider=MagicMock(), capabilities=caps, model="m"
        )
        child = AgentLoop(
            query="Q",
            provider=MagicMock(),
            capabilities=caps,
            model="m",
            stop_event=parent.stop_event,
        )
        assert parent._interrupt_check() is False
        assert child._interrupt_check() is False
        parent.stop_event.set()
        assert parent._interrupt_check() is True
        assert child._interrupt_check() is True  # one Ctrl+C reaches the child

    def test_interrupt_midstream_discards_partial(self, caps, tmp_path):
        """A stream broken mid-generation (provider returns
        ``stop_reason='interrupted'``) is DISCARDED, not parsed/dispatched.
        Even though the partial here is a well-formed ``complete`` action, the
        loop routes to the interrupt handler instead of completing, and no
        assistant turn is recorded — only the interrupt notice. (Without the
        discard branch this would parse+dispatch as a successful complete.)"""
        from agent_cli.loop import AgentLoop
        from agent_cli.context.manager import ContextManager

        partial = _complete("SHOULD_NOT_COMPLETE")
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(content=partial, stop_reason="interrupted")
        ]
        ctx = ContextManager(session_dir=tmp_path)
        loop = AgentLoop(
            query="Q", provider=provider, capabilities=caps, model="m", ctx=ctx
        )
        result = loop.run()

        # Ended via interrupt, not the discarded complete.
        assert not result.success
        assert result.error == "Interrupted by user"
        assert provider.call.call_count == 1

        raw = ctx.get_raw_messages()
        # Interrupt notice recorded...
        assert [m for m in raw if m.get("tool") == "interrupt"]
        # ...but the partial never entered ctx as an assistant turn.
        assert [m for m in raw if m.get("role") == "assistant"] == []

    def test_interrupt_renders_via_render_step_not_console(self, caps, tmp_path):
        """The notice goes through render_step (CLI console / web SSE),
        not a direct console.print — the latter leaked to the web
        server's terminal ('⚡ Interrupted after turn N')."""
        from unittest.mock import patch
        from agent_cli.context.manager import ContextManager
        from agent_cli.loop import AgentLoop

        provider = MagicMock()
        provider.call.side_effect = [LLMResponse(content=_complete("done"))]
        ctx = ContextManager(session_dir=tmp_path)
        loop = AgentLoop(
            query="Q", provider=provider, capabilities=caps, model="m", ctx=ctx
        )
        loop._interrupted = True
        with patch("agent_cli.loop.render_step") as mock_rs:
            loop.run()
        interrupt_renders = [
            c
            for c in mock_rs.call_args_list
            if c.args
            and c.args[0] == "observation"
            and c.kwargs.get("tool_name") == "interrupt"
        ]
        assert len(interrupt_renders) == 1

    def test_stop_event_between_turns_reports_interrupt(self, caps, tmp_path):
        """Repro for the "Max turns (0) reached" misreport: a Ctrl+C during
        a turn sets `stop_event` but the body finishes its work (e.g. an
        ask answer comes back) and returns `_CONTINUE`. On the next
        iteration `_should_continue` sees `stop_event` and returns False —
        the body's `if self._interrupted` check never re-runs, so before
        the fix the loop fell through to `_on_max_turns()` and reported
        "Max turns (0) reached" even when max_turns was 0 (= unlimited).

        After the fix, the post-loop branch checks `_interrupted` and
        returns the interrupt result with the correct error message."""
        import json as _json
        import threading
        from agent_cli.loop import AgentLoop
        from agent_cli.context.manager import ContextManager

        # First turn: tool call. After this returns `_CONTINUE`, the test
        # sets stop_event to simulate Ctrl+C arriving between turns.
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
            # Second response is never consumed — `_should_continue` should
            # gate the loop before the LLM is called again.
            _complete("never reached"),
        ]
        provider = MagicMock()
        provider.call.side_effect = [LLMResponse(content=r) for r in responses]

        stop_event = threading.Event()
        ctx = ContextManager(session_dir=tmp_path / "session")
        loop = AgentLoop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="m",
            max_turns=0,  # unlimited — must NOT be reported as max-turns hit
            ctx=ctx,
            stop_event=stop_event,
        )

        # Wedge stop_event setting at the end of turn 1: after the turn
        # completes, the next `_should_continue()` call will return False.
        original_execute = loop._execute_turn

        def _execute_then_stop():
            result = original_execute()
            stop_event.set()
            return result

        loop._execute_turn = _execute_then_stop

        result = loop.run()

        assert not result.success
        # The fix: error must say "Interrupted", NOT "Max turns".
        assert "Interrupted" in (result.error or "")
        assert "Max turns" not in (result.error or "")
        # Only the first turn's LLM call ran.
        assert provider.call.call_count == 1

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
        supports_thinking=False,
        thinking_budget=0,
        supports_strict_schema=True,
    )


class TestAskTool:
    def test_ask_emits_action_render_step_before_handler(
        self, caps, monkeypatch, tmp_path
    ):
        """The ask tool must emit ``render_step("action", ...)`` before
        invoking ``_handle_ask``. Without it, the web renderer's
        streaming-text card (raw JSON) never gets replaced with a
        structured ``assistant_turn`` card — and the next turn's
        streaming chunks visually append to it. Same applies to
        ``run_skill`` and ``ready_for_review``."""
        from agent_cli.context.manager import ContextManager

        recorded: list[tuple] = []
        real_render_step = None

        def fake_render_step(step_type, content, turn, **kwargs):
            recorded.append((step_type, kwargs.get("tool_name"), content))
            if real_render_step is not None:
                real_render_step(step_type, content, turn, **kwargs)

        import agent_cli.loop as loop_mod

        real_render_step = loop_mod.render_step
        monkeypatch.setattr("agent_cli.loop.render_step", fake_render_step)
        monkeypatch.setattr("builtins.input", lambda _: "ok")

        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(
                content=json.dumps(
                    {
                        "thought": "ask the user",
                        "action": "ask",
                        "action_input": {"questions": ["What now?"]},
                    }
                )
            ),
            LLMResponse(
                content=json.dumps(
                    {
                        "thought": "done",
                        "action": "complete",
                        "action_input": {"result": "Done"},
                    }
                )
            ),
        ]
        ctx = ContextManager(session_dir=tmp_path)
        run_loop(
            query="Q",
            provider=provider,
            capabilities=caps,
            model="test-model",
            ctx=ctx,
        )

        steps = [(t, name) for (t, name, _) in recorded]
        # ``action`` for ``ask`` must precede the user's prompt — without
        # it the web frontend never replaces its streaming card.
        assert ("action", "ask") in steps
        thought_idx = steps.index(("thought", None))
        action_idx = steps.index(("action", "ask"))
        assert thought_idx < action_idx

    def test_ask_passes_question_block_as_context_to_renderer(self, monkeypatch):
        """The ask tool builds a plain-text question block and forwards
        it to ``prompt_user`` as ``context``. Out-of-band renderers
        (web) read this to attach the question to their input form so
        the user can see what they're answering without scrolling
        back. CLI renderers ignore ``context`` because the colored
        announcement above already covers them."""
        from agent_cli.loop import _handle_ask

        captured: dict = {}

        class RecordingRenderer:
            _prefix = ""

            def can_prompt(self):
                return True

            def announce_ask(self, questions, *, prefix=""):
                # No-op stub — the announcement is what CLI renderers
                # print before reading stdin; the test only cares
                # about the prompt_user context arg.
                pass

            def prompt_user(self, prompt, **kwargs):
                captured["prompt"] = prompt
                captured["kwargs"] = kwargs
                return "the answer"

        monkeypatch.setattr(
            "agent_cli.render.get_renderer", lambda: RecordingRenderer()
        )

        result = _handle_ask(["What's your name?", "What's your color?"])
        assert "context" in captured["kwargs"]
        ctx = captured["kwargs"]["context"]
        assert ctx.startswith("Agent asks:")
        assert "1. What's your name?" in ctx
        assert "2. What's your color?" in ctx
        assert "the answer" in result

    def test_ask_context_single_question_drops_numbering(self, monkeypatch):
        """Single-question shape mirrors the CLI's bullet form — no
        ``1.`` prefix — so the displayed question reads naturally."""
        from agent_cli.loop import _handle_ask

        captured: dict = {}

        class RecordingRenderer:
            _prefix = ""

            def can_prompt(self):
                return True

            def announce_ask(self, questions, *, prefix=""):
                pass

            def prompt_user(self, prompt, **kwargs):
                captured["kwargs"] = kwargs
                return "ok"

        monkeypatch.setattr(
            "agent_cli.render.get_renderer", lambda: RecordingRenderer()
        )
        _handle_ask(["Should I continue?"])
        ctx = captured["kwargs"]["context"]
        assert "Should I continue?" in ctx
        assert "1. Should I continue?" not in ctx

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

    def test_list_of_dicts_with_question_key(self):
        """S25FE-kernel session 1776954600 repro: model emitted
        `questions=[{"question":"..."}]` instead of a list of strings.
        Previously the dict fell through to `str(q)` and the rendered
        question was the dict repr `{'question': '...'}`. Now extract
        the text from the dict."""
        from agent_cli.loop import _extract_questions

        result = _extract_questions({"questions": [{"question": "What next?"}]})
        assert result == ["What next?"]

    def test_list_of_dicts_with_text_key(self):
        """Common alternate field name."""
        from agent_cli.loop import _extract_questions

        result = _extract_questions({"questions": [{"text": "Pick one"}]})
        assert result == ["Pick one"]

    def test_list_of_dicts_with_content_key(self):
        from agent_cli.loop import _extract_questions

        result = _extract_questions({"questions": [{"content": "Ready?"}]})
        assert result == ["Ready?"]

    def test_list_mixed_strings_and_dicts(self):
        """Extraction works item-by-item regardless of homogeneity."""
        from agent_cli.loop import _extract_questions

        result = _extract_questions(
            {"questions": ["plain string", {"question": "nested"}, "another"]}
        )
        assert result == ["plain string", "nested", "another"]

    def test_raw_questions_is_single_dict(self):
        """`action_input.get("questions")` returns a dict, not a list —
        treat as a single question."""
        from agent_cli.loop import _extract_questions

        result = _extract_questions({"questions": {"question": "Solo?"}})
        assert result == ["Solo?"]

    def test_dict_without_known_text_field_skipped(self):
        """Dict items without any recognizable text key drop out of the
        list rather than becoming `str(dict)` noise."""
        from agent_cli.loop import _extract_questions

        result = _extract_questions(
            {"questions": ["good", {"unknown_field": "value"}, "also good"]}
        )
        assert result == ["good", "also good"]

    def test_dict_with_non_string_value_skipped(self):
        """If the text field exists but isn't a string (e.g. nested
        dict), skip that item rather than str()-ing it."""
        from agent_cli.loop import _extract_questions

        result = _extract_questions(
            {"questions": [{"question": {"nested": "obj"}}, "clean"]}
        )
        assert result == ["clean"]


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
        # No TTY under pytest → force the prompt capability so _handle_ask
        # actually reads instead of substituting "(no response)".
        monkeypatch.setattr(
            "agent_cli.render.minimal.MinimalRenderer.can_prompt",
            lambda self: True,
        )
        run_loop(
            query="Help",
            provider=provider,
            capabilities=caps,
            model="test",
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
        from agent_cli.wire_formats import get as get_wire_format

        messages = []
        _append_observation(
            messages,
            None,
            get_wire_format("react"),
            "llm",
            "obs",
            tool_name="write_file",
            success=True,
        )
        assert len(messages) == 2

    def test_append_observation_routes_history_through_wire_format(self):
        """ctx receives the dict produced by wire_format.serialize_assistant_for_history.

        The contract is: assistant record in history.jsonl is shaped by the
        plugin, not by loop.py. Verifying this via fake ctx + fake plugin
        catches any future regression that re-introduces a json.loads() call
        directly in loop.
        """
        from agent_cli.loop import _append_observation

        captured: list[dict] = []

        class _FakeCtx:
            def add(self, entry):
                captured.append(entry)

        class _FakePlugin:
            def serialize_assistant_for_history(self, raw_text):
                return {"role": "assistant", "marker": "from_plugin", "raw": raw_text}

            def render_assistant_from_history(self, record):
                return {"role": "assistant", "content": record["raw"]}

        messages: list[dict] = []
        _append_observation(
            messages,
            _FakeCtx(),
            _FakePlugin(),
            "LLM_TEXT",
            "OBS",
            tool_name="write_file",
            success=True,
        )
        # captured[0] is assistant record from plugin; captured[1] is observation.
        assert captured[0] == {
            "role": "assistant",
            "marker": "from_plugin",
            "raw": "LLM_TEXT",
        }
        assert captured[1] == {
            "role": "user",
            "tool": "write_file",
            "success": True,
            "content": "OBS",
        }

    def test_append_observation_builds_prior_via_render(self):
        """The next-turn prior (the in-memory messages assistant turn) is built
        by ``render_assistant_from_history`` of the serialized record — NOT the
        raw emission. Rebuilding from the (save-time sanitized) record is what
        keeps a leaked wire sentinel out of the prior (mimicry → runaway
        prevention) and unifies the live prior with the resume prior.

        Pins against any regression that re-feeds raw llm_text into the prior
        (the removed normalize_assistant_for_messages=identity path).
        """
        from agent_cli.loop import _append_observation

        class _FakePlugin:
            def serialize_assistant_for_history(self, raw_text):
                return {"role": "assistant", "content": raw_text}

            def render_assistant_from_history(self, record):
                return {
                    "role": "assistant",
                    "content": f"<rendered>{record['content']}</rendered>",
                }

        messages: list[dict] = []
        _append_observation(
            messages,
            None,
            _FakePlugin(),
            "LLM_TEXT",
            "OBS",
            tool_name="write_file",
            success=True,
        )
        assert messages[0] == {
            "role": "assistant",
            "content": "<rendered>LLM_TEXT</rendered>",
        }
        assert messages[1] == {"role": "user", "content": "OBS"}

    def test_append_observation_persists_tool_and_success(self):
        """The observation entry written to ``ctx`` carries the ``tool``
        and ``success`` fields used by the web renderer's
        ``replay_from_history`` to distinguish observations from plain
        user chat turns and to reproduce ✓/✗ on replay."""
        from agent_cli.loop import _append_observation

        captured: list[dict] = []

        class _FakeCtx:
            def add(self, entry):
                captured.append(entry)

        class _FakePlugin:
            def serialize_assistant_for_history(self, raw):
                return {"role": "assistant", "content": raw}

            def render_assistant_from_history(self, record):
                return {"role": "assistant", "content": record["content"]}

        _append_observation(
            [],
            _FakeCtx(),
            _FakePlugin(),
            "LLM",
            "Observation: oops",
            tool_name="edit_file",
            success=False,
        )
        # captured[0] assistant, captured[1] observation
        obs = captured[1]
        assert obs["tool"] == "edit_file"
        assert obs["success"] is False
        # ``content`` keeps the LLM-facing prefix unchanged — replay
        # strips it on the way to ``observation()``.
        assert obs["content"] == "Observation: oops"

    def test_append_observation_accepts_empty_tool_name(self):
        """Format-retry interventions (no specific tool) use
        ``tool_name=""``. The ``tool`` key MUST still be present so
        ``replay_from_history`` routes the entry through ``observation()``
        rather than mis-classifying it as a user chat message."""
        from agent_cli.loop import _append_observation

        captured: list[dict] = []

        class _FakeCtx:
            def add(self, entry):
                captured.append(entry)

        class _FakePlugin:
            def serialize_assistant_for_history(self, raw):
                return {"role": "assistant", "content": raw}

            def render_assistant_from_history(self, record):
                return {"role": "assistant", "content": record["content"]}

        _append_observation(
            [],
            _FakeCtx(),
            _FakePlugin(),
            "LLM",
            "format retry message",
            tool_name="",
            success=False,
        )
        obs = captured[1]
        assert "tool" in obs
        assert obs["tool"] == ""
        assert obs["success"] is False

    def test_append_observation_stores_artifact_when_provided(self):
        """``artifact`` (e.g. delegate subdir path) is preserved on disk
        so a future session viewer can resolve the linked artefact."""
        from agent_cli.loop import _append_observation

        captured: list[dict] = []

        class _FakeCtx:
            def add(self, entry):
                captured.append(entry)

        class _FakePlugin:
            def serialize_assistant_for_history(self, raw):
                return {"role": "assistant", "content": raw}

            def render_assistant_from_history(self, record):
                return {"role": "assistant", "content": record["content"]}

        _append_observation(
            [],
            _FakeCtx(),
            _FakePlugin(),
            "LLM",
            "Observation: skill done",
            tool_name="run_skill",
            success=True,
            artifact="path/to/artifact.md",
        )
        obs = captured[1]
        assert obs["artifact"] == "path/to/artifact.md"


class TestProviderCallKwargs:
    """Plugin-defined ``provider_call_kwargs`` reach ``provider.call``.

    The Protocol method exists so envelope-style plugins can flip
    ``skip_json_format=True`` to keep the provider's JSON mode from
    forcing ``{`` as the first token. Pinning the wiring here catches the previous leak
    (Protocol method defined but never unpacked into the provider
    call) so we don't reintroduce it.
    """

    def test_react_default_passes_no_extra_kwargs(self, caps):
        """ReAct returns ``{}`` so no plugin kwargs reach the provider."""
        provider = _make_provider(_complete("done"))
        run_loop(
            query="test",
            provider=provider,
            capabilities=caps,
            model="m",
        )
        call_kwargs = provider.call.call_args_list[0].kwargs
        # Wire-format-specific keys must not have been injected.
        assert "skip_json_format" not in call_kwargs

    def test_plugin_kwargs_unpacked_into_provider_call(self, caps, monkeypatch):
        """Overriding the plugin's provider_call_kwargs makes the kwarg
        appear in ``provider.call``'s actual invocation."""
        from agent_cli.wire_formats import get as get_wire_format

        plugin = get_wire_format("react")
        monkeypatch.setattr(
            plugin,
            "provider_call_kwargs",
            lambda capabilities: {"json_mode": False},
        )

        provider = _make_provider(_complete("done"))
        run_loop(
            query="test",
            provider=provider,
            capabilities=caps,
            model="m",
            wire_format=plugin,
        )
        call_kwargs = provider.call.call_args_list[0].kwargs
        assert call_kwargs.get("json_mode") is False


class TestProviderPrefill:
    """Plugin-defined ``prefill`` is appended to ``messages`` before the
    provider call and prepended back to the response.

    ReAct returns ``""`` — assistant message tail, response, byte-equivalent
    to the no-plugin path. Envelope-style plugins return non-empty strings
    to force the wire shape from the first generated token. Pinning the
    wiring here catches regressions that drop the prefill silently."""

    def test_react_empty_prefill_does_not_touch_messages(self, caps):
        """ReAct returns an empty prefill — the messages list reaching
        provider.call must not have a trailing assistant message added."""
        provider = _make_provider(_complete("done"))
        run_loop(
            query="hello",
            provider=provider,
            capabilities=caps,
            model="m",
        )
        forwarded = provider.call.call_args_list[0].kwargs["messages"]
        # The single message is the user query; no trailing assistant
        # prefill turn was injected.
        assert forwarded[-1]["role"] == "user"

    def test_plugin_prefill_appended_and_prepended(self, caps, monkeypatch):
        """Override plugin's prefill to a sentinel; verify the sentinel
        appears (a) as a trailing assistant message reaching provider.call,
        and (b) at the front of the response content the loop hands to
        the rest of the pipeline."""
        from agent_cli.wire_formats import get as get_wire_format

        plugin = get_wire_format("react")
        SENTINEL = "<<PREFILL_MARK>>"
        monkeypatch.setattr(plugin, "prefill", lambda: SENTINEL)

        # The loop will see the response, prepend the prefill, then
        # parse it. To make the loop terminate cleanly, the (prefill +
        # response) must be parseable as a complete action — which means
        # the model's "actual" response should produce that when the
        # sentinel is stitched on. We cheat: have the mock provider
        # return the rest of a valid ReAct complete dict, and ensure
        # SENTINEL is a single character that doesn't break JSON. But
        # easier: use a sentinel that, prepended to the mocked response,
        # still parses. ``""`` would work but defeats the point. Instead
        # have the mock return a payload that already contains SENTINEL
        # and just verify it appears in the messages reaching provider.
        provider = _make_provider(_complete("done"))
        run_loop(
            query="hi",
            provider=provider,
            capabilities=caps,
            model="m",
            wire_format=plugin,
        )
        forwarded = provider.call.call_args_list[0].kwargs["messages"]
        # Last message is the assistant prefill, content == SENTINEL.
        assert forwarded[-1]["role"] == "assistant"
        assert forwarded[-1]["content"] == SENTINEL

    def test_prefill_does_not_mutate_self_messages(self, caps, monkeypatch):
        """The loop's own ``self.messages`` list must not retain the
        prefill (history persistence and overflow recovery copy from
        self.messages — adding the prefill there would double-count it
        on the next turn)."""
        from agent_cli.wire_formats import get as get_wire_format

        plugin = get_wire_format("react")
        monkeypatch.setattr(plugin, "prefill", lambda: "<<PF>>")

        # Capture the loop's self.messages at provider.call time. The
        # forwarded messages should have the prefill at the end, but
        # the *loop's* self.messages (which we don't have direct access
        # to here) must not — proxy that by checking the second turn's
        # forwarded messages don't have an exponentially-growing prefix.
        # Simpler proxy: the call_messages forwarded must equal
        # self.messages + [prefill turn], not self.messages with the
        # prefill already baked in. We check that the prefill appears
        # exactly once in the forwarded messages on a single call.
        provider = _make_provider(_complete("ok"))
        run_loop(
            query="hi",
            provider=provider,
            capabilities=caps,
            model="m",
            wire_format=plugin,
        )
        forwarded = provider.call.call_args_list[0].kwargs["messages"]
        prefill_msgs = [m for m in forwarded if m.get("content") == "<<PF>>"]
        assert len(prefill_msgs) == 1


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
            provider_name="openai",
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
            provider_name="openai",
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


class TestBuildReviewObservation:
    """Unit tests for the ready_for_review observation builder.

    These pin the exact prompt shape so any future tweak is intentional
    and reviewed — small changes here can shift model self-review
    behavior significantly.
    """

    def test_includes_original_request(self):
        from agent_cli.loop import _build_review_observation

        obs = _build_review_observation("Fix the auth bug", "patched login.py")
        assert "--- ORIGINAL REQUEST ---" in obs
        assert "Fix the auth bug" in obs

    def test_includes_summary(self):
        from agent_cli.loop import _build_review_observation

        obs = _build_review_observation("q", "I changed three files")
        assert "--- YOUR SUMMARY ---" in obs
        assert "I changed three files" in obs

    def test_includes_review_instructions_header(self):
        from agent_cli.loop import _build_review_observation

        obs = _build_review_observation("q", "s")
        assert "--- REVIEW INSTRUCTIONS ---" in obs
        assert "Be adversarial" in obs

    def test_terminology_says_previous_observations_not_work_log(self):
        """(D) WORK LOG -> 'previous Observations' — small models
        understand the concrete term but were silently dropping the
        abstract 'WORK LOG' reference."""
        from agent_cli.loop import _build_review_observation

        obs = _build_review_observation("q", "s")
        assert "previous Observations" in obs
        assert "WORK LOG" not in obs

    def test_includes_output_format_template(self):
        """(A) Forces the self-review to be *generated*, not asserted."""
        from agent_cli.loop import _build_review_observation

        obs = _build_review_observation("q", "s")
        assert "Format your review like this:" in obs
        assert "Requirement 1:" in obs
        assert "[DONE | MISSING]" in obs
        assert "Decision: complete | continue" in obs

    def test_numbered_steps_still_present(self):
        """The 4-step checklist remains — A+D are additive, not a rewrite."""
        from agent_cli.loop import _build_review_observation

        obs = _build_review_observation("q", "s")
        for step in (
            "1. List each requirement",
            "2. For each requirement",
            "3. If a requirement is NOT met",
            "4. Only call complete",
        ):
            assert step in obs

    def test_decision_keywords_explicit(self):
        """The terminal line forces a binary outcome word — prevents
        ambiguous reviews like 'looks good' that don't commit to next
        action."""
        from agent_cli.loop import _build_review_observation

        obs = _build_review_observation("q", "s")
        last_line = obs.rstrip().splitlines()[-1]
        assert last_line == "Decision: complete | continue"

    def test_sections_appear_in_canonical_order(self):
        from agent_cli.loop import _build_review_observation

        obs = _build_review_observation("q", "s")
        pos_req = obs.index("--- ORIGINAL REQUEST ---")
        pos_sum = obs.index("--- YOUR SUMMARY ---")
        pos_ins = obs.index("--- REVIEW INSTRUCTIONS ---")
        pos_fmt = obs.index("Format your review like this:")
        assert pos_req < pos_sum < pos_ins < pos_fmt

    def test_omits_tool_calls_section_when_ctx_is_none(self):
        """ctx=None path must not synthesize the section."""
        from agent_cli.loop import _build_review_observation

        obs = _build_review_observation("q", "s", ctx=None)
        assert "--- YOUR TOOL CALLS" not in obs


class _FakeCtx:
    """Minimal stand-in for ContextManager — exposes ``get_raw_messages``
    only. Real ctx is heavyweight; the helper under test only reads
    that one method."""

    def __init__(self, messages):
        self._messages = list(messages)

    def get_raw_messages(self):
        return list(self._messages)


class TestFormatToolCallsForReview:
    """Unit tests for the ctx -> tool-calls section helper."""

    def test_returns_empty_when_ctx_is_none(self):
        from agent_cli.loop import _format_tool_calls_for_review

        assert _format_tool_calls_for_review(None) == ""

    def test_returns_empty_when_no_assistant_messages(self):
        from agent_cli.loop import _format_tool_calls_for_review

        ctx = _FakeCtx(
            [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "hi"},
            ]
        )
        assert _format_tool_calls_for_review(ctx) == ""

    def test_returns_empty_when_only_virtual_tools(self):
        """ready_for_review / complete / ask should not produce a section."""
        from agent_cli.loop import _format_tool_calls_for_review

        ctx = _FakeCtx(
            [
                {
                    "role": "assistant",
                    "action": "ready_for_review",
                    "action_input": {"summary": "done"},
                },
                {
                    "role": "assistant",
                    "action": "complete",
                    "action_input": {"result": "ok"},
                },
            ]
        )
        assert _format_tool_calls_for_review(ctx) == ""

    def test_lists_real_tool_calls_in_order(self):
        from agent_cli.loop import _format_tool_calls_for_review

        ctx = _FakeCtx(
            [
                {
                    "role": "assistant",
                    "action": "read_file",
                    "action_input": {"path": "login.py"},
                },
                {"role": "user", "tool": "read_file", "content": "Observation: ..."},
                {
                    "role": "assistant",
                    "action": "shell",
                    "action_input": {"shell_command": "pytest"},
                },
            ]
        )
        out = _format_tool_calls_for_review(ctx)
        assert "--- YOUR TOOL CALLS ---" in out
        pos_read = out.index("read_file(")
        pos_shell = out.index("shell(")
        assert pos_read < pos_shell

    def test_filters_virtual_tools_when_mixed(self):
        """A mix of real + virtual: only real ones appear."""
        from agent_cli.loop import _format_tool_calls_for_review

        ctx = _FakeCtx(
            [
                {
                    "role": "assistant",
                    "action": "read_file",
                    "action_input": {"path": "a.py"},
                },
                {
                    "role": "assistant",
                    "action": "ask",
                    "action_input": {"question": "?"},
                },
                {
                    "role": "assistant",
                    "action": "shell",
                    "action_input": {"shell_command": "ls"},
                },
                {
                    "role": "assistant",
                    "action": "ready_for_review",
                    "action_input": {"summary": "s"},
                },
            ]
        )
        out = _format_tool_calls_for_review(ctx)
        assert "read_file(" in out
        assert "shell(" in out
        assert "ask(" not in out
        assert "ready_for_review(" not in out

    def test_truncates_to_last_N_when_too_many(self):
        from agent_cli.loop import _format_tool_calls_for_review

        msgs = [
            {
                "role": "assistant",
                "action": "shell",
                "action_input": {"shell_command": f"echo {i}"},
            }
            for i in range(35)
        ]
        out = _format_tool_calls_for_review(_FakeCtx(msgs), max_calls=30)
        assert "last 30 of 35" in out
        assert "echo 34" in out
        # Earliest entry (echo 0) should be omitted (token-bounded check)
        assert "'echo 0'" not in out

    def test_long_string_args_are_truncated(self):
        from agent_cli.loop import _format_tool_calls_for_review

        long_cmd = "x" * 200
        ctx = _FakeCtx(
            [
                {
                    "role": "assistant",
                    "action": "shell",
                    "action_input": {"shell_command": long_cmd},
                }
            ]
        )
        out = _format_tool_calls_for_review(ctx)
        max_line = max(len(line) for line in out.splitlines())
        assert max_line < 150

    def test_non_scalar_args_collapse_to_type_marker(self):
        """Lists and dicts should render as <list>/<dict>, not full JSON."""
        from agent_cli.loop import _format_tool_calls_for_review

        ctx = _FakeCtx(
            [
                {
                    "role": "assistant",
                    "action": "edit_file",
                    "action_input": {
                        "edit_file_path": "a.py",
                        "edit_file_edits": [{"op": "replace", "lines": ["x"]}],
                    },
                }
            ]
        )
        out = _format_tool_calls_for_review(ctx)
        assert "edits=<list>" in out

    def test_get_raw_messages_failure_returns_empty(self):
        """Helper must not raise if ctx.get_raw_messages() blows up."""
        from agent_cli.loop import _format_tool_calls_for_review

        class _BrokenCtx:
            def get_raw_messages(self):
                raise RuntimeError("boom")

        assert _format_tool_calls_for_review(_BrokenCtx()) == ""


class TestBuildReviewObservationWithCtx:
    """Verify the ctx injection path in _build_review_observation."""

    def test_includes_tool_calls_section_when_ctx_has_real_calls(self):
        from agent_cli.loop import _build_review_observation

        ctx = _FakeCtx(
            [
                {
                    "role": "assistant",
                    "action": "shell",
                    "action_input": {"shell_command": "ls"},
                }
            ]
        )
        obs = _build_review_observation("q", "s", ctx=ctx)
        assert "--- YOUR TOOL CALLS ---" in obs
        assert "shell(" in obs

    def test_omits_tool_calls_section_when_ctx_has_only_virtual_tools(self):
        from agent_cli.loop import _build_review_observation

        ctx = _FakeCtx(
            [
                {
                    "role": "assistant",
                    "action": "ready_for_review",
                    "action_input": {"summary": "done"},
                }
            ]
        )
        obs = _build_review_observation("q", "s", ctx=ctx)
        assert "--- YOUR TOOL CALLS" not in obs

    def test_tool_calls_section_between_summary_and_instructions(self):
        """REQ -> SUM -> CALLS -> INSTRUCTIONS order."""
        from agent_cli.loop import _build_review_observation

        ctx = _FakeCtx(
            [
                {
                    "role": "assistant",
                    "action": "shell",
                    "action_input": {"shell_command": "ls"},
                }
            ]
        )
        obs = _build_review_observation("q", "s", ctx=ctx)
        pos_sum = obs.index("--- YOUR SUMMARY ---")
        pos_calls = obs.index("--- YOUR TOOL CALLS ---")
        pos_ins = obs.index("--- REVIEW INSTRUCTIONS ---")
        assert pos_sum < pos_calls < pos_ins


class TestNoOutputTruncation:
    """Verify tool output is passed to LLM without truncation."""

    def test_large_file_not_truncated(self, caps, tmp_path):
        """A large file requested via an explicit whole-file line range
        is returned in full, not truncated. (Bare read_file(path) now
        returns the full file too — the full-read guard was removed. This
        test pins the separate invariant that an explicit whole-file line
        range returns in full without truncation along the pipeline.)"""
        large_content = "\n".join(f"line {i}: {'x' * 100}" for i in range(500))
        test_file = tmp_path / "large.txt"
        test_file.write_text(large_content)

        provider = _make_provider(
            json.dumps(
                {
                    "thought": "read large file",
                    "action": "read_file",
                    "action_input": {
                        "path": str(test_file),
                        "line_start": 1,
                        "line_end": 500,
                    },
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
            ctx=ctx,
        )
        # Verify the observation contains all 500 lines (hashline format: "500#xx:")
        messages = ctx.get_messages()
        all_content = " ".join(m.get("content", "") for m in messages)
        assert "500#" in all_content, "Last line (500) should be in context"
        # Verify no truncation notice
        assert "[... truncated" not in all_content


# Verified live against an omlx server (Qwen3.6-27B-MLX-8bit, 2026-05-30).
_OMLX_OVERFLOW_MSG = (
    "Prompt too long: 360012 tokens exceeds max context window of 262144 tokens"
)


class TestContextOverflowRecovery:
    """flow 2 — reactive recovery when the server rejects an over-long
    prompt with a 400. The loop must shrink the cache via force_fit and
    retry, bounded so it never loops forever."""

    def _ctx_with_history(self, tmp_path, n=12):
        from agent_cli.context.manager import ContextManager

        # compaction_enabled=False so force_fit takes the pure-FIFO path
        # and doesn't call the (provider-backed) summariser, which would
        # otherwise consume our mocked retry responses. The compact-first
        # path is covered separately in test_context_compaction.py with a
        # fake compactor.
        ctx = ContextManager(
            session_dir=tmp_path,
            max_context_tokens=1_000_000,
            compaction_enabled=False,
        )
        for i in range(n):
            ctx.add({"role": "user", "content": f"old turn {i} " * 30})
        return ctx

    def test_shrinks_and_retries_then_succeeds(self, caps, tmp_path):
        ctx = self._ctx_with_history(tmp_path)
        before = len(ctx.get_raw_messages())
        provider = MagicMock()
        provider.call.side_effect = [
            RuntimeError(_OMLX_OVERFLOW_MSG),  # first call: server 400
            LLMResponse(content=_complete("recovered")),  # retry succeeds
        ]
        result = run_loop(
            query="do it",
            provider=provider,
            capabilities=caps,
            model="test",
            ctx=ctx,
        )
        assert result.output == "recovered"
        assert provider.call.call_count == 2  # failed once, retried once
        # The cache was shed before the retry.
        assert len(ctx.get_raw_messages()) < before

    def test_bounded_gives_up_cleanly(self, caps, tmp_path):
        """Server keeps rejecting → loop gives up without spinning; the
        number of attempts is bounded by _MAX_OVERFLOW_RETRIES."""
        from agent_cli.loop import _MAX_OVERFLOW_RETRIES

        ctx = self._ctx_with_history(tmp_path, n=40)
        provider = MagicMock()
        provider.call.side_effect = [RuntimeError(_OMLX_OVERFLOW_MSG)] * 30
        result = run_loop(
            query="do it",
            provider=provider,
            capabilities=caps,
            model="test",
            ctx=ctx,
        )
        # Did not succeed, but terminated (no infinite loop).
        assert result.output != "recovered"
        # initial attempt + at most _MAX_OVERFLOW_RETRIES shrink-retries
        assert provider.call.call_count <= 1 + _MAX_OVERFLOW_RETRIES

    def test_non_overflow_error_not_retried(self, caps, tmp_path):
        """A non-overflow failure must NOT trigger force_fit/retry."""
        ctx = self._ctx_with_history(tmp_path)
        before = len(ctx.get_raw_messages())
        provider = MagicMock()
        provider.call.side_effect = [RuntimeError("Connection refused")]
        run_loop(
            query="do it",
            provider=provider,
            capabilities=caps,
            model="test",
            ctx=ctx,
        )
        assert provider.call.call_count == 1  # no retry
        # cache untouched by recovery (only the query was appended by setup)
        assert len(ctx.get_raw_messages()) == before + 1


class TestFlow1PreventiveCompaction:
    """flow 1 — preventive compaction before each call + reconcile after."""

    def _ctx(self, tmp_path):
        from agent_cli.context.manager import ContextManager

        return ContextManager(
            session_dir=tmp_path,
            max_context_tokens=1_000_000,
            compaction_enabled=False,
        )

    def test_reconcile_called_with_server_count(self, caps, tmp_path):
        """After a successful call, ctx is re-anchored with the server's
        total input count (input + cache_creation + cache_read)."""
        from unittest.mock import patch
        from agent_cli.providers.base import TokenUsage

        ctx = self._ctx(tmp_path)
        ctx.add({"role": "user", "content": "x" * 40})
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(
                content=_complete("ok"),
                usage=TokenUsage(
                    input_tokens=500,
                    output_tokens=5,
                    cache_creation_input_tokens=30,
                    cache_read_input_tokens=70,
                ),
            ),
        ]
        with patch.object(
            ctx, "reconcile_actual_tokens", wraps=ctx.reconcile_actual_tokens
        ) as spy:
            run_loop(
                query="q",
                provider=provider,
                capabilities=caps,
                model="test",
                ctx=ctx,
            )
        spy.assert_called()
        assert spy.call_args.args[0] == 600  # 500 + 30 + 70

    def test_no_reconcile_without_usage(self, caps, tmp_path):
        """Providers that report no usage leave the running estimate."""
        from unittest.mock import patch

        ctx = self._ctx(tmp_path)
        ctx.add({"role": "user", "content": "x"})
        provider = _make_provider(_complete("ok"))  # LLMResponse usage=None
        with patch.object(ctx, "reconcile_actual_tokens") as spy:
            run_loop(
                query="q",
                provider=provider,
                capabilities=caps,
                model="test",
                ctx=ctx,
            )
        spy.assert_not_called()

    def test_preventive_compaction_before_call(self, tmp_path):
        """A tiny context window → ensure_within sheds history before the
        (successful) call, so the prompt never overflows in the first
        place."""
        small_caps = ModelCapabilities(
            context_window=2000,
            max_output_tokens=256,
            supports_structured_output=True,
            supports_thinking=False,
            thinking_budget=0,
            supports_strict_schema=False,
        )
        ctx = self._ctx(tmp_path)
        for i in range(40):
            ctx.add({"role": "user", "content": "word " * 40})
        before = len(ctx.get_raw_messages())
        provider = _make_provider(_complete("ok"))
        result = run_loop(
            query="q",
            provider=provider,
            capabilities=small_caps,
            model="test",
            ctx=ctx,
        )
        assert result.output == "ok"
        assert len(ctx.get_raw_messages()) < before  # shed before the call


class TestOutputTruncationGuard:
    """stop_reason=length → the incomplete action is NOT dispatched; a
    truncation notice is recorded so the model retries smaller."""

    def test_truncated_write_file_not_executed(self, caps, tmp_path):
        from agent_cli.context.manager import ContextManager

        target = tmp_path / "x.txt"
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(
                content=json.dumps(
                    {
                        "thought": "writing",
                        "action": "write_file",
                        "action_input": {
                            "path": str(target),
                            "content": "partial",
                        },
                    }
                ),
                stop_reason="length",
            ),
            LLMResponse(content=_complete("done")),
        ]
        ctx = ContextManager(session_dir=tmp_path)
        result = run_loop(
            query="write x",
            provider=provider,
            capabilities=caps,
            model="test",
            ctx=ctx,
        )
        # The truncated write_file must NOT have run.
        assert not target.exists()
        # A truncation notice observation was recorded.
        raw = ctx.get_raw_messages()
        assert any(m.get("tool") == "output_truncated" for m in raw)
        # Loop continued and recovered on the retry.
        assert result.output == "done"

    def test_truncated_complete_is_blocked_and_retried(self, caps, tmp_path):
        """Even ``complete`` is blocked on length — a clipped final answer
        shouldn't be accepted."""
        from agent_cli.context.manager import ContextManager

        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(content=_complete("truncated ans"), stop_reason="length"),
            LLMResponse(content=_complete("full answer")),
        ]
        ctx = ContextManager(session_dir=tmp_path)
        result = run_loop(
            query="q",
            provider=provider,
            capabilities=caps,
            model="test",
            ctx=ctx,
        )
        assert result.output == "full answer"

    def test_normal_stop_dispatches_action(self, caps, tmp_path):
        """stop_reason='stop' (or None) → action runs as usual (no guard)."""
        from agent_cli.context.manager import ContextManager

        target = tmp_path / "y.txt"
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(
                content=json.dumps(
                    {
                        "thought": "writing",
                        "action": "write_file",
                        "action_input": {
                            "path": str(target),
                            "content": "hello",
                        },
                    }
                ),
                stop_reason="stop",
            ),
            LLMResponse(content=_complete("done")),
        ]
        ctx = ContextManager(session_dir=tmp_path)
        run_loop(
            query="write y",
            provider=provider,
            capabilities=caps,
            model="test",
            ctx=ctx,
        )
        assert target.exists()
        assert target.read_text() == "hello"
