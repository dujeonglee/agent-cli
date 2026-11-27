"""Tests for the LLM-request retry helper.

Covers post_with_retry's scope (Timeout / ConnectionError plus
transient gateway 5xx), attempt counting, and exception propagation
on exhaustion. Also verifies the provider modules route their
requests.post calls through post_with_retry so the retry applies
uniformly.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from agent_cli.providers import http as http_mod
from agent_cli.providers.credentials import Credentials
from agent_cli.providers.http import post_with_retry


def _ok_response() -> MagicMock:
    """Mock a successful requests.Response."""
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    return resp


def _status_response(status: int) -> MagicMock:
    """Mock a requests.Response with a specific status code (no stream read)."""
    resp = MagicMock()
    resp.status_code = status
    return resp


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Avoid real delays inside retry loops (default 1s × 2 retries would
    slow the suite down). Individual tests verify sleep was called with
    the right argument."""
    monkeypatch.setattr(http_mod.time, "sleep", MagicMock())


class TestDefaults:
    def test_success_first_attempt_no_retry(self):
        post_fn = MagicMock(return_value=_ok_response())
        result = post_with_retry(post_fn, "http://x/llm")
        assert result.status_code == 200
        assert post_fn.call_count == 1

    def test_success_passes_kwargs_through(self):
        post_fn = MagicMock(return_value=_ok_response())
        post_with_retry(post_fn, "http://x/llm", json={"k": 1}, timeout=42, stream=True)
        post_fn.assert_called_once_with(
            "http://x/llm", json={"k": 1}, timeout=42, stream=True
        )


class TestRetryOnTimeout:
    def test_retry_then_success(self):
        good = _ok_response()
        post_fn = MagicMock(
            side_effect=[requests.Timeout("first"), requests.Timeout("second"), good]
        )
        result = post_with_retry(post_fn, "http://x/llm")
        assert result is good
        assert post_fn.call_count == 3

    def test_connect_timeout_is_retried(self):
        good = _ok_response()
        post_fn = MagicMock(side_effect=[requests.ConnectTimeout("dns slow"), good])
        result = post_with_retry(post_fn, "http://x/llm")
        assert result is good
        assert post_fn.call_count == 2

    def test_read_timeout_is_retried(self):
        good = _ok_response()
        post_fn = MagicMock(side_effect=[requests.ReadTimeout("slow first byte"), good])
        result = post_with_retry(post_fn, "http://x/llm")
        assert result is good
        assert post_fn.call_count == 2


class TestRetryOnConnectionError:
    def test_retry_then_success(self):
        good = _ok_response()
        post_fn = MagicMock(side_effect=[requests.ConnectionError("refused"), good])
        result = post_with_retry(post_fn, "http://x/llm")
        assert result is good
        assert post_fn.call_count == 2


