"""Tests for recovery primitives (agent_cli.recovery.primitives).

Primitives are pure functions used as composition blocks for retry/
intervention messages. The contract: take harness-level inputs only,
return a text fragment, never reference provider/model/channel names.

See docs/robust-harness/DESIGN.md §2.2.
"""

import pytest

from agent_cli.recovery.primitives import (
    CALL_VALUE_MAX_CHARS,
    echo_prior_output,
    probe_progress,
    restate_task,
    summarize_call,
    summarize_value,
)


class TestSummarizeValue:
    """값 요약 규칙 (v10.37.0): 짧은 스칼라는 그대로, 긴 문자열은 길이만,
    배열·객체는 개수만 — 한 줄이 한 줄로 남게."""

    @pytest.mark.parametrize(
        ("value", "shown"),
        [
            ("replace", '"replace"'),
            ("12#AB", '"12#AB"'),
            ('say "hi"', '"say \\"hi\\""'),
            ("한글 경로.txt", '"한글 경로.txt"'),
            ("x" * CALL_VALUE_MAX_CHARS, '"' + "x" * CALL_VALUE_MAX_CHARS + '"'),
            ("x" * (CALL_VALUE_MAX_CHARS + 1), "<41 chars>"),
            ("y" * 2341, "<2,341 chars>"),
            ([], "[0 items]"),
            (["a"], "[1 item]"),
            ([1, 2, 3], "[3 items]"),
            ({}, "{0 keys}"),
            ({"a": 1}, "{1 key}"),
            ({"a": 1, "b": 2}, "{2 keys}"),
            (True, "true"),
            (False, "false"),
            (None, "null"),
            (12, "12"),
            (1.5, "1.5"),
        ],
    )
    def test_rules(self, value, shown):
        assert summarize_value(value) == shown

    def test_long_string_is_never_quoted(self):
        body = "secret " * 400
        out = summarize_value(body)
        assert "secret" not in out and out.startswith("<") and out.endswith(" chars>")


class TestSummarizeCall:
    """거부된 호출의 한 줄 (v10.37.0): `You sent: tool(k=v, …) — missing: a;
    unknown: b. <hint>` — native_fc 에서 모델이 자기 호출을 보는 유일한 자리."""

    def test_missing_field_shape(self):
        out = summarize_call(
            "edit_file",
            {"op": "replace", "pos": "12#AB", "lines": ["x"]},
            missing=["path"],
        )
        assert out == (
            'You sent: edit_file(op="replace", pos="12#AB", lines=[1 item]) '
            "— missing: path. Re-emit the whole call with every required field."
        )

    def test_missing_and_unknown_are_both_named(self):
        out = summarize_call(
            "shell", {"cmd": "ls"}, missing=["command"], unknown=["cmd"]
        )
        assert 'shell(cmd="ls") — missing: command; unknown: cmd.' in out
        assert out.endswith("Re-emit the whole call with every required field.")

    def test_several_missing_fields_are_comma_joined(self):
        out = summarize_call("write_file", {}, missing=["path", "content"])
        assert "You sent: write_file() — missing: path, content." in out

    def test_no_gaps_asks_to_fix_the_named_argument(self):
        """타입·enum·의미 검증 실패: 빠진 건 없고 앞 문장이 어느 인자인지 말했다."""
        out = summarize_call("memory", {"mode": "nope", "summary": "s"})
        assert out == (
            'You sent: memory(mode="nope", summary="s"). '
            "Fix the argument named above and re-emit the whole call."
        )

    def test_hint_override(self):
        out = summarize_call("bogus", {"path": "a"}, hint="Use a listed name.")
        assert out == 'You sent: bogus(path="a"). Use a listed name.'

    def test_argument_order_is_as_sent(self):
        out = summarize_call("t", {"z": 1, "a": 2})
        assert "t(z=1, a=2)" in out

    def test_long_values_do_not_bloat_the_line(self):
        out = summarize_call("write_file", {"content": "x" * 5000}, missing=["path"])
        assert "x" * 50 not in out
        assert "write_file(content=<5,000 chars>) — missing: path." in out
        assert len(out) < 160

    @pytest.mark.parametrize(
        ("tool_input", "args"),
        [
            (None, ""),
            ("", '""'),
            ("not json at all", '"not json at all"'),
            ("z" * 100, "<100 chars>"),
            ([{"a": 1}], "[1 item]"),
        ],
    )
    def test_non_object_input_is_summarized_as_one_value(self, tool_input, args):
        assert summarize_call("t", tool_input).startswith(f"You sent: t({args})")

    def test_never_names_a_provider_or_model(self):
        out = summarize_call("edit_file", {"op": "x"}, missing=["path"])
        for word in ("openai", "anthropic", "qwen", "omlx", "native"):
            assert word not in out.lower()


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

    def test_content_up_to_the_cap_is_quoted_in_full(self):
        from agent_cli.recovery.primitives import ECHO_MAX_CHARS

        text = "HEAD" + "x" * (ECHO_MAX_CHARS - 8) + "TAIL"
        out = echo_prior_output(text)
        assert text in out and "omitted" not in out

    def test_long_content_keeps_both_ends(self):
        """v9.23.2: bounded — but **head and tail** both reach the model.

        The full echo existed because a head-only cut once hid the real
        defect (JSON whose closing brace is the missing piece; a prose drift
        that shows only at the tail). Keeping both ends answers that. What
        the full echo cost: a 32K-character runaway was stored AND quoted
        whole, and the model imitated the broken fragment for 22 turns
        (Harbor extract-elf)."""
        from agent_cli.recovery.primitives import ECHO_HEAD_CHARS, ECHO_TAIL_CHARS

        long = "HEAD" + "x" * 30000 + "TAIL"
        out = echo_prior_output(long)
        assert out.startswith("Your prior output:\n---\nHEAD")
        assert out.rstrip().endswith("TAIL\n---") or "TAIL\n---" in out
        omitted = len(long) - ECHO_HEAD_CHARS - ECHO_TAIL_CHARS
        assert f"[… {omitted} characters omitted …]" in out
        assert len(out) < ECHO_HEAD_CHARS + ECHO_TAIL_CHARS + 200

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
# were primitives. They moved onto the dialect plugin in Step 7;
# their replacements live in ``test_dialects_react.py``
# (``TestRecoveryReminders``).


