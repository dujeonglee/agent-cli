"""MCP client manager — connects to MCP servers and executes tools.

Uses the mcp Python SDK for stdio and SSE transports.
Provides sync wrappers around async MCP client operations.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from typing import Any

from agent_cli.mcp.config import McpServerConfig


@dataclass
class McpToolInfo:
    """Metadata for an MCP tool."""

    server: str
    name: str
    description: str
    input_schema: dict


@dataclass
class McpResourceInfo:
    """Metadata for an MCP resource."""

    server: str
    uri: str
    name: str
    description: str


def _leaf_error(e: BaseException) -> str:
    """예외를 사람이 읽을 원인 문장으로 (v9.1.0).

    mcp SDK 는 전송 오류를 anyio TaskGroup 의 ``ExceptionGroup`` 으로 감싼다 —
    그대로 ``str()`` 하면 "unhandled errors in a TaskGroup (1 sub-exception)"
    이라 **진짜 원인(connection refused, ENOENT)이 묻힌다**. 실장 검증에서
    잡힌 것. 그룹은 잎까지 풀고, 잎이 여럿이면 세미콜론으로 잇는다."""
    leaves: list[BaseException] = []

    def _walk(x: BaseException) -> None:
        subs = getattr(x, "exceptions", None)  # ExceptionGroup (3.11+) / anyio
        if subs:
            for sub in subs:
                _walk(sub)
        else:
            leaves.append(x)

    _walk(e)
    parts = []
    for leaf in leaves:
        text = str(leaf).strip() or type(leaf).__name__
        if text not in parts:
            parts.append(text)
    return "; ".join(parts) if parts else type(e).__name__


def humanize_error(msg: str, cfg: McpServerConfig | None = None) -> str:
    """연결 실패 원문 → 다음 행동이 보이는 한 문장 (v9.2.0, 마법사에서 이관).
    CLI 마법사와 웹 🔌 칩이 **같은 오류를 같은 말로** 하도록 한 곳에 둔다."""
    text = msg.removeprefix("error: ").strip()
    low = text.lower()
    cmd = cfg.command if cfg else ""
    url = cfg.url if cfg else ""
    if "no such file" in low or "errno 2" in low:
        return (
            f"실행 파일을 찾을 수 없습니다: {cmd}"
            if cmd
            else "실행 파일을 찾을 수 없습니다"
        )
    if (
        "connection refused" in low
        or "connect call failed" in low
        or "all connection attempts failed" in low  # httpx (sse)
    ):
        return f"연결 거부 — {url} 에 서버가 없습니다" if url else "연결 거부"
    if "no module named 'mcp'" in low:
        return "mcp SDK 가 없습니다 (pip install mcp)"
    # 전송 세대 불일치 (v9.3.0) — 사용자 제보로 드러난 공백. 구 sse 로 신규
    # Streamable HTTP 서버에 붙으면 서버가 Accept 헤더 부족으로 거절하는데,
    # 원문은 "400 Bad Request" 나 -32600 이라 **원인이 전송 방식이라는 걸
    # 알 수 없다**. 마법사를 안 쓰고 손편집한 경우 이게 유일한 단서다.
    # anyio cancel scope 취소 — 서버가 이 전송 방식의 요청에 응답하지 않아
    # transport 가 대기 중인 initialize 를 걷어낸 것. 원문("Cancelled via
    # cancel scope 0x…")은 사용자에게 아무 정보도 아니다.
    if "cancel scope" in low or low == "cancellederror":
        return "서버가 이 전송 방식에 응답하지 않습니다"
    if _looks_like_wrong_transport(low, cfg):
        return (
            "전송 방식이 다릅니다 — 이 서버는 Streamable HTTP 입니다. "
            'mcp.json 의 transport 를 "streamable-http" 로 바꾸거나 '
            "`agent-cli mcp add` 로 다시 등록하세요 (자동 판별)"
        )
    return text or "알 수 없는 오류"


def _looks_like_wrong_transport(low: str, cfg: McpServerConfig | None) -> bool:
    """구 sse 설정으로 Streamable HTTP 서버를 친 징후인가.

    서버가 내는 말이 제각각이라 넓게 본다: -32600 / "not acceptable" /
    "text/event-stream" 언급 / 맨 400. 단 **sse 설정일 때만** — streamable
    설정에서 난 400 은 다른 문제다(잘못된 경로 등)."""
    if cfg is None or not cfg.is_sse:
        return False
    return (
        "-32600" in low
        or "not acceptable" in low
        or "text/event-stream" in low
        or "400 bad request" in low
    )


class McpClientManager:
    """Manages connections to MCP servers.

    Each server gets its own client session. Tools are accessed via
    {server_name}.{tool_name} namespace.
    """

    def __init__(self):
        self._clients: dict[str, Any] = {}  # server_name → (session, cleanup)
        self._tools: dict[str, list[McpToolInfo]] = {}  # server_name → tools
        self._loop: asyncio.AbstractEventLoop | None = None
        # v9.2.0: 마지막 connect_all 의 서버별 결과("connected" | "error: …")와
        # 설정 — 웹 🔌 칩(GET /api/mcp)이 부팅 시점 상태를 보여주려면 실패한
        # 서버도 기억해야 한다(종전엔 stderr 한 줄로 흘려보내고 잊었다).
        self.status: dict[str, str] = {}
        self.configs: dict[str, McpServerConfig] = {}

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        """Get or create event loop for sync wrappers."""
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
        return self._loop

    def _run_sync(self, coro):
        """Run async coroutine synchronously."""
        loop = self._get_loop()
        return loop.run_until_complete(coro)

    # ── Connection management ────────────────────────

    def connect_all(
        self, configs: dict[str, McpServerConfig], *, warn: bool = True
    ) -> dict[str, str]:
        """Connect to all configured servers.

        Returns dict of {server_name: status} where status is
        "connected" or error message.

        ``warn=False`` 면 실패를 stderr 에 찍지 않는다 (v9.3.0) — 마법사의
        전송 방식 자동 판별은 **실패를 전제로 두 번 시도**하므로, 예상된
        첫 실패에 [warn] 을 찍으면 성공한 등록에도 경고가 섞여 보인다.
        부팅 경로는 종전대로 찍는다(그때의 실패는 진짜 문제다)."""
        results = {}
        for name, config in configs.items():
            self.configs[name] = config
            try:
                self._run_sync(self._connect_one(name, config))
                results[name] = "connected"
            # CancelledError 는 BaseException 이라 ``except Exception`` 을
            # 빠져나간다 (v9.3.0 실장에서 잡힘: streamable-http 로 구 SSE
            # 엔드포인트를 치면 transport 의 anyio cancel scope 가 대기 중인
            # initialize 를 취소해 그대로 터졌다). 여기서의 취소는 **이 연결
            # 시도의 실패**이지 프로그램 중단이 아니다 — 사용자 Ctrl-C 는
            # KeyboardInterrupt 로 오므로 여전히 전파된다.
            except (Exception, asyncio.CancelledError) as e:
                msg = _leaf_error(e)
                results[name] = f"error: {msg}"
                if warn:
                    print(
                        f"[warn] MCP server '{name}' connection failed: {msg}",
                        file=sys.stderr,
                    )
            self.status[name] = results[name]
        return results

    def summary(self) -> dict:
        """웹 🔌 칩용 스냅샷 (v9.2.0): 서버별 전송·상태·도구 수/이름·실패 이유
        + 집계. 부팅 시점 상태다 — 세션 중 재연결 경로가 없으므로 sticky 나
        SSE 없이 페이지 로드 시 GET 한 번이면 충분하다."""
        servers = []
        for name in self.configs:
            st = self.status.get(name, "error: unknown")
            ok = st == "connected"
            tools = [t.name for t in self._tools.get(name, [])] if ok else []
            servers.append(
                {
                    "name": name,
                    "transport": self.configs[name].transport,
                    "connected": ok,
                    "tools": tools,
                    "error": None if ok else humanize_error(st, self.configs[name]),
                }
            )
        return {
            "servers": servers,
            "connected": sum(1 for s in servers if s["connected"]),
            "total": len(servers),
            "tool_count": sum(len(s["tools"]) for s in servers),
        }

    async def _connect_one(self, name: str, config: McpServerConfig) -> None:
        """Connect to a single MCP server."""
        if config.is_stdio:
            await self._connect_stdio(name, config)
        elif config.is_streamable_http:
            await self._connect_streamable_http(name, config)
        elif config.is_sse:
            await self._connect_sse(name, config)
        else:
            raise ValueError(
                f"Invalid config for '{name}': need 'command' (stdio) or "
                "'url' (sse / streamable-http)"
            )

    async def _connect_stdio(self, name: str, config: McpServerConfig) -> None:
        """Connect via stdio transport."""
        import os

        # SDK import 는 **의도적으로 함수 안**이다 (v8.62.0 부터 필수 의존성이
        # 됐어도 유지): ``import mcp`` 가 실측 ~235ms 라 최상위로 올리면 MCP 를
        # 쓰지 않는 모든 CLI 실행에 그 비용이 붙는다. 계약은
        # ``test_mcp.py::TestMcpSdkIsADeclaredDependency`` 가 고정한다.
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=config.command,
            args=config.args,
            env={**dict(os.environ), **config.env} if config.env else None,
        )

        # Suppress MCP server stderr to prevent terminal state corruption
        # Handle outlives this function (passed to stdio_client, closed in
        # ``disconnect``), so no context manager; opening /dev/null never blocks.
        devnull = open(os.devnull, "w")  # noqa: SIM115, ASYNC230
        try:
            transport_cm = stdio_client(params, errlog=devnull)
            read, write = await transport_cm.__aenter__()

            session = ClientSession(read, write)
            await session.__aenter__()
            await session.initialize()
        except BaseException:
            # 연결 실패 시 방금 연 fd 를 닫는다 — 저장 전 예외 경로 누수 방지.
            devnull.close()
            raise

        # Store session and cleanup info (errlog fd 는 disconnect 에서 닫는다 —
        # 종전엔 저장하지 않아 서버당 fd 1개가 프로세스 수명 내내 누수됐다,
        # 리뷰 §4.5)
        self._clients[name] = {
            "session": session,
            "transport_cm": transport_cm,
            "session_cm": session,
            "errlog": devnull,
        }
        await self._load_tools(name, session)

    async def _connect_streamable_http(
        self, name: str, config: McpServerConfig
    ) -> None:
        """Streamable HTTP 전송 (MCP 2025-03-26, v9.3.0).

        구 ``sse_client`` 로 이 서버에 붙으면 ``Accept`` 헤더가 모자라
        -32600 "Not Acceptable" 로 거절당한다 — 사용자 제보로 드러난 공백.
        SDK 가 ``streamablehttp_client`` 를 이미 제공하므로 배선만 하면 된다.
        (세 값을 yield 하는 점만 sse 와 다르다 — 셋째는 session id getter.)"""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        transport_cm = streamablehttp_client(config.url)
        read, write, _get_session_id = await transport_cm.__aenter__()

        session = ClientSession(read, write)
        await session.__aenter__()
        await session.initialize()

        self._clients[name] = {
            "session": session,
            "transport_cm": transport_cm,
            "session_cm": session,
        }
        await self._load_tools(name, session)

    async def _load_tools(self, name: str, session) -> None:
        """도구 목록 적재 — 세 전송 공용 (v9.3.0; 종전 stdio/sse 에 복붙 2벌)."""
        tools_result = await session.list_tools()
        self._tools[name] = [
            McpToolInfo(
                server=name,
                name=t.name,
                description=t.description or "",
                input_schema=t.inputSchema if hasattr(t, "inputSchema") else {},
            )
            for t in tools_result.tools
        ]

    async def _connect_sse(self, name: str, config: McpServerConfig) -> None:
        """구 HTTP+SSE 전송 (MCP 초기 규격)."""
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        transport_cm = sse_client(config.url)
        read, write = await transport_cm.__aenter__()

        session = ClientSession(read, write)
        await session.__aenter__()
        await session.initialize()

        self._clients[name] = {
            "session": session,
            "transport_cm": transport_cm,
            "session_cm": session,
        }
        await self._load_tools(name, session)

    @staticmethod
    def _drain(loop: asyncio.AbstractEventLoop) -> None:
        """종료 후 남은 태스크 정리 (v9.3.0).

        streamable-http 전송은 내부 task group 을 쓰므로 ``__aexit__`` 만으로는
        취소가 전파될 틈이 없어, 루프가 GC 될 때 "Task was destroyed but it is
        pending!" 이 **stderr 로 샌다** — 성공한 등록 출력에 섞여 사용자에겐
        에러처럼 보인다(실장에서 잡힘). 취소 후 한 번 돌려 정리한다."""
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        if not pending:
            return
        for t in pending:
            t.cancel()
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))

    def disconnect_all(self) -> None:
        """Disconnect from all servers."""
        for name in list(self._clients.keys()):
            self.disconnect(name)
        if self._loop and not self._loop.is_closed():
            self._loop.close()
            self._loop = None

    def disconnect(self, name: str) -> None:
        """Disconnect from a specific server."""
        client = self._clients.pop(name, None)
        if client is None:
            return
        self._tools.pop(name, None)
        try:
            loop = self._get_loop()
            session = client["session"]
            transport_cm = client["transport_cm"]
            loop.run_until_complete(session.__aexit__(None, None, None))
            loop.run_until_complete(transport_cm.__aexit__(None, None, None))
        except Exception:
            pass
        try:
            self._drain(loop)
        except Exception:
            pass
        finally:
            # stdio 전송의 errlog fd (sse 는 키 없음 — no-op).
            errlog = client.get("errlog")
            if errlog is not None:
                try:
                    errlog.close()
                except Exception:
                    pass

    # ── Tool operations ──────────────────────────────

    def list_tools(self, server: str | None = None) -> list[McpToolInfo]:
        """List tools for a specific server or all servers."""
        if server:
            return self._tools.get(server, [])
        all_tools = []
        for tools in self._tools.values():
            all_tools.extend(tools)
        return all_tools

    def call_tool(self, server: str, tool_name: str, arguments: dict) -> Any:
        """Call an MCP tool synchronously. Returns the tool result."""
        return self._run_sync(self._call_tool_async(server, tool_name, arguments))

    async def _call_tool_async(
        self, server: str, tool_name: str, arguments: dict
    ) -> Any:
        """Call an MCP tool asynchronously."""
        client = self._clients.get(server)
        if client is None:
            raise ConnectionError(f"MCP server '{server}' not connected")

        session = client["session"]
        result = await session.call_tool(tool_name, arguments)
        return result

    # ── Resource operations ──────────────────────────

    def list_resources(self, server: str) -> list[McpResourceInfo]:
        """List resources for a specific server."""
        client = self._clients.get(server)
        if client is None:
            return []
        try:
            result = self._run_sync(client["session"].list_resources())
            return [
                McpResourceInfo(
                    server=server,
                    uri=str(r.uri),
                    name=r.name or "",
                    description=r.description or "",
                )
                for r in result.resources
            ]
        except Exception:
            return []

    def read_resource(self, server: str, uri: str) -> str:
        """Read a resource by URI."""
        return self._run_sync(self._read_resource_async(server, uri))

    async def _read_resource_async(self, server: str, uri: str) -> str:
        """Read a resource asynchronously."""
        client = self._clients.get(server)
        if client is None:
            raise ConnectionError(f"MCP server '{server}' not connected")

        session = client["session"]
        result = await session.read_resource(uri)
        # Extract text content
        if result.contents:
            return result.contents[0].text or ""
        return ""

    # ── Status ───────────────────────────────────────

    @property
    def connected_servers(self) -> list[str]:
        """List of currently connected server names."""
        return list(self._clients.keys())

    def is_connected(self, server: str) -> bool:
        return server in self._clients
