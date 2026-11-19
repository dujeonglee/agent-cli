"""테스트용 MCP 서버 — 빠른 도구, N초 자는 도구, 영영 답하지 않는 도구.

``python slow_mcp_server.py <표식 디렉터리> [mute | sse | streamable-http]`` 로 뜬다. 기본은 stdio,
``mute`` 면 stdio 로 떠서 MCP 로 답하지 않는다. ``sse``·``streamable-http`` 면 127.0.0.1 의 OS 가 고른
포트(0 으로 바인드)에서 HTTP 로 듣고, 그 포트를 ``<디렉터리>/port`` 에 적는다(경로는 SDK 기본값 —
``/sse``, ``/mcp``). 시작하면 ``<디렉터리>/server.pid`` 에
자기 PID 를 적고(끝난 뒤 남지 않았는지 보려고), ``sleep`` 은 자기 시작할 때 ``sleeping-<n>`` 을,
끝까지 자면 ``slept-<n>`` 을,
취소되면 ``cancelled-<n>`` 을 남긴다(포기한 뒤 서버가 하던 일을 계속했는지 보려고).

부모(테스트 프로세스)가 사라지면 스스로 끝난다 — ``hang`` 이 돌고 있으면 stdin 이 닫혀도 서버가
끝나지 않아, 테스트가 정리 전에 죽으면(SIGKILL·시간 초과) 고아로 남았다.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from pathlib import Path

try:
    from mcp.server.fastmcp import FastMCP
except (
    ModuleNotFoundError
):  # mcp 2.x — FastMCP 가 MCPServer 로 이름이 바뀌었다(핀은 `mcp>=1.6,<3`)
    from mcp.server.mcpserver import MCPServer as FastMCP

MARKS = Path(sys.argv[1])
MARKS.mkdir(parents=True, exist_ok=True)
(MARKS / "server.pid").write_text(str(os.getpid()))
PARENT = os.getppid()


def _exit_when_orphaned() -> None:
    while os.getppid() == PARENT:
        time.sleep(0.2)
    os._exit(0)


threading.Thread(target=_exit_when_orphaned, daemon=True).start()

server = FastMCP("slow")


@server.tool()
def fast(text: str) -> str:
    """Echo the text back at once."""
    return f"fast:{text}"


@server.tool()
async def sleep(seconds: float, tag: str) -> str:
    """Sleep, then answer."""
    (MARKS / f"sleeping-{tag}").touch()
    try:
        await asyncio.sleep(seconds)
    except asyncio.CancelledError:
        (MARKS / f"cancelled-{tag}").touch()
        raise
    (MARKS / f"slept-{tag}").touch()
    return f"slept:{tag}"


@server.tool()
async def wait_for_go(tag: str) -> str:
    """``waiting-<tag>`` 를 남기고, 테스트가 ``go-<tag>`` 를 만들 때까지 기다렸다가 답한다 — 끝나는
    때를 시간이 아니라 테스트가 정한다."""
    (MARKS / f"waiting-{tag}").touch()
    while not (MARKS / f"go-{tag}").exists():
        await asyncio.sleep(0.02)
    return f"went:{tag}"


@server.tool()
async def hang(tag: str) -> str:
    """Never answer."""
    (MARKS / f"hanging-{tag}").touch()
    await asyncio.Event().wait()
    return "unreachable"


def _serve_http(transport: str) -> None:
    """``transport`` 의 ASGI 앱을 미리 바인드한 소켓으로 띄운다 — 포트를 0 으로 받아 겹치지 않는다.
    앱을 만드는 메서드 이름은 SDK 세대에 걸쳐 같다(``sse_app``·``streamable_http_app``)."""
    import socket

    import uvicorn

    app = server.sse_app() if transport == "sse" else server.streamable_http_app()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(
        128
    )  # 포트를 알리기 전에 듣는다 — 그 사이 온 연결은 backlog 에서 기다린다
    port = sock.getsockname()[1]
    config = uvicorn.Config(app, log_level="warning")
    tmp = MARKS / "port.tmp"
    tmp.write_text(str(port))
    tmp.replace(MARKS / "port")  # 다 쓴 뒤에 보이게
    asyncio.run(uvicorn.Server(config).serve(sockets=[sock]))


if __name__ == "__main__":
    mode = sys.argv[2] if len(sys.argv) > 2 else "stdio"
    if mode == "mute":
        # 떠서 stdin 은 열어 두되 아무 말도 하지 않는다 — ``initialize`` 에 답하지 않는 서버
        threading.Event().wait()
    elif mode in ("sse", "streamable-http"):
        _serve_http(mode)
    else:
        server.run()
