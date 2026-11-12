"""선택 의존성 설치 안내 (v10.123.3) — 빠진 패키지를 직접 대고, 대괄호가 살아남는다.

종전 안내 ``[bright_cyan]pip install agent-cli[web][/]`` 은 rich 가 ``[web]`` 을 태그로
삼켜 ``pip install agent-cli`` 만 보였고, 그 명령은 PyPI 에 패키지가 없어 실패한다.
"""

from __future__ import annotations

import sys
from importlib import metadata

import pytest
from typer.testing import CliRunner

from agent_cli import extras

_REQUIRES = [
    "typer>=0.9",
    'pytest>=7.0; extra == "dev"',
    'gnureadline>=8.0; sys_platform == "darwin" and extra == "macos"',
    'fastapi>=0.110; extra == "web"',
    'uvicorn[standard]>=0.27; extra == "web"',
    "sse-starlette>=2.0; extra == 'web'",
    'webcolors>=1; extra == "webx"',  # 이름이 접두어로 겹치는 extra 는 섞이지 않는다
]


@pytest.fixture
def fake_metadata(monkeypatch):
    monkeypatch.setattr(metadata, "requires", lambda dist: list(_REQUIRES))


@pytest.fixture
def no_metadata(monkeypatch):
    def _missing(dist):
        raise metadata.PackageNotFoundError(dist)

    monkeypatch.setattr(metadata, "requires", _missing)


def test_requirements_of_one_extra_in_metadata_order(fake_metadata):
    assert extras.extra_requirements("web") == [
        "fastapi>=0.110",
        "uvicorn[standard]>=0.27",
        "sse-starlette>=2.0",
    ]
    assert extras.extra_requirements("macos") == ["gnureadline>=8.0"]


def test_hint_names_the_packages_shell_quoted(fake_metadata):
    assert extras.install_hint("web") == (
        "Install with: pip install 'fastapi>=0.110' "
        "'uvicorn[standard]>=0.27' 'sse-starlette>=2.0'"
    )


def test_no_metadata_falls_back_to_the_extra_name(no_metadata):
    assert extras.extra_requirements("web") == []
    hint = extras.install_hint("web")
    assert '"web" extra' in hint
    assert "pip install" not in hint


def _web_without_uvicorn(monkeypatch) -> str:
    from agent_cli.main import app
    from agent_cli.render import console

    # CI 의 폭(80) 으로 고정 — 로컬 터미널이 넓으면 줄 끊김을 못 본다
    monkeypatch.setattr(console, "_width", 80)
    # None 이 든 sys.modules 항목은 import 를 ImportError 로 만든다
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    result = CliRunner().invoke(app, ["web"])
    assert result.exit_code == 1, result.output
    return result.output


def test_web_missing_dependency_prints_brackets_intact(fake_metadata, monkeypatch):
    """실제 ``console`` 로 렌더한 출력 — 대괄호가 태그로 삼켜지지 않는다."""
    out = _web_without_uvicorn(monkeypatch)
    assert "Missing optional dependency for 'web' command" in out
    assert "'uvicorn[standard]>=0.27'" in out
    # 명령은 한 줄로 — 80칸을 넘는 안내를 rich 가 끊으면 복사해 쓸 수 없다
    hint = extras.install_hint("web")
    assert len(hint) > 80
    assert hint in out
    assert "agent-cli[web]" not in out


def test_web_missing_dependency_without_metadata(no_metadata, monkeypatch):
    out = _web_without_uvicorn(monkeypatch)
    assert '"web" extra' in out
