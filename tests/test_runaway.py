"""v10.10.0 — stream-side runaway stop + cut-generation rows.

Room 67qcmb (2026-10-02): three generations degenerated into tabs/spaces right
after ``node -e "`` and ran to the 32,768-token output cap — 27 to 40 minutes
each, all discarded, and never written to turns.jsonl (dispatch never ran),
so "context or output cap?" needed the omlx server log. The dialect-level
early stop never looked: it runs only on chunks carrying the dialect's
trigger character and matches repeated wire-shape headers.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from agent_cli.loop import run_loop
from agent_cli.providers.base import LLMResponse, ModelCapabilities, TokenUsage
from agent_cli.providers.runaway import (
    NO_WORDS_WINDOW,
    REASON_TEXT,
    WHITESPACE_RUN_CHARS,
    RunawayDetector,
)
from agent_cli.recovery.observability import (
    FAILURE_OUTPUT_TRUNCATED,
    FAILURE_RUNAWAY,
    TurnRecord,
    classify_output_cap,
)
from tests.test_loop import TEST_PORTS, _complete


class TestRunawayDetector:
    def test_whitespace_run_across_chunks(self):
        d = RunawayDetector()
        assert d.feed('cd repo && node -e "') is None
        seen = None
        fed = 0
        while seen is None and fed < WHITESPACE_RUN_CHARS * 2:
            seen = d.feed("\t\t \t  \t\t")  # the 67qcmb pattern, one token at a time
            fed += 8
        assert seen == "whitespace_run"
        assert (
            fed <= WHITESPACE_RUN_CHARS + 8
        )  # trips as soon as the run is long enough

    def test_a_real_character_resets_the_run(self):
        d = RunawayDetector(whitespace_run=100)
        for _ in range(12):
            assert d.feed(" " * 9) is None  # 108 blanks in total …
            assert d.feed("x") is None  # … but never 100 in a row

    def test_trailing_blanks_of_a_mixed_chunk_start_the_run(self):
        d = RunawayDetector(whitespace_run=20)
        assert d.feed("abc" + " " * 15) is None
        assert d.feed(" " * 5) == "whitespace_run"

    def test_no_words_in_a_full_window(self):
        d = RunawayDetector(window=400)
        soup = ",\t ;\t, . ; -- ,,\t"  # punctuation and blanks, no letters
        seen = None
        while seen is None:
            seen = d.feed(soup)
        assert seen == "no_words"

    def test_no_words_needs_a_full_window(self):
        d = RunawayDetector(window=400, whitespace_run=10_000)
        assert d.feed(", ; , ; " * 40) is None  # 320 chars — window not full yet

    def test_numeric_map_grid_is_not_a_runaway(self):
        """The Doom room writes maps as ``0,0,0,…`` grids — half alphanumeric."""
        d = RunawayDetector()
        row = ",".join(["0"] * 64) + "\n"
        for _ in range(64):
            assert d.feed(row) is None

    def test_indented_code_is_not_a_runaway(self):
        d = RunawayDetector()
        for i in range(400):
            assert d.feed(" " * 24 + f"const v{i} = fn(a, b);\n") is None

    def test_cjk_counts_as_words(self):
        d = RunawayDetector(window=200, whitespace_run=10_000)
        for _ in range(40):
            assert d.feed("한글 日本語 — ") is None

    def test_defaults_are_generous(self):
        assert WHITESPACE_RUN_CHARS >= 1024 and NO_WORDS_WINDOW >= 2048
        assert set(REASON_TEXT) == {"whitespace_run", "no_words"}


class TestStreamStopsOnRunaway:
    def _run(self, texts):
        from agent_cli.providers.http import StreamEvent, run_sse_stream

        r = MagicMock()
        r.iter_lines.return_value = iter(
            [f"data: {json.dumps({'c': t})}".encode() for t in texts]
        )
        got: list[str] = []
        acc = run_sse_stream(
            r,
            got.append,
            map_payload=lambda data: StreamEvent(text=data.get("c", "")),
        )
        return acc, got, r

    def test_whitespace_runaway_closes_the_stream(self):
        texts = (
            ['node -e "'] + ["\t\t \t"] * (WHITESPACE_RUN_CHARS // 4 + 2) + ["NEVER"]
        )
        acc, got, r = self._run(texts)
        assert acc.stop_reason == "runaway" and acc.stop_detail == "whitespace_run"
        assert "NEVER" not in acc.content and "NEVER" not in got
        r.close.assert_called_once()

    def test_normal_stream_is_untouched(self):
        acc, _got, r = self._run(["hello ", "world"])
        assert acc.stop_reason is None and acc.stop_detail == ""
        assert acc.content == "hello world"
        r.close.assert_not_called()

    def test_openai_response_carries_the_detail(self):
        """The provider copies ``stop_detail`` onto ``LLMResponse``."""
        import inspect

        from agent_cli.providers import anthropic, openai

        assert "stop_detail=acc.stop_detail" in inspect.getsource(openai)
        assert "stop_detail=acc.stop_detail" in inspect.getsource(anthropic)
        assert LLMResponse(content="x").stop_detail == ""


class TestClassifyOutputCap:
    def test_model_cap(self):
        assert classify_output_cap(32768, 32768, False) == "model_cap"

    def test_context_clamp(self):
        assert classify_output_cap(5000, 5000, True) == "context_clamp"

    def test_server_cap_when_short_of_the_request(self):
        """omlx clamps on its own (max_tokens vs request_max_tokens)."""
        assert classify_output_cap(4096, 32768, False) == "server_cap"
        assert classify_output_cap(4000, 5000, True) == "server_cap"

    def test_unknown_without_usage_or_request(self):
        assert classify_output_cap(None, 32768, False) == "unknown"
        assert classify_output_cap(10, None, False) == "unknown"
        assert classify_output_cap(10, 0, False) == "unknown"

    def test_record_fields_default_to_none(self):
        rec = TurnRecord(model="m", timestamp="t", parse_stage=1)
        assert rec.stop_reason is None and rec.stop_detail is None


@pytest.fixture
def caps():
    return ModelCapabilities(
        context_window=32768, max_output_tokens=4096, supports_thinking=False
    )


def _rows(tmp_path):
    return [
        json.loads(ln)
        for ln in (tmp_path / "turns.jsonl").read_text().splitlines()
        if ln.strip()
    ]


class TestLoopRecordsCutGenerations:
    def test_length_cut_is_a_row_with_the_limit_named(self, caps, tmp_path):
        from agent_cli.context.manager import ContextManager

        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(
                content='{"action": "shell", "command": "echo partial',
                stop_reason="length",
                usage=TokenUsage(input_tokens=100, output_tokens=4096),
            ),
            LLMResponse(content=_complete("done")),
        ]
        ctx = ContextManager(session_dir=tmp_path)
        result = run_loop(
            ports=TEST_PORTS,
            query="q",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
        )
        assert result.output == "done"
        rows = _rows(tmp_path)
        cut = [r for r in rows if r["failure_signal"] == FAILURE_OUTPUT_TRUNCATED]
        assert len(cut) == 1
        assert cut[0]["stop_reason"] == "length"
        assert cut[0]["stop_detail"] == "model_cap"  # 4096 == max_output_tokens
        assert cut[0]["output_tokens"] == 4096 and cut[0]["parse_stage"] == 0
        # the dispatched turn after it is a normal row
        assert rows[-1]["stop_reason"] is None and rows[-1]["stop_detail"] is None

    def test_length_cut_short_of_the_request_is_server_cap(self, caps, tmp_path):
        from agent_cli.context.manager import ContextManager

        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(
                content="x",
                stop_reason="length",
                usage=TokenUsage(input_tokens=100, output_tokens=1000),
            ),
            LLMResponse(content=_complete("done")),
        ]
        run_loop(
            ports=TEST_PORTS,
            query="q",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ContextManager(session_dir=tmp_path),
        )
        cut = [r for r in _rows(tmp_path) if r["stop_reason"] == "length"]
        assert cut[0]["stop_detail"] == "server_cap"

    def test_runaway_is_not_executed_and_is_a_row(self, caps, tmp_path):
        from agent_cli.context.manager import ContextManager

        target = tmp_path / "x.txt"
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(
                content=json.dumps(
                    {"action": "write_file", "path": str(target), "content": "real"}
                )[:-2]
                + "\t\t \t" * 600,
                stop_reason="runaway",
                stop_detail="whitespace_run",
                usage=TokenUsage(input_tokens=100, output_tokens=700),
            ),
            LLMResponse(content=_complete("done")),
        ]
        ctx = ContextManager(session_dir=tmp_path)
        result = run_loop(
            ports=TEST_PORTS,
            query="write x",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ctx,
        )
        assert not target.exists()
        assert result.output == "done"
        retry_msgs = (
            provider.call.call_args_list[1].kwargs.get("messages")
            or (provider.call.call_args_list[1].args[0])
        )
        blob = "\n".join(str(m.get("content", "")) for m in retry_msgs)
        assert "ran away" in blob and "a long run of whitespace" in blob
        assert "NOT executed" in blob and "Your prior output:" in blob
        # the quote ends at the real content — trailing filler stripped
        assert '"content": "real' in blob and "\t\t \t\t\t" not in blob
        assert not any(
            m.get("role") == "assistant" and "real" in str(m.get("content", ""))
            for m in retry_msgs
        )
        cut = [r for r in _rows(tmp_path) if r["failure_signal"] == FAILURE_RUNAWAY]
        assert len(cut) == 1
        assert cut[0]["stop_reason"] == "runaway"
        assert cut[0]["stop_detail"] == "whitespace_run"
        # folded away once the model recovered — like the cap-cut note
        assert not any(m.get("tool") == "runaway" for m in ctx.get_raw_messages())

    def test_runaway_notice_is_not_about_the_context(self):
        from agent_cli.constants import RUNAWAY_NOTICE

        text = RUNAWAY_NOTICE.format(what="x")
        for bad in ("context", "window", "full", "limit"):
            assert bad not in text, bad
        assert "NOT executed" in text
