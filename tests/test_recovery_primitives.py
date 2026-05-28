"""Tests for recovery primitives (agent_cli.recovery.primitives).

Primitives are pure functions used as composition blocks for retry/
intervention messages. The contract: take harness-level inputs only,
return a text fragment, never reference provider/model/channel names.

See docs/robust-harness/DESIGN.md §2.2.
"""

from agent_cli.recovery.primitives import (
    echo_prior_output,
    probe_progress,
    restate_task,
)


class TestEchoPriorOutput:
    def test_empty_returns_empty(self):
        assert echo_prior_output("") == ""

    def test_whitespace_only_returns_empty(self):
        assert echo_prior_output("   \n\t  ") == ""

    def test_short_content_quoted_verbatim(self):
        out = echo_prior_output("hello world")
        assert "hello world" in out
        assert "Your prior output:" in out
        assert "---" in out

    def test_block_structure_has_delimiters(self):
        out = echo_prior_output("payload")
        # Header, opening fence, payload, closing fence, trailing newline
        lines = out.split("\n")
        assert lines[0] == "Your prior output:"
        assert lines[1] == "---"
        assert lines[2] == "payload"
        assert lines[3] == "---"

    def test_long_content_quoted_in_full(self):
        # Truncation was removed deliberately — format-failure signals
        # can sit at either end of the malformed output, so giving the
        # model only the head sometimes hid the real defect (e.g.
        # JSON whose closing brace is the missing piece). Full echo
        # costs more tokens but yields more accurate next-turn fixes.
        long = "HEAD" + "x" * 2000 + "TAIL"
        out = echo_prior_output(long)
        assert "HEAD" in out
        assert "TAIL" in out  # tail must reach the model too
        assert "..." not in out
        # All padding survives — no head-cap artifact.
        assert out.count("x") == 2000

    def test_strips_leading_trailing_whitespace(self):
        out = echo_prior_output("  payload  \n\n")
        # Quoted content should be trimmed
        assert "payload" in out
        # Should not have raw whitespace lines around it
        assert "\n\n  payload" not in out

    def test_does_not_reference_provider_or_channel_names(self):
        # Contract invariant: primitive output must not leak runtime concepts.
        out = echo_prior_output("anything")
        forbidden = ["ollama", "anthropic", "openai", "vllm", "thinking", "reasoning"]
        lowered = out.lower()
        for word in forbidden:
            assert word not in lowered, f"primitive leaked '{word}'"


# ``TestConstrainFormatJson`` and ``TestConstrainActionRequired`` lived
# here as long as ``constrain_format_json`` / ``constrain_action_required``
# were primitives. They moved onto the wire-format plugin in Step 7;
# their replacements live in ``test_wire_formats_react.py``
# (``TestRecoveryReminders``).


class TestProbeProgress:
    def test_includes_action_args_count(self):
        out = probe_progress(
            action="read_file", args_repr='{"path": "x.py"}', repeat_count=2
        )
        assert "read_file" in out
        assert '{"path": "x.py"}' in out
        assert "2 times in a row" in out

    def test_starts_with_loop_observed_phrase(self):
        # Shared phrase across B1 primitives — also a SYSTEM_USER_PREFIXES match
        out = probe_progress(action="x", args_repr="{}", repeat_count=2)
        assert out.startswith("You have called")

    def test_does_not_repeat_task_anchor(self):
        # probe_progress is the LIGHT nudge — must NOT include task anchor
        # (that is restate_task's job)
        out = probe_progress(action="x", args_repr="{}", repeat_count=2)
        assert "You were asked to:" not in out
        assert "---" not in out  # no fenced task block

    def test_does_not_ask_diagnostic_questions(self):
        # probe_progress focuses on "look at what you have", not on
        # "why is this needed / what is missing" — those are restate_task's
        out = probe_progress(action="x", args_repr="{}", repeat_count=2)
        # No metacognitive prompts about task↔action causality
        assert "Why does the task" not in out
        assert "NOT getting" not in out

    def test_offers_two_paths_complete_or_different(self):
        out = probe_progress(action="x", args_repr="{}", repeat_count=2)
        assert "complete" in out
        assert "different action" in out

    def test_does_not_reference_provider_or_channel(self):
        out = probe_progress(action="x", args_repr="{}", repeat_count=2)
        forbidden = ["ollama", "anthropic", "thinking", "reasoning"]
        for w in forbidden:
            assert w not in out.lower(), f"primitive leaked '{w}'"


class TestRestateTask:
    def test_includes_task_anchor(self):
        out = restate_task(
            task="Build a sorting algorithm",
            action="read_file",
            args_repr='{"path": "x.py"}',
            repeat_count=3,
        )
        assert "You were asked to:" in out
        assert "Build a sorting algorithm" in out
        # Anchor uses --- fences
        assert "---" in out

    def test_includes_loop_pattern(self):
        out = restate_task(task="t", action="read_file", args_repr="{}", repeat_count=3)
        assert "read_file" in out
        assert "3 times in a row" in out

    def test_acknowledges_previous_nudge_failed(self):
        # restate_task is level 2 — must signal that level 1 didn't work,
        # otherwise the model gets the same nudge twice without escalation cue
        out = restate_task(task="t", action="x", args_repr="{}", repeat_count=3)
        assert "previous nudge did not work" in out.lower()

    def test_asks_causal_question(self):
        # "Why does the task require this call?" — task↔action grounding
        out = restate_task(task="t", action="x", args_repr="{}", repeat_count=3)
        assert "Why does" in out

    def test_asks_information_gap_question(self):
        # "What information are you NOT getting?" — gap diagnosis
        out = restate_task(task="t", action="x", args_repr="{}", repeat_count=3)
        assert "NOT getting" in out

    def test_forbids_another_retry_explicitly(self):
        # Explicit "do not retry" cue — necessary because we're at the
        # last recovery rung before hard-fail
        out = restate_task(task="t", action="x", args_repr="{}", repeat_count=3)
        assert "not from another retry" in out

    def test_long_task_not_truncated(self):
        # Task is the anchor — truncation defeats the purpose
        long_task = "A" * 5000
        out = restate_task(task=long_task, action="x", args_repr="{}", repeat_count=3)
        assert long_task in out

    def test_starts_with_task_anchor_phrase(self):
        # SYSTEM_USER_PREFIXES match
        out = restate_task(task="t", action="x", args_repr="{}", repeat_count=3)
        assert out.startswith("You were asked to:")

    def test_does_not_reference_provider_or_channel(self):
        out = restate_task(task="t", action="x", args_repr="{}", repeat_count=3)
        forbidden = ["ollama", "anthropic", "thinking", "reasoning"]
        for w in forbidden:
            assert w not in out.lower()
