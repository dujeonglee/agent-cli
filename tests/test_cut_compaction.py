"""v10.11.0 — compaction on a window-clamp cut (ratio 1.0 default) and the
always-on failures.jsonl.

Room 67qcmb: the fixed 80% target compacted ahead of need, and the model —
reading the percentage — stalled just under it for two hours. Now the window
itself is the trigger: the loop clamps ``max_tokens`` to what the window has
left, and a generation that hits that clamp (``length`` with detail
``context_clamp``) compacts right there and is retried with the room back.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from agent_cli.context.manager import (
    COMPACTION_RATIO_MAX,
    DEFAULT_COMPACTION_RATIO,
    ContextManager,
)
from agent_cli.loop import run_loop
from agent_cli.loop.llm import LLMCaller
from agent_cli.providers.base import LLMResponse, ModelCapabilities, TokenUsage
from agent_cli.recovery.failures import FILE_NAME, TEXT_CAP, record_failure
from tests.test_loop import TEST_PORTS, _complete, _make_provider


@pytest.fixture
def caps():
    return ModelCapabilities(
        context_window=32768, max_output_tokens=4096, supports_thinking=False
    )


def _failures(tmp_path):
    p = tmp_path / FILE_NAME
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]


class TestRatioDefaults:
    def test_default_and_max_are_the_full_window(self):
        assert DEFAULT_COMPACTION_RATIO == 1.0 and COMPACTION_RATIO_MAX == 1.0

    def test_preventive_target_reserves_the_minimum_output(self, caps, tmp_path):
        """At ratio 1.0 the cache may fill the window — minus what a request
        must still be able to generate, or the clamp's floor would push the
        request over the window."""
        seen = []
        ctx = ContextManager(session_dir=tmp_path)  # ratio 1.0
        with patch.object(ctx, "ensure_within", side_effect=seen.append):
            run_loop(
                ports=TEST_PORTS,
                query="q",
                provider=_make_provider(_complete("ok")),
                capabilities=caps,
                model="test",
                ctx=ctx,
            )
        reserve = LLMCaller._MIN_REQUEST_OUTPUT_TOKENS + LLMCaller._MAX_TOKENS_MARGIN
        assert seen[0] <= caps.context_window - reserve
        assert seen[0] > caps.context_window // 2  # but it is the window, not 80%


class TestClampCutCompacts:
    def _run(self, caps, tmp_path, *, clamped: bool, output_tokens: int):
        ctx = ContextManager(session_dir=tmp_path)
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(
                content='{"action": "shell", "command": "echo cut',
                stop_reason="length",
                usage=TokenUsage(input_tokens=30_000, output_tokens=output_tokens),
            ),
            LLMResponse(content=_complete("done")),
        ]
        compacts = []

        def fake_compact_now():
            compacts.append(True)
            return (30_000, 12_000)

        # the loop's own clamp bookkeeping is what classify_output_cap reads
        real_call = LLMCaller._call_llm

        def call_and_mark(self_llm):
            resp = real_call(self_llm)
            self_llm.last_max_tokens_clamped = clamped
            self_llm.last_max_tokens_requested = output_tokens if clamped else 4096
            return resp

        with (
            patch.object(ctx, "compact_now", side_effect=fake_compact_now),
            patch.object(LLMCaller, "_call_llm", call_and_mark),
        ):
            result = run_loop(
                ports=TEST_PORTS,
                query="q",
                provider=provider,
                capabilities=caps,
                model="m",
                ctx=ctx,
            )
        retry = (
            provider.call.call_args_list[1].kwargs.get("messages")
            or (provider.call.call_args_list[1].args[0])
        )
        blob = "\n".join(str(m.get("content", "")) for m in retry)
        return result, compacts, blob

    def test_context_clamp_cut_compacts_and_says_the_room_is_back(self, caps, tmp_path):
        result, compacts, blob = self._run(
            caps, tmp_path, clamped=True, output_tokens=2000
        )
        assert result.output == "done"
        assert compacts == [True]
        assert "context window was full" in blob and "room is back" in blob
        assert "re-emit it as it was" in blob
        assert "smaller unit" not in blob  # not the shrink-your-work lesson
        rows = [
            json.loads(ln)
            for ln in (tmp_path / "turns.jsonl").read_text().splitlines()
            if ln.strip()
        ]
        cut = [r for r in rows if r.get("stop_reason") == "length"]
        assert cut[0]["stop_detail"] == "context_clamp"

    def test_model_cap_cut_does_not_compact(self, caps, tmp_path):
        result, compacts, blob = self._run(
            caps, tmp_path, clamped=False, output_tokens=4096
        )
        assert result.output == "done"
        assert compacts == []
        assert "output-token limit" in blob and "smaller unit" in blob

    def test_nothing_to_compact_keeps_the_plain_notice(self, caps, tmp_path):
        """compact_now that evicts nothing (equal before/after) must not claim
        the room is back."""
        ctx = ContextManager(session_dir=tmp_path)
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(
                content="x",
                stop_reason="length",
                usage=TokenUsage(input_tokens=1, output_tokens=1024),
            ),
            LLMResponse(content=_complete("done")),
        ]
        real_call = LLMCaller._call_llm

        def call_and_mark(self_llm):
            resp = real_call(self_llm)
            self_llm.last_max_tokens_clamped = True
            self_llm.last_max_tokens_requested = 1024
            return resp

        with (
            patch.object(ctx, "compact_now", return_value=(500, 500)),
            patch.object(LLMCaller, "_call_llm", call_and_mark),
        ):
            run_loop(
                ports=TEST_PORTS,
                query="q",
                provider=provider,
                capabilities=caps,
                model="m",
                ctx=ctx,
            )
        retry = (
            provider.call.call_args_list[1].kwargs.get("messages")
            or (provider.call.call_args_list[1].args[0])
        )
        blob = "\n".join(str(m.get("content", "")) for m in retry)
        assert "output-token limit" in blob and "room is back" not in blob


class TestFailuresLog:
    def test_clean_session_writes_nothing(self, caps, tmp_path):
        run_loop(
            ports=TEST_PORTS,
            query="q",
            provider=_make_provider(_complete("ok")),
            capabilities=caps,
            model="m",
            ctx=ContextManager(session_dir=tmp_path),
        )
        assert not (tmp_path / FILE_NAME).exists()

    def test_format_failure_is_recorded_with_its_text(self, caps, tmp_path):
        provider = _make_provider("this is not json at all", _complete("ok"))
        run_loop(
            ports=TEST_PORTS,
            query="q",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ContextManager(session_dir=tmp_path),
        )
        rows = _failures(tmp_path)
        assert len(rows) == 1
        r = rows[0]
        # bare prose parses as a 0-op turn → NO_ACTION (the loop's label, kept as is)
        assert r["failure_signal"] == "NO_ACTION"
        assert r["text"] == "this is not json at all"
        assert r["model"] == "m" and r["dialect"] == "json_fc"
        assert r["parse_stage"] == 1 and isinstance(r["primitives"], list)
        assert r["turn"] == 1 and r["v"] == 1 and r["text_truncated"] is False

    def test_cut_and_runaway_are_recorded(self, caps, tmp_path):
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(
                content="cut here",
                stop_reason="length",
                usage=TokenUsage(input_tokens=10, output_tokens=4096),
            ),
            LLMResponse(
                content="real\t\t\t",
                stop_reason="runaway",
                stop_detail="whitespace_run",
                thinking="hmm",
            ),
            LLMResponse(content=_complete("ok")),
        ]
        run_loop(
            ports=TEST_PORTS,
            query="q",
            provider=provider,
            capabilities=caps,
            model="m",
            ctx=ContextManager(session_dir=tmp_path),
        )
        rows = _failures(tmp_path)
        assert [r["failure_signal"] for r in rows] == ["OUTPUT_TRUNCATED", "RUNAWAY"]
        assert rows[0]["stop_reason"] == "length" and rows[0]["text"] == "cut here"
        assert rows[0]["usage"]["output_tokens"] == 4096
        assert rows[1]["stop_detail"] == "whitespace_run"
        assert rows[1]["text"] == "real\t\t\t" and rows[1]["thinking"] == "hmm"
        assert rows[1]["usage"] is None

    def test_text_is_capped_head_and_tail(self, tmp_path):
        big = "A" * TEXT_CAP + "MIDDLE" + "Z" * TEXT_CAP
        record_failure(
            tmp_path,
            turn=1,
            model="m",
            dialect="d",
            failure_signal="RUNAWAY",
            stop_reason="runaway",
            stop_detail="no_words",
            parse_stage=None,
            primitives=[],
            text=big,
            thinking=None,
            usage=None,
        )
        r = _failures(tmp_path)[0]
        assert r["text_truncated"] is True
        assert len(r["text"]) < len(big) and "MIDDLE" not in r["text"]
        assert r["text"].startswith("A") and r["text"].endswith("Z")
        assert "[truncated]" in r["text"]

    def test_no_session_dir_is_a_noop(self):
        assert (
            record_failure(
                None,
                turn=1,
                model="m",
                dialect="d",
                failure_signal="NO_JSON",
                stop_reason=None,
                stop_detail=None,
                parse_stage=0,
                primitives=[],
                text="x",
                thinking=None,
                usage=None,
            )
            is None
        )