class TestRetryOnGatewayStatus:
    """Transient gateway 5xx (502/503/504) is retried on a separate budget
    (default 3); other statuses return immediately for the caller to handle."""

    @pytest.mark.parametrize("status", [502, 503, 504])
    def test_gateway_status_retried_then_success(self, status):
        good = _ok_response()
        post_fn = MagicMock(side_effect=[_status_response(status), good])
        result = post_with_retry(post_fn, "http://x/llm")
        assert result is good
        assert post_fn.call_count == 2

    def test_error_response_closed_before_resend(self):
        bad = _status_response(502)
        good = _ok_response()
        post_fn = MagicMock(side_effect=[bad, good])
        post_with_retry(post_fn, "http://x/llm")
        bad.close.assert_called_once()

    def test_bare_500_not_retried(self):
        bad = _status_response(500)
        post_fn = MagicMock(return_value=bad)
        result = post_with_retry(post_fn, "http://x/llm")
        assert result is bad
        assert post_fn.call_count == 1

    def test_4xx_not_retried(self):
        bad = _status_response(400)
        post_fn = MagicMock(return_value=bad)
        result = post_with_retry(post_fn, "http://x/llm")
        assert result is bad
        assert post_fn.call_count == 1

    def test_exhaustion_returns_last_response_for_caller_to_raise(self):
        # Default 3 attempts of 502: the helper does NOT raise — it returns the
        # last error response so the caller's raise_for_status surfaces it
        # (with body) exactly as an un-retried 502 would.
        responses = [_status_response(502) for _ in range(3)]
        post_fn = MagicMock(side_effect=responses)
        result = post_with_retry(post_fn, "http://x/llm")
        assert result is responses[-1]
        assert result.status_code == 502
        assert post_fn.call_count == 3

    def test_default_budget_is_three(self):
        # 4 available, but only 3 attempts made (default budget).
        responses = [_status_response(503) for _ in range(4)]
        post_fn = MagicMock(side_effect=responses)
        result = post_with_retry(post_fn, "http://x/llm")
        assert result.status_code == 503
        assert post_fn.call_count == 3

    def test_sleep_between_status_retries(self):
        # 3 × 502 → 2 sleeps (between attempts, not after the last).
        post_fn = MagicMock(side_effect=[_status_response(502) for _ in range(3)])
        post_with_retry(post_fn, "http://x/llm")
        assert http_mod.time.sleep.call_count == 2

    def test_status_retry_uses_default_delay(self):
        post_fn = MagicMock(side_effect=[_status_response(502), _ok_response()])
        post_with_retry(post_fn, "http://x/llm")
        http_mod.time.sleep.assert_called_once_with(http_mod._DEFAULT_DELAY)

    def test_network_and_status_use_independent_budgets(self):
        # A Timeout AND a 502 in the same call are both retried through to
        # success — the two budgets don't consume each other.
        good = _ok_response()
        post_fn = MagicMock(
            side_effect=[requests.Timeout("t"), _status_response(502), good]
        )
        result = post_with_retry(post_fn, "http://x/llm")
        assert result is good
        assert post_fn.call_count == 3

    def test_retry_statuses_param_can_disable(self):
        # Passing an empty set opts out — a 502 returns immediately.
        bad = _status_response(502)
        post_fn = MagicMock(return_value=bad)
        result = post_with_retry(post_fn, "http://x/llm", retry_statuses=frozenset())
        assert result is bad
        assert post_fn.call_count == 1

    def test_render_status_running_on_gateway_retry(self):
        post_fn = MagicMock(side_effect=[_status_response(502), _ok_response()])
        with patch("agent_cli.render.render_status") as mock_status:
            post_with_retry(post_fn, "http://x/llm")
        states = [call.args[0] for call in mock_status.call_args_list]
        assert "running" in states


def _rate_limited(status: int = 429, retry_after: str | None = None) -> MagicMock:
    """429/529 응답 — ``retry_after`` 가 있으면 그 헤더를 단다 (없으면 헤더 없음)."""
    resp = _status_response(status)
    resp.headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return resp


