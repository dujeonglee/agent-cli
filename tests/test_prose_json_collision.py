"""산문 속 괄호가 op JSON 을 강탈하던 실패 (json_fc).

실제 제보 2건에서 출발한다. 코드 리뷰 턴의 산문이 코드를 인용하면 —
``board[sr + dr * k][sc + dc * k]`` 나 ``{ passive: false }`` — 파서가 **본문
첫 ``[``/``{``** 를 op 시작으로 못박고, 그 덩어리가 파싱에 실패하면 **뒤를 보지
않고 포기**했다. 끝에 완전한 op 배열이 있는데도 stage 0(턴 낭비 + 사용자에게
"must end with a valid JSON array" 에러)이 됐다.

더 나쁜 변종: 산문 조각이 **유효한** JSON 이면(``[1]`` 각주, ``- [ ]``
체크박스, ``{"a": 1}`` 예시) 그걸 op 으로 채택해 — action 이 없으니 진짜
``complete`` op 이 유실되고, thought 도 그 괄호에서 잘렸다. 잘린 산문 조각이
``prose_completion`` 을 통해 최종답변으로 수용될 수 있었다.

수리 방향: op 서명 계층(PHASE4 §3.1 의 ``any("action")`` 가드를 **span 선택
시점**으로 끌어올림)으로 후보를 걸러 최선 계층의 첫 span 을 앵커로 쓴다.
★방향(뒤에서부터)이 아니라 **계층**이 문제를 푼다 — 뒤에서 읽는 방식은
"배열 뒤 산문"·"배열 재개 병합"·"산문 사이 배열 보수적 처리" 세 계약을
동시에 깨뜨린다(아래 TestPositionSemanticsPreserved 가 고정).
"""

from __future__ import annotations

import pytest

from agent_cli.wire_formats import get

WF = get("json_fc")
OPS = '[{"action": "read_file", "path": "a.py"}]'


def _paths(turn):
    return [(o.action, (o.action_input or {}).get("path")) for o in turn.ops]


class TestProseBraceDoesNotStealTheOps:
    """제보된 산문 패턴 + 같은 계열 전부: op 이 온전히 회수되고 thought 도
    잘리지 않아야 한다."""

    @pytest.mark.parametrize(
        "label,prose",
        [
            (
                "array index (제보 2)",
                "`board[sr + dr * k][sc + dc * k]` 를 호출할 때 문제.",
            ),
            ("unquoted brace (제보 1)", "`{ passive: false }` 로 스크롤을 막습니다."),
            ("markdown link", "자세한 내용은 [문서](http://example.com/a) 참고."),
            ("footnote ref", "알려진 이슈입니다 [1]."),
            ("task list", "- [ ] 첫 항목\n- [x] 둘째 항목"),
            ("slice", "`arr[0:3]` 를 쓰면 됩니다."),
            ("valid json in prose", 'It sends `{"a": 1}` to the server.'),
            (
                "fenced code",
                "```js\nel.addEventListener('t', h, { passive: false });\n```",
            ),
            ("unbalanced bracket", "배열 `[0` 이 열린 채로 남습니다."),
        ],
    )
    def test_ops_survive_prose_brackets(self, label, prose):
        turn = WF.parse_turn(prose + "\n" + OPS)
        assert turn.parse_stage == 1, label
        assert _paths(turn) == [("read_file", "a.py")], label

    def test_thought_is_not_truncated_at_the_brace(self):
        # 옛 동작: thought 가 첫 괄호에서 잘려 "It sends `" 만 남았다.
        turn = WF.parse_turn('It sends `{"a": 1}` to the server. 리뷰 완료.\n' + OPS)
        assert turn.thought is not None
        assert "리뷰 완료" in turn.thought
        assert '{"a": 1}' in turn.thought

    def test_prose_json_is_not_promoted_to_an_op(self):
        # action 없는 산문 dict 가 op 자리를 차지하면 진짜 complete 이 유실된다.
        turn = WF.parse_turn(
            'The payload is `{"a": 1}`.\n[{"action": "complete", "result": "done"}]'
        )
        assert [o.action for o in turn.ops] == ["complete"]
        assert turn.ops[0].action_input["result"] == "done"


