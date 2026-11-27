"""버려진 응답(하니스가 저장하지 않고 되묻기만 남긴 응답)의 연속 상한 (NOTE-unbounded-retries §4).

형식 되묻기·홀로 거절된 호출(A4·A5·인자 JSON 오류)·B1 넛지는 턴을 세지 않는다 — 같은 응답이 되풀이되면 다른
상한이 없었다. 가짜 provider 는 응답 수 상한(:data:`CAP`)에서 멈추고, 그때까지 런이 끝나지 않았음을 단언한다."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from agent_cli.context.manager import ContextManager
from agent_cli.dialects import get
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities
from tests.loop_ports import TEST_PORTS
from tests.loopkit import run_loop

CAPS = ModelCapabilities(
    context_window=32768, max_output_tokens=4096, supports_thinking=False
)
#: 가짜 provider 의 응답 수 상한 — 넘으면 멈춘다(런이 끝나지 않았다는 뜻)
CAP = 30
DIALECTS = ("json_fc", "native_fc")


class ProviderCap(BaseException):
    """가짜 provider 가 상한에 닿았다 — 런이 그때까지 끝나지 않았다. ``BaseException`` 인 까닭: 엔진은 공급자의
    ``Exception`` 을 "LLM call failed" 로 받아 런을 끝낸다 — 그 길로 새면 끝난 것처럼 보인다."""


def call(dialect: str, tool: str, args: dict) -> LLMResponse:
    if dialect == "native_fc":
        return LLMResponse(
            content="ok", tool_calls=[{"id": "c0", "name": tool, "input": args}]
        )
    return LLMResponse(content="x\n\n" + json.dumps([{"action": tool, **args}]))


def prose(dialect: str) -> LLMResponse:
    return LLMResponse(content="I am thinking about it.")


def bad_arguments(dialect: str) -> LLMResponse:
    """native_fc: 인자가 JSON 이 아닌 호출 / json_fc: 깨진 배열(NO_JSON)."""
    if dialect == "native_fc":
        return LLMResponse(
            content="ok",
            tool_calls=[
                {"id": "c0", "name": "read_file", "input": None, "arguments": "{bad"}
            ],
        )
    return LLMResponse(content='x\n\n[{"action": "read_file", "path": }]')


FAILED_EDIT = (
    "edit_file",
    {"path": "f.py", "op": "replace", "pos": "2#ZZ", "lines": ["x"]},
)
REFUSED_EDIT = (
    "edit_file",
    {"path": "f.py", "op": "bogus", "pos": "2#ZZ", "lines": ["x"]},
)


def run(tmp_path, monkeypatch, dialect, responses):
    """``responses``: 응답 목록 — 마지막 것을 끝없이 되풀이한다. (결과, 호출 수); 상한이면 ProviderCap."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "f.py").write_text("a\nb\nc\n")
    n = [0]

    def respond(**kw):
        n[0] += 1
        if n[0] > CAP:
            raise ProviderCap
        return responses[min(n[0], len(responses)) - 1]

    provider = MagicMock()
    provider.call.side_effect = respond
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
    return result, n[0]


@pytest.mark.parametrize("dialect", DIALECTS)
class TestUnboundedToday:
    def test_same_prose_repeats_without_end(self, tmp_path, monkeypatch, dialect):
        with pytest.raises(ProviderCap):
            run(tmp_path, monkeypatch, dialect, [prose(dialect)])

    def test_same_bad_arguments_repeat_without_end(
        self, tmp_path, monkeypatch, dialect
    ):
        with pytest.raises(ProviderCap):
            run(tmp_path, monkeypatch, dialect, [bad_arguments(dialect)])

    def test_refused_call_after_a_failed_run_of_the_same_tool(
        self, tmp_path, monkeypatch, dialect
    ):
        """처음 보고한 결함 — 실행 실패(해시 불일치) 뒤 같은 도구를 실행 전 거절 인자로 되풀이."""
        responses = [
            call(dialect, "read_file", {"path": "f.py"}),
            call(dialect, *FAILED_EDIT),
            call(dialect, *REFUSED_EDIT),
        ]
        with pytest.raises(ProviderCap):
            run(tmp_path, monkeypatch, dialect, responses)
