"""bench/harbor/agent_command.py — 명령 문자열을 harbor 와 같은 모양(``bash -c "set -o
pipefail; <명령>"``)으로 실제로 돌려, 가짜 ``agent-cli`` 의 종료 코드가 어떻게 harbor 에
보이는지 본다: 0·1 은 흡수(검증기로), 2 이상은 그대로(harbor 예외)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import agent_command

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash 필요")

_FAKE = """#!/bin/sh
echo "fake agent-cli $*"
echo "to stderr" >&2
exit "$FAKE_RC"
"""


def _run(tmp_path: Path, rc: int) -> tuple[int, Path]:
    home = tmp_path / "home"
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    fake = bin_dir / "agent-cli"
    fake.write_text(_FAKE, encoding="utf-8")
    fake.chmod(0o755)
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    cmd = agent_command.build_run_command(
        provider="openai", model="m x", flags="--max-turns 60", agent_dir=str(agent_dir)
    )
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "FAKE_RC": str(rc),
        "AGENT_CLI_TASK": "do it",
    }
    # harbor 0.22.0 `_exec` 와 docker main 서비스의 `bash -c` 그대로
    proc = subprocess.run(
        ["bash", "-c", f"set -o pipefail; {cmd}"],
        env=env,
        capture_output=True,
        check=False,
        timeout=30,
    )
    return proc.returncode, agent_dir


@pytest.mark.parametrize(("rc", "seen"), [(0, 0), (1, 0), (2, 2), (130, 130)])
def test_pipeline_status_and_exit_code_file(tmp_path, rc, seen):
    status, agent_dir = _run(tmp_path, rc)
    assert status == seen
    assert (agent_dir / "exit-code.txt").read_text(encoding="utf-8").strip() == str(rc)
    log = (agent_dir / "agent-cli.txt").read_text(encoding="utf-8")
    assert "fake agent-cli run do it -p openai -m m x --max-turns 60" in log
    assert "to stderr" in log, "stderr 도 로그로 (종전 2>&1 | tee 와 같다)"
    assert f"--result-file {agent_dir}/result.txt" in log


def test_missing_binary_is_still_a_harbor_error(tmp_path):
    """설치가 깨져 명령이 없으면(127) 종전처럼 예외 — 흡수하지 않는다."""
    status, agent_dir = _run(tmp_path, 0)
    (tmp_path / "home" / ".local" / "bin" / "agent-cli").unlink()
    cmd = agent_command.build_run_command(
        provider="openai", model="m", flags="", agent_dir=str(agent_dir)
    )
    proc = subprocess.run(
        ["bash", "-c", f"set -o pipefail; {cmd}"],
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home")},
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert status == 0 and proc.returncode == 127
    assert (agent_dir / "exit-code.txt").read_text(encoding="utf-8").strip() == "127"


def test_container_first_run_needs_no_config_or_wizard(tmp_path, monkeypatch):
    """벤치 컨테이너의 첫 실행(config.json 없음·``</dev/null``) — 어댑터가 넘기는 ``-m`` 과
    ``AGENT_CLI_BASE_URL`` 만으로 마법사 없이 지나간다 (agent-cli v10.125.0 의 "플래그 완비").
    ``_runtime_env`` 는 ``AGENT_CLI_BASE_URL`` 이 없으면 거부하므로 그 env 는 늘 있다."""
    import shlex

    import agent_cli.config as config
    import agent_cli.main as main

    argv = shlex.split(
        agent_command.build_run_command(
            provider="openai", model="some/model", flags="", agent_dir="/logs/agent"
        )
    )
    model = argv[argv.index("-m") + 1]
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        config, "_CONFIG_PATHS", [tmp_path / ".agent-cli" / "config.json"]
    )
    monkeypatch.setenv("AGENT_CLI_NO_INPUT", "1")
    monkeypatch.setenv("AGENT_CLI_BASE_URL", "http://host.docker.internal:8000/v1")
    config.reload_config()
    assert main._flags_complete(None, model) is True

    def _no_wizard():
        raise AssertionError("wizard must not run")

    monkeypatch.setattr("agent_cli.setup.SetupWizard", _no_wizard)
    main._maybe_setup(None, model)
    config.reload_config()
