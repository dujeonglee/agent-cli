"""turns.jsonl 의 시간·크기·버전·방언 (v10.129.0).

원칙: "안 쟀다" 와 "0" 을 구별한다 — 못 잰 값은 키가 없다. 버전·방언은 줄마다.
계약은 그대로: 구조 메타데이터만, 프롬프트·응답 본문·인자·경로 없음.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from agent_cli import __version__
from agent_cli.constants import STOP_SERVER_ERROR
from agent_cli.context.manager import ContextManager
from agent_cli.loop import run_loop
from agent_cli.providers.base import LLMResponse, TokenUsage
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.recovery import observability as obs
from agent_cli.recovery.observability import CallMetrics, TurnRecorder
from tests.loop_ports import TEST_PORTS

SECRET = "SENTINEL_9f3c"


@pytest.fixture
def caps():
    return ModelCapabilities(
        context_window=32768, max_output_tokens=4096, supports_thinking=False
    )


def _complete(result: str) -> str:
    return json.dumps({"action": "complete", "result": result})


def _rows(session_dir) -> list[dict]:
    path = session_dir / "turns.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def _run(caps, session_dir, *responses, **kw):
    provider = MagicMock()
    provider.call.side_effect = list(responses)
    ctx = ContextManager(session_dir=session_dir)
    run_loop(
        ports=TEST_PORTS,
        query="q",
        provider=provider,
        capabilities=caps,
        model="m",
        ctx=ctx,
        **kw,
    )
    return ctx


class TestRecorderRow:
    def test_identity_on_every_row(self, tmp_path):
        rec = TurnRecorder(tmp_path, dialect="xml_fc")
        rec.record(model="m", parse_stage=1)
        rec.record_compaction(
            tokens_before=10, tokens_after=5, evicted_count=1, fallback_used=False
        )
        rows = _rows(tmp_path)
        for row in rows:
            assert row["harness_version"] == __version__
            assert row["dialect"] == "xml_fc"
        assert rows[1]["event"] == "compaction"

    def test_unmeasured_values_are_absent_keys(self, tmp_path):
        rec = TurnRecorder(tmp_path, dialect="json_fc")
        rec.record(model="m", parse_stage=1)
        row = _rows(tmp_path)[0]
        for key in (
            "reasoning_tokens",
            "ttft_ms",
            "llm_ms",
            "tool_ms",
            "tools",
            "output_chars",
            "thinking_chars",
        ):
            assert key not in row, key
        # 기존 토큰 넷은 모양 그대로(없으면 0)
        assert row["input_tokens"] == row["output_tokens"] == 0

    def test_measured_values_written(self, tmp_path):
        rec = TurnRecorder(tmp_path, dialect="json_fc")
        rec.record(
            model="m",
            parse_stage=1,
            usage=TokenUsage(
                input_tokens=10, output_tokens=8, reasoning_tokens=3, ttft_ns=2_400_000
            ),
            call=CallMetrics(llm_ms=900, output_chars=12, thinking_chars=0),
            tool_ms=0,
            tools=[["read_file", True, 4], ["shell", False, 70]],
        )
        row = _rows(tmp_path)[0]
        assert row["reasoning_tokens"] == 3
        assert row["ttft_ms"] == 2
        assert row["llm_ms"] == 900
        assert (row["output_chars"], row["thinking_chars"]) == (12, 0)
        assert row["tool_ms"] == 0  # 잰 0 은 0 이다
        assert row["tools"] == [["read_file", True, 4], ["shell", False, 70]]

    def test_usage_without_ttft_omits_ttft(self, tmp_path):
        rec = TurnRecorder(tmp_path, dialect="json_fc")
        rec.record(model="m", parse_stage=1, usage=TokenUsage(1, 1))
        row = _rows(tmp_path)[0]
        assert row["reasoning_tokens"] == 0
        assert "ttft_ms" not in row


class TestLoopRows:
    def test_dispatched_turns(self, caps, tmp_path):
        target = tmp_path / "a.txt"
        target.write_text("hello")
        sess = tmp_path / "sess"
        _run(
            caps,
            sess,
            LLMResponse(
                content=json.dumps({"action": "read_file", "path": str(target)}),
                thinking="think",
                usage=TokenUsage(
                    input_tokens=20, output_tokens=9, reasoning_tokens=2, ttft_ns=5e6
                ),
            ),
            LLMResponse(content=_complete("done")),
        )
        first, second = _rows(sess)
        assert first["dialect"] == second["dialect"] != ""
        assert first["harness_version"] == __version__
        assert isinstance(first["llm_ms"], int) and first["llm_ms"] >= 0
        assert first["output_chars"] == len(
            json.dumps({"action": "read_file", "path": str(target)})
        )
        assert first["thinking_chars"] == len("think")
        assert first["reasoning_tokens"] == 2 and first["ttft_ms"] == 5
        assert isinstance(first["tool_ms"], int)
        [[name, ok, ms]] = first["tools"]
        assert (name, ok) == ("read_file", True) and isinstance(ms, int)
        # 두 번째 턴: usage 없음 → 토큰 상세·ttft 키 없음, 실행 없음 → tools 키 없음
        assert "reasoning_tokens" not in second and "ttft_ms" not in second
        assert "tools" not in second
        assert "tool_ms" in second and "llm_ms" in second

    def test_failed_tool_is_ok_false(self, caps, tmp_path):
        sess = tmp_path / "sess"
        _run(
            caps,
            sess,
            LLMResponse(
                content=json.dumps(
                    {"action": "read_file", "path": str(tmp_path / "missing.txt")}
                )
            ),
            LLMResponse(content=_complete("done")),
        )
        [[name, ok, _ms]] = _rows(sess)[0]["tools"]
        assert (name, ok) == ("read_file", False)

    def test_cut_turns_carry_call_size_but_no_dispatch(self, caps, tmp_path):
        sess = tmp_path / "sess"
        _run(
            caps,
            sess,
            LLMResponse(content="cut here", stop_reason="length", thinking="ab"),
            LLMResponse(content="", stop_reason=STOP_SERVER_ERROR, stop_detail="boom"),
            LLMResponse(
                content="real\t\t\t",
                stop_reason="runaway",
                stop_detail="whitespace_run",
            ),
            LLMResponse(content=_complete("ok")),
        )
        cut, err, runaway, _done = _rows(sess)
        assert [r["failure_signal"] for r in (cut, err, runaway)] == [
            "OUTPUT_TRUNCATED",
            "NO_OUTPUT",
            "RUNAWAY",
        ]
        assert (cut["output_chars"], cut["thinking_chars"]) == (8, 2)
        assert err["output_chars"] == 0
        assert runaway["output_chars"] == len("real\t\t\t")
        for row in (cut, err, runaway):
            assert "llm_ms" in row
            assert "tool_ms" not in row and "tools" not in row
            assert row["dialect"] and row["harness_version"] == __version__

    def test_rows_hold_no_body_args_or_paths(self, caps, tmp_path):
        """본문·사고·인자·경로가 줄에 없다 — 문자열 값은 허용 목록뿐."""
        secret_file = tmp_path / f"{SECRET}_dir" / "f.txt"
        secret_file.parent.mkdir()
        secret_file.write_text(SECRET)
        sess = tmp_path / "sess"
        _run(
            caps,
            sess,
            LLMResponse(
                content=json.dumps({"action": "read_file", "path": str(secret_file)}),
                thinking=f"thinking about {SECRET}",
                usage=TokenUsage(5, 5),
            ),
            LLMResponse(content=_complete(f"answer {SECRET}")),
        )
        raw = (sess / "turns.jsonl").read_text()
        assert SECRET not in raw
        signals = {
            v
            for k, v in vars(obs).items()
            if k.startswith("FAILURE_") and isinstance(v, str)
        }
        for row in _rows(sess):
            allowed = (
                {"m", row["dialect"], __version__, row["timestamp"], "read_file"}
                | signals
                | set(row["primitives_applied"])
            )
            strings = [v for v in row.values() if isinstance(v, str)]
            for tool in row.get("tools", []):
                strings += [x for x in tool if isinstance(x, str)]
            strings += row["primitives_applied"]
            assert set(strings) <= allowed, set(strings) - allowed


def test_resumed_recorder_rows_name_the_running_version(tmp_path, monkeypatch):
    """버전은 줄마다 — 같은 파일에 다른 버전의 줄이 이어 붙을 수 있다."""
    TurnRecorder(tmp_path, dialect="json_fc").record(model="m", parse_stage=1)
    monkeypatch.setattr(obs, "__version__", "99.0.0")
    TurnRecorder(tmp_path, dialect="json_fc").record(model="m", parse_stage=1)
    assert [r["harness_version"] for r in _rows(tmp_path)] == [__version__, "99.0.0"]
