"""failures.jsonl · verbose.jsonl 이 서버가 준 호출 목록(``tool_calls``)을 싣는다 (v10.38.0).

실측 계기: v10.37.0 회귀 런(extract-moves turn 48)에서 ``memory`` 가 ``mode`` 없이
거부됐는데, native 함수 호출은 호출이 ``text`` 밖(``tool_calls``)에 있어 두 로그
어디에도 인자가 없었다 — 길이(376자)만 남아 재생 측정에서 본문을 재구성해야 했다.
두 기록은 ``AgentLoop._generation_fields`` 한 곳에서 같은 필드를 받는다.
"""

from __future__ import annotations

import inspect
import json
from unittest.mock import MagicMock

import pytest

from agent_cli import verbose
from agent_cli.context.manager import ContextManager
from agent_cli.dialects import get
from agent_cli.loop import run_loop
from agent_cli.loop.core import AgentLoop
from agent_cli.providers.base import LLMResponse, ModelCapabilities, TokenUsage
from agent_cli.recovery import failures
from agent_cli.recovery.failures import (
    FILE_NAME,
    TEXT_CAP,
    VERSION,
    _bounded_value,
    record_failure,
)
from tests.loop_ports import TEST_PORTS

#: 두 로그가 한 헬퍼에서 받는 필드
SHARED = (
    "model",
    "text",
    "thinking",
    "tool_calls",
    "stop_reason",
    "stop_detail",
    "parse_stage",
    "primitives",
    "usage",
)

BAD_EDIT = {
    "id": "c1",
    "name": "edit_file",
    "input": {"op": "replace", "pos": "12#AB", "lines": ["y"]},  # path 누락
}
DONE = {"id": "c9", "name": "complete", "input": {"result": "ok"}}


@pytest.fixture
def caps():
    return ModelCapabilities(
        context_window=32768, max_output_tokens=4096, supports_thinking=False
    )


def _rows(path):
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def _calls(tmp_path):
    return [r for r in _rows(tmp_path / "verbose.jsonl") if r["kind"] == "llm_call"]


def _resp(content="", calls=None, **kw):
    kw.setdefault("usage", TokenUsage(input_tokens=10, output_tokens=5))
    return LLMResponse(content=content, tool_calls=calls, **kw)


def _run(tmp_path, caps, responses, *, dialect="native_fc", verbose_flag=False):
    provider = MagicMock()
    provider.call.side_effect = responses
    ctx = ContextManager(
        session_dir=tmp_path, max_context_tokens=30_000, dialect=get(dialect)
    )
    return run_loop(
        ports=TEST_PORTS,
        query="Q",
        provider=provider,
        capabilities=caps,
        model="m",
        ctx=ctx,
        max_turns=5,
        verbose=verbose_flag,
    )


def _record(tmp_path, **kw):
    base = {
        "turn": 1,
        "model": "m",
        "dialect": "native_fc",
        "failure_signal": "SCHEMA_MISMATCH",
        "stop_reason": "stop",
        "stop_detail": None,
        "parse_stage": 1,
        "primitives": [],
        "text": "",
        "thinking": None,
        "usage": None,
    }
    record_failure(tmp_path, **{**base, **kw})
    return _rows(tmp_path / FILE_NAME)[-1]


class TestBoundedValue:
    @pytest.mark.parametrize(
        "value",
        [None, "", "short", 3, 1.5, True, [], {}, {"a": [1, "b", {"c": None}]}],
    )
    def test_small_values_pass_through_unflagged(self, value):
        assert _bounded_value(value) == (value, False)

    def test_string_at_the_cap_is_kept(self):
        s = "x" * TEXT_CAP
        assert _bounded_value(s) == (s, False)

    def test_long_string_is_split_head_and_tail(self):
        big = "A" * TEXT_CAP + "MIDDLE" + "Z" * TEXT_CAP
        out, truncated = _bounded_value(big)
        assert truncated is True
        assert out.startswith("A") and out.endswith("Z") and "MIDDLE" not in out
        assert "[truncated]" in out and len(out) < len(big)

    def test_long_string_is_found_at_any_depth(self):
        big = "A" * (TEXT_CAP * 2)
        value = [{"name": "write_file", "input": {"path": "a", "lines": ["k", big]}}]
        out, truncated = _bounded_value(value)
        assert truncated is True
        assert out[0]["name"] == "write_file" and out[0]["input"]["path"] == "a"
        assert out[0]["input"]["lines"][0] == "k"
        assert len(out[0]["input"]["lines"][1]) < len(big)

    def test_input_is_not_mutated(self):
        big = "A" * (TEXT_CAP + 10)
        value = [{"input": {"content": big}}]
        _bounded_value(value)
        assert value[0]["input"]["content"] == big

    def test_shares_the_text_cap_rule(self):
        """문자열 규칙은 ``_bounded`` 하나 — 본문과 인자의 자르는 모양이 같다."""
        big = "A" * TEXT_CAP + "Z" * TEXT_CAP
        assert _bounded_value(big) == failures._bounded(big)


