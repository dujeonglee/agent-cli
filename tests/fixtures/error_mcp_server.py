"""테스트용 MCP 서버 — 예외를 던지는 도구 하나(SDK 가 ``isError: true`` 결과로 바꾼다).

``python error_mcp_server.py`` 로 stdio 에 뜬다. 부모가 사라지면 stdin 이 닫혀 스스로 끝난다
(오래 걸리는 도구가 없다).
"""

# ``from __future__ import annotations`` 를 쓰지 않는다 — slow_mcp_server.py 와 같은 이유(FastMCP 가
# 도구 인자의 주석을 ``issubclass`` 로 본다).
try:
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as FastMCP

server = FastMCP("errors")


@server.tool()
def denied(path: str) -> str:
    """Always fails."""
    raise PermissionError(f"permission denied: {path}")


if __name__ == "__main__":
    server.run()