class TestPositionSemanticsPreserved:
    """계층 필터가 있으면 위치 규칙은 **기존 그대로** 유지된다 — 뒤에서부터
    읽는 대안이 깨뜨렸을 세 계약."""

    def test_trailing_prose_after_ops_is_ignored(self):
        turn = WF.parse_turn(OPS + "\n\n위 결과를 `arr[0]` 과 비교하겠습니다.")
        assert _paths(turn) == [("read_file", "a.py")]

    def test_prose_on_both_sides(self):
        turn = WF.parse_turn("`m[0]` 부터 본다.\n" + OPS + "\n끝으로 `n[1]` 도.")
        assert _paths(turn) == [("read_file", "a.py")]

    def test_reopened_arrays_still_merge(self):
        # 모델이 op 마다 배열을 다시 연 실측 shape → 병합되어 두 op 모두.
        turn = WF.parse_turn(
            "`x[0]` 확인 후 두 파일:\n"
            '[{"action": "read_file", "path": "a"}]\n'
            '[{"action": "read_file", "path": "b"}]'
        )
        assert _paths(turn) == [("read_file", "a"), ("read_file", "b")]

    def test_prose_between_arrays_stays_conservative(self):
        # 산문이 끼면 병합하지 않고 첫 배열만 (기존 방어 유지).
        turn = WF.parse_turn(
            "`y[1]` 먼저.\n"
            '[{"action": "write_file", "path": "a.c"}]\n'
            "이제 다음 파일:\n"
            '[{"action": "write_file", "path": "b.c"}]'
        )
        assert _paths(turn) == [("write_file", "a.c")]


class TestRepairPathStillReachable:
    """자격 후보가 없는 **진짜 파손**은 수리 기계로 가야 한다 — 앵커가 산문
    괄호가 아니라 op 시작을 짚는지가 핵심."""

    def test_unclosed_array_after_prose_brackets_repairs(self):
        turn = WF.parse_turn(
            "`board[i][j]` 를 고칩니다.\n"
            '[{"action": "read_file", "path": "a"},{"action": "read_file", "path": "b"}'
        )
        assert turn.parse_stage == 2  # close_unbalanced 복구
        assert _paths(turn) == [("read_file", "a"), ("read_file", "b")]

    def test_unclosed_array_first_element_is_not_a_bare_op(self):
        # 미닫힘 배열의 첫 원소를 완결 1-op 으로 오인하면 뒤 op 이 유실된다.
        turn = WF.parse_turn(
            '[{"action": "read_file", "path": "a"},{"action": "read_file", "path": "b"}'
        )
        assert len(turn.ops) == 2

    def test_broken_json_with_action_marker_is_stage0_not_swallowed(self):
        turn = WF.parse_turn('`z[0]` 확인.\n[{"action": "read_file", "path": ')
        assert turn.parse_stage == 0  # NO_JSON 진단 대상 (산문으로 삼키지 않음)


class TestNonOpJsonIsNotAnOp:
    """계층 0 = op 이 아니다. op 이 아예 없는 턴은 산문-only 로 남아야 한다
    (v8.4.0 부터 산문-only 는 NO_ACTION 넛지 — 명시적 complete 만 종결)."""

    @pytest.mark.parametrize("payload", ["[1, 2, 3]", "[]", '["a", "b"]', "42"])
    def test_non_op_json_alone_is_prose_only(self, payload):
        turn = WF.parse_turn("결과 요약입니다: " + payload)
        assert turn.ops == []
        assert turn.thought and payload in turn.thought

    def test_actionless_dict_op_at_the_end_is_still_preserved(self):
        # action 을 흘린 op 는 infer_action 재료로 보존 (action_required=False).
        turn = WF.parse_turn('읽습니다.\n[{"path": "a.py"}]')
        assert len(turn.ops) == 1
        assert turn.ops[0].action is None
        assert turn.ops[0].action_input == {"path": "a.py"}


