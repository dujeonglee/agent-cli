"""`Credentials` — 요청 인증 헤더를 만드는 유일한 곳.

헤더 모양은 통합 전 자리들(provider ``call``·프로브 전송·``model_check``·setup)이 보내던
것과 같다. 키 값은 ``repr``·``str`` 에 나오지 않는다.
"""

from __future__ import annotations

import json

import pytest

from agent_cli.providers.credentials import (
    Credentials,
    ExtraHeaderError,
    credentials_from_config,
    validate_extra_headers,
)

_KEY = "test-key-cred-0001"
_V1 = "test-hdr-value-aaa"
_V2 = "test-hdr-value-bbb"


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
        assert repr(Credentials()) == "Credentials(api_key=, extra_headers=[])"

    def test_extra_header_values_are_masked_names_shown(self):
        c = Credentials(api_key=_KEY, extra_headers=(("x-service-id", _V1),))
        text = repr(c)
        assert _V1 not in text and _KEY not in text
        assert "x-service-id=***" in text

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


class TestExtraHeaders:
    def test_appended_after_auth_in_order(self):
        c = Credentials(
            api_key=_KEY, extra_headers=(("x-service-id", _V1), ("x-user-id", _V2))
        )
        assert list(c.headers("openai").items()) == [
            ("Authorization", f"Bearer {_KEY}"),
            ("x-service-id", _V1),
            ("x-user-id", _V2),
        ]
        assert list(c.headers("anthropic"))[-2:] == ["x-service-id", "x-user-id"]

    def test_without_extras_headers_are_unchanged(self):
        assert credentials_from_config({"api_key": _KEY}).headers(
            "openai"
        ) == Credentials(api_key=_KEY).headers("openai")

    def test_from_config_keeps_only_enabled(self):
        c = credentials_from_config(
            {
                "api_key": "ignored",
                "extra_headers": [
                    {"name": "x-service-id", "value": _V1, "enabled": True},
                    {"name": "x-user-id", "value": _V2, "enabled": False},
                    {"name": "x-default-on", "value": "v"},
                ],
            },
            api_key=_KEY,
        )
        assert c.api_key == _KEY  # 넘긴 해석값이 이긴다
        assert c.extra_headers == (("x-service-id", _V1), ("x-default-on", "v"))

    def test_from_config_api_key_none_reads_cfg(self):
        assert credentials_from_config({"api_key": _KEY}).api_key == _KEY

    def test_missing_key_is_empty_list(self):
        assert validate_extra_headers(None) == []
        assert credentials_from_config({}).extra_headers == ()

    def test_normalized_shape_keeps_disabled(self):
        got = validate_extra_headers([{"name": "a", "value": "1", "enabled": False}])
        assert got == [{"name": "a", "value": "1", "enabled": False}]

    @pytest.mark.parametrize(
        "raw",
        [
            "x-a: b",
            {"name": "x-a"},
            ["x-a"],
            [{"value": "v"}],
            [{"name": "", "value": "v"}],
            [{"name": "bad name", "value": "v"}],
            [{"name": "x:a", "value": "v"}],
            [{"name": "x-a", "value": 3}],
            [{"name": "x-a", "value": "v", "enabled": "yes"}],
        ],
    )
    def test_shape_errors(self, raw):
        with pytest.raises(ExtraHeaderError):
            validate_extra_headers(raw)

    @pytest.mark.parametrize(
        "name",
        [
            "Authorization",
            "proxy-authorization",
            "X-API-KEY",
            "anthropic-version",
            "Content-Type",
            "content-length",
            "Host",
            "transfer-encoding",
            "Connection",
        ],
    )
    def test_reserved_names_rejected(self, name):
        with pytest.raises(ExtraHeaderError, match="cannot be overridden"):
            validate_extra_headers([{"name": name, "value": _V1}])

    def test_reserved_name_rejected_even_when_disabled(self):
        with pytest.raises(ExtraHeaderError):
            validate_extra_headers(
                [{"name": "authorization", "value": _V1, "enabled": False}]
            )

    @pytest.mark.parametrize("bad", ["a\r\nX-Evil: 1", "a\nb", "a\rb", "a\0b"])
    def test_line_breaks_and_nul_rejected_without_echoing_value(self, bad):
        with pytest.raises(ExtraHeaderError) as ei:
            validate_extra_headers([{"name": "x-a", "value": _V1 + bad}])
        assert _V1 not in str(ei.value)
        assert "x-a" in str(ei.value)

    def test_duplicate_enabled_names_case_insensitive(self):
        with pytest.raises(ExtraHeaderError, match="more than once"):
            validate_extra_headers(
                [{"name": "X-A", "value": "1"}, {"name": "x-a", "value": "2"}]
            )

    def test_duplicate_allowed_when_one_is_disabled(self):
        got = validate_extra_headers(
            [
                {"name": "x-a", "value": "1", "enabled": False},
                {"name": "X-A", "value": "2"},
            ]
        )
        assert len(got) == 2

    def test_no_error_message_contains_a_value(self):
        cases = [
            [{"name": "x-a", "value": _V1}, {"name": "x-a", "value": _V2}],
            [{"name": "x-a", "value": _V1 + "\n"}],
            [{"name": "authorization", "value": _V1}],
            [{"name": "x-a", "value": _V1, "enabled": "on"}],
        ]
        for raw in cases:
            with pytest.raises(ExtraHeaderError) as ei:
                validate_extra_headers(raw)
            assert _V1 not in str(ei.value) and _V2 not in str(ei.value)


