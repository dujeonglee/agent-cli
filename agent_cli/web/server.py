"""FastAPI server backing ``agent-cli web``.

Endpoints:
  - ``GET  /api/health`` — liveness probe (no auth)
  - ``GET  /api/stream`` — SSE event stream (auth via ``token`` query)
  - ``POST /api/input``  — submit chat message / ask answer / confirm reply
  - ``POST /api/abort``  — interrupt the current ``prompt_user`` / ``confirm``
  - ``POST /api/stop``   — stop the in-flight chat/skill/agent turn

Auth: the ``_AuthMiddleware`` is the SOLE enforcer, default-deny. Every
``/api/*`` path except ``/api/health`` requires authentication; ``/``, ``/static``
and the health probe are public. A request authenticates via ``--trust-local``
(loopback gateway), a valid ``act`` cookie, or a valid ``?token=`` (the one-time
bootstrap URL / curl). Unauthenticated requests to a protected path get 401 from
the middleware — the endpoint never runs — so a newly added ``/api`` route is
protected BY CONSTRUCTION (endpoints carry no auth code of their own). A
bootstrap ``?token=`` also Sets the ``act`` cookie, so the browser authenticates
by cookie thereafter and the token leaves per-request URLs. Every response
carries ``Referrer-Policy: no-referrer``.

Multi-viewer, all equal: every authenticated SSE connection RECEIVES the
stream AND may send input (no controller/observer split). Each connection
learns its ``conn_id`` from the ``identity`` event for the viewer-roster
"(you)" mark and queued-message ownership.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import socket
import tempfile
import threading
import zipfile
from pathlib import Path
from queue import Empty, SimpleQueue

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse
from starlette.background import BackgroundTask

from agent_cli.input_queue import InputQueue
from agent_cli.render.web import WebConnection, WebRenderer

# C3: 도메인 로직은 분리 모듈 소유 — server 는 전송(라우팅·SSE·미들웨어) 전용.
from agent_cli.web.directives import generate_directive_section
from agent_cli.web.inspector import _dynamic_context_sections

_STATIC_DIR = Path(__file__).resolve().parent / "static"


_NO_CACHE_HEADERS = {"Cache-Control": "no-cache, must-revalidate"}

_MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB (workspace upload 상한)


class _NoCacheStaticFiles(StaticFiles):
    """``StaticFiles`` that stamps every response with ``no-cache``.

    Mounting plain ``StaticFiles`` leaves caching at Starlette's
    defaults — no ``Cache-Control`` set, so browsers fall back to
    heuristic caching of CSS/JS. Editable-install iteration becomes
    "edit, restart server, hard-refresh browser"; the override drops
    the last step.
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = _NO_CACHE_HEADERS["Cache-Control"]
        return response


def _port_has_live_listener(host: str, port: int) -> bool:
    """True if a process is ALREADY listening on ``(host, port)`` — distinct
    from a stale ``TIME_WAIT`` remnant (which is safe to reuse).

    Why a connect probe and not just a bind probe: the bind probe sets
    ``SO_REUSEADDR`` (so a restart can reclaim its own port out of TIME_WAIT),
    but on macOS/BSD that lets a SPECIFIC-IP bind silently COEXIST with another
    process's ``0.0.0.0:port`` listener — the bind "succeeds" yet two servers
    then fight for the port. Connecting reflects what a real client sees: a live
    listener answers (port genuinely taken); a closed/TIME_WAIT port refuses.
    For a wildcard ``host`` we connect via loopback (you can't connect to
    ``0.0.0.0``)."""
    target = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as c:
        c.settimeout(0.25)
        return c.connect_ex((target, port)) == 0


def pick_port(host: str, preferred: int) -> int:
    """Pick a bindable port for the web server.

    Prefer ``preferred`` (the web command passes 0xC0DE) when free; if a live
    server already holds it, fall back to an OS-assigned ephemeral port. The caller passes the
    result straight to ``uvicorn.Config(port=...)``, so the URL printed before
    ``server_obj.run()`` shows whatever the OS actually gave us.

    Liveness is checked with a connect probe FIRST (``_port_has_live_listener``)
    so a second instance (e.g. ``--host <ip>`` while another runs on
    ``0.0.0.0:8080``) doesn't double-bind the same port — the bind probe's
    ``SO_REUSEADDR`` alone false-positives there on macOS/BSD. The bind probe is
    kept (with ``SO_REUSEADDR``) for the genuinely-free / TIME_WAIT cases so a
    restart can still reclaim its own port. The socket is closed before
    returning — a tiny TOCTOU window before uvicorn re-binds remains, but a
    same-host race in that window is rare enough not to handle.
    """
    for candidate in (preferred, 0):
        if candidate and _port_has_live_listener(host, candidate):
            continue  # a live server already answers here — try the next
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, candidate))
            except OSError:
                continue
            return s.getsockname()[1]
    # Both candidates failed — preferred busy AND OS refused 0. That's
    # a misconfigured host (no IPv4 stack, etc.); let uvicorn surface
    # the underlying error to the operator instead of silently retrying.
    return preferred


# ── CLI-parity slash commands (web mode) ──────────────────


