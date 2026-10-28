"""shell 시간 초과 (v10.77.0) — 명령이 띄운 것까지 끝내고, 그때까지의 출력과
background 교정을 돌려준다.

실측: `subprocess.run(shell=True, timeout=)` 은 `/bin/sh` 만 죽여서 그 아래
`apt-get` 이 dpkg 잠금을 쥔 채 살아남았다(GLM video — 다음 apt 가 실패).
"""

from __future__ import annotations

import os
import time

from agent_cli.tools.shell import ShellTool, tool_shell


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_timeout_stops_what_the_command_started(tmp_path):
    pidfile = tmp_path / "child.pid"
    r = tool_shell(
        {
            "command": f"echo started; sleep 30 & echo $! > {pidfile}; wait",
            "timeout": 1,
        }
    )
    assert not r.success
    child = int(pidfile.read_text())
    deadline = time.time() + 5
    while _alive(child) and time.time() < deadline:
        time.sleep(0.1)
    assert not _alive(child), "자식 sleep 이 살아남았다"
    assert r.error.startswith(
        "Command timed out after 1s and was stopped, together with everything it started."
    )
    assert "Output so far (last 1 lines):\nstarted" in r.error
    assert r.error.endswith(
        "send it again with `background` true and you will be told when it ends."
    )


def test_timeout_with_no_output_says_so():
    r = tool_shell({"command": "sleep 30", "timeout": 1})
    assert "It printed nothing before that." in r.error


def test_output_tail_is_bounded():
    r = tool_shell(
        {
            "command": "for i in $(seq 1 50); do echo line$i; done; sleep 30",
            "timeout": 1,
        }
    )
    assert "(last 8 lines):" in r.error
    assert "line50" in r.error and "line42" not in r.error


def test_normal_result_shape_is_unchanged():
    r = tool_shell({"command": "echo out; echo err >&2; exit 3"})
    assert r.success
    assert r.output == "out\n[stderr]\nerr\n[exit code: 3]"


def test_description_states_the_result_shape_not_task_kinds():
    d = ShellTool.description
    assert "`[stderr]`" in d and "`[exit code: N]`" in d
    assert "no `echo $?` or `2>&1`" in d
    for kind in ("test suite", "a build", "an install", "clone"):
        assert kind not in d
    bg = ShellTool.parameters["properties"]["background"]["description"]
    assert "installs" not in bg and "test suites" not in bg
