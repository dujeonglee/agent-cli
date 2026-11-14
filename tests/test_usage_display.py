"""사용량 표시 (v10.136.0) — 루프의 호출 합(``UsageTotals``)·턴 줄의 ``Σin``·런 끝 합계 줄.

웹 상단 칩의 글은 ``tests/test_app_markdown.py::TestUsageTopBar`` (node) 가 본다.
"""

from __future__ import annotations

import json
from io import StringIO
from unittest.mock import MagicMock, patch

from rich.console import Console

from agent_cli.loop.llm import UsageTotals, _build_token_stats
from agent_cli.providers.base import LLMResponse, TokenUsage
from agent_cli.render.minimal import (
    MinimalRenderer,
    _fmt_tok,
    _format_token_stats,
    format_usage_totals,
)


def _usage(**kw) -> TokenUsage:
    base = {"input_tokens": 100, "output_tokens": 50}
    base.update(kw)
    return TokenUsage(**base)


class TestUsageTotals:
    def test_add_sums_input_cache_output_calls_time(self):
        t = UsageTotals()
        t.add(
            _usage(
                input_tokens=200,
                cache_read_input_tokens=800,
                prompt_eval_ns=1,
                eval_ns=2,
            )
        )
        t.add(_usage(input_tokens=50, cache_creation_input_tokens=10, output_tokens=7))
        # 입력은 ctx 분자와 같은 정의(total_input_tokens) — 캐시 읽기·쓰기 포함
        assert (t.input, t.cached, t.output, t.calls, t.llm_ns) == (1060, 800, 57, 2, 3)

    def test_payload_carries_the_running_sums(self):
        t = UsageTotals()
        t.add(_usage(input_tokens=300, cache_read_input_tokens=700))
        stats = _build_token_stats(_usage(), 4096, t)
        assert stats["total_in"] == 1000
        assert stats["total_cached"] == 700
        assert stats["total_out"] == 50
        assert stats["calls"] == 1


class TestTurnLine:
    def test_sigma_in_next_to_sigma_out(self):
        t = UsageTotals(input=1_430_000, output=9_100, calls=3)
        line = _format_token_stats(_build_token_stats(_usage(), 4096, t))
        assert "Σin: 1.4M | Σout: 9.1K" in line

    def test_zero_sums_omitted(self):
        line = _format_token_stats(_build_token_stats(_usage(), 4096, UsageTotals()))
        assert "Σin" not in line and "Σout" not in line

    def test_fmt_tok_millions(self):
        assert (_fmt_tok(980), _fmt_tok(5234), _fmt_tok(1_430_000)) == (
            "980",
            "5.2K",
            "1.4M",
        )


class TestTotalLine:
    def test_full(self):
        t = UsageTotals(
            input=1_430_000,
            cached=1_100_000,
            output=9_100,
            calls=23,
            llm_ns=252 * 10**9,
        )
        assert (
            format_usage_totals(t)
            == "Total: in 1.4M (cached 1.1M) · out 9.1K · 23 calls · LLM 4m12s"
        )

    def test_no_cache_no_time_one_call(self):
        t = UsageTotals(input=900, output=5, calls=1)
        assert format_usage_totals(t) == "Total: in 900 · out 5 · 1 call"

    def test_durations(self):
        def line(sec):
            return format_usage_totals(UsageTotals(calls=1, llm_ns=int(sec * 1e9)))

        assert line(38.4).endswith("LLM 38s")
        assert line(3725).endswith("LLM 1h02m")

    def test_minimal_renderer_prints_one_line(self):
        buf = StringIO()
        r = MinimalRenderer(Console(file=buf, force_terminal=False, width=200))
        r.usage_totals(UsageTotals(input=900, output=5, calls=1))
        assert buf.getvalue().strip() == "● Total: in 900 · out 5 · 1 call"


def complete(result: str) -> str:
    return json.dumps({"action": "complete", "result": result})


class TestRunEnd:
    """main 런이 끝나면 그 런의 합을 한 번 알린다 — 호출이 있었을 때만, main 만."""

    def _run(self, tmp_path, responses, ports=None, **attrs):
        from agent_cli.context.manager import ContextManager
        from agent_cli.providers.capabilities import DEFAULT_CAPABILITIES
        from tests.loop_ports import TEST_PORTS
        from tests.loopkit import run_loop

        ctx = ContextManager(session_dir=tmp_path / "s", max_context_tokens=100_000)
        provider = MagicMock()
        provider.call.side_effect = responses
        seen: list = []
        with (
            patch(
                "agent_cli.loop.core.render_usage_totals",
                side_effect=lambda t: seen.append(UsageTotals(**vars(t))),
            ),
            patch("agent_cli.loop.core.render_token_usage"),
        ):
            run_loop(
                ports=ports or TEST_PORTS,
                query="q",
                provider=provider,
                capabilities=DEFAULT_CAPABILITIES,
                model="test",
                ctx=ctx,
                **attrs,
            )
        return seen

    def test_sums_every_call_of_the_run(self, tmp_path):
        responses = [
            LLMResponse(
                content='[{"action": "read_file", "path": "nope.txt"}]',
                usage=_usage(
                    input_tokens=1000, cache_read_input_tokens=0, eval_ns=10**9
                ),
            ),
            LLMResponse(
                content=complete("ok"),
                usage=_usage(
                    input_tokens=200, cache_read_input_tokens=1000, eval_ns=10**9
                ),
            ),
        ]
        seen = self._run(tmp_path, responses)
        assert len(seen) == 1
        t = seen[0]
        assert (t.input, t.cached, t.output, t.calls, t.llm_ns) == (
            2200,
            1000,
            100,
            2,
            2 * 10**9,
        )

    def test_no_usage_no_line(self, tmp_path):
        assert self._run(tmp_path, [LLMResponse(content=complete("ok"))]) == []

    def test_non_main_loop_prints_nothing(self, tmp_path):
        # 서브에이전트(owner ≠ main)도 자기 합을 쌓지만(턴 줄의 Σ) 런 끝 줄은 main 만
        from tests.loop_ports import make_ports

        responses = [LLMResponse(content=complete("ok"), usage=_usage())]
        assert self._run(tmp_path, responses, ports=make_ports(owner="agent:x")) == []

    def test_skill_loop_prints_nothing(self, tmp_path):
        # 스킬은 main 의 주소를 물려받지만 그리지 않는 종류다 — 합계 줄은 main 런의 것만
        responses = [LLMResponse(content=complete("ok"), usage=_usage())]
        assert self._run(tmp_path, responses, skill_name="eqskill") == []