class TestRecordFailure:
    def test_version_is_two(self, tmp_path):
        assert VERSION == 2 and _record(tmp_path)["v"] == 2

    def test_calls_are_stored_as_delivered(self, tmp_path):
        r = _record(tmp_path, tool_calls=[BAD_EDIT])
        assert r["tool_calls"] == [BAD_EDIT]
        assert r["tool_calls_truncated"] is False

    def test_non_json_arguments_keep_the_raw_string(self, tmp_path):
        call = {"id": "c1", "name": "shell", "input": None, "arguments": '{"cmd": '}
        assert _record(tmp_path, tool_calls=[call])["tool_calls"] == [call]

    @pytest.mark.parametrize("absent", [None, []])
    def test_no_calls_is_null(self, tmp_path, absent):
        r = _record(tmp_path, tool_calls=absent)
        assert r["tool_calls"] is None and r["tool_calls_truncated"] is False

    def test_omitting_the_argument_is_null(self, tmp_path):
        assert _record(tmp_path)["tool_calls"] is None

    def test_oversized_argument_is_capped_and_flagged(self, tmp_path):
        big = "A" * (TEXT_CAP * 2)
        call = {"id": "c1", "name": "write_file", "input": {"content": big}}
        r = _record(tmp_path, tool_calls=[call, BAD_EDIT])
        assert r["tool_calls_truncated"] is True
        assert len(r["tool_calls"][0]["input"]["content"]) < len(big)
        assert r["tool_calls"][1] == BAD_EDIT  # 다른 호출은 그대로
        assert r["text_truncated"] is False  # 본문 플래그와 독립


class TestNativeLoop:
    def test_rejected_call_arguments_reach_failures_jsonl(self, tmp_path, caps):
        """고치려던 그 구멍: 산문 없는 native 호출이 거부되면 인자가 남는다."""
        _run(tmp_path, caps, [_resp(calls=[BAD_EDIT]), _resp(calls=[DONE])])
        rows = _rows(tmp_path / FILE_NAME)
        assert len(rows) == 1
        r = rows[0]
        assert r["failure_signal"] == "SCHEMA_MISMATCH" and r["dialect"] == "native_fc"
        assert r["text"] is None  # 호출은 본문에 없다
        assert r["tool_calls"] == [BAD_EDIT]
        assert r["turn"] == 1 and r["v"] == VERSION

    def test_prose_and_calls_are_kept_apart(self, tmp_path, caps):
        _run(
            tmp_path,
            caps,
            [_resp("Fixing the regex.", [BAD_EDIT]), _resp(calls=[DONE])],
        )
        r = _rows(tmp_path / FILE_NAME)[0]
        assert r["text"] == "Fixing the regex." and r["tool_calls"] == [BAD_EDIT]

    def test_unknown_tool_call_is_recorded(self, tmp_path, caps):
        call = {"id": "c1", "name": "edit", "input": {"path": "a.txt"}}
        _run(tmp_path, caps, [_resp(calls=[call]), _resp(calls=[DONE])])
        r = _rows(tmp_path / FILE_NAME)[0]
        assert r["failure_signal"] == "UNKNOWN_TOOL" and r["tool_calls"] == [call]

    def test_non_json_arguments_are_recorded_raw(self, tmp_path, caps):
        call = {"id": "c1", "name": "shell", "input": None, "arguments": '{"command": '}
        _run(tmp_path, caps, [_resp(calls=[call]), _resp(calls=[DONE])])
        rows = _rows(tmp_path / FILE_NAME)
        assert len(rows) == 1 and rows[0]["tool_calls"] == [call]

    def test_every_call_of_a_batch_is_recorded(self, tmp_path, caps):
        ok = {"id": "c0", "name": "read_file", "input": {"path": "nope.txt"}}
        _run(tmp_path, caps, [_resp(calls=[ok, BAD_EDIT]), _resp(calls=[DONE])])
        assert _rows(tmp_path / FILE_NAME)[0]["tool_calls"] == [ok, BAD_EDIT]

    def test_clean_session_still_writes_no_file(self, tmp_path, caps):
        _run(tmp_path, caps, [_resp(calls=[DONE])])
        assert not (tmp_path / FILE_NAME).exists()

    def test_verbose_carries_the_same_calls(self, tmp_path, caps):
        _run(
            tmp_path,
            caps,
            [_resp(calls=[BAD_EDIT]), _resp(calls=[DONE])],
            verbose_flag=True,
        )
        calls = _calls(tmp_path)
        assert [c["tool_calls"] for c in calls] == [[BAD_EDIT], [DONE]]
        assert calls[0]["failure_signal"] == "SCHEMA_MISMATCH"
        assert calls[1]["failure_signal"] is None  # 깨끗한 턴도 verbose 엔 남는다

    def test_verbose_records_match_the_schema(self, tmp_path, caps):
        jsonschema = pytest.importorskip("jsonschema")
        raw = {"id": "c1", "name": "shell", "input": None, "arguments": "{"}
        _run(
            tmp_path,
            caps,
            [
                _resp(calls=[BAD_EDIT]),
                _resp(calls=[raw], usage=None),  # usage 없는 서버
                _resp("just prose"),  # 호출 없는 턴 → tool_calls null
                _resp(calls=[DONE]),
            ],
            verbose_flag=True,
        )
        rows = _rows(tmp_path / "verbose.jsonl")
        for r in rows:
            jsonschema.validate(r, verbose.SCHEMA)
        calls = [r for r in rows if r["kind"] == "llm_call"]
        assert calls[1]["usage"] is None and calls[2]["tool_calls"] is None
        assert all(r["v"] == verbose.VERSION == 2 for r in rows)


