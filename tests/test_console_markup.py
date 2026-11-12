"""밖에서 온 값은 콘솔 마크업으로 읽히지 않는다 (v10.127.9).

`main.py` 의 콘솔 줄이 사용자·모델·파일·서버·예외에서 온 글을 마크업 f-string 에 끼워 찍어, ``[x]`` 는
사라지고 ``[/x]`` 같은 닫는 태그는 rich ``MarkupError`` 로 명령을 죽였다. 규칙 하나 — 줄 전체가 그 값이면
``style=`` + ``markup=False, highlight=False``, 서식 있는 줄에 섞이면 ``rich.markup.escape``. 값 종류마다
크래시가 나던 길 하나씩을 실제 호출로 고정한다.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

import agent_cli.main as m
from agent_cli.main import app
from agent_cli.model_check import ModelListing, ModelNotFound
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import DEFAULT_CAPABILITIES
from tests.binding_support import make_binding

_CLOSE = "[/x]"


class _Answer:
    def call(self, messages, system, model, capabilities, **kwargs):
        return LLMResponse(
            content=json.dumps(
                {"action": "complete", "result": "done", "answers": ["u-1"]}
            )
        )


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def out(monkeypatch):
    con = Console(width=200, record=True, color_system=None)
    monkeypatch.setattr(m, "console", con)
    return con


def test_session_title_from_the_flag(isolated):
    """사용자 값: ``--title`` 이 ``  Title:`` 줄에 닿는다(run; web 은 같은 모양의 줄)."""
    binding = make_binding(provider=_Answer(), capabilities=DEFAULT_CAPABILITIES)
    with (
        patch("agent_cli.main._bootstrap_provider", return_value=binding),
        patch("agent_cli.main._setup_mcp", return_value=(None, {})),
    ):
        r = CliRunner().invoke(app, ["run", "hi", "--title", f"t {_CLOSE} [b]"])
    assert r.exit_code == 0, r.output
    assert f"Title:   t {_CLOSE} [b]" in r.output


def test_sh_output_is_printed_verbatim(isolated):
    """명령 출력: ``/sh`` 의 stdout·stderr."""
    r = CliRunner().invoke(app, ["run", f"/sh echo 'a {_CLOSE}'; echo 'e [b]' >&2"])
    assert r.exit_code == 0, r.output
    assert f"a {_CLOSE}" in r.output
    assert "e [b]" in r.output


def test_exception_text_from_image_flag(isolated):
    """예외 문자열: ``--image`` 의 ValueError(없는 파일 경로가 든다)."""
    r = CliRunner().invoke(app, ["run", "hi", "--image", f"no{_CLOSE}.png"])
    assert r.exit_code == 1, r.output
    assert f"--image: no{_CLOSE}.png: no such file" in r.output


def test_agent_and_skill_results(out):
    """모델 값: ``@agent``·``/skill`` 의 결과 줄."""
    o = m._ConsoleDispatchOutput()
    o.agent_result(f"agent said {_CLOSE} [b]")
    o.skill_result("s", f"skill said {_CLOSE} [b]")
    o.skill_result("s", f"skill failed {_CLOSE}", ok=False)
    text = out.export_text()
    assert f"agent said {_CLOSE} [b]" in text
    assert f"skill said {_CLOSE} [b]" in text
    assert f"skill failed {_CLOSE}" in text


def test_names_from_files_and_input(out):
    o = m._ConsoleDispatchOutput()
    o.agent_not_found(f"a{_CLOSE}")
    o.skill_not_found(f"s{_CLOSE}")
    o.skill_result(f"s{_CLOSE}", None, ok=False)
    text = out.export_text()
    assert f"Agent not found: @a{_CLOSE}" in text
    assert f"Unknown command: /s{_CLOSE}" in text
    assert f"Skill /s{_CLOSE} stopped" in text


def test_model_names_from_the_server(out):
    """서버 값: 모델 이름(설정값·서버 목록) — 서식 있는 줄 안에 섞인 값."""
    listing = ModelListing(models=(f"m{_CLOSE}", "[b]plain"), available=True)
    err = ModelNotFound(f"cfg{_CLOSE}", listing, "http://127.0.0.1:1/v1", "--model")
    with pytest.raises(typer.Exit):
        m._choose_model_or_exit(
            listing, "http://127.0.0.1:1/v1", err, "--model", False, interactive=False
        )
    text = out.export_text()
    assert f"model  cfg{_CLOSE}" in text
    assert f"1) m{_CLOSE}" in text
    assert "2) [b]plain" in text
