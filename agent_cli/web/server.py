"""FastAPI server backing ``agent-cli web``.

Endpoints:
  - ``GET  /api/health`` — liveness probe (no auth)
  - ``GET  /api/stream`` — SSE event stream (auth via ``token`` query)
  - ``POST /api/input``  — submit chat message / ask answer / confirm reply
  - ``POST /api/abort``  — interrupt the current ``prompt_user`` / ``confirm``

Auth: every authenticated endpoint requires the ``token`` query param to
match ``WebServer.token``. The token is generated at startup (or
provided by ``--token``) and printed to stdout so the operator can share
the URL with the LAN.

Single-active-client: only one SSE connection at a time. A second
connection takes over — the first receives a ``takeover`` event and
disconnects cleanly. Implemented via :meth:`WebRenderer.register_connection`.

FIFO sync: after every chat turn (i.e. after ``AgentLoop.run`` returns)
the server compares the renderer's persistent event count to the
``ContextManager`` cache size. Any drop is broadcast as a ``prune``
event so the frontend trims the same prefix from view.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import subprocess
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, SimpleQueue
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from agent_cli.constants import SHELL_COMMAND_TIMEOUT
from agent_cli.render.web import WebConnection, WebRenderer

_STATIC_DIR = Path(__file__).resolve().parent / "static"


# ── CLI-parity slash commands (web mode) ──────────────────


_WEB_HELP_TEXT = (
    "Web mode slash commands:\n"
    "  /help                    Show this help\n"
    "  /sh <command>            Run a shell command directly (LLM bypass)\n"
    "  /skills                  List available skills\n"
    "\n"
    "All other input goes to the LLM. Chat REPL slash commands like\n"
    "/clear, /<skill>, @<agent> are not yet wired for web mode."
)


def handle_slash_command(message: str, renderer: WebRenderer) -> bool:
    """Intercept CLI-parity slash commands before forwarding to AgentLoop.

    Returns ``True`` if the message was handled here (caller skips
    ``run_loop``); ``False`` otherwise. Output surfaces as an
    ``observation`` event so the frontend renders it as a tool-result
    card alongside whatever else is in the session.

    Handled:
      - ``/help`` — list supported web slash commands
      - ``/sh <cmd>`` — direct shell execution
      - ``/skills`` — list available skills (informational)

    Other chat REPL slash commands (``/clear``, ``/<skill>``,
    ``@<agent>``, ``/mcp``, ``/compact``) intentionally fall through to
    the LLM in this Phase scope — they need real dispatcher wiring to
    work in web (skill / agent invocation paths run their own loops).
    Future work: extract the chat REPL's slash dispatcher into a shared
    helper so both surfaces can call it.
    """
    if message == "/help":
        renderer.observation(
            _WEB_HELP_TEXT,
            turn=0,
            tool_name="help",
            success=True,
        )
        return True

    if message == "/skills":
        return _handle_skills_listing(renderer)

    if message.startswith("/sh"):
        return _handle_sh(message, renderer)

    return False


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


def _handle_skills_listing(renderer: WebRenderer) -> bool:
    """``/skills`` — informational listing of available skills.

    Best-effort: a load failure (corrupt skill file, missing dir) is
    surfaced as a non-success observation so the user can see what
    happened instead of silently falling through.
    """
    try:
        from agent_cli.skills import load_skills

        skills = load_skills()
    except Exception as exc:  # noqa: BLE001 — surfacing for user
        renderer.observation(
            f"Failed to load skills: {exc}",
            turn=0,
            tool_name="skills",
            success=False,
        )
        return True

    visible = [s for s in skills.values() if not s.disable_model_invocation]
    if not visible:
        renderer.observation(
            "No skills available.",
            turn=0,
            tool_name="skills",
            success=True,
        )
        return True
    lines = ["Available skills (LLM-invocable via `run_skill` tool):"]
    for s in visible:
        hint = f" {s.argument_hint}" if s.argument_hint else ""
        lines.append(f"  {s.name}{hint} — {s.description}")
    lines.append("")
    lines.append(
        "Note: direct ``/<skill>`` invocation from web is not yet wired — "
        "ask the LLM to run a skill, or use chat mode."
    )
    renderer.observation(
        "\n".join(lines),
        turn=0,
        tool_name="skills",
        success=True,
    )
    return True


@dataclass
class WebServerConfig:
    """Static config the FastAPI app pulls from at request time.

    Most fields are CLI flags; ``token`` defaults to a fresh random
    secret when ``None`` (``--token`` omitted).
    """

    token: str
    # Hook fed each user chat message — the loop runner uses this to
    # advance the AgentLoop. Returns the raw user content; the server
    # already echoed it into the renderer's persistent buffer via
    # ``WebRenderer.push_user_message``.
    on_user_message: Any  # Callable[[str], None]
    # FIFO sync helper: takes the renderer's current persistent count
    # and returns the prune drop (0 if no eviction). Server polls this
    # after each turn — see ``WebServer.process_chat_turn``.
    compute_prune_drop: Any  # Callable[[int], int]


class WebServer:
    """Owns the renderer + the worker thread that drives AgentLoop.

    The FastAPI app delegates all stateful operations here so the
    request handlers stay thin and unit-testable.
    """

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

    # ─── External hooks (used by the CLI ``web`` command) ─────

    def push_chat(self, message: str) -> None:
        """Queue a top-level chat message for the worker loop."""
        self._chat_queue.put(message)

    def pop_chat(self, timeout: float | None = None) -> str | None:
        """Worker-side: pop the next chat message (blocks)."""
        try:
            return self._chat_queue.get(timeout=timeout)
        except Empty:
            return None

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


# ── FastAPI app factory ────────────────────────────────────


def create_app(server: WebServer) -> FastAPI:
    """Build the FastAPI app over a ``WebServer`` instance.

    Kept as a factory so tests can swap in a mock renderer / server and
    drive ``httpx.AsyncClient`` against an in-process app without
    spinning up uvicorn.
    """
    app = FastAPI(title="agent-cli web")

    @app.get("/")
    async def index():
        """Serve the static chat UI. JS reads ``?token=…`` from the
        URL — no auth gate here because the page itself contains no
        secrets; the SSE / input endpoints are token-protected."""
        return FileResponse(_STATIC_DIR / "index.html")

    if _STATIC_DIR.exists():
        app.mount(
            "/static",
            StaticFiles(directory=_STATIC_DIR),
            name="static",
        )

    @app.get("/api/health")
    async def health():
        """Unauthenticated liveness probe."""
        return {"status": "ok"}

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

    return app


__all__ = ["WebServer", "WebServerConfig", "create_app"]