class TestActionlessOpBehindProseBrace:
    """살아남은 뮤테이션 2개(계층 2 · JSON-value prefilter)가 실제로 필요한
    코너를 고정한다.

    페이로드가 **action 을 흘린 bare dict** 이면 fallback 앵커(``[{`` /
    ``{"action"`` 검색)가 짚을 수 없다. 그래서 (1) 산문의 비-JSON 중괄호가
    prefilter 에 걸려 열거를 멈추지 않아야 하고(멈추면 후보 0개 → 첫 괄호
    = 산문으로 폴백), (2) action 없는 dict 가 계층 2 로 후보 자격을 유지해야
    한다. 둘 중 하나만 빠져도 이 턴은 stage 0 으로 무너진다.
    """

    RAW = '설정에서 `{ passive: false` 가 누락됐습니다.\n{"path": "a.py"}'

    def test_actionless_payload_is_found_past_an_unbalanced_prose_brace(self):
        turn = WF.parse_turn(self.RAW)
        assert turn.parse_stage == 1
        assert len(turn.ops) == 1
        assert turn.ops[0].action is None
        assert turn.ops[0].action_input == {"path": "a.py"}

    def test_prose_brace_stays_in_the_thought(self):
        turn = WF.parse_turn(self.RAW)
        assert turn.thought and "passive: false" in turn.thought

    def test_same_shape_with_an_array_payload(self):
        turn = WF.parse_turn(
            '`{ not json` 이 남아 있습니다.\n[{"path": "a.py"}, {"path": "b.py"}]'
        )
        assert [o.action_input["path"] for o in turn.ops] == ["a.py", "b.py"]


class TestValueContentUnaffected:
    def test_brackets_inside_a_string_value_are_not_spans(self):
        turn = WF.parse_turn(
            '[{"action": "write_file", "path": "a.py", "content": "x = arr[0][1]\\n"}]'
        )
        assert turn.ops[0].action_input["content"] == "x = arr[0][1]\n"

    def test_bare_single_op_object_still_accepted(self):
        turn = WF.parse_turn('끝냅니다.\n{"action": "complete", "result": "done"}')
        assert [o.action for o in turn.ops] == ["complete"]


