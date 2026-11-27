"""서버 파싱 방언(native_fc)이 글로 쓴 호출을 어떻게 다루나 (감사 2-5).

native_fc 의 형식 규칙은 "never write a tool call as text … the server will not deliver them" 이다. 호출은 서버의
``tool_calls`` 로만 온다 — 응답의 글 속 JSON 배열·``<tool_call>`` 블록은 호출이 아니다."""

from __future__ import annotations

from unittest.mock import MagicMock

from agent_cli.context.manager import ContextManager
from agent_cli.dialects import get
from agent_cli.loop.dispatch import DISCARDED_RESPONSE_LIMIT
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


NO_CALL = (
    "Your response had no usable tool call (no function call reached the harness)."
)


def test_json_array_in_text_is_not_run(tmp_path, monkeypatch):
    """글 속 JSON 배열은 호출이 아니다 — 실행하지 않고, 호출 없는 턴의 기존 되묻기를 받는다."""
    result, observed = _run(tmp_path, monkeypatch, JSON_TEXT)
    assert result.output == "ok"
    assert not (tmp_path / "made.txt").exists()
    assert observed.startswith(NO_CALL)
    assert JSON_TEXT in observed  # 되묻기가 그 글을 인용한다


def test_foreign_tool_call_block_in_text_is_not_run(tmp_path, monkeypatch):
    """외래 형식 구제도 타지 않는다."""
    result, observed = _run(tmp_path, monkeypatch, XML_TEXT)
    assert result.output == "ok"
    assert not (tmp_path / "made.txt").exists()
    assert observed.startswith(NO_CALL)


def test_prose_turn_is_read_as_before(tmp_path, monkeypatch):
    """호출 없는 산문 턴은 종전과 같은 되묻기다(글 파싱을 끈 것이 산문 턴을 바꾸지 않는다)."""
    _, observed = _run(tmp_path, monkeypatch, "Just prose, no call.")
    assert observed.startswith(NO_CALL)


def test_repeated_text_calls_end_at_the_discarded_response_limit(tmp_path, monkeypatch):
    """글로 쓴 호출만 되풀이하면 — 호출 없는 턴의 되묻기(버려진 응답)라 v10.177.10 의 상한에서 끝난다."""
    monkeypatch.chdir(tmp_path)
    provider = MagicMock()
    provider.call.side_effect = [LLMResponse(content=JSON_TEXT)] * (
        DISCARDED_RESPONSE_LIMIT + 5
    )
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
    assert result.success is False and result.error.startswith("No progress:")
    assert provider.call.call_count == DISCARDED_RESPONSE_LIMIT
    assert not (tmp_path / "made.txt").exists()
