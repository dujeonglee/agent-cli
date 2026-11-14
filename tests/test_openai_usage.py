"""OpenAI 경로가 서버의 cached·reasoning 토큰을 읽는다 (v10.128.0).

``prompt_tokens`` 는 캐시 적중분을, ``completion_tokens`` 는 추론분을 포함한다
(omlx 0.7.3·vLLM·LiteLLM 공통 모양). ``TokenUsage`` 의 뜻에 맞춰 캐시분은
``input_tokens`` 에서 빠져 ``cache_read_input_tokens`` 로 가고, 추론분은
``output_tokens`` 안에 남은 채 ``reasoning_tokens`` 로 따로 센다. 불변식:
``total_input_tokens`` 는 서버의 ``prompt_tokens`` 그대로 — 예산 재조정·ctx% 가
캐시 적중에 흔들리지 않는다.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from agent_cli.providers.base import LLMResponse, TokenUsage
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.providers.openai import OpenAIProvider, _map_openai_payload
from tests.loop_ports import TEST_PORTS

_CAPS = ModelCapabilities(
    context_window=4096, max_output_tokens=2048, supports_thinking=False
)

# (usage 블록, 기대 (input, output, cache_read, reasoning))
CASES = {
    "details": (
        {
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "prompt_tokens_details": {"cached_tokens": 800},
            "completion_tokens_details": {"reasoning_tokens": 150},
        },
        (200, 200, 800, 150),
    ),
    "no_details": (
        {"prompt_tokens": 1000, "completion_tokens": 200},
        (1000, 200, 0, 0),
    ),
    "null_details": (
        {
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "prompt_tokens_details": None,
            "completion_tokens_details": {"reasoning_tokens": None},
        },
        (1000, 200, 0, 0),
    ),
    "wrong_types": (
        {
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "prompt_tokens_details": {"cached_tokens": "800"},
            "completion_tokens_details": [150],
        },
        (1000, 200, 0, 0),
    ),
    "bool_and_negative": (
        {
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "prompt_tokens_details": {"cached_tokens": True},
            "completion_tokens_details": {"reasoning_tokens": -5},
        },
        (1000, 200, 0, 0),
    ),
    "over_total": (
        {
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "prompt_tokens_details": {"cached_tokens": 5000},
            "completion_tokens_details": {"reasoning_tokens": 999},
        },
        (0, 200, 1000, 200),
    ),
}


def _fields(u: TokenUsage) -> tuple[int, int, int, int]:
    return (
        u.input_tokens,
        u.output_tokens,
        u.cache_read_input_tokens,
        u.reasoning_tokens,
    )


def _sse(usage: dict) -> MagicMock:
    lines = [
        b'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":null}]}',
        (
            "data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        ).encode(),
        ("data: " + json.dumps({"choices": [], "usage": usage})).encode(),
        b"data: [DONE]",
    ]
    resp = MagicMock()
    resp.status_code = 200
    resp.iter_lines.return_value = iter(lines)
    resp.raise_for_status.return_value = None
    return resp


def _json_resp(usage: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
        "usage": usage,
    }
    return resp


def _call(resp: MagicMock, *, stream: bool) -> LLMResponse:
    with patch("agent_cli.providers.openai.requests.post", return_value=resp):
        provider = OpenAIProvider("http://localhost:8000/v1", "key")
        kwargs = {"on_chunk": lambda _c: None} if stream else {}
        return provider.call(
            messages=[], system="", model="m", capabilities=_CAPS, **kwargs
        )


@pytest.mark.parametrize("name", sorted(CASES))
class TestThreePaths:
    def test_stream_payload_mapper(self, name):
        usage, want = CASES[name]
        ev = _map_openai_payload({"choices": [], "usage": usage})
        assert ev is not None
        assert _fields(TokenUsage(**ev.usage_fields)) == want

    def test_streaming_call(self, name):
        usage, want = CASES[name]
        result = _call(_sse(usage), stream=True)
        assert _fields(result.usage) == want
        # 시간은 클라이언트가 잰 것 — usage 를 다시 짜도 그대로 실린다
        assert result.usage.ttft_ns > 0

    def test_non_streaming_call(self, name):
        usage, want = CASES[name]
        result = _call(_json_resp(usage), stream=False)
        assert _fields(result.usage) == want

    def test_total_input_is_prompt_tokens(self, name):
        """불변식: 캐시를 나눠 적어도 합은 서버의 ``prompt_tokens``."""
        usage, _ = CASES[name]
        for result in (
            _call(_sse(usage), stream=True),
            _call(_json_resp(usage), stream=False),
        ):
            assert result.usage.total_input_tokens == usage["prompt_tokens"]


def test_non_int_prompt_tokens_counts_as_zero():
    """``prompt_tokens: null`` 같은 깨진 블록도 TokenUsage 를 정수로만 채운다."""
    ev = _map_openai_payload({"usage": {"prompt_tokens": None, "completion_tokens": 3}})
    assert ev.usage_fields["input_tokens"] == 0
    assert ev.usage_fields["output_tokens"] == 3


class TestBudgetInvariant:
    """캐시 적중 응답과 없는 응답에서 예산 재조정·ctx% 에 들어가는 값이 같다."""

    _WITH = CASES["details"][0]
    _WITHOUT = CASES["no_details"][0]

    def _reconciled(self, tmp_path, usage: dict) -> tuple[int, dict]:
        from agent_cli.context.manager import ContextManager
        from tests.loopkit import run_loop

        ctx = ContextManager(
            session_dir=tmp_path, max_context_tokens=1_000_000, compaction_enabled=False
        )
        ctx.add({"role": "user", "content": "x" * 40})
        parsed = OpenAIProvider("http://localhost:8000/v1", "k")._parse_response(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                [{"action": "complete", "result": "ok"}]
                            )
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": usage,
            }
        )
        provider = MagicMock()
        provider.call.side_effect = [parsed]
        seen: list[dict] = []
        with (
            patch.object(
                ctx, "reconcile_actual_tokens", wraps=ctx.reconcile_actual_tokens
            ) as spy,
            patch("agent_cli.loop.core.render_token_usage") as tok,
        ):
            run_loop(
                ports=TEST_PORTS,
                query="q",
                provider=provider,
                capabilities=_CAPS,
                model="test",
                ctx=ctx,
            )
            seen = [c.args[0] for c in tok.call_args_list]
        return spy.call_args.args[0], seen[0]

    def test_same_reconcile_and_ctx_in(self, tmp_path):
        r_with, s_with = self._reconciled(tmp_path / "a", self._WITH)
        r_without, s_without = self._reconciled(tmp_path / "b", self._WITHOUT)
        assert r_with == r_without == 1000
        assert s_with["in"] == s_without["in"] == 1000
        # 바뀌는 것은 칸 나눔뿐 — 캐시 read 와 추론 몫이 보인다
        assert (s_with["cache_read"], s_with["reasoning"]) == (800, 150)
        assert (s_without["cache_read"], s_without["reasoning"]) == (0, 0)


def test_in_speed_counts_only_non_cached_tokens():
    """``in_speed`` = 캐시 아닌 입력 / 프리필 시간 — 적중 턴의 속도가 부풀지 않는다."""
    from agent_cli.loop.llm import _build_token_stats

    usage = TokenUsage(
        input_tokens=200,
        output_tokens=10,
        cache_read_input_tokens=800,
        reasoning_tokens=4,
        prompt_eval_ns=1_000_000_000,
    )
    stats = _build_token_stats(usage, context_window=4096, total_out=10)
    assert stats["in_speed"] == 200
    assert stats["in"] == 1000
    assert stats["reasoning"] == 4


def test_cli_usage_line_shows_cache_hit():
    """CLI 사용량 줄은 cache_read 가 0 이 아니면 이미 ``cache hit:`` 칸을 그린다."""
    from agent_cli.loop.llm import _build_token_stats
    from agent_cli.render.minimal import _format_token_stats

    usage = TokenUsage(input_tokens=200, output_tokens=10, cache_read_input_tokens=800)
    line = _format_token_stats(_build_token_stats(usage, 4096, 10))
    assert "in: 1.0K" in line
    assert "cache hit: 800" in line
