"""--verbose → verbose.jsonl (v9.24.3).

verbose 는 화면에 찍던 것(원문·사고·컨텍스트 덤프)을 세션 폴더의 JSONL 로
보낸다. 하위 에이전트까지 한 파일에 scope 로 구분돼 쌓이고, 첫 줄의 JSON
Schema 로 파일 스스로 필드를 설명한다 — grep/cat 이 아니라 JSON 쿼리로
분석하기 위해. 실측 계기: 보드 1zfgc2 의 실패 9건이 전부 상주 에이전트에서
났는데 원문이 어디에도 없었다.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from agent_cli import verbose
from agent_cli.context.manager import ContextManager
from agent_cli.providers.base import LLMResponse, TokenUsage
from tests.loop_ports import TEST_PORTS


def _caps():
    from agent_cli.providers.capabilities import ModelCapabilities

    return ModelCapabilities(
        context_window=32768, max_output_tokens=4096, supports_thinking=False
    )


def _lines(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _run(session_dir, responses, *, verbose_flag, **kw):
    from agent_cli.loop import run_loop

    it = iter(responses)
    provider = MagicMock()
    provider.call = MagicMock(side_effect=lambda *a, **k: next(it))
    return run_loop(
        ports=TEST_PORTS,
        query="do it",
        provider=provider,
        capabilities=_caps(),
        model="m",
        ctx=ContextManager(session_dir=session_dir),
        verbose=verbose_flag,
        **kw,
    )


def _resp(obj, **kw):
    content = obj if isinstance(obj, str) else json.dumps(obj)
    return LLMResponse(
        content=content,
        usage=TokenUsage(input_tokens=10, output_tokens=5),
        **kw,
    )


class TestRecorder:
    def test_off_by_default_and_noop(self, tmp_path):
        assert not verbose.enabled()
        verbose.record("debug", message="x")  # 조용히 무시
        assert not (tmp_path / "verbose.jsonl").exists()

    def test_first_line_is_the_schema(self, tmp_path):
        p = verbose.configure(tmp_path)
        verbose.record("debug", scope="main", message="hello")
        rows = _lines(p)
        assert rows[0]["kind"] == "schema" and rows[0]["schema"] == verbose.SCHEMA
        assert rows[1] == {**rows[1], "kind": "debug", "message": "hello", "v": 1}

    def test_scope_is_relative_to_the_main_session(self, tmp_path):
        verbose.configure(tmp_path)
        assert verbose.scope_of(tmp_path) == "main"
        assert verbose.scope_of(tmp_path / "agents" / "agt-1") == "agents/agt-1"
        assert verbose.scope_of(None) == "?"

    def test_context_entries_are_bounded(self):
        big = "x" * 5000
        (e,) = verbose.context_entries([{"role": "user", "content": big}])
        assert e == {"role": "user", "chars": 5000, "head": "x" * 300}

    def test_context_entries_count_native_tool_calls(self):
        """native_fc (v10.2.1): assistant 의 ``tool_calls`` 도 ``chars``/``head`` 에
        든다 — content 만 세면 호출 턴이 0자로 기록된다."""
        (e,) = verbose.context_entries(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1_0",
                            "type": "function",
                            "function": {"name": "shell", "arguments": '{"cmd": "ls"}'},
                        }
                    ],
                }
            ]
        )
        assert e["role"] == "assistant"
        assert e["head"] == '⚡ shell {"cmd": "ls"}  (call_1_0)'
        assert e["chars"] == len(e["head"])

    def test_debug_log_goes_to_the_file_not_stderr(self, tmp_path, capsys):
        p = verbose.configure(tmp_path)
        verbose.debug_log("probe")
        assert capsys.readouterr().err == ""
        assert _lines(p)[-1]["message"] == "probe"


class TestLoopIntegration:
    def test_failed_turn_is_recorded_with_its_raw_text(self, tmp_path):
        bad = {"action": "memory", "type": "decision", "summary": ""}  # mode 누락
        good = {"action": "complete", "result": "ok"}
        _run(tmp_path, [_resp(bad), _resp(good)], verbose_flag=True)
        rows = _lines(tmp_path / "verbose.jsonl")
        calls = [r for r in rows if r["kind"] == "llm_call"]
        assert len(calls) == 2
        first = calls[0]
        assert first["scope"] == "main" and first["turn"] == 1
        assert first["failure_signal"] == "SCHEMA_MISMATCH"
        assert first["text"] == json.dumps(bad)  # 모델이 쓴 그대로
        assert first["ops"] == [
            {"action": "memory", "input": {"type": "decision", "summary": ""}}
        ]
        assert first["usage"]["output_tokens"] == 5
        assert calls[1]["failure_signal"] is None
        # 호출 직전 컨텍스트도 (크기 제한된 미리보기)
        assert any(r["kind"] == "context" for r in rows)

    def test_nothing_is_written_without_the_flag(self, tmp_path):
        _run(
            tmp_path,
            [_resp({"action": "complete", "result": "ok"})],
            verbose_flag=False,
        )
        assert not (tmp_path / "verbose.jsonl").exists()

    def test_sub_loops_record_under_their_scope(self, tmp_path):
        """하위 루프는 verbose 플래그가 없어도(러너가 False 를 넘긴다) 메인이
        켠 기록기에 자기 scope 로 쓴다."""
        verbose.configure(tmp_path)
        sub = tmp_path / "agents" / "agt-9"
        _run(sub, [_resp({"action": "complete", "result": "ok"})], verbose_flag=False)
        calls = [
            r for r in _lines(tmp_path / "verbose.jsonl") if r["kind"] == "llm_call"
        ]
        assert [c["scope"] for c in calls] == ["agents/agt-9"]
        assert not (sub / "verbose.jsonl").exists()

    def test_output_cut_is_recorded_unparsed(self, tmp_path):
        cut = _resp("<tool_call>\n<function=write_file>", stop_reason="length")
        _run(
            tmp_path,
            [cut, _resp({"action": "complete", "result": "ok"})],
            verbose_flag=True,
        )
        calls = [
            r for r in _lines(tmp_path / "verbose.jsonl") if r["kind"] == "llm_call"
        ]
        assert calls[0]["stop_reason"] == "length"
        assert calls[0]["parse_stage"] is None and calls[0]["ops"] == []

    def test_every_record_matches_the_schema(self, tmp_path):
        jsonschema = pytest.importorskip("jsonschema")
        bad = {"action": "memory", "type": "decision", "summary": ""}
        _run(
            tmp_path,
            [_resp(bad), _resp({"action": "complete", "result": "ok"})],
            verbose_flag=True,
        )
        rows = _lines(tmp_path / "verbose.jsonl")
        for r in rows:
            jsonschema.validate(r, verbose.SCHEMA)
        assert {r["kind"] for r in rows} >= {"schema", "llm_call", "context"}
