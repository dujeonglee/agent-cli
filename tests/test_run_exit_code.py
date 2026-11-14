"""`agent-cli run` 의 종료 코드 (v10.124.0) — 실제 run 커맨드를 가짜 provider 로 태운다.

규약: 0 답(빈 답 포함) · 1 실패/무답(LLM 실패·max turns·실패한 스킬·못 찾은 명령) ·
2 설정/사용법 · 130 중단 · ``/sh <cmd>`` 는 그 명령의 코드(시간 초과 124).
종전엔 전부 0 이라 help·README 의 "exit code 로 판별하라" 가 거짓이었다.
판정 자체(사슬·라우팅)의 경우의 수는 ``test_pump.py::TestConsoleExitCode`` 가 본다.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from agent_cli.main import app
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import DEFAULT_CAPABILITIES
from tests.binding_support import make_binding


class _Provider:
    """``call`` 마다 ``script`` 의 다음 것을 돌려준다(예외면 던진다)."""

    def __init__(self, *script):
        self._script = list(script)
        self.calls = 0

    def call(self, messages, system, model, capabilities, **kwargs):
        self.calls += 1
        step = self._script.pop(0) if self._script else self._script_last
        self._script_last = step
        if isinstance(step, BaseException):
            raise step
        return LLMResponse(content=step)


def _complete(result: str) -> str:
    # 질의는 장부의 첫 열린 항목 u-1 — 덮는다고 말해야 답에 꼬리가 안 붙는다
    return json.dumps({"action": "complete", "result": result, "answers": ["u-1"]})


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """세션·스냅샷이 cwd 에 생기므로 임시 디렉터리에서, HOME 도 임시로."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _run(provider, *args):
    binding = make_binding(provider=provider, capabilities=DEFAULT_CAPABILITIES)
    with (
        patch("agent_cli.main._bootstrap_provider", return_value=binding),
        patch("agent_cli.main._setup_mcp", return_value=(None, {})),
    ):
        return CliRunner().invoke(app, ["run", *args])


def test_answer_is_0_and_lands_in_the_result_file(isolated):
    r = _run(_Provider(_complete("done")), "hi", "--result-file", "out.txt")
    assert r.exit_code == 0, r.output
    assert (isolated / "out.txt").read_text(encoding="utf-8") == "done"


def test_llm_failure_is_1_without_a_file(isolated):
    p = _Provider(ConnectionError("connection refused"))
    r = _run(p, "hi", "--max-turns", "2", "--result-file", "out.txt")
    assert r.exit_code == 1, r.output
    assert p.calls >= 1
    assert not (isolated / "out.txt").exists()


def test_max_turns_without_an_answer_is_1(isolated):
    # 답 없는 산문만 계속 — max turns 로 끝난다
    p = _Provider(json.dumps({"action": "read_file", "path": "nope.txt"}))
    r = _run(p, "hi", "--max-turns", "1", "--result-file", "out.txt")
    assert r.exit_code == 1, r.output
    assert "max" in r.output.lower(), r.output
    assert not (isolated / "out.txt").exists()


def test_ctrl_c_is_130(isolated):
    r = _run(_Provider(KeyboardInterrupt()), "hi")
    assert r.exit_code == 130, r.output
    assert "Interrupted" in r.output


def test_unknown_command_is_1_and_listing_is_0(isolated):
    p = _Provider(_complete("unused"))
    assert _run(p, "/no-such-skill-xyz").exit_code == 1
    assert _run(p, "/skills").exit_code == 0
    assert _run(p, "@agents").exit_code == 0
    assert p.calls == 0, "라우팅 명령은 LLM 을 부르지 않는다"


@pytest.mark.parametrize(("cmd", "code"), [("true", 0), ("false", 1), ("exit 3", 3)])
def test_sh_exits_with_the_command_status(isolated, cmd, code):
    # `/sh` 는 부트 전에 끝난다 — provider 도 세션도 없다
    r = CliRunner().invoke(app, ["run", f"/sh {cmd}"])
    assert r.exit_code == code, r.output


def test_sh_timeout_is_124(isolated, monkeypatch):
    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="sleep", timeout=1)

    monkeypatch.setattr("agent_cli.main.subprocess.run", _timeout)
    r = CliRunner().invoke(app, ["run", "/sh sleep 99"])
    assert r.exit_code == 124, r.output
    assert "timed out" in r.output


def _session_dirs(root) -> list[str]:
    import agent_cli.session.store as session_mod

    d = root / session_mod._SESSIONS_DIR
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


def test_config_error_leaves_no_session_dir(isolated):
    """v10.127.4: 새 세션은 디렉터리를 차지하며 태어난다 — 그 자리는 부트 뒤라
    설정 오류(Exit 2)로 끝난 ``run`` 은 빈 세션을 남기지 않는다."""
    before = _session_dirs(isolated)
    r = CliRunner().invoke(
        app,
        ["run", "hi", "-p", "no-such-provider", "--base-url", "http://127.0.0.1:1/v1"],
    )
    assert r.exit_code == 2, r.output
    assert _session_dirs(isolated) == before


def test_two_runs_in_one_second_get_two_sessions(isolated, monkeypatch):
    import agent_cli.session.store as session_mod

    monkeypatch.setattr(session_mod.time, "time", lambda: 1_800_000_000.0)
    for _ in range(2):
        assert _run(_Provider(_complete("done")), "hi").exit_code == 0
    assert _session_dirs(isolated) == ["1800000000", "1800000001"]
