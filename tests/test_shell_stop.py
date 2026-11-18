"""런의 정지가 전경 `shell` 명령을 끝낸다 (v10.146.2).

실측(v10.146.0): 웹 방에서 "Stop everything" 을 눌러도 전경 `sleep 100; echo finished` 는 95초 뒤
끝까지 돌았고, 그동안 에이전트는 busy 였다 — 전경 실행이 `communicate(timeout=)` 로만 기다리며 정지를
보지 않았다. 이제 기다리는 사이마다 `RunContext.stopped` 를 보고, 서면 시간 초과 때처럼 프로세스
그룹을 끝낸다.

명령은 스스로는 안 끝난다(파일이 생길 때까지 도는 루프) — 시간 단언 없이 정지로만 끝나는지,
명령이 띄운 것까지 사라지는지를 조건 대기로 본다.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from pathlib import Path

from agent_cli.subagent.agent_stop import LinkedStop
from agent_cli.tools.base import RunContext
from agent_cli.tools.shell import ShellTool, tool_shell

STOPPED_HEAD = re.compile(
    r"^This run was stopped, so the command was ended after \d+s, together "
    r"with everything it started\."
)


def wait_until(pred, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return bool(pred())


def _alive(pid: int) -> bool:
    """살아 있는가 — 거둬지지 않은 좀비는 죽은 것으로 본다(컨테이너의 PID 1 은 고아를 안 거둘 수 있다)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # Linux — 신호 0 뒤에 거둬졌으면 파일이 없다(ps 가 없는 이미지도 있다)
    if Path("/proc").is_dir():
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            return stat.rsplit(")", 1)[1].split()[0] != "Z"
        except (OSError, IndexError):
            return False
    r = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    state = r.stdout.strip()
    return bool(state) and not state.startswith("Z")


def _endless(tmp: Path, *, prints: bool = True) -> str:
    """스스로는 안 끝나는 명령 — 리더(셸)와 자식(sleep)의 pid 를 적고 ``started`` 를 만든 뒤 돈다."""
    say = "echo started; " if prints else ""
    return (
        f"{say}echo $$ > {tmp}/leader.pid; sleep 300 & echo $! > {tmp}/child.pid; "
        f"touch {tmp}/started; while [ ! -f {tmp}/never ]; do sleep 0.05; done"
    )


def _stop_when_started(tmp: Path, stop: threading.Event) -> threading.Thread:
    def go():
        if wait_until(lambda: (tmp / "started").exists()):
            stop.set()

    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t


def _pids(tmp: Path) -> list[int]:
    return [int((tmp / n).read_text()) for n in ("leader.pid", "child.pid")]


def _assert_all_gone(tmp: Path) -> None:
    for pid in _pids(tmp):
        assert wait_until(lambda p=pid: not _alive(p)), f"pid {pid} 가 살아남았다"


def test_stop_ends_the_command_and_what_it_started(tmp_path):
    stop = threading.Event()
    t = _stop_when_started(tmp_path, stop)
    r = tool_shell({"command": _endless(tmp_path)}, stopped=stop.is_set)
    t.join()
    assert not r.success
    assert STOPPED_HEAD.match(r.error), r.error
    assert "Output so far (last 1 lines):\nstarted" in r.error
    assert "background" not in r.error  # 시간 문제가 아니다 — 교정 줄 없음
    assert not (tmp_path / "never").exists()
    _assert_all_gone(tmp_path)


def test_stop_with_no_output_says_so(tmp_path):
    stop = threading.Event()
    t = _stop_when_started(tmp_path, stop)
    r = tool_shell({"command": _endless(tmp_path, prints=False)}, stopped=stop.is_set)
    t.join()
    assert STOPPED_HEAD.match(r.error), r.error
    assert r.error.endswith(
        "together with everything it started. It printed nothing before that."
    )
    _assert_all_gone(tmp_path)


def test_agent_lifetime_stop_reaches_the_command_through_linked_stop(tmp_path):
    """✕·세션 종료는 부모(평생 정지)만 세운다 — 런의 `LinkedStop` 을 거쳐 명령에 닿는다."""
    lifetime = threading.Event()
    run_stop = LinkedStop(lifetime)
    t = _stop_when_started(tmp_path, lifetime)
    r = ShellTool()._run(
        {"command": _endless(tmp_path)},
        ctx=RunContext(owner="agent:agt-1", stopped=run_stop.is_set),
    )
    t.join()
    assert not run_stop._flag  # 자기 것은 서지 않았다 — 부모만
    assert STOPPED_HEAD.match(r.error), r.error
    _assert_all_gone(tmp_path)


def test_without_a_stop_the_result_is_unchanged():
    r = ShellTool()._run(
        {"command": "echo out; echo err >&2; exit 3"},
        ctx=RunContext(stopped=lambda: False),
    )
    assert r.success
    assert r.output == "out\n[stderr]\nerr\n[exit code: 3]"
    assert tool_shell({"command": "echo hi"}).output == "hi"


def test_bridge_context_reads_the_current_run_stop():
    """main 은 런마다 새 Event 다 — 캐시된 `RunContext` 의 ``stopped`` 가 부를 때의 Event 를 본다."""
    from agent_cli.loop.state import LoopState
    from agent_cli.loop.tool_bridge import ToolBridge
    from tests.loopkit import make_config

    cfg = make_config(tools_list=["shell", "complete"])
    st = LoopState(query="q")
    bridge = ToolBridge(cfg, st, None, None)
    ctx = bridge._run_ctx()
    assert ctx.stopped() is False
    st.stop_event.set()
    assert ctx.stopped() is True
    st.stop_event = threading.Event()  # 다음 런
    assert bridge._run_ctx() is ctx  # 같은 캐시
    assert ctx.stopped() is False
    st.stop_event = None
    assert ctx.stopped() is False


def test_interrupt_while_waiting_ends_the_command_and_propagates(tmp_path):
    """기다리는 도중 끊기면(CLI 의 창 밖 Ctrl+C 가 던지는 ``KeyboardInterrupt``, SIGTERM 의
    ``Terminated``) 그룹을 끝내고 그대로 던진다 — 종전엔 자기 세션의 명령이 고아로 남았다."""
    import pytest

    def interrupted() -> bool:
        if (tmp_path / "started").exists():
            raise KeyboardInterrupt
        return False

    with pytest.raises(KeyboardInterrupt):
        tool_shell({"command": _endless(tmp_path)}, stopped=interrupted)
    _assert_all_gone(tmp_path)
