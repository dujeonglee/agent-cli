"""문법은 마지막 호출에서 끝나나 (PROP-grammar-stalls — 가설 (나)).

GLM·omlx 실측: 뿌리 끝의 무제한 `ws` 때문에 호출을 다 쓴 모델이 멈추지 못하고 공백만 냈다(문법 켬 96런 중 15런
상한, 끔 0/48; 리드 실험 — 지금 문법 4/4 공백 폭주, 뿌리 끝 `ws` 만 지운 문법 4/4 정상 종료)."""

from __future__ import annotations

import pytest

from agent_cli.dialects import get
from agent_cli.tools.registry import (
    TOOLS,
    allows_extra_keys,
    effective_tool_names,
    flat_param_schemas,
)

TEXT_DIALECTS = ("xml_fc", "glm_argkey", "hermes_json", "json_fc")


def grammar(name: str, thinking_open: bool) -> str:
    d = get(name)
    tools = [
        (
            n,
            flat_param_schemas(n, TOOLS[n].parameters),
            allows_extra_keys(TOOLS[n].parameters),
        )
        for n in effective_tool_names(list(TOOLS), d)
    ]
    return d.grammar(tools, thinking_open=thinking_open)


def root(g: str) -> str:
    (line,) = [ln for ln in g.splitlines() if ln.startswith("root ::= ")]
    return line


@pytest.mark.parametrize("thinking_open", [False, True])
@pytest.mark.parametrize("name", ["xml_fc", "glm_argkey", "hermes_json"])
def test_tag_and_hermes_roots_end_at_the_last_call(name, thinking_open):
    """뿌리는 마지막 호출(또는 산문)에서 끝난다 — 끝의 `ws` 가 없다. 호출 안·호출 사이의 `ws` 는 그대로다."""
    g = grammar(name, thinking_open)
    pre = 'think "</think>\\n\\n" ' if thinking_open else ""
    assert root(g) == f'root ::= {pre}( calls | prose "\\n" calls | prose )'
    assert "ws ::= [ \\t\\r\\n]*" in g.splitlines()
    assert any(" ws " in ln for ln in g.splitlines() if ln.startswith("calls"))


@pytest.mark.parametrize("thinking_open", [False, True])
def test_json_fc_root_ends_at_the_array(thinking_open):
    assert root(grammar("json_fc", thinking_open)).endswith(" | prose )")
