"""테스트용 MCP 서버 (stdio) — 빠른 도구, N초 자는 도구, 영영 답하지 않는 도구.

``python slow_mcp_server.py <표식 디렉터리> [mute]`` 로 뜬다(``mute`` 면 MCP 로 답하지 않는다). 시작하면 ``<디렉터리>/server.pid`` 에
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
async def hang(tag: str) -> str:
    """Never answer."""
    (MARKS / f"hanging-{tag}").touch()
    await asyncio.Event().wait()
    return "unreachable"


if __name__ == "__main__":
    if sys.argv[2:] == ["mute"]:
        # 떠서 stdin 은 열어 두되 아무 말도 하지 않는다 — ``initialize`` 에 답하지 않는 서버
        threading.Event().wait()
    server.run()