class WebServer:
    """Owns the renderer + the worker thread that drives AgentLoop.

    The FastAPI app delegates all stateful operations here so the
    request handlers stay thin and unit-testable.
    """

    # Identity sentinel returned by :meth:`dequeue_blocking` once
    # :meth:`shutdown` flips the queue's shutdown flag, so the worker
    # thread's blocking call wakes up and breaks its loop cleanly. Workers
    # compare with ``is`` (identity); queued items are dicts, so a user
    # message can never collide with this object.
    SHUTDOWN = InputQueue.SHUTDOWN

    def __init__(
        self,
        renderer: WebRenderer,
        token: str | None = None,
        ctx=None,
        trust_local: bool = False,
        base_path: str = "",
        runtime: dict | None = None,
    ) -> None:
        self.renderer = renderer
        # ✨ directive 생성이 쓰는 LLM 배선 (provider 객체·model·capabilities·
        # provider_name·base_url·api_key·session·ctx). None(테스트/미배선)
        # 이면 생성 엔드포인트가 503. run 엔진 경유라 worker 루프와 독립.
        self.runtime = runtime or {}
        # --trust-local: skip token auth for loopback requests (the gateway in
        # front of a 127.0.0.1-bound instance already authenticated the user).
        self.trust_local = trust_local
        # --base-path: URL prefix when served under a reverse proxy
        # (``/<prefix>/*`` → this instance). Stored without a trailing slash
        # ("" = root). The served index.html gets ``<base href="<prefix>/">``
        # and all frontend URLs are relative, so they resolve under the prefix.
        self.base_path = (base_path or "").rstrip("/")
        # The live ContextManager (shared with the worker's run_loop) — read
        # by the Prompt Inspector to show the DYNAMIC context (conversation +
        # observations), not just the static system prompt. May be None
        # (tests / pre-session).
        self.ctx = ctx
        # teammate P4: 대화 창의 인간 개입(input/kill) 대상 — worker 부트가
        # AgentRegistry 를 꽂는다. None(테스트/기동 전)이면 엔드포인트 503.
        self.agent_registry = None
        # ``secrets.token_urlsafe`` gives a URL-safe random token —
        # ``--token`` override sticks if provided.
        self.token = token or secrets.token_urlsafe(32)
        # Pending user-message queue. Every connection may enqueue; the
        # worker pops one (blocking) to START a run, and the running loop
        # pops more at turn boundaries (non-blocking) to INJECT mid-run.
        # 골격은 공용 InputQueue (teammate P5 — CLI run 의 큐 펌프와 공유);
        # 서버는 닉네임 해석 + 라이브 큐 브로드캐스트(on_change)만 얹는다.
        # Items: ``{id, conn_id, nickname, text}``.
        self._queue = InputQueue(on_change=self._broadcast_queue)
        # Stop handle for the in-flight chat turn. The worker registers a
        # fresh ``threading.Event`` per message and passes it to
        # ``run_loop(stop_event=…)``; ``/api/stop`` sets it so the loop
        # exits at the next turn boundary (same path as Ctrl+C in chat).
        # Guarded by a lock — set from the worker thread, read from the
        # request handler thread.
        self._stop_lock = threading.Lock()
        self._stop_handle: threading.Event | None = None
        # Workspace root for the download feature = the dir the server (and
        # agent) runs in. Resolved once at startup; downloads are confined to
        # this subtree (path-traversal guarded in ``_safe_workspace_path``).
        self.workspace = Path.cwd().resolve()

    def _safe_workspace_path(self, rel: str) -> Path:
        """Resolve ``rel`` under the workspace root, rejecting traversal /
        symlink escapes. ``""`` / ``"."`` → the workspace root itself."""
        p = (self.workspace / (rel or ".")).resolve()
        if p != self.workspace and self.workspace not in p.parents:
            raise HTTPException(status_code=400, detail="path outside workspace")
        return p

    # ─── External hooks (used by the CLI ``web`` command) ─────

    def set_stop_handle(self, event: threading.Event | None) -> None:
        """Register (or clear) the stop Event for the current chat turn.

        Called by the worker: a fresh Event before each ``run_loop``,
        then ``None`` once the turn returns.
        """
        with self._stop_lock:
            self._stop_handle = event

    def trigger_stop(self) -> bool:
        """Signal the in-flight turn to stop at the next turn boundary.

        Returns ``True`` if a turn was active (a handle was registered),
        ``False`` otherwise — lets ``/api/stop`` report whether anything
        was actually stopped.
        """
        with self._stop_lock:
            handle = self._stop_handle
        if handle is not None:
            handle.set()
            return True
        return False

    def enqueue(self, conn_id: str | None, text: str) -> dict:
        """Add a user message to the pending queue (any connection may).
        Returns the queued item ``{id, conn_id, nickname, text}``."""
        nickname = self.renderer.nickname_for(conn_id)
        return self._queue.enqueue(conn_id, text, nickname=nickname)

    def dequeue_blocking(self):
        """Worker-idle: block until a message is queued (or shutdown).

        Returns ``WebServer.SHUTDOWN`` if :meth:`shutdown` was called, else
        the queued item dict. Workers compare with ``is WebServer.SHUTDOWN``.
        """
        return self._queue.dequeue_blocking()

    def dequeue_nowait(self) -> dict | None:
        """Turn-boundary (running loop): pop one queued message if available,
        else ``None`` (don't block — the loop keeps going)."""
        return self._queue.dequeue_nowait()

    def cancel_pending(self, conn_id: str | None, msg_id: str) -> bool:
        """Remove a still-pending message — only its OWNER (matching
        ``conn_id``) may cancel. Returns True if removed."""
        return self._queue.cancel(conn_id, msg_id)

    def queue_snapshot(self) -> list[dict]:
        return self._queue.snapshot()

    def pending_count(self) -> int:
        """Number of queued-but-undelivered user messages — the idle-reaper's
        'work is waiting' signal."""
        return self._queue.pending_count()

    def _broadcast_queue(self) -> None:
        """Push the live queue state to all clients (released the queue lock
        first — ``queue_state`` takes the renderer lock; InputQueue 도
        on_change 를 락 밖에서 호출한다)."""
        self.renderer.queue_state(self.queue_snapshot())

    def shutdown(self) -> None:
        """Wake any worker blocked in :meth:`dequeue_blocking`.

        Sets the shutdown flag and notifies so the worker's wait returns
        ``SHUTDOWN``. Idempotent.
        """
        self._queue.shutdown()

    # ─── Auth helper ──────────────────────────────────────────

    def token_ok(self, candidate: str | None) -> bool:
        """Constant-time compare against the configured token (avoids a LAN
        timing side-channel). Used by ``_AuthMiddleware`` for cookie/query
        token validation."""
        return candidate is not None and secrets.compare_digest(candidate, self.token)

    def is_trusted_client(self, host: str | None) -> bool:
        """Whether a request from ``host`` may skip token auth — only when
        ``--trust-local`` is on AND the peer is loopback (the trusted gateway).
        """
        return bool(self.trust_local) and host in ("127.0.0.1", "::1")

    # ─── Stream lifecycle ────────────────────────────────────

    async def stream_events(self, conn: WebConnection):
        """Async generator feeding the SSE response.

        Yields the persistent buffer snapshot first (replay) then loops
        on the connection's queue for live events. Heartbeat comments
        keep proxies from closing the connection during idle periods.

        The ``__close__`` sentinel — pushed by
        ``WebRenderer.unregister_connection`` when the renderer side
        drops this connection — ends the loop promptly without waiting
        for the keep-alive timer.
        """
        snapshot = self.renderer.register_connection(conn)
        for event, data in snapshot:
            # ``_emit`` payloads carry a pre-serialized ``json_str`` (dumped
            # once at the fan-out point); per-connection synthetics (identity /
            # sticky / viewers) are plain dicts — dump those here (a handful
            # per connect, vs. the whole buffer per reconnect before).
            payload = getattr(data, "json_str", None)
            if payload is None:
                payload = json.dumps(data, ensure_ascii=False)
            yield {"event": event, "data": payload}

        loop = asyncio.get_event_loop()
        try:
            while not conn.closed.is_set():
                try:
                    event_and_data = await loop.run_in_executor(
                        None, _queue_get_with_timeout, conn.queue, 15.0
                    )
                except _QueueEmpty:
                    # Heartbeat — sse-starlette emits ``: keep-alive``
                    # for ``None`` payloads but we yield a comment-style
                    # event explicitly so the wire shape is predictable
                    # under inspection.
                    yield {"comment": "keep-alive"}
                    continue
                event, data = event_and_data
                if event == "__close__":
                    # Sentinel from unregister — leave the loop without
                    # serialising to the client.
                    break
                payload = getattr(data, "json_str", None)
                if payload is None:
                    payload = json.dumps(data, ensure_ascii=False)
                yield {"event": event, "data": payload}
        finally:
            self.renderer.unregister_connection(conn)


