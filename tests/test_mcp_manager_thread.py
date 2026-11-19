"""MCP 매니저의 루프 스레드 (v10.150.0).

종전엔 부르는 스레드가 매니저의 루프를 ``run_until_complete`` 로 돌렸다:
- main 과 에이전트가 겹쳐 부르면 뒤의 것이 ``This event loop is already running``.
- 연결을 연 태스크와 닫는 태스크가 달라 anyio 가 해제를 거절했다 — 서버 프로세스가 끝나지 않았고
  (1.x), mcp 2.x 에서는 ``disconnect_all`` 이 돌아오지 않았다.

이제 매니저가 자기 스레드에서 루프를 돌리고, 서버마다 태스크 하나가 연결을 열고 닫는다. 가짜 MCP
서버(``tests/fixtures/slow_mcp_server.py``)를 실제 프로세스로 띄워 본다.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from agent_cli.mcp.adapter import McpTool
from agent_cli.mcp.client import McpClientManager
from agent_cli.mcp.config import McpServerConfig
from tests.test_mcp_call_limit import NO_HANG_S, SERVER, _alive, loop_tasks, wait_until

REPO = Path(__file__).resolve().parent.parent


def _config(name: str, marks: Path, limit_s: float = 60) -> McpServerConfig:
    return McpServerConfig(
        name=name,
        command=sys.executable,
        args=[str(SERVER), str(marks)],
        call_timeout_s=limit_s,
    )


def _pid(marks: Path) -> int:
    return int((marks / "server.pid").read_text())


@pytest.fixture
def connected(tmp_path):
    """서버 하나에 붙은 매니저 — 끝날 때 끊고, 서버가 끝났는지 본다(못 끝냈으면 거두고 실패)."""
    m = McpClientManager()
    assert m.connect_all({"s": _config("s", tmp_path)}, warn=False) == {
        "s": "connected"
    }
    pid = _pid(tmp_path)
    yield m, tmp_path
    try:
        m.disconnect_all()
        gone = wait_until(lambda: not _alive(pid))
    finally:
        if _alive(pid):
            os.kill(pid, signal.SIGTERM)
    assert gone, f"disconnect_all 뒤에도 서버 {pid} 가 살아 있었다"


def _in_thread(fn) -> tuple[threading.Thread, dict]:
    box: dict = {}

    def go():
        box["r"] = fn()

    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t, box


# ── 해제가 서버를 끝낸다 ─────────────────────────────────

_CHILD = """
import json, sys
sys.path.insert(0, sys.argv[1])
from agent_cli.mcp.adapter import McpTool
from agent_cli.mcp.client import McpClientManager
from agent_cli.mcp.config import McpServerConfig
m = McpClientManager()
cfg = McpServerConfig(name="s", command=sys.executable, args=[sys.argv[2], sys.argv[3]],
                      call_timeout_s=0.5)
status = m.connect_all({"s": cfg}, warn=False)
hang = McpTool(m, "s", "hang", "", {}).run({"tag": "child"}).error if sys.argv[4] == "hang" else ""
m.disconnect_all()
import threading
print(json.dumps({"status": status, "hang": hang,
                  "threads": [t.name for t in threading.enumerate()]}))
