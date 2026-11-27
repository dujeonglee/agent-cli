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
def test_missing_close_swallows_the_next_parameter(dialect):
    (op,) = get(dialect).parse_turn(CASES[dialect].format(pos="1#AA")).ops
    assert "op" not in op.action_input
    assert isinstance(op.action_input["lines"], str)
    assert "op" in op.action_input["lines"]


def test_missing_close_after_answers_swallows_the_result():
    (op,) = get("glm_argkey").parse_turn(GLM_ANSWERS).ops
    assert "result" not in op.action_input
    assert isinstance(op.action_input["answers"], str)


@pytest.mark.parametrize("dialect", sorted(CASES))
def test_the_call_is_refused_until_the_action_loop_ends_it(
    dialect, tmp_path, monkeypatch
):
    """`op` 누락 거절 → 모델은 같은 응답을 되풀이 → B1 하드 실패. 파일은 그대로."""
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "f.py"
    f.write_text("def g():\n    return 1\n")
    pos = TOOLS["read_file"].run({"path": str(f)}).output.split("\n")[1].split(":")[0]
    text = CASES[dialect].format(pos=pos)
    provider = MagicMock()
    provider.call.side_effect = [LLMResponse(content=text)] * 12
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
    assert result.success is False
    assert result.error.startswith("Action loop unresolved")
    assert f.read_text() == "def g():\n    return 1\n"
