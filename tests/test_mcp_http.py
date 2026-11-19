"""원격 MCP 전송(sse·streamable-http)을 실제 HTTP 서버로 (vNEXT).

mcp 2.x 에는 ``streamablehttp_client`` 가 없다(새 이름 ``streamable_http_client`` 만, 두 값을 내준다) —
agent-cli 는 옛 이름을 import 해서 2.x 의 streamable-http 서버에는 붙지 못했다. 전송 선택의 단위
테스트(test_mcp.py)는 가짜 모듈로 돌아 SDK 세대와 무관했다 — 여기서 설치된 SDK 로 실제 서버에
연결·호출·상한 포기·해제를 본다.

가짜 서버는 ``tests/fixtures/slow_mcp_server.py`` 를 HTTP 모드로 — 127.0.0.1 의 OS 가 고른 포트.
mcp 1.6·1.7 에는 streamable-http 가 클라이언트·서버 모두 없어 그 경우만 건너뛴다.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import threading

import pytest

from agent_cli.mcp.adapter import McpTool
from agent_cli.mcp.client import McpClientManager
from agent_cli.mcp.config import McpServerConfig
from tests.test_mcp_call_limit import NO_HANG_S, SERVER, _alive, loop_tasks, wait_until

HAS_STREAMABLE_HTTP = importlib.util.find_spec("mcp.client.streamable_http") is not None

TRANSPORTS = [
    pytest.param(("sse", "/sse"), id="sse"),
    pytest.param(
        ("streamable-http", "/mcp"),
        id="streamable-http",
        marks=pytest.mark.skipif(
            not HAS_STREAMABLE_HTTP,
            reason="this mcp SDK has no streamable-http (added in 1.8)",
        ),
    ),
]


@pytest.fixture(params=TRANSPORTS)
def http_server(request, tmp_path):
    """HTTP 로 듣는 가짜 서버 하나 → ``(transport, url, marks)``. 끝나면 끝내고 사라졌는지 본다."""
    transport, path = request.param
    proc = subprocess.Popen(
        [sys.executable, str(SERVER), str(tmp_path), transport],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert wait_until(
            lambda: (tmp_path / "port").exists() or proc.poll() is not None
        )
        assert proc.poll() is None, f"서버가 뜨지 못했다 (exit {proc.returncode})"
        port = int((tmp_path / "port").read_text())
        yield transport, f"http://127.0.0.1:{port}{path}", tmp_path
    finally:
        # 테스트 서버라 바로 끊는다 — 포기한 hang 연결이 남아 있으면 uvicorn 의 정상 종료는 그것을 기다린다
        proc.kill()
        proc.wait(NO_HANG_S)
    assert not _alive(proc.pid)


def _connect(transport: str, url: str, limit_s: float = 0.5) -> McpClientManager:
    m = McpClientManager(connect_timeout_s=NO_HANG_S)
    cfg = McpServerConfig(
        name="h", url=url, transport=transport, call_timeout_s=limit_s
    )
    assert m.connect_all({"h": cfg}, warn=False) == {"h": "connected"}
    return m


def _disconnect_within_bound(m: McpClientManager) -> None:
    t = threading.Thread(target=m.disconnect_all, daemon=True)
    t.start()
    t.join(NO_HANG_S)
    assert not t.is_alive(), "disconnect_all 이 돌아오지 않았다"


def test_connect_call_give_up_and_disconnect(http_server):
    transport, url, marks = http_server
    m = _connect(transport, url)
    try:
        assert {t.name for t in m.list_tools("h")} >= {"fast", "sleep", "hang"}
        assert McpTool(m, "h", "fast", "", {}).run({"text": "x"}).output == "fast:x"
        r = McpTool(m, "h", "hang", "", {}).run({"tag": "http"})
        assert r.error == (
            "MCP mcp__h__hang did not answer within 0.5s, so the call was given up. "
            "The server may still be working on it."
        )
        assert (marks / "hanging-http").exists()
        # 포기한 뒤 같은 연결이 그대로 답한다, 떠 있는 태스크도 늘지 않는다 — HTTP 전송은 요청마다
        # 제 태스크를 잠깐 띄웠다 거두므로(SDK 안) 가라앉기를 기다려 센다
        baseline = loop_tasks(m)
        assert McpTool(m, "h", "fast", "", {}).run({"text": "y"}).output == "fast:y"
        assert wait_until(lambda: loop_tasks(m) <= baseline)
    finally:
        _disconnect_within_bound(m)
    assert m._thread is None


def test_two_threads_call_the_http_server_at_once(http_server):
    transport, url, marks = http_server
    m = _connect(transport, url, limit_s=30)
    finished: list[str] = []
    out: dict = {}

    def slow():
        out["slow"] = McpTool(m, "h", "sleep", "", {}).run(
            {"seconds": 1.0, "tag": "both"}
        )
        finished.append("slow")

    def fast():
        assert wait_until(lambda: (marks / "sleeping-both").exists())
        out["fast"] = McpTool(m, "h", "fast", "", {}).run({"text": "meanwhile"})
        finished.append("fast")

    try:
        ts = [threading.Thread(target=f, daemon=True) for f in (slow, fast)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(NO_HANG_S)
        assert out["slow"].output == "slept:both"
        assert out["fast"].output == "fast:meanwhile"
        assert finished == ["fast", "slow"]
    finally:
        _disconnect_within_bound(m)


def test_a_url_with_no_server_fails_that_connection_quickly():
    """듣는 이가 없는 포트 — 그 서버만 실패로 적히고 매니저는 그대로 내려간다."""
    import socket

    with socket.socket() as s:  # 비어 있는 포트를 하나 얻고 닫는다
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    m = McpClientManager(connect_timeout_s=NO_HANG_S)
    cfg = McpServerConfig(name="h", url=f"http://127.0.0.1:{port}/sse", transport="sse")
    try:
        status = m.connect_all({"h": cfg}, warn=False)
    finally:
        _disconnect_within_bound(m)
    assert status["h"].startswith("error:")
    assert not m.is_connected("h")
