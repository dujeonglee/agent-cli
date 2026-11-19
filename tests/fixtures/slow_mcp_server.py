"""테스트용 MCP 서버 (stdio) — 빠른 도구, N초 자는 도구, 영영 답하지 않는 도구.

``python slow_mcp_server.py <표식 디렉터리>`` 로 뜬다. 시작하면 ``<디렉터리>/server.pid`` 에
자기 PID 를 적고(끝난 뒤 남지 않았는지 보려고), ``sleep`` 이 끝까지 자면 ``slept-<n>`` 을,
취소되면 ``cancelled-<n>`` 을 남긴다(포기한 뒤 서버가 하던 일을 계속했는지 보려고).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

MARKS = Path(sys.argv[1])
MARKS.mkdir(parents=True, exist_ok=True)
(MARKS / "server.pid").write_text(str(os.getpid()))

server = FastMCP("slow")


@server.tool()
def fast(text: str) -> str:
    """Echo the text back at once."""
    return f"fast:{text}"


@server.tool()
async def sleep(seconds: float, tag: str) -> str:
    """Sleep, then answer."""
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
    server.run()
