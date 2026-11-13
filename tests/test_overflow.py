"""Tests for context/overflow."""

import requests

from agent_cli.context.overflow import (
    ContextOverflowError,
    classify_overflow,
    is_context_overflow,
    parse_overflow_amounts,
)


def _http_error(status: int, body: str) -> requests.HTTPError:
    """An HTTPError carrying a ``.response`` with the given status code."""
    r = requests.Response()
    r.status_code = status
    return requests.HTTPError(f"{status} Error: {body}", response=r)


# Verified live against an omlx server (Qwen3.6-27B-MLX-8bit, 2026-05-30):
# a prompt over the configured context returns HTTP 400 with this body.
OMLX_400_MESSAGE = (
    "Prompt too long: 360012 tokens exceeds max context window of 262144 tokens"
)


class TestIsContextOverflow:
    def test_anthropic_pattern(self):
        assert is_context_overflow("Error: prompt is too long") is True

    def test_openai_pattern(self):
        assert (
            is_context_overflow("This model's maximum context length is 4096 tokens")
            is True
        )

    def test_context_length_exceeded_pattern(self):
        assert is_context_overflow("context length exceeded") is True

    def test_generic_pattern(self):
        assert is_context_overflow("too many tokens in request") is True

    def test_omlx_pattern(self):
        """The real omlx 400 body must classify as overflow."""
        assert is_context_overflow(OMLX_400_MESSAGE) is True

    def test_unrelated_error(self):
        assert is_context_overflow("Connection refused") is False
        assert is_context_overflow("404 Not Found") is False

    def test_unrelated_400_not_overflow(self):
        """A non-overflow 400 must NOT be treated as overflow — otherwise
        the recovery layer would shed history to 'fix' an unrelated bug."""
        assert (
            is_context_overflow("invalid_request_error: unknown field 'foo'") is False
        )

    def test_empty(self):
        assert is_context_overflow("") is False


class TestParseOverflowAmounts:
    def test_omlx_real_message(self):
        assert parse_overflow_amounts(OMLX_400_MESSAGE) == (360012, 262144)

    def test_anthropic_shape(self):
        msg = "prompt is too long: 270000 tokens > 262144 maximum"
        assert parse_overflow_amounts(msg) == (270000, 262144)

    def test_openai_shape_limit_first(self):
        msg = (
            "This model's maximum context length is 8192 tokens. However, "
            "your messages resulted in 10000 tokens"
        )
        # actual=10000, limit=8192 — order is reversed vs omlx/anthropic
        assert parse_overflow_amounts(msg) == (10000, 8192)

    def test_vllm_contains_at_least(self):
        # vLLM phrasing the original single-regex pattern missed entirely
        # (the actual lead-in differs, which previously dropped the limit too).
        msg = (
            "This model's maximum context length is 8192 tokens. However, "
            "your prompt contains at least 10000 input tokens."
        )
        assert parse_overflow_amounts(msg) == (10000, 8192)

    def test_openai_classic_requested(self):
        msg = (
            "This model's maximum context length is 4097 tokens. However, you "
            "requested 4500 tokens (3500 in the messages, 1000 in the completion)."
        )
        assert parse_overflow_amounts(msg) == (4500, 4097)

    def test_limit_recovered_when_actual_phrasing_unknown(self):
        # Independent extraction: an unrecognised actual lead-in must not
        # cost us the limit (the probe needs only the limit; recovery treats
        # actual as best-effort).
        msg = (
            "This model's maximum context length is 8192 tokens. However, the "
            "input was far too large to process."
        )
        assert parse_overflow_amounts(msg) == (None, 8192)

    def test_openai_multiline_message(self):
        # limit and actual on separate lines still parse (independent search).
        msg = (
            "This model's maximum context length is 32768 tokens.\n"
            "However, your messages resulted in 40000 tokens.\n"
            "Please reduce the length."
        )
        assert parse_overflow_amounts(msg) == (40000, 32768)

    def test_overflow_without_numbers_returns_none(self):
        # Classified as overflow, but no amounts to extract.
        assert parse_overflow_amounts("context length exceeded") == (None, None)

    def test_empty_returns_none(self):
        assert parse_overflow_amounts("") == (None, None)

    def test_unrelated_message_returns_none(self):
        assert parse_overflow_amounts("Connection refused") == (None, None)


class TestContextOverflowError:
    def test_carries_amounts(self):
        e = ContextOverflowError(360012, 262144, "too long")
        assert e.actual == 360012
        assert e.limit == 262144
        assert "too long" in str(e)

    def test_amounts_may_be_none(self):
        e = ContextOverflowError(None, None)
        assert e.actual is None
        assert e.limit is None


