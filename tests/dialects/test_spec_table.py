"""표 기반 방언 테스트 (Phase 5 S4, PHASE5.md §9).

네 스펙(json_fc·xml_fc·hermes_json·glm_argkey)에 **같은 논리 입력**을 각자의
캐노니컬 렌더로 돌린다 — 모양만 다르고 의미는 같아야 한다(PHASE2 "같은 내용
다른 모양" 원칙). 그 위에 방언마다 고유한 드리프트·변종을 얹는다.
"""

from __future__ import annotations

import pytest

from agent_cli.dialects import get, list_names, try_foreign_parse

ALL = ["json_fc", "xml_fc", "hermes_json", "glm_argkey"]
TAGGED = ["xml_fc", "hermes_json", "glm_argkey"]  # <tool_call> 래퍼가 있는 셋


def _render(name, thought, ops):
    return get(name).render_assistant_from_history(
        {"thought": thought, "ops": [{"action": a, "action_input": i} for a, i in ops]}
    )["content"]


def _ops(turn):
    return [(o.action, o.action_input, o.truncated) for o in turn.ops]


class TestRegistry:
    def test_five_builtin_dialects(self):
        assert list_names() == [
            "glm_argkey",
            "hermes_json",
            "json_fc",
            "native_fc",
            "xml_fc",
        ]

    @pytest.mark.parametrize("name", ALL)
    def test_surface_is_complete(self, name):
        wf = get(name)
        assert wf.name == name and wf.multi_op
        assert "## Response Format" in wf.format_rules()
        for m in (
            "constraint_reminder_call",
            "constraint_reminder_action_required",
            "failure_framing_parse_fail",
            "failure_framing_no_action",
            "no_action_detail",
            "static_retry_hint_no_json",
            "static_retry_hint_no_action",
        ):
            assert getattr(wf, m)().strip()
        assert all(isinstance(p, str) and p for p in wf.system_user_prefixes())
        assert wf.grammar([("read_file", {"path": ({"type": "string"}, True)}, False)])


@pytest.mark.parametrize("name", ALL)
class TestCanonicalRoundTrip:
    def test_single_call(self, name):
        text = _render(name, "look first", [("read_file", {"path": "src/a.c"})])
        t = get(name).parse_turn(text)
        assert t.thought == "look first"
        assert _ops(t) == [("read_file", {"path": "src/a.c"}, False)]
        assert t.parse_stage == 1

    def test_multi_op_one_thought(self, name):
        ops = [
            ("read_file", {"path": "a.c"}),
            ("shell", {"command": "ls -la"}),
            ("write_file", {"path": "b.txt", "content": "line one\nline two"}),
        ]
        t = get(name).parse_turn(_render(name, "batch", ops))
        assert [(a, i) for a, i, _ in _ops(t)] == ops
        assert t.parse_stage == 1

    def test_terminal_complete(self, name):
        wf = get(name)
        rec = wf.serialize_terminal_for_history("done", "the answer", ["r1"])
        t = wf.parse_turn(wf.render_assistant_from_history(rec)["content"])
        assert t.ops[0].action == "complete"
        assert t.ops[0].action_input["result"] == "the answer"

    def test_non_string_values_round_trip(self, name):
        ops = [("shell", {"command": "ls", "timeout": 30}), ("ask", {"question": "x?"})]
        t = get(name).parse_turn(_render(name, None, ops))
        got = {a: i for a, i, _ in _ops(t)}
        assert got["shell"]["timeout"] == 30  # raw 방언은 스키마로 복원, JSON 은 그대로
        assert got["ask"] == {"question": "x?"}

    def test_history_record_shape_is_shared(self, name):
        wf = get(name)
        rec = wf.serialize_assistant_for_history(
            _render(name, "t", [("read_file", {"path": "a"})])
        )
        assert rec == {
            "role": "assistant",
            "thought": "t",
            "ops": [{"action": "read_file", "action_input": {"path": "a"}}],
        }

    def test_prose_only_and_blank(self, name):
        wf = get(name)
        t = wf.parse_turn("just thinking out loud.")
        assert t.ops == [] and t.parse_stage == 1 and t.thought
        assert wf.parse_turn("   \n ").parse_stage == 0

    def test_thinking_block_is_isolated(self, name):
        text = "<think>scratch</think>\n" + _render(
            name, "t", [("read_file", {"path": "a"})]
        )
        t = get(name).parse_turn(text)
        assert t.thinking == "scratch" and t.thought == "t"
        assert _ops(t)[0][0] == "read_file"