class TestRetryOnRateLimit:
    """429 (and Anthropic's 529) is re-sent on its own budget (8) after
    ``Retry-After`` or an exponential 2s→64s pause — v10.25.0. Before, a single
    429 from a shared gateway failed the turn and ended the run."""

    @pytest.mark.parametrize("status", [429, 529])
    def test_rate_limit_retried_then_success(self, status):
        good = _ok_response()
        post_fn = MagicMock(side_effect=[_rate_limited(status), good])
        result = post_with_retry(post_fn, "http://x/llm")
        assert result is good
        assert post_fn.call_count == 2

    def test_backoff_doubles_without_retry_after(self):
        post_fn = MagicMock(
            side_effect=[_rate_limited() for _ in range(4)] + [_ok_response()]
        )
        post_with_retry(post_fn, "http://x/llm")
        waits = [c.args[0] for c in http_mod.time.sleep.call_args_list]
        assert waits == [2.0, 4.0, 8.0, 16.0]

    def test_backoff_is_capped(self):
        post_fn = MagicMock(
            side_effect=[_rate_limited() for _ in range(7)] + [_ok_response()]
        )
        post_with_retry(post_fn, "http://x/llm")
        waits = [c.args[0] for c in http_mod.time.sleep.call_args_list]
        assert waits == [2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 64.0]

    def test_retry_after_seconds_header_wins_over_backoff(self):
        post_fn = MagicMock(
            side_effect=[_rate_limited(retry_after="17"), _ok_response()]
        )
        post_with_retry(post_fn, "http://x/llm")
        http_mod.time.sleep.assert_called_once_with(17.0)

    def test_retry_after_http_date_is_honoured(self):
        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime

        when = datetime.now(timezone.utc) + timedelta(seconds=30)
        post_fn = MagicMock(
            side_effect=[
                _rate_limited(retry_after=format_datetime(when)),
                _ok_response(),
            ]
        )
        post_with_retry(post_fn, "http://x/llm")
        (wait,) = http_mod.time.sleep.call_args.args
        assert 27.0 <= wait <= 30.0

    def test_retry_after_is_capped(self):
        post_fn = MagicMock(
            side_effect=[_rate_limited(retry_after="3600"), _ok_response()]
        )
        post_with_retry(post_fn, "http://x/llm")
        http_mod.time.sleep.assert_called_once_with(http_mod._RETRY_AFTER_CAP)

    def test_retry_after_in_the_past_waits_zero(self):
        post_fn = MagicMock(
            side_effect=[
                _rate_limited(retry_after="Wed, 21 Oct 2015 07:28:00 GMT"),
                _ok_response(),
            ]
        )
        post_with_retry(post_fn, "http://x/llm")
        http_mod.time.sleep.assert_called_once_with(0.0)

    def test_unreadable_retry_after_falls_back_to_backoff(self):
        post_fn = MagicMock(
            side_effect=[_rate_limited(retry_after="soon"), _ok_response()]
        )
        post_with_retry(post_fn, "http://x/llm")
        http_mod.time.sleep.assert_called_once_with(http_mod._RATE_LIMIT_BASE_DELAY)

    def test_error_response_closed_before_resend(self):
        bad = _rate_limited()
        post_fn = MagicMock(side_effect=[bad, _ok_response()])
        post_with_retry(post_fn, "http://x/llm")
        bad.close.assert_called_once()

    def test_exhaustion_returns_last_response_for_caller_to_raise(self):
        responses = [_rate_limited() for _ in range(http_mod._RATE_LIMIT_ATTEMPTS + 1)]
        post_fn = MagicMock(side_effect=responses)
        result = post_with_retry(post_fn, "http://x/llm")
        assert result is responses[http_mod._RATE_LIMIT_ATTEMPTS - 1]
        assert post_fn.call_count == http_mod._RATE_LIMIT_ATTEMPTS
        # 마지막 시도 뒤에는 기다리지 않는다.
        assert http_mod.time.sleep.call_count == http_mod._RATE_LIMIT_ATTEMPTS - 1

    def test_rate_limit_and_gateway_budgets_are_independent(self):
        # 3 × 502 would exhaust the 5xx budget; a 429 between them must not
        # count against it, and vice versa.
        good = _ok_response()
        post_fn = MagicMock(
            side_effect=[
                _status_response(502),
                _rate_limited(),
                _status_response(502),
                _rate_limited(),
                good,
            ]
        )
        result = post_with_retry(post_fn, "http://x/llm")
        assert result is good
        assert post_fn.call_count == 5

    def test_disabling_gateway_retry_keeps_rate_limit_retry(self):
        good = _ok_response()
        post_fn = MagicMock(side_effect=[_rate_limited(), good])
        result = post_with_retry(post_fn, "http://x/llm", retry_statuses=frozenset())
        assert result is good

    def test_render_status_names_the_wait(self):
        post_fn = MagicMock(
            side_effect=[_rate_limited(retry_after="5"), _ok_response()]
        )
        with patch("agent_cli.render.render_status") as mock_status:
            post_with_retry(post_fn, "http://x/llm")
        (state, text) = mock_status.call_args.args
        assert state == "running"
        assert "429" in text and "5s" in text and "2/8" in text

    def test_other_4xx_still_not_retried(self):
        bad = _status_response(401)
        post_fn = MagicMock(return_value=bad)
        assert post_with_retry(post_fn, "http://x/llm") is bad
        assert post_fn.call_count == 1