class TestXmlFcSameClass:
    """xml_fc 의 같은 계열 — v7.28.1 수리 (json_fc 의 op-서명 검증과 동형).

    괄호류는 원래 무해(구조 토큰이 특이). 산문 **언급**과 코드펜스 **예시**가
    문제였다: 언급은 유령 op(`{'tool_call': ''}`)를 실호출 앞에 끼워 넣고, 펜스
    예시는 실호출을 이겨 그대로 실행됐다. 수리=후보 검증(`_call_opens`: 세그먼트에
    `<parameter=`/클로저가 있거나 EOF 도달) + 펜스-인용 인지 앵커(`_call_anchor`,
    전부 펜스 안이면 실호출 드리프트로 수용). v7.27.1 에서 xfail(strict) 로
    고정해 뒀던 두 케이스가 본 계약이 됐다.
    """

    XWF = get("xml_fc")
    BLOCK = (
        "<tool_call>\n<function=read_file>\n"
        "<parameter=path>a.py</parameter>\n</function>\n</tool_call>"
    )

    def test_brackets_and_braces_are_harmless_here(self):
        turn = self.XWF.parse_turn(
            "`board[i][j]` 와 `{ passive: false }` 를 확인.\n" + self.BLOCK
        )
        assert [o.action for o in turn.ops] == ["read_file"]
        assert turn.ops[0].action_input["path"] == "a.py"

    def test_prose_mentioning_a_wire_token_keeps_params(self):
        turn = self.XWF.parse_turn(
            "xml_fc 는 `<function=read_file>` 형태로 받습니다.\n" + self.BLOCK
        )
        # 유령 op 없이 실호출 하나만, 파라미터 온전.
        assert [(o.action, o.action_input) for o in turn.ops] == [
            ("read_file", {"path": "a.py"})
        ]
        assert turn.parse_stage == 1
        assert "<function=read_file>" in (turn.thought or "")  # 언급은 thought 로

    def test_unquoted_mention_also_skipped(self):
        # 백틱 없는 언급 — 세그먼트 자격(파람/클로저 없음)으로 걸러진다.
        turn = self.XWF.parse_turn(
            "xml_fc 는 <function=read_file> 형태로 받습니다.\n" + self.BLOCK
        )
        assert [(o.action, o.action_input) for o in turn.ops] == [
            ("read_file", {"path": "a.py"})
        ]
        assert turn.parse_stage == 1

    def test_fenced_example_does_not_win_over_the_real_call(self):
        raw = (
            "```\n<tool_call>\n<function=shell>\n"
            "<parameter=command>ls</parameter>\n</function>\n</tool_call>\n```\n"
            "설명 후 실제 호출:\n" + self.BLOCK
        )
        turn = self.XWF.parse_turn(raw)
        assert [o.action for o in turn.ops] == ["read_file"]
        assert turn.ops[0].action_input == {"path": "a.py"}

    def test_fully_fenced_real_call_still_accepted(self):
        """모델이 실호출을 통째로 펜스에 싼 드리프트 — 자격 후보가 전부 펜스
        안이면 인용이 아니라 실호출이다 (기존 관용 유지)."""
        turn = self.XWF.parse_turn("읽겠습니다.\n```\n" + self.BLOCK + "\n```")
        assert [(o.action, o.action_input) for o in turn.ops] == [
            ("read_file", {"path": "a.py"})
        ]

    def test_example_between_two_calls_no_param_bleed(self):
        """두 실호출 사이의 펜스 예시: 예시는 op 가 안 되지만 세그먼트 경계는
        유지 — 예시의 파라미터가 앞 호출로 새면 안 된다."""
        raw = (
            self.BLOCK
            + "\n예시: ```\n<function=shell>\n<parameter=command>rm -rf /</parameter>\n</function>\n```\n"
            + self.BLOCK.replace("a.py", "b.py")
        )
        turn = self.XWF.parse_turn(raw)
        assert [(o.action, o.action_input) for o in turn.ops] == [
            ("read_file", {"path": "a.py"}),
            ("read_file", {"path": "b.py"}),
        ]

    def test_bare_open_truncation_keeps_the_tool_precise_op(self):
        """파람 직전에 잘린 트렁케이션(`<function=X>` + EOF): op {} 로 보존해야
        A5 가 도구-정밀 진단을 낸다 — 버리면 generic 경로로 후퇴한다. 자격
        규칙의 'EOF 도달 = 유지' 가지가 이걸 담당한다."""
        turn = self.XWF.parse_turn("쓰겠습니다.\n<tool_call>\n<function=write_file>")
        assert [(o.action, o.action_input) for o in turn.ops] == [("write_file", {})]

    def test_inner_fenced_function_inside_wrapper_still_parses(self):
        """래퍼는 밖, `<function=…>` 부분만 펜스 안에 싼 변종 — 자격 후보가 전부
        펜스 안이라도 버리면 안 되는 실호출이다(전부-펜스 관용의 존재 이유:
        인용 판정은 비인용 후보가 **있을 때만** 유효한 상대 신호다)."""
        raw = (
            "<tool_call>\n```\n<function=read_file>\n"
            "<parameter=path>a.py</parameter>\n</function>\n```\n</tool_call>"
        )
        turn = self.XWF.parse_turn(raw)
        assert [(o.action, o.action_input) for o in turn.ops] == [
            ("read_file", {"path": "a.py"})
        ]

    def test_orphan_fence_in_value_does_not_mask_later_calls(self):
        """파라미터 값 속 고아 ``` 하나가 뒤쪽 실호출을 가리면 안 된다 —
        balanced 쌍만 마스킹하는 이유. (고아를 EOF 까지 마스킹하면 값에 홀수
        펜스를 쓴 write_file 뒤의 모든 호출이 인용으로 오인돼 유실된다.)"""
        first = (
            "<tool_call>\n<function=write_file>\n"
            "<parameter=path>doc.md</parameter>\n"
            "<parameter=content>여는 펜스만 있는 문서\n```python</parameter>\n"
            "</function>\n</tool_call>"
        )
        turn = self.XWF.parse_turn(first + "\n" + self.BLOCK)
        acts = [(o.action, o.action_input.get("path")) for o in turn.ops]
        assert acts == [("write_file", "doc.md"), ("read_file", "a.py")]

    def test_truncated_call_and_zero_param_still_qualify(self):
        # EOF 도달 세그먼트(트렁케이션)와 클로저-보유 제로파람 — 자격 유지.
        t1 = self.XWF.parse_turn(
            "<tool_call>\n<function=write_file>\n<parameter=path>a.py</parameter>\n<parameter=content>x = 1"
        )
        assert t1.ops[0].action == "write_file"
        assert t1.ops[0].action_input["path"] == "a.py"
        t2 = self.XWF.parse_turn(
            "<tool_call>\n<function=complete>\n</function>\n</tool_call>"
        )
        assert [o.action for o in t2.ops] == ["complete"]