class TestTextDialect:
    def test_server_extracted_calls_are_recorded_next_to_the_text(self, tmp_path, caps):
        """텍스트 방언은 호출을 본문으로 되살려 파싱한다(v10.1.4) — 본문은 종전
        그대로고, 서버가 구조로 준 사실이 ``tool_calls`` 에 따로 남는다."""
        _run(
            tmp_path,
            caps,
            [
                _resp(calls=[BAD_EDIT]),
                _resp(json.dumps({"action": "complete", "result": "ok"})),
            ],
            dialect="json_fc",
        )
        r = _rows(tmp_path / FILE_NAME)[0]
        assert r["failure_signal"] == "SCHEMA_MISMATCH" and r["dialect"] == "json_fc"
        assert json.loads(r["text"]) == [{"action": "edit_file", **BAD_EDIT["input"]}]
        assert r["tool_calls"] == [BAD_EDIT]

    def test_plain_text_failure_has_null_calls(self, tmp_path, caps):
        _run(
            tmp_path,
            caps,
            [
                _resp("this is not json at all"),
                _resp(json.dumps({"action": "complete", "result": "ok"})),
            ],
            dialect="json_fc",
        )
        r = _rows(tmp_path / FILE_NAME)[0]
        assert r["text"] == "this is not json at all" and r["tool_calls"] is None


class TestOneSource:
    def test_both_logs_agree_on_the_shared_fields(self, tmp_path, caps):
        _run(
            tmp_path,
            caps,
            [
                _resp("Fixing it.", [BAD_EDIT], thinking="hmm", stop_reason="stop"),
                _resp(calls=[DONE]),
            ],
            verbose_flag=True,
        )
        failed = _rows(tmp_path / FILE_NAME)[0]
        logged = _calls(tmp_path)[0]
        for key in SHARED:
            assert failed[key] == logged[key], key
        assert failed["failure_signal"] == logged["failure_signal"]
        assert failed["usage"] == {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }

    def test_helper_returns_exactly_the_shared_fields(self):
        loop = AgentLoop.__new__(AgentLoop)
        loop._config = MagicMock(model="m")
        loop._dispatch = MagicMock(last_outcome=None)
        fields = loop._generation_fields("t", _resp("t", [BAD_EDIT]))
        assert tuple(fields) == SHARED

    def test_shared_fields_are_recorders_keyword_arguments(self):
        """헬퍼가 필드를 더하면 ``record_failure`` 가 받아야 한다 — 어긋나면 여기서."""
        params = inspect.signature(record_failure).parameters
        assert set(SHARED) <= set(params)

    def test_recorders_do_not_assemble_fields_themselves(self):
        """usage 딕셔너리를 두 기록 함수가 따로 조립하던 중복이 돌아오지 않게."""
        for fn in (AgentLoop._record_failure, AgentLoop._record_verbose_call):
            src = inspect.getsource(fn)
            assert "_generation_fields(" in src
            assert "input_tokens" not in src and "thinking" not in src
