"""`Credentials` — 요청 인증 헤더를 만드는 유일한 곳.

헤더 모양은 통합 전 자리들(provider ``call``·프로브 전송·``model_check``·setup)이 보내던
것과 같다. 키 값은 ``repr``·``str`` 에 나오지 않는다.
"""

from __future__ import annotations

import pytest

from agent_cli.providers.credentials import Credentials

_KEY = "test-key-cred-0001"


class TestHeaders:
    def test_openai_with_key_is_bearer(self):
        assert Credentials(api_key=_KEY).headers("openai") == {
            "Authorization": f"Bearer {_KEY}"
        }

    def test_openai_without_key_is_empty(self):
        assert Credentials().headers("openai") == {}

    def test_unknown_provider_is_openai_shape(self):
        assert Credentials(api_key=_KEY).headers("custom") == {
            "Authorization": f"Bearer {_KEY}"
        }

    def test_anthropic_version_then_key(self):
        h = Credentials(api_key=_KEY).headers("anthropic")
        assert list(h.items()) == [
            ("anthropic-version", "2023-06-01"),
            ("x-api-key", _KEY),
        ]

    def test_anthropic_without_key_sends_only_version(self):
        assert Credentials().headers("anthropic") == {"anthropic-version": "2023-06-01"}

    def test_returns_a_fresh_dict(self):
        c = Credentials(api_key=_KEY)
        c.headers("openai")["X"] = "y"
        assert "X" not in c.headers("openai")


class TestNoValueLeak:
    @pytest.mark.parametrize("fmt", [repr, str, lambda c: f"{c}", lambda c: f"{c!r}"])
    def test_key_value_is_masked(self, fmt):
        text = fmt(Credentials(api_key=_KEY))
        assert _KEY not in text
        assert "***" in text

    def test_empty_key_shows_nothing(self):
        assert repr(Credentials()) == "Credentials(api_key=)"

    def test_frozen(self):
        with pytest.raises(AttributeError):
            Credentials().api_key = "x"  # type: ignore[misc]


class TestProvidersUseIt:
    """프로바이더·프로브 전송이 자기 헤더를 만들지 않는다 — `Credentials.headers` 를 쓴다."""

    def test_openai_call_headers(self, monkeypatch):
        from agent_cli.providers.capabilities import ModelCapabilities
        from agent_cli.providers.openai import OpenAIProvider

        seen = {}

        class _R:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": "x"}}]}

        def fake_post(url, headers=None, json=None, timeout=None):
            seen["headers"] = headers
            return _R()

        monkeypatch.setattr("agent_cli.providers.openai.requests.post", fake_post)
        OpenAIProvider("http://x/v1", Credentials(api_key=_KEY)).call(
            [{"role": "user", "content": "hi"}],
            "sys",
            "m",
            ModelCapabilities(10_000, 100, False),
        )
        assert list(seen["headers"].items()) == [
            ("Content-Type", "application/json"),
            ("Authorization", f"Bearer {_KEY}"),
        ]

    def test_probe_transport_headers(self):
        from agent_cli.providers.capabilities import (
            _AnthropicTransport,
            _OpenAITransport,
        )

        c = Credentials(api_key=_KEY)
        assert _OpenAITransport("http://x", "m", c)._auth_headers() == c.headers(
            "openai"
        )
        assert _AnthropicTransport("http://x", "m", c)._post_headers() == {
            "Content-Type": "application/json",
            **c.headers("anthropic"),
        }