@pytest.mark.parametrize("name", TAGGED)
class TestWrapperDrift:
    def test_missing_close_is_drift(self, name):
        text = _render(name, "t", [("read_file", {"path": "a"})])
        cut = text[: text.rindex("</tool_call>")]
        t = get(name).parse_turn(cut)
        assert _ops(t)[0][0] == "read_file"
        assert t.parse_stage == 2

    def test_fenced_example_does_not_shadow_real_call(self, name):
        real = _render(name, None, [("read_file", {"path": "real.c"})])
        example = _render(name, None, [("read_file", {"path": "example.c"})])
        text = (
            f"The format looks like this:\n```\n{example}\n```\nNow for real:\n\n{real}"
        )
        t = get(name).parse_turn(text)
        assert [i["path"] for _, i, _ in _ops(t)] == ["real.c"]

    def test_inline_code_mention_is_not_a_call(self, name):
        wf = get(name)
        real = _render(name, None, [("shell", {"command": "ls"})])
        text = "Use `<tool_call>` blocks like `<tool_call>{}</tool_call>`.\n\n" + real
        t = wf.parse_turn(text)
        assert [a for a, _, _ in _ops(t)] == ["shell"]

    def test_empty_wrapper_runaway_is_degenerate(self, name):
        wf = get(name)
        assert wf.is_degenerate("<tool_call></tool_call><tool_call></tool_call>")
        assert not wf.is_degenerate(_render(name, "t", [("shell", {"command": "ls"})]))

    def test_sentinel_lines_stripped_from_thought(self, name):
        wf = get(name)
        assert wf.sanitize_thought("ok\n<tool_call>\nrest") == "ok\n\nrest".strip()


class TestHermesVariants:
    WF = "hermes_json"

    def test_parameters_alias_is_drift(self):
        t = get(self.WF).parse_turn(
            '<tool_call>\n{"name": "read_file", "parameters": {"path": "a"}}\n</tool_call>'
        )
        assert _ops(t) == [("read_file", {"path": "a"}, False)] and t.parse_stage == 2

    def test_string_serialized_arguments(self):
        t = get(self.WF).parse_turn(
            '<tool_call>{"name": "shell", "arguments": "{\\"command\\": \\"ls\\"}"}</tool_call>'
        )
        assert _ops(t) == [("shell", {"command": "ls"}, False)] and t.parse_stage == 2

    def test_bare_object_without_wrapper(self):
        t = get(self.WF).parse_turn(
            'thinking.\n\n{"name": "shell", "arguments": {"command": "ls"}}'
        )
        assert _ops(t) == [("shell", {"command": "ls"}, False)]
        assert t.parse_stage == 2 and t.thought == "thinking."

    def test_flat_object_keeps_input_for_recovery(self):
        t = get(self.WF).parse_turn('<tool_call>{"path": "a.py"}</tool_call>')
        assert _ops(t) == [(None, {"path": "a.py"}, False)]

    def test_broken_json_in_wrapper_is_repaired(self):
        t = get(self.WF).parse_turn(
            '<tool_call>\n{"name": "shell", "arguments": {"command": "ls"}\n</tool_call>'
        )
        assert _ops(t)[0][:2] == ("shell", {"command": "ls"}) and t.parse_stage == 2

    def test_prose_array_is_not_a_call(self):
        t = get(self.WF).parse_turn('the list [1, 2, 3] and {"a": 1} are data.')
        assert t.ops == []

    def test_diagnose_points_at_json(self):
        assert get(self.WF).diagnose_syntax_error(
            '<tool_call>{"name": "x", </tool_call>'
        )


class TestGlmVariants:
    WF = "glm_argkey"

    def test_one_line_form_glm47(self):
        t = get(self.WF).parse_turn(
            "<tool_call>read_file<arg_key>path</arg_key><arg_value>a.c</arg_value></tool_call>"
        )
        assert _ops(t) == [("read_file", {"path": "a.c"}, False)] and t.parse_stage == 1

    def test_multi_line_form_glm45(self):
        t = get(self.WF).parse_turn(
            "<tool_call>shell\n<arg_key>command</arg_key>\n<arg_value>ls -la</arg_value>\n"
            "<arg_key>timeout</arg_key>\n<arg_value>30</arg_value>\n</tool_call>"
        )
        assert _ops(t) == [("shell", {"command": "ls -la", "timeout": 30}, False)]
        assert t.parse_stage == 1

    def test_unclosed_value_is_truncated(self):
        t = get(self.WF).parse_turn(
            "<tool_call>write_file\n<arg_key>path</arg_key><arg_value>a</arg_value>\n"
            "<arg_key>content</arg_key><arg_value>partial"
        )
        assert _ops(t) == [("write_file", {"path": "a", "content": "partial"}, True)]
        assert t.parse_stage == 2

    def test_value_may_contain_angle_brackets(self):
        t = get(self.WF).parse_turn(
            "<tool_call>write_file\n<arg_key>path</arg_key><arg_value>x.html</arg_value>\n"
            "<arg_key>content</arg_key><arg_value>\n<div>hi</div>\n</arg_value>\n</tool_call>"
        )
        assert _ops(t)[0][1]["content"] == "<div>hi</div>"

    def test_no_tag_name_variant_rescue(self):
        # xml_fc 의 <X>/<k> 붕괴 구제는 GLM 스펙에서 꺼져 있다 — 산문으로 남는다
        t = get(self.WF).parse_turn("<read_file>\n<path>a</path>\n</read_file>")
        assert t.ops == []


