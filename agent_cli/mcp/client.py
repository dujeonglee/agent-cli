"""MCP client manager — connects to MCP servers and executes tools.

Uses the mcp Python SDK for stdio, SSE and Streamable HTTP transports. The
async SDK runs on the manager's own event-loop thread; the public methods are
synchronous and safe to call from several threads (main and agents).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from agent_cli.mcp.config import (
    DEFAULT_CALL_TIMEOUT_S,
    McpServerConfig,
    call_timeout_problem,
)

#: 도구 호출을 기다리며 런의 정지를 보는 간격(초) — 전경 `shell` 의 ``STOP_POLL_S`` 와 같은 값.
STOP_POLL_S = 0.2

#: 연결 하나(전송 열기·``initialize``·도구 목록)를 기다리는 상한(초). 마법사의 연결 테스트와 부팅이
#: 같은 값이다 — 45초는 마법사가 느린 첫 실행(``npx`` 가 패키지를 내려받는 동안)을 위해 고른 값이다.
#: 넘으면 그 연결을 취소하고(같은 태스크 안의 정리라 서버 프로세스도 끝난다) 실패로 적는다.
CONNECT_TIMEOUT_S = 45

#: 연결 하나를 닫을 때 그 태스크가 스스로 닫기를 기다리는 시간(초) — 넘으면 취소한다. 루프 스레드를
#: 내릴 때 join 도 이만큼. stdio 전송은 닫을 때 서버에 2초를 주고 그 뒤 끝낸다(mcp 1.25).
DISCONNECT_TIMEOUT_S = 5


class McpConnectTimedOut(Exception):
    """연결이 상한 안에 준비되지 않았다 — 문장은 마법사가 써 온 그대로다."""

    def __init__(self, limit_s: float) -> None:
        super().__init__(
            f"no response within {limit_s:g}s — the server started but does not "
            "answer initialize (check the package name and arguments)"
        )
        self.limit_s = limit_s


class McpCallTimedOut(Exception):
    """도구 호출이 상한 안에 답하지 않아 기다림을 그만뒀다. ``limit_s`` 는 그 상한(초)."""

    def __init__(self, limit_s: float) -> None:
        super().__init__(f"no answer within {limit_s:g}s")
        self.limit_s = limit_s


class McpCallStopped(Exception):
    """런의 정지가 서서 도구 호출을 기다리기를 그만뒀다. ``elapsed_s`` 는 그때까지 기다린 초
    (전경 `shell` 의 정지 문장과 같이 버림)."""

    def __init__(self, elapsed_s: int) -> None:
        super().__init__(f"run stopped after {elapsed_s}s")
        self.elapsed_s = elapsed_s


async def await_answer_or_give_up(
    call: Awaitable[Any],
    *,
    limit_s: float,
    stopped: Callable[[], bool],
    clock: Callable[[], float] = time.monotonic,
    poll_s: float = STOP_POLL_S,
) -> Any:
    """``call`` 의 답·상한·런의 정지 중 먼저 온 것으로 끝낸다 — 답이면 그 값을 돌려주고, 상한이면
    :class:`McpCallTimedOut`, 정지면 :class:`McpCallStopped`. 그만둘 때는 기다리던 요청을
    취소하고 그 취소가 끝날 때까지 기다린다(떠 있는 태스크를 남기지 않는다).

    이 코드는 서버에 ``notifications/cancelled`` 를 보내지 않는다: 1.x SDK 에는 요청 id 를 얻는
    공개 길이 없고, mcp 1.6 서버는 그 통지 뒤의 다음 호출에 답하지 않았다(실측). 1.x 에서는 서버가
    하던 일을 계속할 수 있고 늦게 온 답은 SDK 가 모르는 요청 id 로 버린다. 2.x SDK 는 취소된 요청에
    그 통지를 스스로 보낸다(실측 2.3.0 — 서버가 그만둔다).
    ``clock``·``poll_s`` 는 시간 단언 없는 테스트용."""
    task = asyncio.ensure_future(call)
    started = clock()
    try:
        while not task.done():
            left = started + limit_s - clock()
            if left <= 0:
                raise McpCallTimedOut(limit_s)
            if stopped():
                raise McpCallStopped(int(clock() - started))
            await asyncio.wait({task}, timeout=min(poll_s, left))
        return task.result()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@dataclass
class McpToolInfo:
    """Metadata for an MCP tool."""

    server: str
    name: str
    description: str
    input_schema: dict


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
        return f"executable not found: {cmd}" if cmd else "executable not found"
    if (
        "connection refused" in low
        or "connect call failed" in low
        or "all connection attempts failed" in low  # httpx (sse)
    ):
        return (
            f"connection refused — no server at {url}" if url else "connection refused"
        )
    if "no module named 'mcp'" in low:
        return "mcp SDK is not installed (pip install mcp)"
    # 전송 세대 불일치 (v9.3.0) — 사용자 제보로 드러난 공백. 구 sse 로 신규
    # Streamable HTTP 서버에 붙으면 서버가 Accept 헤더 부족으로 거절하는데,
    # 원문은 "400 Bad Request" 나 -32600 이라 **원인이 전송 방식이라는 걸
    # 알 수 없다**. 마법사를 안 쓰고 손편집한 경우 이게 유일한 단서다.
    # anyio cancel scope 취소 — 서버가 이 전송 방식의 요청에 응답하지 않아
    # transport 가 대기 중인 initialize 를 걷어낸 것. 원문("Cancelled via
    # cancel scope 0x…")은 사용자에게 아무 정보도 아니다.
    if "cancel scope" in low or low == "cancellederror":
        return "the server does not respond to this transport"
    if _looks_like_wrong_transport(low, cfg):
        return (
            "transport mismatch — this server speaks Streamable HTTP. "
            'Set transport to "streamable-http" in mcp.json, or '
            "re-register it with `agent-cli mcp add` (auto-detects)"
        )
    return text or "unknown error"


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
    ``mcp__{server}__{tool}`` (adapter).

    **루프는 매니저의 스레드 하나**(v10.152.0). 처음 연결할 때 데몬 스레드 ``mcp-loop`` 가 이벤트 루프를
    돌기 시작하고, 동기 API 는 전부 :meth:`_submit` 으로 그 루프에 코루틴을 보내고 답을 기다린다.
    종전엔 부르는 스레드가 ``run_until_complete`` 를 돌려서 (1) main 과 에이전트가 겹쳐 부르면
    ``This event loop is already running`` 이었고 (2) 연결을 연 태스크와 닫는 태스크가 달라 anyio 가
    해제를 거절해 서버 프로세스가 끝나지 않았다(mcp 2.x 에서는 ``disconnect_all`` 이 돌아오지 않았다).

    서버 하나의 연결은 **태스크 하나가 처음부터 끝까지 든다**(:meth:`_serve`) — 전송·세션을 열고,
    도구 목록을 받고, 닫으라는 신호(``closing``)를 기다렸다가 같은 태스크에서 닫는다.

    잠금이 없는 이유: ``_sessions``·``_closers``·``_tools``·``status``·``configs`` 는
    ``connect_all``(부팅 한 번)과 ``disconnect``·``disconnect_all``(종료) 안에서만 쓰인다 — 루프
    스레드가 쓴 것은 ``_submit`` 의 ``future.result()`` 가 돌아오기 전에 끝나 있으므로(완료가
    happens-before) 그 뒤 어느 스레드가 읽어도 다 쓴 값을 본다. 런타임 중에 다시 연결하는 길은 없다
    (웹 🔌 칩·훅·도구 호출은 읽기만). 스레드 시작도 ``connect_all`` 에서만이라 경합이 없다.
    """

    def __init__(self, *, connect_timeout_s: float = CONNECT_TIMEOUT_S):
        self._connect_timeout_s = connect_timeout_s
        self._sessions: dict[str, Any] = {}  # server_name → ClientSession
        # server_name → (닫으라는 신호, 그 연결을 든 태스크) — 둘 다 루프 스레드의 것
        self._closers: dict[str, tuple[asyncio.Event, asyncio.Task]] = {}
        self._tools: dict[str, list[McpToolInfo]] = {}  # server_name → tools
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        # v9.2.0: 마지막 connect_all 의 서버별 결과("connected" | "error: …")와
        # 설정 — 웹 🔌 칩(GET /api/mcp)이 부팅 시점 상태를 보여주려면 실패한
        # 서버도 기억해야 한다(종전엔 stderr 한 줄로 흘려보내고 잊었다).
        self.status: dict[str, str] = {}
        self.configs: dict[str, McpServerConfig] = {}

    def _loop_running_in_its_thread(self) -> asyncio.AbstractEventLoop:
        """매니저의 루프 — 없으면 만들고 데몬 스레드 ``mcp-loop`` 에서 돌리기 시작한다."""
        if self._loop is None:
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=loop.run_forever, name="mcp-loop", daemon=True
            )
            thread.start()
            self._loop, self._thread = loop, thread
        return self._loop

    def _submit(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """``coro`` 를 매니저의 루프에서 돌리고 그 결과를 돌려준다(예외는 그대로). 기다리던 스레드가
        ``KeyboardInterrupt`` 등으로 빠지면 그 코루틴을 취소한다 — 루프에 떠 있게 두지 않는다.
        루프 스레드 자신이 부르면 자기를 기다리게 되므로 거절한다."""
        if self._thread is not None and threading.current_thread() is self._thread:
            coro.close()
            raise RuntimeError("the MCP loop thread cannot wait on itself")
        future = asyncio.run_coroutine_threadsafe(
            coro, self._loop_running_in_its_thread()
        )
        try:
            return future.result()
        except BaseException:
            future.cancel()
            raise

    # ── Connection management ────────────────────────

    def connect_all(
        self, configs: dict[str, McpServerConfig], *, warn: bool = True
    ) -> dict[str, str]:
        """Connect to all configured servers — 동시에, 서버마다 ``CONNECT_TIMEOUT_S`` 까지.

        Returns dict of {server_name: status} where status is
        "connected" or error message — 설정 순서대로(부팅 줄·🔌 칩 순서가 그대로다).

        ``warn=False`` 면 실패를 stderr 에 찍지 않는다 (v9.3.0) — 마법사의
        전송 방식 자동 판별은 **실패를 전제로 두 번 시도**하므로, 예상된
        첫 실패에 [warn] 을 찍으면 성공한 등록에도 경고가 섞여 보인다.
        부팅 경로는 종전대로 찍는다(그때의 실패는 진짜 문제다)."""
        self.configs.update(configs)
        failures = self._submit(self._connect_each(configs)) if configs else {}
        results = {}
        for name in configs:
            e = failures[name]
            if e is None:
                results[name] = "connected"
            else:
                msg = _leaf_error(e)
                results[name] = f"error: {msg}"
                if warn:
                    print(
                        f"[warn] MCP server '{name}' connection failed: {msg}",
                        file=sys.stderr,
                    )
            self.status[name] = results[name]
        return results

    async def _connect_each(
        self, configs: dict[str, McpServerConfig]
    ) -> dict[str, BaseException | None]:
        """서버들에 동시에 연결한다 → 서버별 실패(성공이면 None). 하나가 늦거나 실패해도 다른 서버는
        그대로 붙는다. 연결의 취소(``CancelledError``)도 **그 연결 시도의 실패**로 받는다 (v9.3.0
        실장에서 잡힘: streamable-http 로 구 SSE 엔드포인트를 치면 transport 의 anyio cancel scope 가
        대기 중인 initialize 를 취소한다) — 사용자 Ctrl-C 는 기다리던 스레드의 ``KeyboardInterrupt``
        로 오므로(`_submit`) 여전히 전파된다."""
        names = list(configs)
        outcomes = await asyncio.gather(
            *(self._connect_within_limit(n, configs[n]) for n in names),
            return_exceptions=True,
        )
        return dict(zip(names, outcomes))

    async def _connect_within_limit(self, name: str, config: McpServerConfig) -> None:
        """:meth:`_connect_one` 을 ``connect_timeout_s`` 까지 기다린다 — 넘으면 그 연결을 취소하고
        :class:`McpConnectTimedOut`."""
        try:
            await asyncio.wait_for(
                self._connect_one(name, config), self._connect_timeout_s
            )
        except asyncio.TimeoutError:
            raise McpConnectTimedOut(self._connect_timeout_s) from None

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
        """서버 하나에 연결한다 — 연결을 들 태스크(:meth:`_serve`)를 띄우고, 세션과 도구 목록이
        준비되면 기록한다. 준비 전에 실패하면 그 예외가 여기서 난다(태스크는 이미 끝났다)."""
        _check_config(name, config)
        ready: asyncio.Future = asyncio.get_running_loop().create_future()
        closing = asyncio.Event()
        task = asyncio.ensure_future(self._serve(config, ready, closing))
        try:
            session, tools = await ready
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        self._sessions[name] = session
        self._closers[name] = (closing, task)
        self._tools[name] = [
            McpToolInfo(
                server=name,
                name=t.name,
                description=t.description or "",
                input_schema=t.inputSchema if hasattr(t, "inputSchema") else {},
            )
            for t in tools
        ]

    async def _serve(
        self, config: McpServerConfig, ready: asyncio.Future, closing: asyncio.Event
    ) -> None:
        """연결 하나의 수명 — 이 태스크가 전송과 세션을 열고(``initialize``·``list_tools``), ``ready``
        에 ``(세션, 도구 목록)`` 을 넣고, ``closing`` 이 설 때까지 기다렸다가 **같은 태스크에서** 닫는다
        (anyio 는 연 태스크가 닫아야 해제한다 — 전송의 정리가 서버 프로세스를 끝낸다). 준비 전의
        실패는 ``ready`` 로 건너간다. 준비 뒤의 실패(서버가 먼저 끊음·닫을 때의 오류)는 삼킨다 — 그
        연결의 호출이 각자 실패로 알게 된다."""
        from mcp import ClientSession

        try:
            async with (
                _open_transport(config) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                listed = await session.list_tools()
                if not ready.done():
                    ready.set_result((session, listed.tools))
                await closing.wait()
        except asyncio.CancelledError:
            if not ready.done():
                ready.cancel()
            raise
        except BaseException as e:  # 전송 오류는 anyio ExceptionGroup 으로도 온다
            if not ready.done():
                ready.set_exception(e)

    @staticmethod
    async def _close(closing: asyncio.Event, task: asyncio.Task) -> None:
        """연결 하나를 닫는다 — 닫으라는 신호를 세우고 그 태스크가 스스로 닫기를
        ``DISCONNECT_TIMEOUT_S`` 까지 기다린다. 넘으면 취소한다(취소도 같은 태스크 안의 정리다)."""
        closing.set()
        done, _ = await asyncio.wait({task}, timeout=DISCONNECT_TIMEOUT_S)
        if not done:
            task.cancel()
            await asyncio.wait({task}, timeout=DISCONNECT_TIMEOUT_S)

    def disconnect(self, name: str) -> None:
        """Disconnect from a specific server — 그 서버의 연결 태스크가 세션과 전송을 닫는다
        (stdio 면 서버 프로세스가 끝난다)."""
        entry = self._closers.pop(name, None)
        self._sessions.pop(name, None)
        self._tools.pop(name, None)
        if entry is not None:
            self._submit(self._close(*entry))

    def disconnect_all(self) -> None:
        """모든 연결을 닫고 루프 스레드를 내린다 — 런타임 종료·마법사 끝에서 부른다.

        순서: 연결 태스크마다 닫기(동시에) → 루프에 남은 태스크(그 사이 끝나지 않은 도구 호출)를
        취소하고 끝나기를 기다림 — 그 호출을 기다리던 스레드는 예외로 풀린다 → 비동기 생성기 정리 →
        루프 정지 → 스레드 join(``DISCONNECT_TIMEOUT_S``) → 루프 닫기. 스레드가 그 안에 안 끝나면
        루프를 닫지 않고 둔다(데몬이라 프로세스 종료를 막지 않는다)."""
        entries = list(self._closers.values())
        self._closers.clear()
        self._sessions.clear()
        self._tools.clear()
        loop, thread = self._loop, self._thread
        if loop is None or thread is None:
            return
        self._submit(self._shut_down(entries))
        loop.call_soon_threadsafe(loop.stop)
        thread.join(DISCONNECT_TIMEOUT_S)
        if not thread.is_alive():
            loop.close()
        self._loop, self._thread = None, None

    async def _shut_down(
        self, entries: list[tuple[asyncio.Event, asyncio.Task]]
    ) -> None:
        """``disconnect_all`` 의 루프 쪽 — 연결을 닫고, 남은 태스크를 취소하고, 비동기 생성기를 닫는다."""
        await asyncio.gather(
            *(self._close(closing, task) for closing, task in entries),
            return_exceptions=True,
        )
        rest = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for t in rest:
            t.cancel()
        if rest:
            await asyncio.wait(rest, timeout=DISCONNECT_TIMEOUT_S)
        await asyncio.get_running_loop().shutdown_asyncgens()

    # ── Tool operations ──────────────────────────────

    def list_tools(self, server: str | None = None) -> list[McpToolInfo]:
        """List tools for a specific server or all servers."""
        if server:
            return self._tools.get(server, [])
        all_tools = []
        for tools in self._tools.values():
            all_tools.extend(tools)
        return all_tools

    def call_tool(
        self,
        server: str,
        tool_name: str,
        arguments: dict,
        *,
        stopped: Callable[[], bool] | None = None,
    ) -> Any:
        """Call an MCP tool synchronously. Returns the tool result.

        서버 설정의 ``call_timeout_s``(없으면 기본 120초) 안에 답이 없으면
        :class:`McpCallTimedOut`, ``stopped``(이 런의 정지 — `RunContext.stopped`)가 서면
        :class:`McpCallStopped` — 둘 다 기다리던 요청을 취소한 뒤다(`await_answer_or_give_up`).
        여러 스레드가 동시에 불러도 된다 — 한 루프에서 함께 돈다. 기다리는 사이 연결이 닫히면
        (``disconnect_all``) ``ConnectionError``."""
        session = self._sessions.get(server)
        if session is None:
            raise ConnectionError(f'MCP server "{server}" not connected')
        config = self.configs.get(server)
        limit_s = config.call_timeout_s if config else DEFAULT_CALL_TIMEOUT_S
        try:
            return self._submit(
                await_answer_or_give_up(
                    session.call_tool(tool_name, arguments),
                    limit_s=limit_s,
                    stopped=stopped or (lambda: False),
                )
            )
        except (McpCallTimedOut, McpCallStopped):
            raise
        except (Exception, asyncio.CancelledError) as e:
            if server not in self._sessions:
                raise ConnectionError(f'MCP server "{server}" was disconnected') from e
            raise

    # ── Status ───────────────────────────────────────

    @property
    def connected_servers(self) -> list[str]:
        """List of currently connected server names."""
        return list(self._sessions.keys())

    def is_connected(self, server: str) -> bool:
        return server in self._sessions


def _check_config(name: str, config: McpServerConfig) -> None:
    """연결할 수 없는 설정이면 ``ValueError`` — 상한 값이 쓸 수 없거나 전송을 정할 수 없을 때."""
    problem = call_timeout_problem(config.call_timeout_s)
    if problem:
        raise ValueError(f"Invalid config for '{name}': {problem}")
    if not (config.is_stdio or config.is_streamable_http or config.is_sse):
        raise ValueError(
            f"Invalid config for '{name}': need 'command' (stdio) or "
            "'url' (sse / streamable-http)"
        )


@contextlib.asynccontextmanager
async def _open_transport(config: McpServerConfig):
    """설정의 전송을 연다 → ``(read, write)`` 스트림. 닫힐 때 전송이 정리한다(stdio 는 서버
    프로세스를 끝내고 errlog fd 를 닫는다).

    SDK import 는 **의도적으로 함수 안**이다 (v8.62.0 부터 필수 의존성이 됐어도 유지): ``import mcp``
    가 실측 ~235ms 라 최상위로 올리면 MCP 를 쓰지 않는 모든 CLI 실행에 그 비용이 붙는다. 계약은
    ``test_mcp.py::TestMcpSdkIsADeclaredDependency`` 가 고정한다."""
    if config.is_stdio:
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=config.command,
            args=config.args,
            env={**dict(os.environ), **config.env} if config.env else None,
        )
        # 서버 stderr 는 버린다 — 터미널 상태가 깨지지 않게. 여는 데 막히지 않는다.
        with open(os.devnull, "w") as errlog:  # noqa: ASYNC230
            async with stdio_client(params, errlog=errlog) as (read, write):
                yield read, write
    elif config.is_streamable_http:
        # Streamable HTTP (MCP 2025-03-26, v9.3.0) — 구 sse_client 로 붙으면 Accept 헤더가 모자라
        # -32600 "Not Acceptable" 로 거절당한다. SDK 세대마다 이름과 내주는 것이 다르다(v10.154.0 실측):
        # 1.9~1.23 은 ``streamablehttp_client`` 만, 1.24 부터 ``streamable_http_client`` 가 생겼고(옛 이름은
        # 폐기 예고), 2.x 에는 새 이름만 있다. 1.x 는 세 값(셋째는 session id getter), 2.x 는 두 값을
        # 내준다. 그 차이는 여기 한 곳에서 — 새 이름이 있으면 그것, 없으면 옛 이름. 둘 다 URL 하나로
        # 부르고 SDK 기본 HTTP 클라이언트를 쓴다(헤더는 넘기지 않는다 — 넘긴 적이 없다).
        from mcp.client import streamable_http

        open_http = getattr(streamable_http, "streamable_http_client", None) or (
            streamable_http.streamablehttp_client
        )
        async with open_http(config.url) as (read, write, *_rest):
            yield read, write
    else:
        # 구 HTTP+SSE 전송 (MCP 초기 규격)
        from mcp.client.sse import sse_client

        async with sse_client(config.url) as (read, write):
            yield read, write
