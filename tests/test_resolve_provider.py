"""`_resolve_provider` — 사용자가 적은 값이 쓰이는가 (v10.123.1).

종전엔 config 에 ``provider`` 가 없으면 ``config_provider == ""`` 라 "같은
provider 의 설정만 쓴다" 검사에 늘 떨어졌다 — 플래그 없이 돌리면 config·env 의
``base_url``·``default_model``·``api_key`` 를 버리고 ``https://api.openai.com/v1``
로 요청이 나갔다. provider 를 안 적은 설정은 openai 의 것이다.

실행은 하지 않는다 — 반환값만 본다(플래그 없는 실제 실행은 공개 엔드포인트로 간다).
"""

from __future__ import annotations

import pytest
import typer

import agent_cli.config as _config
from agent_cli.main import _resolve_provider
from agent_cli.providers.credentials import Credentials
from tests.endpoint_files import point_config

_U = "http://127.0.0.1:1/v1"
_M = "SENTINEL-model"
_K = "SENTINEL-key"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """깨끗한 env·모델 표 — 고른 서버의 값(dict)을 그대로 돌려주는 함수를 준다.

    v10.144.0: `_resolve_provider` 는 파일을 읽지 않는다 — 고른 서버의 값(`endpoint_config`)이나
    서버 없는 실행의 env 값(`env_endpoint_config`)을 받는다."""
    for key in (
        "AGENT_CLI_PROVIDER",
        "AGENT_CLI_BASE_URL",
        "AGENT_CLI_API_KEY",
        "AGENT_CLI_MODEL",
        "AGENT_CLI_ENDPOINT",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    point_config(monkeypatch, tmp_path / "home" / ".agent-cli")

    def values(data: dict | None) -> dict:
        return dict(data or {})

    return values


def _resolve(flag, model, url, key, values):
    return _resolve_provider(flag, model, url, key, values, "server 'home'")


def test_no_flag_config_without_provider_uses_its_url_and_model(cfg):
    values = cfg({"base_url": _U, "default_model": _M})
    provider, url, model, _key = _resolve(None, None, None, None, values)
    assert (provider, url, model) == ("openai", _U, _M)


def test_no_flag_env_base_url_only(cfg, monkeypatch):
    """서버 없는 실행 — env 가 이름 없는 서버다(`env_endpoint_config`)."""
    monkeypatch.setenv("AGENT_CLI_BASE_URL", _U)
    provider, url, _model, _key = _resolve(
        None, None, None, None, _config.env_endpoint_config()
    )
    assert (provider, url) == ("openai", _U)


def test_no_flag_config_api_key_without_provider(cfg):
    values = cfg({"base_url": _U, "api_key": _K})
    _provider, _url, _model, key = _resolve(None, None, None, None, values)
    assert key == Credentials(api_key=_K)


def test_openai_flag_matches_config_without_provider(cfg):
    values = cfg({"base_url": _U})
    provider, url, _model, _key = _resolve("openai", None, None, None, values)
    assert (provider, url) == ("openai", _U)


def test_other_provider_flag_ignores_config_url(cfg, capsys):
    """서버 값은 그 서버의 provider 에만 — 다른 provider 를 고르면 주소를 직접 적어야 한다.

    v10.144.0: 종전엔 프로바이더 공개 주소(``https://api.anthropic.com/v1``)로 갔다 — 주소는
    사용자가 적은 것만 쓴다."""
    values = cfg({"provider": "openai", "base_url": _U})
    with pytest.raises(typer.Exit) as ei:
        _resolve("anthropic", None, None, None, values)
    assert ei.value.exit_code == 2
    out = capsys.readouterr()
    assert "--provider anthropic differs" in out.out + out.err
    provider, url, _model, _key = _resolve(
        "anthropic", None, "http://127.0.0.1:3/v1", None, values
    )
    assert (provider, url) == ("anthropic", "http://127.0.0.1:3/v1")


def test_config_provider_anthropic_uses_its_url(cfg):
    values = cfg({"provider": "anthropic", "base_url": _U})
    provider, url, _model, _key = _resolve(None, None, None, None, values)
    assert (provider, url) == ("anthropic", _U)


def test_flags_still_win(cfg):
    values = cfg({"base_url": "http://127.0.0.1:2/v1", "default_model": "other"})
    _provider, url, model, key = _resolve("openai", _M, _U, _K, values)
    assert (url, model, key) == (_U, _M, Credentials(api_key=_K))


@pytest.mark.parametrize(
    ("flag", "data", "label"),
    [
        ("gemini", {"base_url": _U}, "--provider"),
        (None, {"provider": "opneai"}, "config provider"),
    ],
)
def test_unknown_provider_exits_2_with_names(cfg, capsys, flag, data, label):
    from agent_cli.render import console

    values = cfg(data)
    with console.capture() as cap, pytest.raises(typer.Exit) as exc:
        _resolve(flag, None, None, None, values)
    assert exc.value.exit_code == 2
    out = cap.get()
    assert label in out
    assert "Available: anthropic, openai" in out
    assert "Traceback" not in out


# ── 깨진 config.json 은 멈춘다 (v10.123.4) ────────────────────────


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ('{"base_url": "http://127.0.0.1:1/v1",}', "line 1"),
        ('["openai"]', "expected a JSON object, got list"),
    ],
)
def test_unreadable_config_raises_with_path(cfg, text, needle):
    path = _config._CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    with pytest.raises(_config.ConfigError) as exc:
        _config.list_endpoints()
    assert str(path) in str(exc.value)
    assert needle in str(exc.value)


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ('{\n  "base_url": "http://127.0.0.1:1/v1",\n}', "line 3"),
        ("[1, 2]", "expected a JSON object, got list"),
    ],
)
def test_run_stops_with_exit_2_on_unreadable_config(
    cfg, tmp_path, monkeypatch, text, needle
):
    """계속 가면 기본값(공개 엔드포인트)이 된다 — 한 줄 오류 + Exit(2), traceback 없음.

    경로는 80칸을 넘게, 콘솔 폭은 CI 와 같은 80 — 경로가 한 덩어리로 나와야 복사해 쓸 수
    있다(rich 가 끊으면 "c onfig.json"; CI 에서 실측)."""
    from typer.testing import CliRunner

    from agent_cli.main import app
    from agent_cli.render import console

    path = tmp_path / ("d" * 40) / ("e" * 40) / ".agent-cli" / "config.json"
    monkeypatch.setattr(_config, "_CONFIG_PATH", path)
    monkeypatch.setattr(console, "_width", 80)
    assert len(str(path)) > 80
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    r = CliRunner().invoke(
        app, ["run", "hi", "-p", "openai", "--base-url", "http://127.0.0.1:1/v1"]
    )
    assert r.exit_code == 2, r.output
    assert "Could not read config" in r.output
    assert str(path) in r.output
    assert needle in r.output
    assert r.exception is None or isinstance(r.exception, SystemExit)