class TestConfigFlow:
    """config.json → `_resolve_provider` → `Credentials`."""

    @pytest.fixture
    def write_cfg(self, tmp_path, monkeypatch):
        from agent_cli import config as config_mod

        path = tmp_path / "config.json"
        monkeypatch.setattr(config_mod, "_CONFIG_PATHS", [path])
        for k in ("AGENT_CLI_PROVIDER", "AGENT_CLI_BASE_URL", "AGENT_CLI_API_KEY"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        def _w(data):
            path.write_text(json.dumps(data))
            config_mod.reload_config()

        yield _w
        config_mod.reload_config()

    def test_load_config_keeps_the_list(self, write_cfg):
        from agent_cli.config import load_config

        hdrs = [{"name": "x-a", "value": _V1, "enabled": True}]
        write_cfg({"base_url": "http://h/v1", "extra_headers": hdrs})
        assert load_config()["extra_headers"] == hdrs

    def test_resolve_provider_carries_enabled_headers(self, write_cfg):
        from agent_cli.main import _resolve_provider

        write_cfg(
            {
                "base_url": "http://h/v1",
                "api_key": _KEY,
                "extra_headers": [
                    {"name": "x-service-id", "value": _V1, "enabled": True},
                    {"name": "x-user-id", "value": _V2, "enabled": False},
                ],
            }
        )
        _p, _u, _m, cred = _resolve_provider(None, None, None, None)
        assert cred.headers("openai") == {
            "Authorization": f"Bearer {_KEY}",
            "x-service-id": _V1,
        }

    def test_other_provider_does_not_get_config_headers(self, write_cfg):
        """키와 같은 범위 — 설정의 provider 가 아닌 곳으로는 그 파일의 것이 안 간다."""
        from agent_cli.main import _resolve_provider

        write_cfg(
            {
                "provider": "openai",
                "base_url": "http://h/v1",
                "extra_headers": [{"name": "x-a", "value": _V1}],
            }
        )
        _p, _u, _m, cred = _resolve_provider("anthropic", None, None, None)
        assert cred.extra_headers == ()

    def test_invalid_headers_exit_2_naming_only_the_header(self, write_cfg, capsys):
        import typer

        from agent_cli.main import _resolve_provider

        write_cfg(
            {
                "base_url": "http://h/v1",
                "extra_headers": [{"name": "x-bad", "value": _V1 + "\r\nX: y"}],
            }
        )
        with pytest.raises(typer.Exit) as ei:
            _resolve_provider(None, None, None, None)
        assert ei.value.exit_code == 2
        out = capsys.readouterr()
        text = out.out + out.err
        assert "x-bad" in text
        assert _V1 not in text


class TestSetupKeepsExtraHeaders:
    def test_wizard_save_preserves_existing_extra_headers(self, tmp_path, monkeypatch):
        from unittest.mock import patch

        from agent_cli import config as config_mod
        from agent_cli.setup import SetupWizard

        monkeypatch.setattr("agent_cli.setup.user_dir", lambda: tmp_path, raising=False)
        monkeypatch.setattr("agent_cli.paths.user_dir", lambda: tmp_path)
        hdrs = [
            {"name": "x-service-id", "value": _V1, "enabled": True},
            {"name": "x-user-id", "value": _V2, "enabled": False},
        ]
        (tmp_path / "config.json").write_text(
            json.dumps({"provider": "openai", "api_key": "old", "extra_headers": hdrs})
        )
        seen = {}

        def fake_list(base_url, credentials, provider="openai"):
            seen["headers"] = credentials.headers(provider)
            return ["m1"]

        wiz = SetupWizard()
        with (
            patch("agent_cli.setup._list_models", fake_list),
            patch.object(wiz, "_welcome"),
            patch.object(wiz, "_show_existing_configs"),
            patch.object(wiz, "_select_provider", return_value="openai"),
            patch.object(
                wiz, "_configure_connection", return_value=("http://h/v1", _KEY)
            ),
            patch("agent_cli.setup.IntPrompt.ask", return_value=1),
            patch.object(wiz, "_review", return_value=True),
            patch.object(wiz, "_done"),
        ):
            wiz.run()
        saved = json.loads((tmp_path / "config.json").read_text())
        assert saved["extra_headers"] == hdrs
        assert saved["api_key"] == _KEY
        assert seen["headers"]["x-service-id"] == _V1
        assert "x-user-id" not in seen["headers"]
        config_mod.reload_config()


class TestSensitiveConfigText:
    def test_config_reason_mentions_extra_headers(self):
        from agent_cli.tools._sensitive import sensitive_reason

        assert "extra_headers" in (sensitive_reason("~/.agent-cli/config.json") or "")
        assert "extra_headers" in (
            sensitive_reason("./proj/.agent-cli/config.json") or ""
        )
