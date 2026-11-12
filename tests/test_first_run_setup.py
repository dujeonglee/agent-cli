"""설정 파일 없는 첫 실행 (v10.125.0) — 마법사는 대화형이고 플래그로 다 주지 않았을 때만.

종전엔 config.json 이 없으면 `_maybe_setup` 이 무조건 마법사를 띄워, ``-p openai --base-url …
-m …`` 를 다 준 비대화식 실행(CI·컨테이너·스크립트)도 첫 질문에서 EOF → ``Aborted.`` rc=1 이었다.
"""

from __future__ import annotations

import pytest
import typer
from rich.console import Console

import agent_cli.config as _config
import agent_cli.main as m


class _Reached(Exception):
    """provider 해석 다음 단계(모델 확인)에 닿았다는 표시."""


class _WizardRan(Exception):
    pass


@pytest.fixture
def empty_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(_config, "_CONFIG_PATHS", [home / ".agent-cli" / "config.json"])
    for key in (*_config._ENV_MAP, "AGENT_CLI_MODEL", "AGENT_CLI_NO_INPUT"):
        monkeypatch.delenv(key, raising=False)
    _config.reload_config()
    yield home
    _config.reload_config()


@pytest.fixture
def wizard(monkeypatch):
    calls = []

    class _Wizard:
        def run(self):
            calls.append(1)
            raise _WizardRan

    monkeypatch.setattr("agent_cli.setup.SetupWizard", _Wizard)
    return calls


@pytest.fixture
def out(monkeypatch):
    con = Console(width=80, record=True, color_system=None)
    monkeypatch.setattr(m, "console", con)
    return con


def _interactive(monkeypatch, value: bool):
    monkeypatch.setattr("agent_cli.model_check.is_interactive", lambda: value)


def _reach(monkeypatch):
    def _stop(*a, **k):
        raise _Reached

    monkeypatch.setattr(m, "_verify_model_or_exit", _stop)


def test_noninteractive_with_flags_skips_wizard_and_reaches_resolution(
    empty_home, wizard, out, monkeypatch
):
    _interactive(monkeypatch, False)
    _reach(monkeypatch)
    with pytest.raises(_Reached):
        m._setup_provider("openai", "some-model", "http://127.0.0.1:1/v1", None)
    assert wizard == []
    assert "wizard" not in out.export_text()


def test_base_url_from_env_counts(empty_home, wizard, monkeypatch):
    # AGENT_CLI_BASE_URL 은 has_config 도 참으로 만든다 — 판정 함수 자체도 env 를 본다
    monkeypatch.setenv("AGENT_CLI_BASE_URL", "http://127.0.0.1:1/v1")
    assert m._flags_complete(None, "some-model") is True
    monkeypatch.delenv("AGENT_CLI_BASE_URL")
    assert m._flags_complete(None, "some-model") is False
    assert m._flags_complete("http://127.0.0.1:1/v1", None) is False
    assert m._flags_complete("http://127.0.0.1:1/v1", "") is False


def test_noninteractive_without_flags_exits_2_without_aborted(
    empty_home, wizard, out, monkeypatch
):
    _interactive(monkeypatch, False)
    with pytest.raises(typer.Exit) as ei:
        m._maybe_setup(None, None)
    assert ei.value.exit_code == 2
    assert wizard == []
    text = out.export_text()
    assert "Aborted" not in text
    assert "agent-cli setup" in text
    assert "--base-url" in text
    # 경로는 80칸에서도 한 덩어리 — 복사해 쓸 수 있게
    path = str(empty_home / ".agent-cli" / "config.json")
    assert path in text


def test_noninteractive_missing_model_exits_2(empty_home, wizard, out, monkeypatch):
    _interactive(monkeypatch, False)
    with pytest.raises(typer.Exit) as ei:
        m._maybe_setup("http://127.0.0.1:1/v1", None)
    assert ei.value.exit_code == 2
    assert wizard == []


def test_long_path_is_not_broken_at_80_columns(tmp_path, wizard, out, monkeypatch):
    home = tmp_path / ("h" * 90)
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(_config, "_CONFIG_PATHS", [home / ".agent-cli" / "config.json"])
    for key in (*_config._ENV_MAP, "AGENT_CLI_MODEL"):
        monkeypatch.delenv(key, raising=False)
    _interactive(monkeypatch, False)
    with pytest.raises(typer.Exit):
        m._maybe_setup(None, None)
    path = str(home / ".agent-cli" / "config.json")
    assert len(path) > 80
    assert path in out.export_text()


def test_interactive_without_flags_runs_the_wizard(empty_home, wizard, monkeypatch):
    _interactive(monkeypatch, True)
    with pytest.raises(_WizardRan):
        m._maybe_setup(None, None)
    assert wizard == [1]


def test_interactive_with_flags_does_not_ask(empty_home, wizard, monkeypatch):
    _interactive(monkeypatch, True)
    m._maybe_setup("http://127.0.0.1:1/v1", "some-model")
    assert wizard == []


def test_existing_config_never_runs_the_wizard(empty_home, wizard, monkeypatch):
    cfg = empty_home / ".agent-cli" / "config.json"
    cfg.parent.mkdir()
    cfg.write_text('{"provider": "openai"}', encoding="utf-8")
    _interactive(monkeypatch, False)
    m._maybe_setup(None, None)
    _interactive(monkeypatch, True)
    m._maybe_setup(None, None)
    assert wizard == []


def test_run_command_noninteractive_empty_home_exits_2(empty_home, wizard, monkeypatch):
    """실제 `run` — 빈 HOME·비대화식·플래그 없음 → 2, 마법사·Aborted 없음."""
    from typer.testing import CliRunner

    monkeypatch.setenv("AGENT_CLI_NO_INPUT", "1")
    monkeypatch.chdir(empty_home)
    r = CliRunner().invoke(m.app, ["run", "hi"])
    assert r.exit_code == 2, r.output
    assert wizard == []
    assert "Aborted" not in r.output