# Sentinel + helper for SSE queue polling. Using a custom timeout
# exception keeps the executor-bound call signature simple — every
# polling iteration returns either an event tuple or raises.
class _QueueEmpty(Exception):
    pass


def _queue_get_with_timeout(q: SimpleQueue, timeout: float):
    try:
        return q.get(timeout=timeout)
    except Empty as e:
        raise _QueueEmpty() from e


class _IncompleteResponseLogFilter(logging.Filter):
    """Drop uvicorn's "ASGI callable returned without completing response"
    line.

    On Ctrl+C with an SSE client connected, sse-starlette's
    ``EventSourceResponse`` cancels its ``_stream_response`` task (its
    ``_listen_for_exit_signal`` watcher fires and cancels the whole task
    group) *before* it sends the final ``more_body=False`` chunk. uvicorn
    then logs this error for the truncated response. It is cosmetic — the
    session finalises normally — and only ever appears during shutdown,
    never in normal operation (verified live, 2026-05-30). Suppressing
    just this one message keeps real ASGI errors visible.
    """

    _MSG = "ASGI callable returned without completing response"

    def filter(self, record: logging.LogRecord) -> bool:
        return self._MSG not in record.getMessage()


def suppress_incomplete_response_log() -> None:
    """Attach ``_IncompleteResponseLogFilter`` to uvicorn's error logger.

    Idempotent — calling twice won't stack duplicate filters.
    """
    logger = logging.getLogger("uvicorn.error")
    if not any(isinstance(f, _IncompleteResponseLogFilter) for f in logger.filters):
        logger.addFilter(_IncompleteResponseLogFilter())


# ── FastAPI app factory ────────────────────────────────────


_AUTH_COOKIE = "act"  # agent-cli token cookie — set once from the bootstrap URL


def _is_public_path(path: str) -> bool:
    """Paths served WITHOUT authentication: the UI shell, its static assets, and
    the liveness probe. Everything else under ``/api`` is default-deny."""
    return (
        path == "/"
        or path == "/api/health"
        or path == "/static"
        or path.startswith("/static/")
    )


def _needs_auth(path: str) -> bool:
    """Default-deny: any ``/api`` path that is not explicitly public requires
    authentication. A newly added ``/api`` route is protected by construction."""
    return path.startswith("/api") and not _is_public_path(path)


def _query_token(query_string: bytes) -> str | None:
    """Extract the ``token`` value from a raw ASGI query string, or None."""
    from urllib.parse import parse_qsl

    for k, v in parse_qsl(query_string.decode(errors="ignore"), keep_blank_values=True):
        if k == "token":
            return v
    return None


def _cookie_value(scope, name: str) -> str | None:
    """Read cookie ``name`` from the raw ASGI scope headers, or None."""
    from http.cookies import SimpleCookie

    for k, v in scope.get("headers", []):
        if k == b"cookie":
            jar = SimpleCookie()
            try:
                jar.load(v.decode("latin-1"))
            except Exception:
                return None
            m = jar.get(name)
            return m.value if m else None
    return None


