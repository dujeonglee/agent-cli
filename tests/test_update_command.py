"""``agent-cli update`` — GitHub release check + gh/pip self-update.

The real command shells out to ``gh`` and ``pip``; these tests mock both so
they run offline and don't touch the environment. The version-comparison and
gh-presence / up-to-date / available branches are what we cover.
"""

from __future__ import annotations

import shutil
import subprocess

from typer.testing import CliRunner

import agent_cli
from agent_cli.main import _parse_version, app

runner = CliRunner()


def test_parse_version_ordering():
    assert _parse_version("v2.0.0") == (2, 0, 0)
    assert _parse_version("2.1.0-dev") == (2, 1, 0)  # pre-release suffix dropped
    assert _parse_version("v2.0.0") < _parse_version("v2.1.0")
    assert _parse_version("v10.0.0") > _parse_version("v9.9.9")  # numeric, not lexical
    assert _parse_version("garbage") == (0,)  # never crashes


def _fake_gh(tag: str):
    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout=tag + "\n", stderr="")

    return run


def test_no_gh_cli_errors(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    res = runner.invoke(app, ["update", "--check"])
    assert res.exit_code == 1
    assert "gh cli not found" in res.output.lower()


def test_check_up_to_date(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(subprocess, "run", _fake_gh("v" + agent_cli.__version__))
    res = runner.invoke(app, ["update", "--check"])
    assert res.exit_code == 0
    assert "up to date" in res.output.lower()


def test_check_update_available(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(subprocess, "run", _fake_gh("v999.0.0"))
    res = runner.invoke(app, ["update", "--check"])
    assert res.exit_code == 0
    assert "update available" in res.output.lower()
    assert "v999.0.0" in res.output


def test_no_releases_errors(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(subprocess, "run", _fake_gh(""))  # empty tag
    res = runner.invoke(app, ["update", "--check"])
    assert res.exit_code == 1
    assert "no releases" in res.output.lower()