"""


@pytest.mark.parametrize("mode", ["idle", "hang"])
def test_disconnect_all_ends_the_server_and_the_thread_quietly(tmp_path, mode):
    """별도 프로세스에서 — 인터프리터가 끝날 때의 GC 경고("Event loop is closed"·"Task was
    destroyed but it is pending")까지 stderr 로 본다. ``hang`` 은 상한으로 포기한 호출이 서버 쪽에서
    아직 도는 채로 끊는 경우(그 서버는 stdin 이 닫혀도 스스로 끝나지 않는다)."""
    r = subprocess.run(
        [sys.executable, "-c", _CHILD, str(REPO), str(SERVER), str(tmp_path), mode],
        capture_output=True,
        text=True,
        timeout=NO_HANG_S,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["status"] == {"s": "connected"}
    if mode == "hang":
        assert "did not answer within 0.5s" in out["hang"]
    assert out["threads"] == ["MainThread"]  # 루프 스레드가 내려갔다
    assert "Event loop is closed" not in r.stderr
    assert "Task was destroyed" not in r.stderr
    pid = _pid(tmp_path)
    try:
        assert wait_until(lambda: not _alive(pid)), f"서버 {pid} 가 살아 있다"
    finally:
        if _alive(pid):
            os.kill(pid, signal.SIGTERM)


def test_disconnect_one_server_ends_that_server_only(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    m = McpClientManager()
    assert m.connect_all({"a": _config("a", a), "b": _config("b", b)}, warn=False) == {
        "a": "connected",
        "b": "connected",
    }
    pa, pb = _pid(a), _pid(b)
    try:
        m.disconnect("a")
        assert wait_until(lambda: not _alive(pa))
        assert not m.is_connected("a")
        r = McpTool(m, "b", "fast", "", {}).run({"text": "still"})
        assert r.output == "fast:still"
    finally:
        m.disconnect_all()
        for p in (pa, pb):
            if _alive(p):
                os.kill(p, signal.SIGTERM)
    assert wait_until(lambda: not _alive(pb))


# ── 동시 호출 ─────────────────────────────────────────


def test_two_threads_call_the_same_server_at_once(connected):
    """종전엔 뒤의 호출이 ``This event loop is already running`` 이었다. 이제 둘 다 답을 받고, 먼저 간
    호출이 도는 사이 뒤의 호출이 답을 받는다. 먼저 간 호출(``wait_for_go``)은 뒤의 호출이 끝난 다음
    테스트가 ``go`` 표식을 만들어야 끝난다 — 순서를 시간이 아니라 표식이 정한다."""
    m, marks = connected
    finished: list[str] = []

    def slow():
        r = McpTool(m, "s", "wait_for_go", "", {}).run({"tag": "both"})
        finished.append("slow")
        return r

    def fast():
        assert wait_until(lambda: (marks / "waiting-both").exists())
        r = McpTool(m, "s", "fast", "", {}).run({"text": "meanwhile"})
        finished.append("fast")
        (marks / "go-both").touch()
        return r

    ts, bs = _in_thread(slow)
    tf, bf = _in_thread(fast)
    ts.join(NO_HANG_S)
    tf.join(NO_HANG_S)
    assert not ts.is_alive() and not tf.is_alive()
    assert bs["r"].output == "went:both"
    assert bf["r"].output == "fast:meanwhile"
    assert finished == ["fast", "slow"]


def test_two_servers_answer_calls_from_two_threads(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    m = McpClientManager()
    m.connect_all({"a": _config("a", a), "b": _config("b", b)}, warn=False)
    pids = [_pid(a), _pid(b)]
    try:
        calls = [
            _in_thread(
                lambda n=n: McpTool(m, n, "fast", "", {}).run({"text": n}).output
            )
            for n in ("a", "b", "a", "b")
        ]
        for t, _ in calls:
            t.join(NO_HANG_S)
        assert [box["r"] for _, box in calls] == [
            "fast:a",
            "fast:b",
            "fast:a",
            "fast:b",
        ]
    finally:
        m.disconnect_all()
        for p in pids:
            if _alive(p):
                os.kill(p, signal.SIGTERM)


# ── 끊는 사이 도는 호출 ────────────────────────────────


def test_a_call_in_flight_when_everything_disconnects_ends_with_an_error(tmp_path):
    """에이전트 스레드의 호출이 도는 중에 런타임이 끝나며 ``disconnect_all`` — 그 호출은 걸리지 않고
    연결이 닫혔다는 실패로 끝난다."""
    m = McpClientManager()
    m.connect_all({"s": _config("s", tmp_path, limit_s=60)}, warn=False)
    pid = _pid(tmp_path)
    t, box = _in_thread(lambda: McpTool(m, "s", "hang", "", {}).run({"tag": "late"}))
    try:
        assert wait_until(lambda: (tmp_path / "hanging-late").exists())
        m.disconnect_all()
        t.join(NO_HANG_S)
        assert not t.is_alive(), "끊긴 뒤에도 호출이 끝나지 않았다"
        assert box["r"].error == (
            'MCP mcp__s__hang failed: MCP server "s" was disconnected'
        )
        assert wait_until(lambda: not _alive(pid))
    finally:
        if _alive(pid):
            os.kill(pid, signal.SIGTERM)


def test_after_disconnect_a_call_says_not_connected_and_starts_no_thread(connected):
    m, _ = connected
    m.disconnect_all()
    r = McpTool(m, "s", "fast", "", {}).run({"text": "x"})
    assert r.error == 'MCP mcp__s__fast failed: MCP server "s" not connected'
    assert m._thread is None


# ── 루프 스레드의 경계 ─────────────────────────────────


def test_no_thread_until_the_first_connection():
    m = McpClientManager()
    assert m.connect_all({}) == {}
    assert m._thread is None
    m.disconnect_all()


def test_the_loop_thread_cannot_wait_on_itself(connected):
    m, _ = connected

    async def nested():
        async def inner():
            return 1

        try:
            m._submit(inner())
        except RuntimeError as e:
            return str(e)
        return "no error"

    assert m._submit(nested()) == "the MCP loop thread cannot wait on itself"


def test_keyboard_interrupt_while_waiting_cancels_the_coroutine(connected):
    """기다리던 스레드가 Ctrl+C 로 빠지면 루프에 그 코루틴을 남기지 않는다. 진짜 SIGINT 를 보낸다
    — 기다림(``future.result()``)이 신호에 풀리는지가 계약의 절반이다."""
    m, _ = connected
    baseline = loop_tasks(m)
    started = threading.Event()

    async def forever():
        started.set()
        await asyncio.Event().wait()

    def interrupt_when_started():
        if started.wait(NO_HANG_S):
            os.kill(os.getpid(), signal.SIGINT)

    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        threading.Thread(target=interrupt_when_started, daemon=True).start()
        with pytest.raises(KeyboardInterrupt):
            m._submit(forever())
    finally:
        signal.signal(signal.SIGINT, previous)
    assert wait_until(lambda: loop_tasks(m) == baseline)


# ── 연결의 상한과 동시 연결 ─────────────────────────────


def test_a_server_that_never_answers_initialize_fails_at_the_limit_and_ends(tmp_path):
    """부팅이 끝없이 막히던 자리 — 상한이 **지나면** 그 연결이 실패하고(마법사가 써 온 문장), 그
    프로세스는 끝난다. 그 서버는 영영 답하지 않으니 상한이 짧아도 결과가 시간에 기대지 않는다."""
    m = McpClientManager(connect_timeout_s=0.5)
    mute_cfg = McpServerConfig(
        name="mute", command=sys.executable, args=[str(SERVER), str(tmp_path), "mute"]
    )
    try:
        t, box = _in_thread(lambda: m.connect_all({"mute": mute_cfg}, warn=False))
        t.join(NO_HANG_S)
        assert not t.is_alive(), "답하지 않는 서버에 연결이 끝나지 않았다"
        assert box["r"] == {
            "mute": "error: no response within 0.5s — the server started but does "
            "not answer initialize (check the package name and arguments)"
        }
        assert not m.is_connected("mute")
        assert wait_until(lambda: not _alive(_pid(tmp_path))), (
            "답하지 않던 서버가 남았다"
        )
    finally:
        m.disconnect_all()
        if (tmp_path / "server.pid").exists() and _alive(_pid(tmp_path)):
            os.kill(_pid(tmp_path), signal.SIGTERM)


def test_one_server_failing_does_not_stop_the_others_and_order_is_kept(tmp_path):
    """같은 부팅에서 하나가 실패해도 다른 서버는 붙고, 결과는 설정 순서다. 실패하는 쪽은 **곧바로**
    실패한다(없는 명령) — 기본 상한(45초)이라 정상 서버가 뜨는 데 걸리는 시간에 기대지 않는다."""
    m = McpClientManager()
    missing = McpServerConfig(name="missing", command=str(tmp_path / "no-such-command"))
    try:
        t, box = _in_thread(
            lambda: m.connect_all(
                {"missing": missing, "ok": _config("ok", tmp_path)}, warn=False
            )
        )
        t.join(NO_HANG_S)
        assert not t.is_alive()
        status = box["r"]
        assert list(status) == ["missing", "ok"]
        assert status["missing"].startswith("error:")
        assert status["ok"] == "connected"
        assert m.is_connected("ok") and not m.is_connected("missing")
    finally:
        m.disconnect_all()
        if (tmp_path / "server.pid").exists() and _alive(_pid(tmp_path)):
            os.kill(_pid(tmp_path), signal.SIGTERM)


def test_servers_connect_at_the_same_time():
    """``a`` 의 연결은 ``b`` 의 연결이 시작돼야 끝난다 — 차례로 연결하면 ``a`` 가 상한에 걸린다."""
    from unittest.mock import AsyncMock, patch

    b_started = asyncio.Event()

    async def fake_connect(name, config):
        if name == "a":
            await b_started.wait()
        else:
            b_started.set()

    m = McpClientManager(connect_timeout_s=5)
    cfgs = {n: McpServerConfig(name=n, command="x") for n in ("a", "b")}
    try:
        with patch.object(m, "_connect_one", new=AsyncMock(side_effect=fake_connect)):
            t, box = _in_thread(lambda: m.connect_all(cfgs, warn=False))
            t.join(NO_HANG_S)
            assert box["r"] == {
                "a": "connected",
                "b": "connected",
            }
    finally:
        m.disconnect_all()