class TestClassifyOverflow:
    """★ AUDIT L-1 core: force_fit (destructive) must fire ONLY for a genuine
    server-side context rejection. classify_overflow is the single gate."""

    def test_typed_error_passes_through_amounts(self):
        assert classify_overflow(ContextOverflowError(900, 500)) == (900, 500)

    def test_http_400_overflow_parses_amounts(self):
        assert classify_overflow(_http_error(400, OMLX_400_MESSAGE)) == (360012, 262144)

    def test_http_413_overflow_recognised(self):
        assert classify_overflow(_http_error(413, OMLX_400_MESSAGE)) == (360012, 262144)

    def test_http_500_with_overflow_text_is_ignored(self):
        # A proxy/500 page that merely mentions "context window" must NOT be
        # treated as overflow — this false positive shredded history.
        assert classify_overflow(_http_error(500, "context window blah")) is None

    def test_bare_exception_with_overflow_text_is_ignored(self):
        # No HTTP response → not an overflow the loop recovers from.
        assert classify_overflow(RuntimeError(OMLX_400_MESSAGE)) is None

    def test_http_400_non_overflow_is_ignored(self):
        assert classify_overflow(_http_error(400, "missing field foo")) is None

    def test_connection_error_is_ignored(self):
        assert classify_overflow(requests.ConnectionError("Connection refused")) is None


# ── 디코딩 불가 그림 (v10.113.0, docs/vision/DESIGN.md §13) ─────────────

_UNDECODABLE_BODY = (
    '{"error":{"message":"image_url does not contain a decodable image.",'
    '"type":"invalid_request_error","param":"image_url"}}'
)


class TestUndecodableImageRetry:
    """omlx 가 400 "image_url does not contain a decodable image" 을 주면, 마지막 성공
    요청 뒤 새로 들어간 그림만 막고 한 번 재시도한다. 두 번째 400·새 그림 없음은 실패."""

    @staticmethod
    def _run(tmp_path, replies):
        import json
        from unittest.mock import MagicMock

        from agent_cli.context.manager import ContextManager
        from agent_cli.providers.base import LLMResponse
        from agent_cli.providers.capabilities import ModelCapabilities
        from tests.loop_ports import TEST_PORTS
        from tests.loopkit import run_loop

        def as_call(r):
            if isinstance(r, Exception):
                return r
            return LLMResponse(content=json.dumps(r))

        provider = MagicMock()
        provider.call.side_effect = [as_call(r) for r in replies]
        result = run_loop(
            ports=TEST_PORTS,
            query="look at the image",
            provider=provider,
            capabilities=ModelCapabilities(
                context_window=32768,
                max_output_tokens=4096,
                supports_thinking=False,
                supports_vision=True,
            ),
            model="m",
            ctx=ContextManager(session_dir=tmp_path),
            max_turns=8,
        )
        settings = [c.kwargs["settings"] for c in provider.call.call_args_list]
        return result, settings

    @staticmethod
    def _image(tmp_path, monkeypatch):
        from PIL import Image

        monkeypatch.chdir(tmp_path)
        Image.new("RGB", (40, 20), (0, 0, 255)).save(tmp_path / "p.png", format="PNG")
        import hashlib

        return "p.png", hashlib.sha256((tmp_path / "p.png").read_bytes()).hexdigest()

    def test_new_image_is_blocked_and_the_call_retried_once(
        self, tmp_path, monkeypatch
    ):
        path, sha = self._image(tmp_path, monkeypatch)
        result, settings = self._run(
            tmp_path,
            [
                {"action": "view_image", "path": path},
                _http_error(400, _UNDECODABLE_BODY),
                {"action": "complete", "result": "done"},
            ],
        )
        assert result.success and result.output == "done"
        assert [s.blocked_images for s in settings] == [
            frozenset(),
            frozenset(),
            frozenset({sha}),  # 재시도는 그 그림을 사유 줄로
        ]

    def test_a_second_400_fails(self, tmp_path, monkeypatch):
        path, _sha = self._image(tmp_path, monkeypatch)
        result, settings = self._run(
            tmp_path,
            [
                {"action": "view_image", "path": path},
                _http_error(400, _UNDECODABLE_BODY),
                _http_error(400, _UNDECODABLE_BODY),
            ],
        )
        assert not result.success and "LLM call failed" in result.error
        assert len(settings) == 3  # 재시도는 한 번뿐

    def test_no_new_image_means_no_retry(self, tmp_path):
        result, settings = self._run(tmp_path, [_http_error(400, _UNDECODABLE_BODY)])
        assert not result.success and "LLM call failed" in result.error
        assert len(settings) == 1

    def test_other_400_is_not_retried(self, tmp_path, monkeypatch):
        path, _sha = self._image(tmp_path, monkeypatch)
        result, settings = self._run(
            tmp_path,
            [
                {"action": "view_image", "path": path},
                _http_error(400, '{"error":{"message":"bad request"}}'),
            ],
        )
        assert not result.success and len(settings) == 2
