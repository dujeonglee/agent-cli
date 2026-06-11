"""Tests for the LLM-request retry helper.

Covers post_with_retry's scope (Timeout / ConnectionError only),
attempt counting, env-var overrides, and exception propagation on
exhaustion. Also verifies the provider modules route their
requests.post calls through post_with_retry so the retry applies
uniformly.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from agent_cli.providers import http as http_mod
from agent_cli.providers.http import post_with_retry


def _ok_response() -> MagicMock:
    """Mock a successful requests.Response."""
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    return resp


@pytest.fixture(autouse=True)
def _clear_retry_env(monkeypatch):
    """Start each test with defaults; individual tests set overrides."""
    monkeypatch.delenv("AGENT_CLI_LLM_RETRY_ATTEMPTS", raising=False)
    monkeypatch.delenv("AGENT_CLI_LLM_RETRY_DELAY", raising=False)


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


class TestExhaustion:
    # Pin attempts=3 so these exercise the exhaustion MECHANISM independent of
    # the default (10) — the default is covered separately below.
    def test_all_attempts_fail_raises_last(self, monkeypatch):
        monkeypatch.setenv("AGENT_CLI_LLM_RETRY_ATTEMPTS", "3")
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
        monkeypatch.setenv("AGENT_CLI_LLM_RETRY_ATTEMPTS", "3")
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


class TestEnvOverrides:
    def test_attempts_env_var_honored(self, monkeypatch):
        monkeypatch.setenv("AGENT_CLI_LLM_RETRY_ATTEMPTS", "5")
        post_fn = MagicMock(side_effect=[requests.Timeout(f"t{i}") for i in range(5)])
        with pytest.raises(requests.Timeout):
            post_with_retry(post_fn, "http://x/llm")
        assert post_fn.call_count == 5

    def test_attempts_zero_clamped_to_one(self, monkeypatch):
        """0 must not silently skip the call — at least one attempt."""
        monkeypatch.setenv("AGENT_CLI_LLM_RETRY_ATTEMPTS", "0")
        post_fn = MagicMock(return_value=_ok_response())
        result = post_with_retry(post_fn, "http://x/llm")
        assert result.status_code == 200
        assert post_fn.call_count == 1

    def test_attempts_one_disables_retry(self, monkeypatch):
        monkeypatch.setenv("AGENT_CLI_LLM_RETRY_ATTEMPTS", "1")
        post_fn = MagicMock(side_effect=requests.Timeout("t1"))
        with pytest.raises(requests.Timeout):
            post_with_retry(post_fn, "http://x/llm")
        assert post_fn.call_count == 1

    def test_invalid_attempts_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("AGENT_CLI_LLM_RETRY_ATTEMPTS", "not-a-number")
        post_fn = MagicMock(
            side_effect=[requests.Timeout("t1"), requests.Timeout("t2"), _ok_response()]
        )
        result = post_with_retry(post_fn, "http://x/llm")
        assert result.status_code == 200
        # Invalid value → falls back to default (10), which is ≥ 3, so the
        # call succeeds on the 3rd attempt here.
        assert post_fn.call_count == 3

    def test_delay_env_var_honored(self, monkeypatch):
        monkeypatch.setenv("AGENT_CLI_LLM_RETRY_DELAY", "0.25")
        post_fn = MagicMock(side_effect=[requests.Timeout("t1"), _ok_response()])
        post_with_retry(post_fn, "http://x/llm")
        http_mod.time.sleep.assert_called_once_with(0.25)

    def test_delay_zero_allowed(self, monkeypatch):
        monkeypatch.setenv("AGENT_CLI_LLM_RETRY_DELAY", "0")
        post_fn = MagicMock(side_effect=[requests.Timeout("t1"), _ok_response()])
        post_with_retry(post_fn, "http://x/llm")
        http_mod.time.sleep.assert_called_once_with(0.0)

    def test_invalid_delay_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("AGENT_CLI_LLM_RETRY_DELAY", "not-a-float")
        post_fn = MagicMock(side_effect=[requests.Timeout("t1"), _ok_response()])
        post_with_retry(post_fn, "http://x/llm")
        http_mod.time.sleep.assert_called_once_with(1.0)


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
        monkeypatch.setenv("AGENT_CLI_LLM_RETRY_ATTEMPTS", "3")
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
        monkeypatch.setenv("AGENT_CLI_LLM_RETRY_ATTEMPTS", "3")
        post_fn = MagicMock(
            side_effect=[
                requests.Timeout("t1"),
                requests.Timeout("t2"),
                requests.Timeout("t3"),
            ]
        )
        with patch("agent_cli.render.render_status") as mock_status:
            with pytest.raises(requests.Timeout):
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
            supports_structured_output=False,
            supports_thinking=False,
            thinking_budget=0,
            supports_strict_schema=False,
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
            provider = AnthropicProvider("https://api.anthropic.com/v1", "test-key")
            resp = provider.call(messages=[], system="", model="m", capabilities=caps)
            assert resp.content == "ok"
            assert mock_post.call_count == 2

    def test_openai_retries_on_connection_error(self, monkeypatch):
        from agent_cli.providers.openai import OpenAIProvider
        from agent_cli.providers.capabilities import ModelCapabilities

        caps = ModelCapabilities(
            context_window=4096,
            max_output_tokens=1024,
            supports_structured_output=False,
            supports_thinking=False,
            thinking_budget=0,
            supports_strict_schema=False,
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
            provider = OpenAIProvider("https://api.openai.com/v1", "test-key")
            resp = provider.call(messages=[], system="", model="m", capabilities=caps)
            assert resp.content == "ok"
            assert mock_post.call_count == 2

    def test_openai_retries_on_timeout(self, monkeypatch):
        from agent_cli.providers.openai import OpenAIProvider
        from agent_cli.providers.capabilities import ModelCapabilities

        caps = ModelCapabilities(
            context_window=4096,
            max_output_tokens=1024,
            supports_structured_output=False,
            supports_thinking=False,
            thinking_budget=0,
            supports_strict_schema=False,
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
            provider = OpenAIProvider("https://api.openai.com/v1", "test-key")
            resp = provider.call(messages=[], system="", model="m", capabilities=caps)
            assert resp.content == "ok"
            assert mock_post.call_count == 2


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

    def test_400_message_includes_body_and_is_recognised_as_overflow(self):
        from agent_cli.context.overflow import is_context_overflow

        body = (
            '{"error": "Prompt 270000 tokens exceeds max context window of '
            '262144 tokens"}'
        )
        with pytest.raises(requests.HTTPError) as ei:
            http_mod.raise_for_status_with_body(self._resp(400, body))
        msg = str(ei.value)
        assert "exceeds max context window" in msg
        # the loop's reactive recovery keys on exactly this — without the body
        # it returned False and the 400 surfaced as a hard failure.
        assert is_context_overflow(msg) is True

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