def _probe(action="x", args_repr="{}", repeat_count=2, prior_result="prior-out"):
    # v10.32.0: ``prior_result`` 는 필수 — 1단계는 실행된 호출의 반복에만 나간다
    return probe_progress(
        action=action,
        args_repr=args_repr,
        repeat_count=repeat_count,
        prior_result=prior_result,
    )


class TestProbeProgress:
    def test_includes_action_args_count(self):
        out = _probe(action="read_file", args_repr='{"path": "x.py"}')
        assert "read_file" in out
        assert '{"path": "x.py"}' in out
        assert "2 times in a row" in out

    def test_starts_with_loop_observed_phrase(self):
        # Shared phrase across B1 primitives — also a SYSTEM_USER_PREFIXES match
        assert _probe().startswith("You have called")

    def test_does_not_repeat_task_anchor(self):
        # probe_progress is the LIGHT nudge — must NOT include task anchor
        # (that is restate_task's job); the only fenced block is the result
        out = _probe()
        assert "You were asked to:" not in out
        assert out.count("---") == 2

    def test_does_not_ask_diagnostic_questions(self):
        # probe_progress focuses on "look at what you have", not on
        # "why is this needed / what is missing" — those are restate_task's
        out = _probe()
        # No metacognitive prompts about task↔action causality
        assert "Why does the task" not in out
        assert "NOT getting" not in out

    def test_offers_complete_only_conditionally_and_different_action(self):
        out = _probe()
        assert "complete" in out
        assert "different action" in out

    def test_does_not_reference_provider_or_channel(self):
        out = _probe()
        forbidden = ["ollama", "anthropic", "thinking", "reasoning"]
        for w in forbidden:
            assert w not in out.lower(), f"primitive leaked '{w}'"

    def test_quotes_prior_result_in_a_fence(self):
        # v10.32.0: 직전 결과를 인용한다 — 모델이 컨텍스트를 뒤지지 않게
        out = _probe(
            action="shell", args_repr='{"command": "ls"}', prior_result="a.py\nb.py"
        )
        assert "The previous call already returned:\n---\na.py\nb.py\n---" in out
        assert "Re-read" not in out

    def test_prior_result_is_bounded(self):
        from agent_cli.recovery.primitives import ECHO_MAX_CHARS

        out = _probe(prior_result="z" * 5000)
        assert "characters omitted" in out
        assert len(out) < ECHO_MAX_CHARS + 400

    def test_complete_is_conditional_on_the_task(self):
        # 종전 "summarize what you have learned and call complete" 는 complete 을
        # 첫 번째 출구로 제시했다 — 이제 과제가 끝났을 때만, 반복을 끝내려고는 아님
        out = _probe()
        assert "Call complete only if the task itself is finished" in out
        assert "not to end the repetition" in out
        assert "summarize" not in out


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