class TestExhaustion:
    # Pin attempts=3 so these exercise the exhaustion MECHANISM independent of
    # the default (10) — the default is covered separately below.
    def test_all_attempts_fail_raises_last(self, monkeypatch):
        monkeypatch.setattr(http_mod, "_DEFAULT_ATTEMPTS", 3)
        post_fn = MagicMock(
            side_effect=[
                requests.Timeout("t1"),
                requests.Timeout("t2"),
                requests.Timeout("t3-last"),
            ]
        )
        with pytest.raises(requests.Timeout, match="t3-last"):
            post_with_retry(post_fn, "http://x/llm")
        assert post_fn.call_count == 3

    def test_mixed_exceptions_still_raises_last(self, monkeypatch):
        monkeypatch.setattr(http_mod, "_DEFAULT_ATTEMPTS", 3)
        post_fn = MagicMock(
            side_effect=[
                requests.ConnectionError("c1"),
                requests.Timeout("t2"),
                requests.ConnectionError("c3-last"),
            ]
        )
        with pytest.raises(requests.ConnectionError, match="c3-last"):
            post_with_retry(post_fn, "http://x/llm")
        assert post_fn.call_count == 3


class TestNonRetryable:
    def test_http_error_not_retried(self):
        """HTTPError comes from response.raise_for_status() AFTER post_fn
        returns; the helper never sees it. But directly raising HTTPError
        from post_fn should also bypass retry since we only catch
        Timeout/ConnectionError."""
        post_fn = MagicMock(side_effect=requests.HTTPError("500 server error"))
        with pytest.raises(requests.HTTPError, match="500 server error"):
            post_with_retry(post_fn, "http://x/llm")
        assert post_fn.call_count == 1

    def test_value_error_propagates_immediately(self):
        post_fn = MagicMock(side_effect=ValueError("bad arg"))
        with pytest.raises(ValueError, match="bad arg"):
            post_with_retry(post_fn, "http://x/llm")
        assert post_fn.call_count == 1


class TestSleepBehavior:
    def test_sleep_called_only_between_attempts(self):
        """N attempts means N-1 sleeps (no sleep after the final attempt)."""
        post_fn = MagicMock(
            side_effect=[requests.Timeout("t1"), requests.Timeout("t2"), _ok_response()]
        )
        post_with_retry(post_fn, "http://x/llm")
        assert http_mod.time.sleep.call_count == 2

    def test_no_sleep_on_first_success(self):
        post_fn = MagicMock(return_value=_ok_response())
        post_with_retry(post_fn, "http://x/llm")
        assert http_mod.time.sleep.call_count == 0

    def test_no_sleep_after_final_exhausted_attempt(self, monkeypatch):
        monkeypatch.setattr(http_mod, "_DEFAULT_ATTEMPTS", 3)
        post_fn = MagicMock(
            side_effect=[
                requests.Timeout("t1"),
                requests.Timeout("t2"),
                requests.Timeout("t3"),
            ]
        )
        with pytest.raises(requests.Timeout):
            post_with_retry(post_fn, "http://x/llm")
        # 3 attempts → 2 sleeps only (between attempts, not after last).
        assert http_mod.time.sleep.call_count == 2


class TestUserVisibility:
    def test_render_status_called_on_retry(self):
        post_fn = MagicMock(side_effect=[requests.Timeout("t1"), _ok_response()])
        with patch("agent_cli.render.render_status") as mock_status:
            post_with_retry(post_fn, "http://x/llm")
        # At least one "running" status for the retry announcement.
        states = [call.args[0] for call in mock_status.call_args_list]
        assert "running" in states

    def test_render_status_error_on_exhaustion(self, monkeypatch):
        monkeypatch.setattr(http_mod, "_DEFAULT_ATTEMPTS", 3)
        post_fn = MagicMock(
            side_effect=[
                requests.Timeout("t1"),
                requests.Timeout("t2"),
                requests.Timeout("t3"),
            ]
        )
        with (
            patch("agent_cli.render.render_status") as mock_status,
            pytest.raises(requests.Timeout),
        ):
            post_with_retry(post_fn, "http://x/llm")
        states = [call.args[0] for call in mock_status.call_args_list]
        assert "error" in states


