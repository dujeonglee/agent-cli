"""태그 방언(xml_fc·glm_argkey)에서 JSON 인자 값의 닫는 태그가 빠진 호출 (PROP-argkey-missing-close).

실측(GLM, glm_argkey): `lines` 의 JSON 배열 뒤 `</arg_value>` 를 빠뜨리고 바로 다음 `<arg_key>` 를 쓴다 — 값이 다음
파라미터의 닫는 태그까지 삼켜 그 파라미터가 사라진다."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from agent_cli.context.manager import ContextManager
from agent_cli.dialects import get
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.tools.registry import TOOLS
from tests.loop_ports import TEST_PORTS
from tests.loopkit import run_loop

CAPS = ModelCapabilities(
    context_window=32768, max_output_tokens=4096, supports_thinking=False
)
LINES = ["def g():", '    return "x\\\\y"']

#: 측정 기록(L1-glm_argkey-1-cand) 의 모양 — `lines` 배열 뒤 닫는 태그 없이 `op`
GLM_MISSING = (
    "<tool_call>edit_file\n<arg_key>lines</arg_key>\n<arg_value>"
    + json.dumps(LINES)
    + "\n<arg_key>op</arg_key>\n<arg_value>replace</arg_value>\n"
    "<arg_key>path</arg_key>\n<arg_value>f.py</arg_value>\n"
    "<arg_key>pos</arg_key>\n<arg_value>{pos}</arg_value>\n</tool_call>"
)
XML_MISSING = (
    "<tool_call>\n<function=edit_file>\n<parameter=lines>"
    + json.dumps(LINES)
    + "\n<parameter=op>replace</parameter>\n<parameter=path>f.py</parameter>\n"
    "<parameter=pos>{pos}</parameter>\n</function>\n</tool_call>"
)
#: `complete` 의 `answers` 뒤 닫는 태그 없이 `result` (측정 기록 10건의 모양)
GLM_ANSWERS = (
    '<tool_call>complete\n<arg_key>answers</arg_key>\n<arg_value>["u-1"]\n'
    "<arg_key>result</arg_key>\n<arg_value>done</arg_value>\n</tool_call>"
)
CASES = {"glm_argkey": GLM_MISSING, "xml_fc": XML_MISSING}


@pytest.mark.parametrize("dialect", sorted(CASES))
def test_json_value_ends_where_the_json_ends(dialect):
    """배열 값은 `]` 에서 끝나고 그 뒤 파라미터는 다시 읽힌다 — 모델이 쓴 순서대로, 구제 표지(parse_stage 2)와 함께."""
    turn = get(dialect).parse_turn(CASES[dialect].format(pos="1#AA"))
    (op,) = turn.ops
    assert op.action_input == {
        "lines": LINES,
        "op": "replace",
        "path": "f.py",
        "pos": "1#AA",
    }
    assert list(op.action_input) == ["lines", "op", "path", "pos"]
    assert turn.parse_stage == 2
    assert (
        op.truncated is False
    )  # 절단이 아니다 — 잘린 편집의 마지막 줄 지우기를 타지 않는다


def test_missing_close_after_answers_keeps_the_result():
    (op,) = get("glm_argkey").parse_turn(GLM_ANSWERS).ops
    assert op.action_input == {"answers": ["u-1"], "result": "done"}


@pytest.mark.parametrize("dialect", sorted(CASES))
def test_the_rescued_call_runs(dialect, tmp_path, monkeypatch):
    """구제된 호출은 실행된다 — 파일이 바뀌고 다음 응답의 `complete` 로 끝난다."""
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "f.py"
    f.write_text("def g():\n    return 1\n")
    pos = TOOLS["read_file"].run({"path": str(f)}).output.split("\n")[1].split(":")[0]
    done = (
        "<tool_call>complete\n<arg_key>result</arg_key>\n<arg_value>ok</arg_value>\n</tool_call>"
        if dialect == "glm_argkey"
        else "<tool_call>\n<function=complete>\n<parameter=result>ok</parameter>\n</function>\n</tool_call>"
    )
    provider = MagicMock()
    provider.call.side_effect = [
        LLMResponse(content=CASES[dialect].format(pos=pos)),
        LLMResponse(content=done),
    ]
    ctx = ContextManager(tmp_path / "s", max_context_tokens=30000, dialect=get(dialect))
    result = run_loop(
        ports=TEST_PORTS,
        query="q",
        provider=provider,
        capabilities=CAPS,
        model="m",
        dialect=get(dialect),
        ctx=ctx,
    )
    assert result.success and result.output == "ok"
    # 1번째 줄을 `lines` 둘로 바꿨다 — 백슬래시·따옴표가 든 줄도 그대로
    assert f.read_text() == "\n".join(LINES) + "\n    return 1\n"


@pytest.mark.parametrize(
    "value",
    [
        '["a"]',  # 온전한 JSON
        '["a"] trailing words',  # JSON 뒤 글 — 태그가 아니면 구제하지 않는다(지금처럼 거절 재료)
        '["a", <arg_key>x</arg_key>]',  # JSON 이 끝나기 전의 태그
    ],
)
def test_other_json_shapes_read_as_before(value):
    text = (
        "<tool_call>edit_file\n<arg_key>lines</arg_key>\n<arg_value>"
        + value
        + "</arg_value>\n<arg_key>op</arg_key>\n<arg_value>replace</arg_value>\n</tool_call>"
    )
    (op,) = get("glm_argkey").parse_turn(text).ops
    assert op.action_input["op"] == "replace"
    assert op.action_input["lines"] == (["a"] if value == '["a"]' else value)


def test_text_argument_holding_tags_is_untouched():
    """글 인자(`write_file` `content`)의 값 안 태그는 글이다 — v10.1.3 그대로, JSON 처럼 보여도."""
    content = '["a"]\n<arg_key>path</arg_key>\n<arg_value>evil.txt'
    text = (
        "<tool_call>write_file\n<arg_key>content</arg_key>\n<arg_value>"
        + content
        + "</arg_value>\n<arg_key>path</arg_key>\n<arg_value>a.md</arg_value>\n</tool_call>"
    )
    (op,) = get("glm_argkey").parse_turn(text).ops
    assert op.action_input == {"content": content, "path": "a.md"}


def test_a_repeated_key_is_not_rescued():
    """다시 읽은 키가 이미 있으면 모호하다 — 지금처럼 둔다."""
    text = (
        "<tool_call>edit_file\n<arg_key>op</arg_key>\n<arg_value>replace</arg_value>\n"
        '<arg_key>lines</arg_key>\n<arg_value>["a"]\n<arg_key>op</arg_key>\n<arg_value>append'
        "</arg_value>\n</tool_call>"
    )
    (op,) = get("glm_argkey").parse_turn(text).ops
    assert op.action_input["op"] == "replace"
    assert isinstance(op.action_input["lines"], str)