class TestForeignRescueAcrossDialects:
    def test_hermes_leak_in_xml_stream(self):
        leak = _render("hermes_json", "reading.", [("read_file", {"path": "a"})])
        rescued = try_foreign_parse(get("xml_fc"), leak)
        assert rescued is not None
        turn, src = rescued
        assert src == "hermes_json" and _ops(turn)[0][:2] == (
            "read_file",
            {"path": "a"},
        )

    def test_glm_leak_in_json_stream(self):
        leak = _render("glm_argkey", "reading.", [("shell", {"command": "ls"})])
        turn, src = try_foreign_parse(get("json_fc"), leak)
        assert src == "glm_argkey" and _ops(turn)[0][:2] == ("shell", {"command": "ls"})

    def test_xml_leak_in_hermes_stream(self):
        leak = _render("xml_fc", "reading.", [("shell", {"command": "ls"})])
        turn, src = try_foreign_parse(get("hermes_json"), leak)
        assert src == "xml_fc" and _ops(turn)[0][:2] == ("shell", {"command": "ls"})

    def test_rescued_prior_rerenders_in_bound_shape(self):
        leak = _render("hermes_json", "t", [("read_file", {"path": "a"})])
        turn, _ = try_foreign_parse(get("glm_argkey"), leak)
        rec = {
            "thought": turn.thought,
            "ops": [
                {"action": o.action, "action_input": o.action_input} for o in turn.ops
            ],
        }
        out = get("glm_argkey").render_assistant_from_history(rec)["content"]
        assert out.startswith("t\n\n<tool_call>read_file") and '"name"' not in out


@pytest.mark.parametrize("name", ALL)
class TestPromptExamples:
    def test_render_full_example_parses_back(self, name):
        wf = get(name)
        inp = wf.render_action_input({"read_file_path": "src/a.py"})
        text = wf.render_full_example(thought="t", action="read_file", action_input=inp)
        t = wf.parse_turn(text)
        assert (
            _ops(t) == [("read_file", {"path": "src/a.py"}, False)]
            and t.parse_stage == 1
        )

    def test_format_rules_examples_parse_as_canonical(self, name):
        """산문 조각 속 예시가 자기 파서로 캐노니컬(stage 1)로 읽혀야 한다 — 모델이
        보는 예시가 파서와 어긋나면 가르치는 모양이 틀린 것이다."""
        wf = get(name)
        rules = wf.format_rules()
        # 마지막 예시(완료 호출)만 떼어낸다: "Finishing the task:" 이후
        tail = rules.split("Finishing the task:", 1)[1]
        t = wf.parse_turn(tail)
        assert t.ops and t.ops[-1].action == "complete" and t.parse_stage == 1


@pytest.mark.parametrize("name", ["xml_fc", "glm_argkey"])
def test_format_tokens_inside_values_are_text(name):
    """v10.1.3: 태그 방언 공통 — 닫힌 파라미터 값 안의 포맷 토큰은 텍스트.
    유령 op 없이, 값 그대로, 캐노니컬(stage 1)."""
    wf = get(name)
    value = (
        f"example: {wf.spec.call_open}{wf.spec.name_wrap[0] if wf.spec.name_wrap else ''}"
        "read_file"
        + (wf.spec.name_wrap[1] if wf.spec.name_wrap else "\n")
        + wf.spec.param_open_prefix
        + "path"
        + wf.spec.param_open_suffix
        + "a"
    )
    t = wf.parse_turn(
        _render(name, None, [("write_file", {"path": "x", "content": value})])
    )
    assert [o.action for o in t.ops] == ["write_file"], _ops(t)
    assert t.ops[0].action_input == {"path": "x", "content": value}
    assert t.parse_stage == 1
