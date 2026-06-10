"""FastAPI server backing ``agent-cli web``.

Endpoints:
  - ``GET  /api/health`` — liveness probe (no auth)
  - ``GET  /api/stream`` — SSE event stream (auth via ``token`` query)
  - ``POST /api/input``  — submit chat message / ask answer / confirm reply
  - ``POST /api/abort``  — interrupt the current ``prompt_user`` / ``confirm``
  - ``POST /api/stop``   — stop the in-flight chat/skill/agent turn

Auth: every authenticated endpoint requires the ``token`` query param to
match ``WebServer.token``. The token is generated at startup (or
provided by ``--token``) and printed to stdout so the operator can share
the URL with the LAN.

Single-active-client: only one SSE connection at a time. A second
connection takes over — the first receives a ``takeover`` event and
disconnects cleanly. Implemented via :meth:`WebRenderer.register_connection`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import socket
import threading
import subprocess
from pathlib import Path
from queue import Empty, SimpleQueue

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from agent_cli.constants import SHELL_COMMAND_TIMEOUT
from agent_cli.render.web import WebConnection, WebRenderer

_STATIC_DIR = Path(__file__).resolve().parent / "static"

# ``no-cache`` (revalidate-required) rather than ``no-store`` so the
# browser can still take a 304 fast path when nothing changed, but a
# CSS/JS edit lands without forcing the operator to hard-refresh.
# Editable installs serve files straight from the git checkout, so an
# in-session iteration would otherwise be invisible until the operator
# bypassed cache manually.
_NO_CACHE_HEADERS = {"Cache-Control": "no-cache, must-revalidate"}


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


def pick_port(host: str, preferred: int) -> int:
    """Pick a bindable port for the web server.

    Prefer ``preferred`` (default 8080) when free; if it's bound by
    another process, fall back to an OS-assigned ephemeral port. The
    caller passes the result straight to ``uvicorn.Config(port=...)``,
    so the URL printed before ``server_obj.run()`` shows whatever the
    OS actually gave us.

    Bind to ``host`` (not ``localhost``) so a port that's only free on
    the loopback interface but bound LAN-wide doesn't fool the probe.
    The socket is closed before returning — there's a tiny TOCTOU
    window before uvicorn re-binds, but a same-host race in that
    window is rare enough that handling it would cost more than it
    saves.
    """
    for candidate in (preferred, 0):
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


_WEB_HELP_TEXT = (
    "Web mode commands:\n"
    "  /help                    Show this help\n"
    "  /compact                 Compact context now (summarise oldest half)\n"
    "  /sh <command>            Run a shell command directly (LLM bypass)\n"
    "  /skills                  List available skills\n"
    "  /<skill> <args>          Invoke a skill directly\n"
    "  @agents                  List available agents\n"
    "  @<agent> <task>          Delegate a task to an agent\n"
    "\n"
    "Any other input goes to the LLM as a chat turn."
)


def handle_slash_command(message: str, renderer: WebRenderer, ctx=None) -> bool:
    """Intercept web-specific commands.

    Returns ``True`` if the message was handled here (caller skips
    further dispatch / LLM); ``False`` otherwise. Output surfaces as
    an ``observation`` event so the frontend renders it as a tool-
    result card alongside whatever else is in the session.

    Handled:
      - ``/help`` — list supported web commands
      - ``/sh <cmd>`` — direct shell execution (no CLI parity yet)
      - ``/compact`` — manual context compaction (needs ``ctx``)

    ``@<agent>`` / ``/<skill>`` (including the ``@agents`` /
    ``/skills`` listings and not-found errors) are routed through
    :func:`agent_cli.main.try_dispatch_agent_or_skill` so chat REPL
    and web share one prefix-dispatcher with a thin output adapter
    per surface — see ``WebDispatchOutput`` below.
    """
    if message == "/help":
        renderer.observation(
            _WEB_HELP_TEXT,
            turn=0,
            tool_name="help",
            success=True,
        )
        return True

    if message == "/compact" or message.startswith("/compact "):
        if ctx is None:
            renderer.observation(
                "Compaction unavailable in this session.",
                turn=0,
                tool_name="compact",
                success=False,
            )
            return True
        before, after = ctx.compact_now()
        if after < before:
            msg = f"Compacted: {before:,} → {after:,} tokens."
        else:
            msg = (
                f"Nothing to compact ({before:,} / {ctx.max_context_tokens:,} tokens)."
            )
        renderer.observation(msg, turn=0, tool_name="compact", success=True)
        return True

    if message.startswith("/sh"):
        return _handle_sh(message, renderer)

    return False


class WebDispatchOutput:
    """Web-flavoured ``DispatchOutput`` — every branch maps to a single
    ``observation`` event so the frontend renders consistent tool-result
    cards for listings, errors, and agent results.

    Lives in this module (next to ``handle_slash_command``) because
    it's the only place that needs ``WebRenderer`` knowledge; keeping
    ``agent_cli.main`` free of web-renderer imports preserves the
    optional-extra boundary (``pip install agent-cli`` without ``[web]``
    must still work).
    """

    def __init__(self, renderer: WebRenderer) -> None:
        self.renderer = renderer

    def list_agents(self, names: list[str]) -> None:
        if not names:
            self.renderer.observation(
                "No agents found.",
                turn=0,
                tool_name="agents",
                success=True,
            )
            return
        lines = ["Available agents:"]
        for name in names:
            lines.append(f"  @{name}")
        lines.append("")
        lines.append("Invoke with ``@<agent> <task>``.")
        self.renderer.observation(
            "\n".join(lines),
            turn=0,
            tool_name="agents",
            success=True,
        )

    def list_skills(self, skills: dict) -> None:
        user_skills = {k: v for k, v in skills.items() if v.user_invocable}
        if not user_skills:
            self.renderer.observation(
                "No skills available.",
                turn=0,
                tool_name="skills",
                success=True,
            )
            return
        lines = ["Available skills:"]
        for s in user_skills.values():
            hint = f" {s.argument_hint}" if s.argument_hint else ""
            lines.append(f"  /{s.name}{hint} — {s.description}")
        lines.append("")
        lines.append(
            "Invoke directly with ``/<skill> <args>`` or let the LLM call ``run_skill``."
        )
        self.renderer.observation(
            "\n".join(lines),
            turn=0,
            tool_name="skills",
            success=True,
        )

    def agent_not_found(self, name: str) -> None:
        self.renderer.observation(
            f"Agent not found: @{name}. Type ``@agents`` to list available agents.",
            turn=0,
            tool_name=f"@{name}",
            success=False,
        )

    def agent_result(self, result) -> None:
        # No-op. The delegate path (``_dispatch_agent`` →
        # ``tool_delegate``) already emits the final answer through
        # the renderer's observation channel — re-emitting here would
        # surface the same body twice in the chat thread.
        del result

    def skill_not_found(self, name: str) -> None:
        self.renderer.observation(
            f"Unknown command: /{name}. Type /help for available commands.",
            turn=0,
            tool_name=f"/{name}",
            success=False,
        )

    def skill_result(self, name: str, result) -> None:
        # Same rationale as ``agent_result``: ``_dispatch_skill`` calls
        # ``render_group_end`` which the frontend uses to close the
        # nested skill panel. Re-emitting the answer here would
        # duplicate. The ``None`` (stopped without final) case is
        # already visible via the unsuccessful group_end.
        del name, result


def _handle_sh(message: str, renderer: WebRenderer) -> bool:
    """``/sh <command>`` — run a shell command, render output as a
    tool-result observation card."""
    cmd = message[3:].lstrip()
    if not cmd:
        renderer.observation(
            "Usage: /sh <command>",
            turn=0,
            tool_name="sh",
            success=False,
        )
        return True
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=SHELL_COMMAND_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        renderer.observation(
            f"Command timed out ({SHELL_COMMAND_TIMEOUT}s)",
            turn=0,
            tool_name="sh",
            success=False,
        )
        return True
    parts: list[str] = []
    if result.stdout:
        parts.append(result.stdout)
    if result.stderr:
        parts.append(result.stderr)
    if result.returncode != 0:
        parts.append(f"[exit code: {result.returncode}]")
    output = "".join(parts) or "(no output)"
    renderer.observation(
        output,
        turn=0,
        tool_name="sh",
        success=result.returncode == 0,
    )
    return True


class WebServer:
    """Owns the renderer + the worker thread that drives AgentLoop.

    The FastAPI app delegates all stateful operations here so the
    request handlers stay thin and unit-testable.
    """

    # Identity sentinel pushed onto ``_chat_queue`` from
    # :meth:`shutdown` so the worker thread's blocking ``pop_chat``
    # call wakes up and breaks its loop cleanly. ``is`` comparison
    # (identity, not equality) keeps the sentinel safe even if a user
    # happens to type a chat message whose value collides.
    SHUTDOWN = object()

    def __init__(
        self,
        renderer: WebRenderer,
        token: str | None = None,
    ) -> None:
        self.renderer = renderer
        # ``secrets.token_urlsafe`` gives a URL-safe random token —
        # ``--token`` override sticks if provided.
        self.token = token or secrets.token_urlsafe(32)
        # Queue of pending top-level user chat messages — the worker
        # thread pops one, runs the loop, repeats. Bounded queue is
        # unnecessary (single active client, throttled by SSE flow).
        self._chat_queue: SimpleQueue = SimpleQueue()
        # Stop handle for the in-flight chat turn. The worker registers a
        # fresh ``threading.Event`` per message and passes it to
        # ``run_loop(stop_event=…)``; ``/api/stop`` sets it so the loop
        # exits at the next turn boundary (same path as Ctrl+C in chat).
        # Guarded by a lock — set from the worker thread, read from the
        # request handler thread.
        self._stop_lock = threading.Lock()
        self._stop_handle: threading.Event | None = None

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

    def push_chat(self, message: str) -> None:
        """Queue a top-level chat message for the worker loop."""
        self._chat_queue.put(message)

    def pop_chat(self, timeout: float | None = None):
        """Worker-side: pop the next chat message (blocks).

        Returns ``WebServer.SHUTDOWN`` if :meth:`shutdown` was called,
        a string for normal messages, or ``None`` on poll timeout.
        Workers must compare with ``is WebServer.SHUTDOWN`` and break.
        """
        try:
            return self._chat_queue.get(timeout=timeout)
        except Empty:
            return None

    def shutdown(self) -> None:
        """Wake any worker blocked in :meth:`pop_chat`.

        Pushes the :attr:`SHUTDOWN` sentinel onto the chat queue so the
        worker thread's ``get()`` returns immediately. Idempotent — a
        second call just queues another sentinel (worker exits on the
        first).
        """
        self._chat_queue.put(self.SHUTDOWN)

    # ─── Auth helper ──────────────────────────────────────────

    def _require_token(self, token: str | None) -> None:
        """Constant-time compare against the configured token.

        Constant-time avoids timing-side-channel leaks on the LAN —
        cheap and standard for shared-secret schemes.
        """
        if token is None or not secrets.compare_digest(token, self.token):
            raise HTTPException(status_code=401, detail="invalid or missing token")

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
            yield {"event": event, "data": json.dumps(data, ensure_ascii=False)}

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
                yield {"event": event, "data": json.dumps(data, ensure_ascii=False)}
                if event == "takeover":
                    # Honour the contract: takeover ends this stream.
                    break
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

    @app.get("/")
    async def index():
        """Serve the static chat UI. JS reads ``?token=…`` from the
        URL — no auth gate here because the page itself contains no
        secrets; the SSE / input endpoints are token-protected."""
        return FileResponse(_STATIC_DIR / "index.html", headers=_NO_CACHE_HEADERS)

    if _STATIC_DIR.exists():
        app.mount(
            "/static",
            _NoCacheStaticFiles(directory=_STATIC_DIR),
            name="static",
        )

    @app.get("/api/health")
    async def health():
        """Unauthenticated liveness probe."""
        return {"status": "ok"}

    @app.get("/api/debug/prompt")
    async def debug_prompt(token: str = Query(...)):
        """Prompt Inspector data: the latest LLM call's system prompt as
        named sections with size figures. Token-authenticated; fetched on
        demand when the inspector drawer opens (the ~16KB payload is never
        pushed over SSE). 404-shape (ok=False) before the first LLM call."""
        server._require_token(token)
        snapshot = server.renderer.prompt_snapshot()
        if snapshot is None:
            return {"ok": False, "reason": "no LLM call yet"}
        return {"ok": True, **snapshot}

    @app.get("/api/stream")
    async def stream(token: str = Query(...)):
        """SSE event stream. Token-authenticated, takeover-aware."""
        server._require_token(token)
        conn = WebConnection(id=secrets.token_hex(8))
        return EventSourceResponse(server.stream_events(conn))

    @app.post("/api/input")
    async def input_endpoint(request: Request, token: str = Query(...)):
        """User input → renderer queue.

        Body shape::

            {"kind": "prompt", "content": "..."}
            {"kind": "confirm", "key": "y", "comment": "..."}
            {"kind": "chat", "content": "..."}   # top-level chat msg

        ``chat`` is the only kind that advances the AgentLoop directly;
        ``prompt`` / ``confirm`` answer an in-flight render call.
        """
        server._require_token(token)
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
            # Echo as persistent event so the frontend renders it, then
            # queue for the worker loop.
            server.renderer.push_user_message(content)
            server.push_chat(content)
            return JSONResponse({"accepted": True})
        if kind == "prompt":
            # Echo prompt answers so the UI shows the user's reply
            # immediately. Semantic note: the LLM gets the answer via
            # the ask-tool's observation return (Q/A pair), NOT as a
            # standalone user message — but for the user a silent
            # textarea clear feels broken. The user_message card is a
            # UI-only echo; doesn't change LLM context.
            content = body.get("content", "")
            if isinstance(content, str) and content:
                server.renderer.push_user_message(content)
            server.renderer.push_user_input(kind, body)
            return JSONResponse({"accepted": True})
        if kind == "confirm":
            server.renderer.push_user_input(kind, body)
            return JSONResponse({"accepted": True})
        raise HTTPException(status_code=400, detail=f"unknown kind '{kind}'")

    @app.post("/api/abort")
    async def abort(token: str = Query(...)):
        """Interrupt the current ``prompt_user`` / ``confirm`` wait."""
        server._require_token(token)
        server.renderer.push_abort()
        return JSONResponse({"accepted": True})

    @app.post("/api/stop")
    async def stop(token: str = Query(...)):
        """Stop the in-flight chat turn at the next turn boundary.

        Sets the worker's ``stop_event`` (same mechanism as Ctrl+C in
        the CLI). ``stopped`` is ``False`` when no turn was active.
        """
        server._require_token(token)
        stopped = server.trigger_stop()
        return JSONResponse({"stopped": stopped})

    return app


__all__ = ["WebServer", "create_app"]
