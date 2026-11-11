"""`_resolve_provider` — 사용자가 적은 값이 쓰이는가 (v10.123.1).

종전엔 config 에 ``provider`` 가 없으면 ``config_provider == ""`` 라 "같은
provider 의 설정만 쓴다" 검사에 늘 떨어졌다 — 플래그 없이 돌리면 config·env 의
``base_url``·``default_model``·``api_key`` 를 버리고 ``https://api.openai.com/v1``
로 요청이 나갔다. provider 를 안 적은 설정은 openai 의 것이다.

실행은 하지 않는다 — 반환값만 본다(플래그 없는 실제 실행은 공개 엔드포인트로 간다).
"""

from __future__ import annotations

import json

import pytest
import typer

import agent_cli.config as _config
from agent_cli.main import _resolve_provider

_U = "http://127.0.0.1:1/v1"
_M = "SENTINEL-model"
_K = "SENTINEL-key"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """임시 유저 config.json 과 깨끗한 env·models.json — 값을 쓰는 함수를 돌려준다."""
    for key in (
        "AGENT_CLI_PROVIDER",
        "AGENT_CLI_BASE_URL",
        "AGENT_CLI_API_KEY",
        "AGENT_CLI_MODEL",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / "home" / ".agent-cli" / "config.json"
    monkeypatch.setattr(_config, "_CONFIG_PATHS", [path])
    # 이 머신의 ~/.agent-cli/models.json 의 provider_defaults 가 섞이지 않게
    monkeypatch.setattr(
        _config,
        "_SEARCH_PATHS",
        [_config.Path(_config.__file__).parent / "default_models.json"],
    )
    _config.reload_config()
    _config.reload_registry()

    def write(data: dict | None) -> None:
        if data is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data), encoding="utf-8")
        _config.reload_config()

    yield write
    _config.reload_config()
    _config.reload_registry()


def test_no_flag_config_without_provider_uses_its_url_and_model(cfg):
    cfg({"base_url": _U, "default_model": _M})
    provider, url, model, _key = _resolve_provider(None, None, None, None)
    assert (provider, url, model) == ("openai", _U, _M)


def test_no_flag_env_base_url_only(cfg, monkeypatch):
    monkeypatch.setenv("AGENT_CLI_BASE_URL", _U)
    cfg(None)
    provider, url, _model, _key = _resolve_provider(None, None, None, None)
    assert (provider, url) == ("openai", _U)


def test_no_flag_config_api_key_without_provider(cfg):
    cfg({"api_key": _K})
    _provider, _url, _model, key = _resolve_provider(None, None, None, None)
    assert key == _K


def test_openai_flag_matches_config_without_provider(cfg):
    cfg({"base_url": _U})
    provider, url, _model, _key = _resolve_provider("openai", None, None, None)
    assert (provider, url) == ("openai", _U)


def test_other_provider_flag_ignores_config_url(cfg):
    """기존 규칙 유지 — 설정 값은 그 설정의 provider 에만."""
    cfg({"provider": "openai", "base_url": _U})
    provider, url, _model, _key = _resolve_provider("anthropic", None, None, None)
    assert provider == "anthropic"
    assert url == "https://api.anthropic.com/v1"


def test_config_provider_anthropic_uses_its_url(cfg):
    cfg({"provider": "anthropic", "base_url": _U})
    provider, url, _model, _key = _resolve_provider(None, None, None, None)
    assert (provider, url) == ("anthropic", _U)


def test_flags_still_win(cfg):
    cfg({"base_url": "http://127.0.0.1:2/v1", "default_model": "other"})
    _provider, url, model, key = _resolve_provider("openai", _M, _U, _K)
    assert (url, model, key) == (_U, _M, _K)


@pytest.mark.parametrize(
    ("flag", "data", "label"),
    [
        ("gemini", {"base_url": _U}, "--provider"),
        (None, {"provider": "opneai"}, "config provider"),
    ],
)
def test_unknown_provider_exits_2_with_names(cfg, capsys, flag, data, label):
    from agent_cli.render import console

    cfg(data)
    with console.capture() as cap, pytest.raises(typer.Exit) as exc:
        _resolve_provider(flag, None, None, None)
    assert exc.value.exit_code == 2
    out = cap.get()
    assert label in out
    assert "Available: anthropic, openai" in out
    assert "Traceback" not in out
