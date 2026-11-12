"""명령 표 하나 + `run` 의 부트 전 명령 (v10.127.1).

종전엔 `run "/help"` → ``Unknown command: /help`` + ``Type /help for available commands``.
목록 명령(`/skills`·`@agents`·`@`)도 provider 부트와 세션 생성을 거쳐 `sessions` 에 빈 세션을
남겼다. 웹 도움말은 손으로 든 문자열이었다 — 이제 둘 다 `command_table.COMMANDS` 에서 나온다.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

import agent_cli.main as m
from agent_cli.command_table import COMMANDS, RUN, WEB, help_text

#: 표로 옮기기 전 웹 도움말 원문 — 웹 표면은 바뀌지 않는다
_WEB_HELP_BEFORE = (
    "Web mode commands:\n"
    "  /help                    Show this help\n"
    "  /compact                 Compact context now (summarise oldest half)\n"
    "  /sh <command>            Run a shell command directly (LLM bypass)\n"
    "  /skills                  List available skills\n"
    "  /<skill> <args>          Invoke a skill directly\n"
    "  @agents                  List available agents\n"
    "  @<agent> <task>          Delegate a task to an agent\n"
    "\n"
    "Any other input goes to the LLM as a chat turn."
)


def test_web_help_is_unchanged():
    from agent_cli.web.slash import _WEB_HELP_TEXT

    assert help_text(WEB) == _WEB_HELP_BEFORE
    assert _WEB_HELP_TEXT == _WEB_HELP_BEFORE


def test_run_help_lists_every_command_and_marks_web_only():
    text = help_text(RUN)
    for cmd in COMMANDS:
        assert cmd.usage in text
        line = next(ln for ln in text.splitlines() if ln.strip().startswith(cmd.usage))
        assert ("web only" in line) == (RUN not in cmd.surfaces)
    assert "/compact" in text and "web only" in text


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("AGENT_CLI_SESSIONS_DIR", raising=False)
    monkeypatch.setenv("AGENT_CLI_NO_INPUT", "1")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _no_boot(*a, **k):
    raise AssertionError("listing commands must not boot the provider")


def _sessions(root):
    d = root / ".agent-cli" / "sessions"
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


@pytest.mark.parametrize("query", ["/help", "/skills", "@agents", "@"])
def test_listing_commands_answer_before_boot(isolated, monkeypatch, query):
    monkeypatch.setattr(m, "_bootstrap_provider", _no_boot)
    before = _sessions(isolated)
    r = CliRunner().invoke(m.app, ["run", query])
    assert r.exit_code == 0, r.output
    assert _sessions(isolated) == before == []
    assert "Unknown command" not in r.output


def test_help_output_contains_every_run_command(isolated, monkeypatch):
    monkeypatch.setattr(m, "_bootstrap_provider", _no_boot)
    r = CliRunner().invoke(m.app, ["run", "/help"])
    for cmd in COMMANDS:
        assert cmd.usage in r.output


def test_listings_with_resume_take_the_old_path(isolated, monkeypatch):
    """--resume 이면 세션이 이미 있고, 살아난 상주 에이전트가 목록에 들어가야 한다 —
    목록은 부트 뒤 펌프 라우팅으로 (resume 은 없는 id 라 그 전에 1 로 끝난다)."""
    monkeypatch.setattr(m, "_bootstrap_provider", _no_boot)
    for query in ("/skills", "@agents", "@"):
        r = CliRunner().invoke(m.app, ["run", query, "--resume", "nope"])
        assert r.exit_code == 1, r.output
        assert "Session 'nope' not found" in r.output
    # /help 는 세션에 기대는 것이 없어 --resume 이어도 부트 전에 답한다
    r = CliRunner().invoke(m.app, ["run", "/help", "--resume", "nope"])
    assert r.exit_code == 0, r.output


def test_unknown_command_points_at_run_help(monkeypatch):
    from rich.console import Console

    con = Console(width=80, record=True, color_system=None)
    monkeypatch.setattr(m, "console", con)
    m._ConsoleDispatchOutput().skill_not_found("nosuch")
    text = con.export_text()
    assert "Unknown command: /nosuch" in text
    assert "agent-cli run /help" in text
    assert "Type /help" not in text