def _build_auth_cookie(token: str, base_path: str, *, secure: bool) -> str:
    """``Set-Cookie`` value for the auth cookie.

    Path is scoped to the instance's ``base_path`` so two ``/s/<id>`` instances
    behind one gateway origin never share a cookie. ``HttpOnly`` keeps JS (hence
    XSS) from reading the token; ``SameSite=Strict`` closes CSRF (the cookie is
    never sent on a cross-site request); ``Secure`` is added ONLY on TLS — a
    Secure cookie is silently dropped over the plain-HTTP loopback that
    board-proxy serves."""
    path = (base_path or "") + "/"  # "" → "/", "/s/doom" → "/s/doom/"
    parts = [f"{_AUTH_COOKIE}={token}", f"Path={path}", "HttpOnly", "SameSite=Strict"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


async def _send_401(send) -> None:
    """Emit a JSON 401 straight from the middleware (the endpoint never runs)."""
    body = b'{"detail":"invalid or missing token"}'
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"referrer-policy", b"no-referrer"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class _AuthMiddleware:
    """Pure-ASGI auth chokepoint — the SOLE enforcer, default-deny.

    A request authenticates via a trusted loopback peer (``--trust-local``), a
    valid ``act`` cookie, or a valid ``?token=`` (the one-time bootstrap URL the
    gateway/operator hands out, or a curl/tool call). Any ``/api`` path that is
    not public (:func:`_needs_auth`) and is unauthenticated gets a 401 HERE — the
    endpoint never runs. Endpoints carry NO auth code of their own, so a newly
    added ``/api`` route is protected by construction (fail-closed).

    A valid client ``?token=`` also makes the response Set the ``act`` cookie, so
    the browser's next request — and the SSE stream, which cannot send headers —
    authenticate via the cookie and the token leaves the browser's URLs after one
    hop. The server still accepting ``?token=`` keeps the bootstrap and curl/tools
    working without reintroducing a leak (the browser no longer emits it).

    Every response gets ``Referrer-Policy: no-referrer``."""

    def __init__(self, app, server: WebServer):
        self.app = app
        self.server = server

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        server = self.server
        client = scope.get("client")
        host = client[0] if client else None
        qs = scope.get("query_string", b"")

        valid_cookie = server.token_ok(_cookie_value(scope, _AUTH_COOKIE))
        valid_client = server.token_ok(_query_token(qs))
        authed = server.is_trusted_client(host) or valid_cookie or valid_client

        # Default-deny: reject an unauthenticated request to a protected path
        # before the endpoint runs.
        if _needs_auth(scope.get("path", "")) and not authed:
            await _send_401(send)
            return

        # Bootstrap: a valid ?token= installs the cookie so the browser stops
        # putting the token in per-request URLs.
        set_cookie = valid_client and not valid_cookie
        secure = scope.get("scheme") == "https" or any(
            k == b"x-forwarded-proto" and v == b"https"
            for k, v in scope.get("headers", [])
        )

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                from starlette.datastructures import MutableHeaders

                headers = MutableHeaders(raw=message.setdefault("headers", []))
                headers.append("Referrer-Policy", "no-referrer")
                if set_cookie:
                    headers.append(
                        "Set-Cookie",
                        _build_auth_cookie(
                            server.token, server.base_path, secure=secure
                        ),
                    )
            await send(message)

        await self.app(scope, receive, send_wrapper)


def create_app(server: WebServer) -> FastAPI:
    """Build the FastAPI app over a ``WebServer`` instance.

    Kept as a factory so tests can swap in a mock renderer / server and
    drive ``httpx.AsyncClient`` against an in-process app without
    spinning up uvicorn.
    """
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        # Startup: nothing to do — worker thread / renderer are wired
        # by the CLI caller before uvicorn starts.
        yield
        # Shutdown: uvicorn's SIGINT path cancels live tasks (sse-starlette
        # ping coroutines among them). Closing SSE generators first
        # means those tasks finish quietly instead of bubbling up
        # CancelledError tracebacks to stderr. Idempotent — main.py's
        # finally block calls the same teardown.
        server.renderer.shutdown_all_connections()

    app = FastAPI(title="agent-cli web", lifespan=_lifespan)
    # Single auth chokepoint: trust-local / cookie / bootstrap ?token= → inject
    # token so per-endpoint checks pass; a bootstrap ?token= sets the cookie;
    # Referrer-Policy on every response. Pure-ASGI so the injected token reaches
    # the endpoint context reliably (unlike a BaseHTTPMiddleware contextvar).
    app.add_middleware(_AuthMiddleware, server=server)

    @app.get("/")
    async def index():
        """Serve the static chat UI (no auth gate — the page holds no secrets;
        the API/SSE endpoints are auth-protected).

        When the request carried a valid bootstrap ``?token=``, ``_AuthMiddleware``
        Set the ``act`` cookie on this response, so the JS then authenticates by
        cookie and strips the token from the address bar.

        Injects ``<base href="<prefix>/">`` so the page's RELATIVE asset/API
        URLs resolve under ``--base-path`` (default ``/`` = root, unchanged)."""
        html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
        base_href = f"{server.base_path}/"  # "" → "/", "/s/doom" → "/s/doom/"
        html = html.replace("<head>", f'<head>\n  <base href="{base_href}">', 1)
        return HTMLResponse(html, headers=_NO_CACHE_HEADERS)

    if _STATIC_DIR.exists():
        app.mount(
            "/static",
            _NoCacheStaticFiles(directory=_STATIC_DIR),
            name="static",
        )

    @app.get("/api/health")
    async def health():
        """Unauthenticated liveness probe.

        ``busy`` mirrors the worker state (True while the agent is processing a
        turn / generating a response) and ``awaiting_input`` is True while an
        ask/confirm prompt waits for a reply — so a front controller (e.g. a
        board that spawns instances) can show "working" vs "needs your answer"
        vs "idle" without subscribing to the SSE stream. ``viewers`` is the live
        browser-subscriber count, so the controller can tell 'someone is
        watching' from 'nobody here' before disrupting the session."""
        return {
            "status": "ok",
            "busy": server.renderer.worker_is_busy(),
            "awaiting_input": server.renderer.is_awaiting_input(),
            "viewers": server.renderer.viewer_count(),
            # v7.10.0: 상주 에이전트 요약 (없으면 None) — status.json 과
            # 같은 소스(renderer.agents_summary), board 폴백 경로 파리티.
            "agents": getattr(server.renderer, "agents_summary", lambda: None)(),
        }

    @app.get("/api/share-url")
    async def share_url():
        """Return the instance token so the UI can build a token-bearing share
        link (the 🔗 button). Authenticated — only an already-authorised session
        (cookie / trust-local / token) may read it. The token is the instance's
        shared access credential (all viewers are equal), so handing it out is
        the intended way to invite a teammate; the frontend composes the full
        URL from ``<base href>`` (origin + base-path) + ``?token=``."""
        return {"token": server.token}

    @app.get("/api/debug/prompt")
    async def debug_prompt(task_id: str = Query("")):
        """Prompt Inspector data for a scope: the latest LLM call's system
        prompt as named sections with size figures. ``task_id`` selects a
        delegate sub-agent's prompt; empty (default) is the main loop. Token-
        authenticated; fetched on demand when the inspector drawer opens (the
        ~16KB payload is never pushed over SSE). ``ok=False`` before that
        scope's first LLM call."""
        snapshot = server.renderer.prompt_snapshot(task_id)
        # System sections (kind=system, from the latest LLM call's snapshot —
        # may be absent before the first call) + the live DYNAMIC context
        # (conversation + observations, kind=dynamic) for the MAIN scope.
        # Showing dynamic without a system snapshot is what fills the inspector
        # the moment a resumed session loads (ctx restored, no LLM call yet).
        # Sub-agent scopes (task_id) keep system-only — their ctx isn't here.
        system_sections = []
        turn = None
        if snapshot is not None:
            system_sections = [
                {**s, "kind": s.get("kind", "system")}
                for s in snapshot.get("sections", [])
            ]
            turn = snapshot.get("turn")
        # v4.52.0: 서브 스코프(agent/skill)도 동적 컨텍스트 — 렌더러가
        # 스코프별 live ctx(실행 중) 또는 종료-시 고정 스냅샷을 보유.
        if task_id:
            dynamic = server.renderer.scope_dynamic_sections(task_id)
        else:
            dynamic = _dynamic_context_sections(server.ctx)
        sections = system_sections + dynamic
        if not sections:
            reason = "no LLM call yet for this agent" if task_id else "no LLM call yet"
            return {"ok": False, "reason": reason}
        total_chars = sum(s["chars"] for s in sections) + 2 * max(0, len(sections) - 1)
        est_tokens = sum(s["est_tokens"] for s in sections)
        return {
            "ok": True,
            "task_id": task_id,
            "turn": turn if turn is not None else 0,
            "sections": sections,
            "total_chars": total_chars,
            "est_tokens": est_tokens,
        }

    @app.get("/api/directives")
    async def debug_directives():
        """프로젝트 ``DIRECTIVE.md`` — 스코프 에디터용으로 원문(content)과
        스코프 분해(scopes: common/main/agents)를 함께 반환. 파일이 없어도
        빈 폼으로 항상 표시. 프로젝트 파일 전용(전역 파일은 손편집)."""
        from agent_cli.prompts.system_prompt import (
            project_directive_file,
            read_project_directive,
            split_directive_scopes,
        )

        content = read_project_directive()
        return {
            "content": content,
            "scopes": split_directive_scopes(content),
            "path": str(project_directive_file()),
        }

    @app.post("/api/directives")
    async def debug_directives_save(body: dict):
        """저장 — ``{scopes:{common,main,agents}}``(에디터, 서버가 조립) 또는
        ``{content}``(원문 그대로). 다음 LLM 콜에서 시스템 프롬프트 rebuild
        (KV prefix 리셋), 다른 인스펙터에 브로드캐스트."""
        from agent_cli.prompts.system_prompt import (
            join_directive_scopes,
            write_project_directive,
        )

        if isinstance(body.get("scopes"), dict):
            content = join_directive_scopes(body["scopes"])
        else:
            content = body.get("content") or ""
        write_project_directive(content)
        server.renderer.mark_directives_dirty()
        server.renderer.broadcast_directives_changed()
        return {"ok": True}

    @app.get("/api/compaction")
    async def get_compaction():
        """현재 compaction 목표 비율 + 슬라이더 범위(min/max/step). 라이브
        ctx 가 없으면(headless) 기본값. 프론트가 슬라이더 초기화에 사용."""
        from agent_cli.context.manager import (
            COMPACTION_RATIO_MAX,
            COMPACTION_RATIO_MIN,
            COMPACTION_RATIO_STEP,
            DEFAULT_COMPACTION_RATIO,
        )

        ratio = (
            server.ctx.compaction_ratio
            if server.ctx is not None
            else DEFAULT_COMPACTION_RATIO
        )
        return {
            "ratio": ratio,
            "min": COMPACTION_RATIO_MIN,
            "max": COMPACTION_RATIO_MAX,
            "step": COMPACTION_RATIO_STEP,
        }

    @app.post("/api/compaction")
    async def set_compaction(body: dict):
        """세션 한정 compaction 목표 비율 변경. web 과 loop 이 같은 ctx 를
        공유하므로 다음 LLM 콜이 즉시 새 값을 읽는다(rebuild 불필요). 값은
        [min,max] 로 clamp 되고, 다른 뷰어엔 sticky 브로드캐스트로 슬라이더
        동기화. 서브에이전트는 spawn/resume 시점에 이 값을 상속(기존 서브는
        불변 — 재spawn 으로 적용)."""
        if server.ctx is None:
            return {"ok": False, "error": "no active context"}
        try:
            ratio = float(body.get("ratio"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "ratio must be a number"}
        clamped = server.ctx.set_compaction_ratio(ratio)
        server.renderer.broadcast_compaction_ratio(clamped)
        return {"ok": True, "ratio": clamped}

    @app.get("/api/confirm-mode")
    async def get_confirm_mode():
        """⚡ 자동 승인(확인 없이 실행) 현재 상태 — 헤더 체크박스 초기화용."""
        return {"auto_approve": bool(getattr(server.renderer, "_auto_approve", False))}

    @app.post("/api/confirm-mode")
    async def set_confirm_mode(body: dict):
        """세션 한정 자동 승인 토글. 켜면 위험 명령/경로 확인 프롬프트를 건너뛰고
        자동 허용(타임라인에 관찰로 기록). 런타임 전용 — 재시작 시 OFF 로 리셋.
        다른 뷰어엔 sticky 브로드캐스트로 체크박스 동기화."""
        on = bool(body.get("auto_approve"))
        server.renderer.set_auto_approve(on)
        return {"ok": True, "auto_approve": on}

    @app.get("/api/thinking")
    async def get_thinking():
        """세션 thinking 오버라이드(사고 on/off·reasoning_effort). ctx 없으면 빈값.
        `{enable_thinking: null|bool, reasoning_effort: null|str}` — 헤더 컨트롤 초기화."""
        ov = server.ctx.thinking_override if server.ctx is not None else {}
        return {
            "enable_thinking": ov.get("enable_thinking"),
            "reasoning_effort": ov.get("reasoning_effort"),
        }

    @app.post("/api/thinking")
    async def set_thinking(body: dict):
        """세션 thinking 오버라이드 변경 — 다음 LLM 콜에 즉시 반영(공유 ctx). None/
        "auto" = 미설정(모델 기본). 다른 뷰어엔 sticky 브로드캐스트로 컨트롤 동기화."""
        if server.ctx is None:
            return {"ok": False, "error": "no active context"}
        et = body.get("enable_thinking")
        eff = body.get("reasoning_effort")
        if eff in ("auto", ""):
            eff = None
        ov = server.ctx.set_thinking_override(enable_thinking=et, reasoning_effort=eff)
        server.renderer.broadcast_thinking(ov)
        return {"ok": True, **ov}

    @app.get("/api/max-agents")
    async def get_max_agents():
        """현재 동시 생존 에이전트 상한 + 입력 최소값/기본값. 레지스트리가
        없으면(headless·부트 전) 기본값. 프론트가 입력·체크박스 초기화에
        사용. value=0 이면 무제한(unlimited=True)."""
        from agent_cli.subagent.agents_live import (
            _DEFAULT_MAX_AGENTS,
            MAX_AGENTS_MIN,
        )

        reg = server.agent_registry
        value = reg.max_agents if reg is not None else _DEFAULT_MAX_AGENTS
        return {
            "value": value,
            "min": MAX_AGENTS_MIN,
            "default": _DEFAULT_MAX_AGENTS,
            "unlimited": value == 0,
        }

    @app.post("/api/max-agents")
    async def set_max_agents(body: dict):
        """세션 한정 에이전트 상한 변경. ``value <= 0`` 이면 무제한. 레지스트리
        가 즉시 새 값을 읽어(다음 spawn/resume 게이트) 반영되고, 다른 뷰어엔
        sticky 브로드캐스트로 입력/체크박스 동기화. 상한을 낮춰도 기존 에이전트
        는 안 죽임 — 새 spawn 만 막음."""
        reg = server.agent_registry
        if reg is None:
            return {"ok": False, "error": "agent registry not ready"}
        try:
            value = int(body.get("value"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "value must be an integer"}
        stored = reg.set_max_agents(value)
        server.renderer.broadcast_max_agents(stored)
        return {"ok": True, "value": stored, "unlimited": stored == 0}

    @app.post("/api/directives/generate")
    async def directives_generate(body: dict):
        """✨ 생성 — 대략적 의도(brief)를 해당 청중(audience)용 directive
        초안으로. 산문 직접 호출(provider.call + 구조적 새니타이저 — Qwen3.6
        재실측 16/16 무누출, 5.7.0), 결과는 **미저장** 반환(에디터가 활성 탭에
        반영, 사용자 검토 후 저장). Body: ``{audience, brief, current?}``.
        LLM 미배선 503. 수십 초 블로킹 — executor 오프로드."""
        if not server.runtime or server.runtime.get("provider") is None:
            raise HTTPException(status_code=503, detail="LLM not available")

        def _run() -> str:
            return generate_directive_section(
                str(body.get("audience") or ""),
                str(body.get("brief") or ""),
                str(body.get("current") or ""),
                runtime=server.runtime,
            )

        try:
            content = await asyncio.get_event_loop().run_in_executor(None, _run)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:  # provider/network — 502 로 표면화
            raise HTTPException(status_code=502, detail=f"생성 실패: {e}") from e
        return {"content": content}

    @app.get("/api/debug/prompt/scopes")
    async def debug_prompt_scopes():
        """Scopes that currently have a captured system prompt — the main loop
        plus any delegate sub-agents — for the inspector's scope chip row."""
        return {"ok": True, "scopes": server.renderer.prompt_scopes()}

    @app.delete("/api/debug/prompt")
    async def debug_prompt_delete(task_id: str = Query(...)):
        """Drop a sub-agent's captured prompt (inspector ✕ button). Main is
        not deletable (it regenerates every turn)."""
        removed = server.renderer.delete_prompt_scope(task_id)
        return {"ok": True, "removed": removed}

    @app.get("/api/export/jira/targets")
    async def export_jira_targets():
        """Configured Jira instance names + base URLs (+ deployment) for the
        export dropdown. Token-authenticated; NEVER returns credentials (none
        are stored server-side). Each target's ``deployment`` is the
        config-pinned value or, when absent, probed from serverInfo so the UI
        pre-selects the right credential fields. Empty list when no Jira is
        configured (the UI then disables the Jira option)."""
        from agent_cli.config import load_config
        from agent_cli.integrations import jira as jira_mod

        targets = jira_mod.list_targets(load_config())
        for t in targets:
            if not t.get("deployment"):
                t["deployment"] = jira_mod.detect_deployment(t["base_url"])
        return {"ok": True, "targets": targets}

    @app.post("/api/export/html")
    async def export_html(request: Request):
        """Render selected transcript entries to a self-contained HTML doc and
        return it as a download. Body: ``{title?, entries: [...]}``. Read-only,
        so token-auth (no controller check) — any authenticated viewer may
        export what they can see."""
        from agent_cli.integrations import export as export_mod

        try:
            body = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        entries = body.get("entries")
        if not isinstance(entries, list):
            raise HTTPException(status_code=400, detail="entries must be a list")
        title = body.get("title") or ""
        # Rendering a long transcript to HTML is CPU-bound — off the loop.
        doc = await asyncio.get_event_loop().run_in_executor(
            None, lambda: export_mod.entries_to_html(entries, title=str(title))
        )
        return Response(
            content=doc,
            media_type="text/html; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="agent-cli-export.html"'
            },
        )

    @app.post("/api/export/jira")
    async def export_jira(request: Request):
        """Post selected transcript entries as ONE Jira comment, AS THE
        FRONTEND USER. Body: ``{target?, base_url?, issue_key, deployment?,
        entries: [...], auth: {user, secret}}``. ``base_url`` (optional) lets the
        user point at a URL not in config — works with no config at all, but an
        unconfigured URL must be https. Otherwise the named instance is resolved
        from config. Renders entries to ADF (Cloud) or wiki markup (Server/DC)
        per ``deployment`` and POSTs with the user-supplied credentials, which
        are used ONLY for this request — never logged or persisted. Returns
        ``{ok, url}`` or 400 with the error."""
        from agent_cli.config import load_config
        from agent_cli.integrations import export as export_mod
        from agent_cli.integrations import jira as jira_mod

        try:
            body = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        entries = body.get("entries")
        if not isinstance(entries, list):
            raise HTTPException(status_code=400, detail="entries must be a list")
        issue_key = body.get("issue_key") or ""
        target = body.get("target")
        auth = body.get("auth") or {}
        user = str(auth.get("user") or "").strip()
        secret = str(auth.get("secret") or "")
        if not user or not secret:
            raise HTTPException(
                status_code=400,
                detail="Jira credentials are required (your account + token/password).",
            )
        try:
            inst = jira_mod.resolve_target(load_config(), target, body.get("base_url"))
            deployment = (
                jira_mod._normalize_deployment(body.get("deployment"))
                or inst.get("deployment")
                or jira_mod.detect_deployment(inst["base_url"])
                or "cloud"
            )
            if deployment == "server":
                comment_body = export_mod.entries_to_wiki(entries)
            else:
                comment_body = export_mod.entries_to_adf(entries)
            url = jira_mod.post_comment(
                inst["base_url"],
                deployment,
                user,
                secret,
                issue_key,
                comment_body,
            )
        except jira_mod.JiraError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return JSONResponse(
            {"ok": True, "url": url, "target": inst["name"], "deployment": deployment}
        )

    @app.get("/api/workspace/tree")
    async def workspace_tree(path: str = Query("")):
        """List one directory level of the workspace (lazy tree expansion).
        Returns ``{path, entries:[{name, type, size}]}`` — dirs first, then
        files, name-sorted. Read-only, token-auth."""
        d = server._safe_workspace_path(path)
        if not d.is_dir():
            raise HTTPException(status_code=400, detail="not a directory")

        def _dir_size(p: Path) -> int:
            total = 0
            for f in p.rglob("*"):
                try:
                    if f.is_file():
                        total += f.stat().st_size
                except OSError:
                    pass
            return total

        def _scan() -> list[dict]:
            entries = []
            for child in sorted(
                d.iterdir(), key=lambda c: (c.is_file(), c.name.lower())
            ):
                is_dir = child.is_dir()
                try:
                    size = _dir_size(child) if is_dir else child.stat().st_size
                except OSError:
                    size = 0
                entries.append(
                    {
                        "name": child.name,
                        "rel": str(child.resolve().relative_to(server.workspace)),
                        "type": "dir" if is_dir else "file",
                        "size": size,
                    }
                )
            return entries

        # Recursive dir sizing walks the whole subtree (node_modules/.git …) —
        # run it in the executor so the asyncio thread (and every viewer's SSE
        # delivery) is not stalled for the duration.
        entries = await asyncio.get_event_loop().run_in_executor(None, _scan)
        return JSONResponse({"path": path, "entries": entries})

    @app.post("/api/workspace/download")
    async def workspace_download(request: Request):
        """Zip the selected workspace paths and return the archive, deleting
        the temp file after send. Body: ``{paths:[rel...], all?:bool}``. A dir
        is added recursively; a file individually. Read-only, token-auth."""
        try:
            body = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        rels = ["."] if body.get("all") else (body.get("paths") or [])
        if not isinstance(rels, list) or not rels:
            raise HTTPException(status_code=400, detail="no paths selected")

        targets = [server._safe_workspace_path(r) for r in rels]
        tmp_fd, tmp_name = tempfile.mkstemp(suffix=".zip")
        os.close(tmp_fd)

        def _build_zip() -> None:
            with zipfile.ZipFile(tmp_name, "w", zipfile.ZIP_DEFLATED) as zf:
                seen: set[Path] = set()
                for p in targets:
                    if not p.exists():
                        continue
                    files = (
                        (f for f in p.rglob("*") if f.is_file()) if p.is_dir() else [p]
                    )
                    for f in files:
                        if f in seen:
                            continue
                        seen.add(f)
                        zf.write(f, f.relative_to(server.workspace))

        try:
            # CPU-bound deflate + recursive walk — keep it off the event loop
            # (a large workspace download must not freeze SSE for all viewers).
            await asyncio.get_event_loop().run_in_executor(None, _build_zip)
        except Exception:
            os.unlink(tmp_name)
            raise
        name = "workspace" if body.get("all") else server.workspace.name
        return FileResponse(
            tmp_name,
            media_type="application/zip",
            filename=f"{name}.zip",
            background=BackgroundTask(os.unlink, tmp_name),
        )

    @app.post("/api/workspace/delete")
    async def workspace_delete(request: Request):
        """Delete selected workspace paths. Body: ``{paths:[rel...]}``. A file
        is unlinked, a directory removed recursively. WRITE + DESTRUCTIVE, so
        the strictest guards: under-workspace only (``_safe_workspace_path``),
        and the workspace ROOT itself is never deletable. Per-path errors are
        reported (not fatal) so one bad path doesn't abort the rest."""
        import shutil

        body = await request.json()
        rels = body.get("paths") or []
        if not rels:
            raise HTTPException(status_code=400, detail="no paths given")
        # Validate ALL paths up front (raises 400 before anything is deleted);
        # the recursive rmtree work then runs in the executor so a big delete
        # doesn't stall the event loop.
        checked: list[tuple[str, Path]] = []
        for rel in rels:
            target = server._safe_workspace_path(rel)  # raises 400 on traversal
            if target == server.workspace:
                raise HTTPException(
                    status_code=400, detail="refusing to delete the workspace root"
                )
            checked.append((rel, target))

        def _delete() -> tuple[list[str], list[dict]]:
            deleted: list[str] = []
            errors: list[dict] = []
            for rel, target in checked:
                try:
                    if target.is_dir():
                        shutil.rmtree(target)
                    elif target.exists():
                        target.unlink()
                    else:
                        errors.append({"path": rel, "error": "not found"})
                        continue
                    deleted.append(str(target.relative_to(server.workspace)))
                except OSError as e:
                    errors.append({"path": rel, "error": str(e)})
            return deleted, errors

        deleted, errors = await asyncio.get_event_loop().run_in_executor(None, _delete)
        return JSONResponse({"deleted": deleted, "errors": errors})

    @app.post("/api/workspace/upload")
    async def workspace_upload(
        request: Request,
        name: str = Query(..., min_length=1),
        path: str = Query(""),
    ):
        """Upload one file into the workspace. Body = raw file bytes (no
        python-multipart dep — the frontend loops one request per file).

        ``name`` is the file's path RELATIVE to the target ``path`` — a bare
        name (``a.txt``) for a single file, or a ``/``-joined path
        (``mydir/sub/a.c``) for directory uploads. The nested dirs are created
        under ``path``; intermediate ``..``/absolute/backslash segments are
        rejected and the final path is re-checked under the workspace.

        Guards (this WRITES, so stricter than download):
        - every ``name`` segment is non-empty and not ``.``/``..``; no leading
          ``/`` (absolute) or ``\\``.
        - ``path`` (where the upload is rooted) resolves under the workspace
          (``_safe_workspace_path``) and must already exist; the resolved final
          destination must also be strictly under the workspace.
        - size capped at ``_MAX_UPLOAD_BYTES`` (413 over).
        Overwrites an existing file (the user's own workspace) but reports it.
        """
        segments = name.split("/")
        if (
            not name
            or name.startswith("/")
            or "\\" in name
            or any(seg in ("", ".", "..") for seg in segments)
        ):
            raise HTTPException(status_code=400, detail="invalid filename")
        target_dir = server._safe_workspace_path(path)
        if not target_dir.is_dir():
            raise HTTPException(status_code=400, detail="target dir does not exist")
        dest = server._safe_workspace_path(os.path.join(path, name) if path else name)
        body = await request.body()
        if len(body) > _MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"file too large (max {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB)",
            )
        overwritten = dest.exists()

        def _write() -> None:
            # Create the nested dirs (under the already-validated target).
            # Safe: ``_safe_workspace_path`` confirmed ``dest`` resolves under
            # the workspace, so its parents do too. Runs in the executor — a
            # multi-MB write on slow storage must not stall the event loop.
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(body)

        await asyncio.get_event_loop().run_in_executor(None, _write)
        return JSONResponse(
            {
                "name": segments[-1],
                "rel": str(dest.resolve().relative_to(server.workspace)),
                "size": len(body),
                "overwritten": overwritten,
            }
        )

    @app.get("/api/stream")
    async def stream():
        """SSE event stream. Token-authenticated; multi-viewer (all equal)."""
        conn = WebConnection(id=secrets.token_hex(8))
        return EventSourceResponse(server.stream_events(conn))

    @app.post("/api/agent/{key}/input")
    async def agent_input(key: str, request: Request):
        """teammate 대화 창의 인간 개입 (P4, D8) — 해당 teammate 의 inbox 로
        직접 전송. teammate 가 ask 답변 대기(waiting_ask) 중이면 이 메시지가
        답으로 소비된다 (main 과 선착순). 이 문답의 회신은 main 컨텍스트에
        배달되지 않는다 (창에만 — 레지스트리의 화자 규칙)."""
        registry = server.agent_registry
        if registry is None:
            raise HTTPException(status_code=503, detail="agent registry not ready")
        try:
            body = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        content = body.get("content", "")
        if not isinstance(content, str) or not content.strip():
            raise HTTPException(
                status_code=400, detail="content must be a non-empty string"
            )
        nickname = server.renderer.nickname_for(body.get("conn_id")) or "user"
        error = registry.request(key, content, author=f"user:{nickname}")
        if error:
            raise HTTPException(status_code=404, detail=error)
        return JSONResponse({"accepted": True})

    @app.post("/api/agent/{key}/resume")
    async def agent_resume(key: str):
        """죽은 teammate 를 이전 컨텍스트 그대로 부활 (대화 창의 ↻)."""
        registry = server.agent_registry
        if registry is None:
            raise HTTPException(status_code=503, detail="agent registry not ready")
        error = registry.resume_teammate(key)
        if error:
            code = 409 if "still alive" in error or "limit" in error else 404
            raise HTTPException(status_code=code, detail=error)
        return JSONResponse({"ok": True})

    @app.post("/api/agent/{key}/kill")
    async def agent_kill(key: str):
        """teammate 종료 (P4 창의 ✕) — kill 은 worker join(≤2s)을 포함하므로
        이벤트 루프를 막지 않게 executor 로 오프로드."""
        registry = server.agent_registry
        if registry is None:
            raise HTTPException(status_code=503, detail="agent registry not ready")
        loop = asyncio.get_running_loop()
        error = await loop.run_in_executor(None, registry.kill, key)
        if error:
            raise HTTPException(status_code=404, detail=error)
        return JSONResponse({"ok": True})

    @app.post("/api/input")
    async def input_endpoint(request: Request):
        """User input → renderer queue.

        Body shape::

            {"kind": "prompt", "content": "..."}
            {"kind": "confirm", "key": "y", "comment": "..."}
            {"kind": "chat", "content": "..."}

        ``chat`` is the only kind that advances the AgentLoop directly;
        ``prompt`` / ``confirm`` answer an in-flight render call. Every
        authenticated connection may send input (no controller gate).
        """
        try:
            body = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        kind = body.get("kind")
        if kind == "chat":
            content = body.get("content", "")
            if not isinstance(content, str):
                raise HTTPException(
                    status_code=400, detail="chat content must be a string"
                )
            # Enqueue (no immediate echo): the message shows in the live queue
            # display until it's dequeued — by the worker to START a run, or by
            # the running loop at a turn boundary to INJECT — at which point it
            # is rendered as a conversation card.
            server.enqueue(body.get("conn_id"), content)
            return JSONResponse({"accepted": True})
        if (
            kind in ("prompt", "confirm")
            and server.renderer.awaiting_input_kind() != kind
        ):
            # Gate: accept an answer only while a wait of the SAME kind is
            # outstanding. A keyless answer (flushed stale clicks from a
            # connection-starved browser, the loser of a two-viewers race)
            # must not sit in the input queue and auto-answer the NEXT
            # prompt; a kind mismatch would feed a confirm tuple to a
            # prompt wait expecting a string (or vice versa).
            raise HTTPException(
                status_code=409,
                detail=f"no pending {kind} — already answered or aborted",
            )
        if kind == "prompt":
            # Echo prompt answers so the UI shows the user's reply
            # immediately. Semantic note: the LLM gets the answer via
            # the ask-tool's observation return (Q/A pair), NOT as a
            # standalone user message — but for the user a silent
            # textarea clear feels broken. The user_message card is a
            # UI-only echo; doesn't change LLM context.
            content = body.get("content", "")
            if isinstance(content, str) and content:
                # ``author`` puts the answer on the team view's user lane.
                # UI-echo only (not in history), so it never feeds the
                # ``answers`` attribution — that is loop-owned.
                nick = server.renderer.nickname_for(body.get("conn_id"))
                server.renderer.push_user_message(
                    content, author="" if nick == "?" else nick
                )
            server.renderer.push_user_input(kind, body)
            return JSONResponse({"accepted": True})
        if kind == "confirm":
            server.renderer.push_user_input(kind, body)
            return JSONResponse({"accepted": True})
        raise HTTPException(status_code=400, detail=f"unknown kind '{kind}'")

    @app.post("/api/queue/cancel")
    async def queue_cancel(request: Request):
        """Cancel a still-pending queued message. Body: ``{conn_id, id}`` —
        only the owner (matching ``conn_id``) may cancel; already-dequeued
        messages can't be cancelled. Returns ``{cancelled: bool}``."""
        try:
            body = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        ok = server.cancel_pending(body.get("conn_id"), str(body.get("id", "")))
        return JSONResponse({"cancelled": ok})

    @app.post("/api/nickname")
    async def set_nickname(request: Request):
        """Set the caller's display nickname. Body: ``{conn_id, name}``. The
        UI pre-fills the assigned fun default; the user edits/confirms."""
        try:
            body = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        ok = server.renderer.set_nickname(body.get("conn_id"), body.get("name", ""))
        return JSONResponse({"ok": ok})

    @app.post("/api/abort")
    async def abort():
        """Interrupt the current ``prompt_user`` / ``confirm`` wait."""
        server.renderer.push_abort()
        return JSONResponse({"accepted": True})

    @app.post("/api/stop")
    async def stop():
        """Stop the in-flight chat turn at the next turn boundary.

        Sets the worker's ``stop_event`` (same mechanism as Ctrl+C in
        the CLI). ``stopped`` is ``False`` when no turn was active.
        """
        stopped = server.trigger_stop()
        return JSONResponse({"stopped": stopped})

    return app


__all__ = ["WebServer", "create_app"]
