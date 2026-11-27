"""서버 파싱 방언(native_fc)이 글로 쓴 호출을 어떻게 다루나 (감사 2-5).

native_fc 의 형식 규칙은 "never write a tool call as text … the server will not deliver them" 이다. 호출은 서버의
``tool_calls`` 로만 온다 — 응답의 글 속 JSON 배열·``<tool_call>`` 블록은 호출이 아니다."""

from __future__ import annotations

from unittest.mock import MagicMock

from agent_cli.context.manager import ContextManager
from agent_cli.dialects import get
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities
from tests.loop_ports import TEST_PORTS
from tests.loopkit import run_loop

CAPS = ModelCapabilities(
    context_window=32768, max_output_tokens=4096, supports_thinking=False
)
DONE = LLMResponse(
    content="", tool_calls=[{"id": "1", "name": "complete", "input": {"result": "ok"}}]
)
JSON_TEXT = (
    'I will do this next: [{"action": "write_file", "path": "made.txt", '
    '"content": "x"}]'
)
XML_TEXT = (
    "Example: <tool_call>\n<function=write_file>\n<parameter=path>made.txt"
    "</parameter>\n<parameter=content>x</parameter>\n</function>\n</tool_call>"
)


def _run(tmp_path, monkeypatch, text):
    monkeypatch.chdir(tmp_path)
    provider = MagicMock()
    provider.call.side_effect = [LLMResponse(content=text), DONE]
    sd = tmp_path / "sess"
    sd.mkdir()
    ctx = ContextManager(sd, max_context_tokens=30000, dialect=get("native_fc"))
    result = run_loop(
        ports=TEST_PORTS,
        query="go",
        provider=provider,
        capabilities=CAPS,
        model="m",
        dialect=get("native_fc"),
        ctx=ctx,
    )
    second = provider.call.call_args_list[1].kwargs["messages"]
    return result, second[-1]["content"]


def test_json_array_in_text_is_run(tmp_path, monkeypatch):
    result, _ = _run(tmp_path, monkeypatch, JSON_TEXT)
    assert result.output == "ok"
    assert (tmp_path / "made.txt").read_text() == "x"


def test_foreign_tool_call_block_in_text_is_run(tmp_path, monkeypatch):
    result, _ = _run(tmp_path, monkeypatch, XML_TEXT)
    assert result.output == "ok"
    assert (tmp_path / "made.txt").read_text() == "x"