class TestProviderWiring:
    """Smoke tests: each provider routes its requests.post call through
    post_with_retry, so a Timeout from the first call triggers a retry."""

    def test_anthropic_retries_on_timeout(self, monkeypatch):
        from agent_cli.providers.anthropic import AnthropicProvider
        from agent_cli.providers.capabilities import ModelCapabilities

        caps = ModelCapabilities(
            context_window=4096,
            max_output_tokens=1024,
            supports_thinking=False,
        )
        good = MagicMock()
        good.status_code = 200
        good.raise_for_status.return_value = None
        good.json.return_value = {
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "stop_reason": "end_turn",
        }
        with patch(
            "agent_cli.providers.anthropic.requests.post",
            side_effect=[requests.Timeout("t1"), good],
        ) as mock_post:
            provider = AnthropicProvider(
                "https://api.anthropic.com/v1", Credentials(api_key="test-key")
            )
            resp = provider.call(messages=[], system="", model="m", capabilities=caps)
            assert resp.content == "ok"
            assert mock_post.call_count == 2

    def test_openai_retries_on_connection_error(self, monkeypatch):
        from agent_cli.providers.capabilities import ModelCapabilities
        from agent_cli.providers.openai import OpenAIProvider

        caps = ModelCapabilities(
            context_window=4096,
            max_output_tokens=1024,
            supports_thinking=False,
        )
        good = MagicMock()
        good.status_code = 200
        good.raise_for_status.return_value = None
        good.json.return_value = {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        with patch(
            "agent_cli.providers.openai.requests.post",
            side_effect=[requests.ConnectionError("refused"), good],
        ) as mock_post:
            provider = OpenAIProvider(
                "https://api.openai.com/v1", Credentials(api_key="test-key")
            )
            resp = provider.call(messages=[], system="", model="m", capabilities=caps)
            assert resp.content == "ok"
            assert mock_post.call_count == 2

    def test_openai_retries_on_timeout(self, monkeypatch):
        from agent_cli.providers.capabilities import ModelCapabilities
        from agent_cli.providers.openai import OpenAIProvider

        caps = ModelCapabilities(
            context_window=4096,
            max_output_tokens=1024,
            supports_thinking=False,
        )
        good = MagicMock()
        good.status_code = 200
        good.raise_for_status.return_value = None
        good.json.return_value = {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        with patch(
            "agent_cli.providers.openai.requests.post",
            side_effect=[requests.Timeout("t1"), good],
        ) as mock_post:
            provider = OpenAIProvider(
                "https://api.openai.com/v1", Credentials(api_key="test-key")
            )
            resp = provider.call(messages=[], system="", model="m", capabilities=caps)
            assert resp.content == "ok"
            assert mock_post.call_count == 2

    def test_anthropic_retries_on_gateway_502(self):
        """Default retry_statuses reaches the provider: a 502 then success is
        retried without the caller opting in."""
        from agent_cli.providers.anthropic import AnthropicProvider
        from agent_cli.providers.capabilities import ModelCapabilities

        caps = ModelCapabilities(
            context_window=4096,
            max_output_tokens=1024,
            supports_thinking=False,
        )
        bad = MagicMock()
        bad.status_code = 502
        good = MagicMock()
        good.status_code = 200
        good.raise_for_status.return_value = None
        good.json.return_value = {
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "stop_reason": "end_turn",
        }
        with patch(
            "agent_cli.providers.anthropic.requests.post",
            side_effect=[bad, good],
        ) as mock_post:
            provider = AnthropicProvider(
                "https://api.anthropic.com/v1", Credentials(api_key="test-key")
            )
            resp = provider.call(messages=[], system="", model="m", capabilities=caps)
            assert resp.content == "ok"
            assert mock_post.call_count == 2
            bad.close.assert_called_once()

    def test_openai_waits_out_a_gateway_429(self):
        """A LiteLLM-style 429 with Retry-After on the OpenAI path is waited
        out and re-sent — the turn no longer fails on the first rate limit."""
        from agent_cli.providers.capabilities import ModelCapabilities
        from agent_cli.providers.openai import OpenAIProvider

        caps = ModelCapabilities(
            context_window=4096,
            max_output_tokens=1024,
            supports_thinking=False,
        )
        bad = MagicMock()
        bad.status_code = 429
        bad.headers = {"Retry-After": "3"}
        good = MagicMock()
        good.status_code = 200
        good.raise_for_status.return_value = None
        good.json.return_value = {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        with patch(
            "agent_cli.providers.openai.requests.post",
            side_effect=[bad, good],
        ) as mock_post:
            provider = OpenAIProvider(
                "https://gateway.example/v1", Credentials(api_key="test-key")
            )
            resp = provider.call(messages=[], system="", model="m", capabilities=caps)
            assert resp.content == "ok"
            assert mock_post.call_count == 2
            http_mod.time.sleep.assert_called_once_with(3.0)


class TestRaiseForStatusWithBody:
    """``raise_for_status_with_body`` includes the response BODY so the loop's
    context-overflow recovery can recognise an omlx 400 — the body names the
    limit (`...exceeds max context window of N tokens`), but requests' bare
    message drops it, so the recoverable 400 hard-fails (the iter=37 symptom)."""

    def _resp(self, status, body, reason="Bad Request"):
        # The helper wraps ``raise_for_status()``, so the mock drives it via
        # that (a 200 returns None; a 4xx raises the bare requests message).
        r = MagicMock()
        r.status_code = status
        r.url = "http://127.0.0.1:8000/v1/chat/completions"
        r.text = body
        if status >= 400:
            r.raise_for_status.side_effect = requests.HTTPError(
                f"{status} Client Error: {reason} for url: {r.url}"
            )
        else:
            r.raise_for_status.return_value = None
        return r

    def test_ok_does_not_raise(self):
        http_mod.raise_for_status_with_body(self._resp(200, ""))  # no exception

    def test_400_overflow_raises_typed_error_with_amounts(self):
        # The boundary promotes a 400 overflow to a typed ContextOverflowError
        # carrying the server-reported amounts, so the loop never string-matches
        # arbitrary exceptions (AUDIT L-1). Previously this raised a generic
        # HTTPError the loop re-parsed — the false-positive source.
        from agent_cli.context.overflow import ContextOverflowError

        body = (
            '{"error": "Prompt 270000 tokens exceeds max context window of '
            '262144 tokens"}'
        )
        with pytest.raises(ContextOverflowError) as ei:
            http_mod.raise_for_status_with_body(self._resp(400, body))
        assert ei.value.actual == 270000
        assert ei.value.limit == 262144
        assert "exceeds max context window" in str(ei.value)

    def test_400_non_overflow_stays_generic_httperror(self):
        # A 400 that is NOT an overflow must remain a plain HTTPError-with-body,
        # not the typed error — so recovery is never triggered for it.
        with pytest.raises(requests.HTTPError) as ei:
            http_mod.raise_for_status_with_body(
                self._resp(400, '{"error": "missing field foo"}')
            )
        from agent_cli.context.overflow import ContextOverflowError

        assert not isinstance(ei.value, ContextOverflowError)
        assert "missing field foo" in str(ei.value)

    def test_400_without_body_falls_back_to_standard(self):
        r = self._resp(400, "")
        r.raise_for_status.side_effect = requests.HTTPError("400 Client Error")
        with pytest.raises(requests.HTTPError):
            http_mod.raise_for_status_with_body(r)

    def test_body_is_truncated(self):
        with pytest.raises(requests.HTTPError) as ei:
            http_mod.raise_for_status_with_body(
                self._resp(400, "x" * 5000), max_body=100
            )
        assert "x" * 100 in str(ei.value)
        assert "x" * 200 not in str(ei.value)  # capped


class _FakeClock:
    """``time.sleep``·``time.monotonic`` 대역 — 잠은 시계만 민다(실제로 자지 않는다)."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


class TestStopDuringRetry:
    """정지가 선 뒤의 LLM 요청 — 사용자 신고: ReadTimeout·429 재시도 도중 ■ 가 5·14분 안 풀렸다."""

    @pytest.fixture
    def clock(self, monkeypatch) -> _FakeClock:
        c = _FakeClock()
        monkeypatch.setattr(http_mod.time, "sleep", c.sleep)
        monkeypatch.setattr(http_mod.time, "monotonic", c.monotonic)
        return c

    @staticmethod
    def _call(post, stop: list[bool]):
        from agent_cli.providers.capabilities import ModelCapabilities
        from agent_cli.providers.openai import OpenAIProvider

        caps = ModelCapabilities(
            context_window=4096, max_output_tokens=256, supports_thinking=False
        )
        with patch("agent_cli.providers.openai.requests.post", post):
            return OpenAIProvider("http://x/v1", Credentials()).call(
                messages=[{"role": "user", "content": "hi"}],
                system="s",
                model="m",
                capabilities=caps,
                on_chunk=lambda *a, **k: None,
                interrupt_check=lambda: stop[0],
            )

    def test_read_timeout_after_stop_sends_nothing_more(self, clock):
        stop = [False]

        def post(*a, **k):
            stop[0] = True  # 첫 요청이 응답 머리를 기다리는 동안 ■
            raise requests.ReadTimeout("no headers")

        post = MagicMock(side_effect=post)
        resp = self._call(post, stop)
        assert resp.stop_reason == "interrupted"
        assert resp.content == ""
        assert post.call_count == 1
        assert clock.slept == []  # 쉼이 첫 폴링 전에 끝났다

    def test_rate_limit_after_stop_stops_waiting(self, clock):
        stop = [False]
        limited = _status_response(429)
        limited.headers = {"Retry-After": "120"}
        limited.raise_for_status.side_effect = requests.HTTPError("429")

        def post(*a, **k):
            stop[0] = True
            return limited

        post = MagicMock(side_effect=post)
        resp = self._call(post, stop)
        assert resp.stop_reason == "interrupted"
        assert post.call_count == 1
        assert clock.slept == []

    def test_broken_stream_after_stop_does_not_resend(self, clock):
        from agent_cli.providers.openai import OpenAIProvider

        stop = [False]

        def broken(*a, **k):
            stop[0] = True  # 스트림이 끊긴 순간과 ■ 가 겹쳤다
            raise requests.exceptions.ChunkedEncodingError("eof")

        post = MagicMock(return_value=_ok_response())
        with (
            patch("agent_cli.providers.http.make_stream_patient"),
            patch.object(OpenAIProvider, "_handle_stream", side_effect=broken),
        ):
            resp = self._call(post, stop)
        assert resp.stop_reason == "interrupted"
        assert post.call_count == 1
        assert clock.slept == []

    def test_stop_before_the_call_sends_nothing(self, clock):
        from agent_cli.providers.base import LLMResponse
        from agent_cli.providers.openai import OpenAIProvider

        post = MagicMock(return_value=_ok_response())
        interrupted = LLMResponse(content="", stop_reason="interrupted")
        with (
            patch("agent_cli.providers.http.make_stream_patient"),
            patch.object(OpenAIProvider, "_handle_stream", return_value=interrupted),
        ):
            resp = self._call(post, [True])
        assert resp.stop_reason == "interrupted"
        assert post.call_count == 0

    def test_without_stop_retries_and_waits_are_unchanged(self, clock, capsys):
        """정지가 서지 않으면 재시도 횟수·간격·화면 줄이 그대로다."""
        post = MagicMock(side_effect=requests.ReadTimeout("no headers"))
        with pytest.raises(requests.ReadTimeout):
            self._call(post, [False])
        assert post.call_count == 10
        assert sum(clock.slept) == pytest.approx(9 * http_mod._DEFAULT_DELAY)
        out = capsys.readouterr().out
        assert [ln.strip() for ln in out.splitlines() if ln.strip()] == [
            f"● LLM request failed (ReadTimeout) — retrying ({n}/10)"
            for n in range(2, 11)
        ] + ["● LLM request failed after 10 attempts: ReadTimeout"]

    def test_stop_during_a_wait_ends_it_at_the_next_poll(self, clock):
        """쉼 도중 ■ — 다음 폴링(``_INTERRUPT_POLL_SECONDS``)에서 끝나고 더 보내지 않는다."""
        stop = [False]
        limited = _status_response(429)
        limited.headers = {"Retry-After": "120"}
        post = MagicMock(return_value=limited)

        def sleep(seconds):
            clock.slept.append(seconds)
            clock.now += seconds
            stop[0] = True  # 첫 조각을 자는 동안 ■

        with patch.object(http_mod.time, "sleep", sleep):
            resp = self._call(post, stop)
        assert resp.stop_reason == "interrupted"
        assert post.call_count == 1
        assert clock.slept == [http_mod._INTERRUPT_POLL_SECONDS]

    def test_post_with_retry_returns_none_when_stopped(self, clock):
        post = MagicMock(side_effect=requests.ConnectionError("refused"))
        stop = [False]

        def failing(*a, **k):
            stop[0] = True
            return post(*a, **k)

        assert (
            post_with_retry(failing, "http://x", interrupt_check=lambda: stop[0])
            is None
        )
        assert post.call_count == 1
