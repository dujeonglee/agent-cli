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
import subprocess
import zipfile
from collections import deque
from pathlib import Path
from queue import Empty, SimpleQueue

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse
from starlette.background import BackgroundTask

from agent_cli.constants import SHELL_COMMAND_TIMEOUT
from agent_cli.render.web import WebConnection, WebRenderer

_STATIC_DIR = Path(__file__).resolve().parent / "static"


def _dynamic_context_sections(ctx) -> list[dict]:
    """The Prompt Inspector's DYNAMIC half: the conversation + observations
    currently in the context window (``ctx.get_messages()`` minus the system
    prompt, which the inspector shows separately as ``kind="system"``).

    One message → one section, the SAME shape as the system sections so the
    frontend renders them identically (no new render path). ``kind="dynamic"``
    marks them. Reads a snapshot copy of the cache (``list(...)``) to avoid a
    rare race with the worker thread appending mid-read (debug view — best
    effort, no lock)."""
    if ctx is None:
        return []
    from agent_cli.context.token_estimator import estimate_tokens

    sections: list[dict] = []
    try:
        messages = list(ctx.get_messages())
    except Exception:
        return []
    for m in messages:
        if m.get("role") == "system":
            continue  # already shown as the system snapshot
        content = m.get("content", "")
        if not isinstance(content, str):
            content = str(content)
        role = m.get("role", "?")
        first = content.strip().split("\n", 1)[0][:60]
        name = f"[{role}] {first}" if first else f"[{role}]"
        sections.append(
            {
                "name": name,
                "text": content,
                "chars": len(content),
                "est_tokens": estimate_tokens(content),
                "kind": "dynamic",
            }
        )
    return sections


def capture_startup_system_prompt(
    renderer: WebRenderer,
    *,
    capabilities,
    wire_format,
    session_dir: str,
    max_depth: int,
) -> None:
    """Build + capture the system-prompt snapshot at web startup so the Prompt
    Inspector is populated BEFORE the first message (the loop only captures on
    an LLM call). This mirrors what the main loop builds at depth 0 with all
    tools (web chat uses ``active_tools=None`` → all, ``mcp_manager=None``).
    The first real LLM call rebuilds + overwrites this — including the per-turn
    ``Hook:`` sections, which only exist after ``PreLLMCall`` and so are absent
    from this static preview. Best-effort: a build error must not block
    startup."""
    try:
        from agent_cli.prompts.system_prompt import build_system_prompt_sections
        from agent_cli.tools.registry import TOOLS

        sections = build_system_prompt_sections(
            capabilities=capabilities,
            active_tools=list(TOOLS.keys()),
            session_dir=session_dir,
            mcp_manager=None,
            wire_format=wire_format,
            depth=0,
            max_depth=max_depth,
        )
        renderer.note_system_prompt(sections, turn=0)
    except Exception:
        pass


# ``no-cache`` (revalidate-required) rather than ``no-store`` so the
# browser can still take a 304 fast path when nothing changed, but a
# CSS/JS edit lands without forcing the operator to hard-refresh.
# Editable installs serve files straight from the git checkout, so an
# in-session iteration would otherwise be invisible until the operator
# bypassed cache manually.
_NO_CACHE_HEADERS = {"Cache-Control": "no-cache, must-revalidate"}

# Per-file cap for workspace uploads (POST /api/workspace/upload). A guard
# against an accidental huge upload filling the on-prem disk — generous enough
# for source trees / small assets, not for blobs.
_MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB


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

    Prefer ``preferred`` (default 8080) when free; if a live server already
    holds it, fall back to an OS-assigned ephemeral port. The caller passes the
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
    :func:`agent_cli.main.try_dispatch_agent_or_skill` so the web worker
    and ``run`` share one prefix-dispatcher with a thin output adapter
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
        # Bytes + replace, not ``text=True``: strict UTF-8 decode raises
        # UnicodeDecodeError mid-run on non-UTF-8 output (e.g. ``git show``,
        # binary diffs), which TimeoutExpired wouldn't catch.
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
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
    stdout = result.stdout.decode("utf-8", errors="replace")
    stderr = result.stderr.decode("utf-8", errors="replace")
    parts: list[str] = []
    if stdout:
        parts.append(stdout)
    if stderr:
        parts.append(stderr)
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

    # Identity sentinel returned by :meth:`dequeue_blocking` once
    # :meth:`shutdown` flips the queue's shutdown flag, so the worker
    # thread's blocking call wakes up and breaks its loop cleanly. Workers
    # compare with ``is`` (identity); queued items are dicts, so a user
    # message can never collide with this object.
    SHUTDOWN = object()

    def __init__(
        self,
        renderer: WebRenderer,
        token: str | None = None,
        ctx=None,
        trust_local: bool = False,
    ) -> None:
        self.renderer = renderer
        # --trust-local: skip token auth for loopback requests (the gateway in
        # front of a 127.0.0.1-bound instance already authenticated the user).
        self.trust_local = trust_local
        # The live ContextManager (shared with the worker's run_loop) — read
        # by the Prompt Inspector to show the DYNAMIC context (conversation +
        # observations), not just the static system prompt. May be None
        # (tests / pre-session).
        self.ctx = ctx
        # ``secrets.token_urlsafe`` gives a URL-safe random token —
        # ``--token`` override sticks if provided.
        self.token = token or secrets.token_urlsafe(32)
        # Pending user-message queue. Every connection may enqueue; the
        # worker pops one (blocking) to START a run, and the running loop
        # pops more at turn boundaries (non-blocking) to INJECT mid-run.
        # A deque + condition (not SimpleQueue) so we can also cancel by id
        # and snapshot the queue for the live display. Items:
        # ``{id, conn_id, nickname, text}``.
        self._pending: deque = deque()
        self._pending_cv = threading.Condition()
        self._pending_shutdown = False
        self._msg_seq = 0
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
        # Auto-review toggle. When True, the worker runs a reviewer agent after
        # each ``complete`` and keeps reviewing until it accepts (or the toggle
        # goes off). Read LIVE each review round (a plain bool is fine — set
        # from a request thread, read from the worker thread; a stale read at
        # worst delays the toggle by one round). Off by default.
        self._auto_review = False

    def auto_review_enabled(self) -> bool:
        return self._auto_review

    def set_auto_review(self, enabled: bool) -> None:
        self._auto_review = bool(enabled)
        # Broadcast as sticky state so EVERY browser's toggle button reflects
        # the shared server value (not just the one that clicked) — and a
        # refreshed/new client picks it up via the snapshot.
        self.renderer.auto_review_state(self._auto_review)

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
        with self._pending_cv:
            self._msg_seq += 1
            item = {
                "id": str(self._msg_seq),
                "conn_id": conn_id or "",
                "nickname": nickname,
                "text": text,
            }
            self._pending.append(item)
            self._pending_cv.notify()
        self._broadcast_queue()
        return item

    def dequeue_blocking(self):
        """Worker-idle: block until a message is queued (or shutdown).

        Returns ``WebServer.SHUTDOWN`` if :meth:`shutdown` was called, else
        the queued item dict. Workers compare with ``is WebServer.SHUTDOWN``.
        """
        with self._pending_cv:
            while not self._pending and not self._pending_shutdown:
                self._pending_cv.wait()
            # Drain pending BEFORE shutting down (FIFO: messages queued before
            # shutdown are still processed, then SHUTDOWN on the empty queue).
            if not self._pending:
                return self.SHUTDOWN
            item = self._pending.popleft()
        self._broadcast_queue()
        return item

    def dequeue_nowait(self) -> dict | None:
        """Turn-boundary (running loop): pop one queued message if available,
        else ``None`` (don't block — the loop keeps going)."""
        with self._pending_cv:
            if not self._pending:
                return None
            item = self._pending.popleft()
        self._broadcast_queue()
        return item

    def cancel_pending(self, conn_id: str | None, msg_id: str) -> bool:
        """Remove a still-pending message — only its OWNER (matching
        ``conn_id``) may cancel. Returns True if removed."""
        removed = False
        with self._pending_cv:
            for it in list(self._pending):
                if it["id"] == msg_id and it["conn_id"] == (conn_id or ""):
                    self._pending.remove(it)
                    removed = True
                    break
        if removed:
            self._broadcast_queue()
        return removed

    def queue_snapshot(self) -> list[dict]:
        with self._pending_cv:
            return [dict(it) for it in self._pending]

    def pending_count(self) -> int:
        """Number of queued-but-undelivered user messages — the idle-reaper's
        'work is waiting' signal."""
        with self._pending_cv:
            return len(self._pending)

    def _broadcast_queue(self) -> None:
        """Push the live queue state to all clients (released the queue lock
        first — ``queue_state`` takes the renderer lock)."""
        self.renderer.queue_state(self.queue_snapshot())

    def shutdown(self) -> None:
        """Wake any worker blocked in :meth:`dequeue_blocking`.

        Sets the shutdown flag and notifies so the worker's wait returns
        ``SHUTDOWN``. Idempotent.
        """
        with self._pending_cv:
            self._pending_shutdown = True
            self._pending_cv.notify_all()

    # ─── Auth helper ──────────────────────────────────────────

    def _require_token(self, token: str | None) -> None:
        """Constant-time compare against the configured token.

        Constant-time avoids timing-side-channel leaks on the LAN —
        cheap and standard for shared-secret schemes.
        """
        if token is None or not secrets.compare_digest(token, self.token):
            raise HTTPException(status_code=401, detail="invalid or missing token")

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


def _with_token_query(query_string: bytes, token: str) -> bytes:
    """Return ``query_string`` with any client ``token`` replaced by ``token``
    (ours first). Used by the trust-local middleware to make per-endpoint token
    checks pass for trusted loopback requests without the gateway plumbing it."""
    from urllib.parse import parse_qsl, urlencode

    pairs = [
        (k, v)
        for k, v in parse_qsl(query_string.decode(), keep_blank_values=True)
        if k != "token"
    ]
    return urlencode([("token", token), *pairs]).encode()


class _TrustLocalMiddleware:
    """Pure-ASGI: for a trusted loopback request (``--trust-local`` on + peer is
    127.0.0.1/::1 → only the local gateway can reach a loopback-bound instance,
    and it already authenticated the user), inject the valid token into the
    query string so the existing per-endpoint token checks pass. No-op when
    ``--trust-local`` is off, so the default auth path is byte-identical."""

    def __init__(self, app, server: WebServer):
        self.app = app
        self.server = server

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            client = scope.get("client")
            host = client[0] if client else None
            if self.server.is_trusted_client(host):
                scope = dict(scope)
                scope["query_string"] = _with_token_query(
                    scope.get("query_string", b""), self.server.token
                )
        await self.app(scope, receive, send)


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
    # Trust-local auth bypass (no-op unless --trust-local). Pure-ASGI so the
    # injected token reaches the per-endpoint check reliably (unlike a
    # BaseHTTPMiddleware contextvar, which Starlette runs in a separate context).
    app.add_middleware(_TrustLocalMiddleware, server=server)

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
    async def debug_prompt(token: str = Query(...), task_id: str = Query("")):
        """Prompt Inspector data for a scope: the latest LLM call's system
        prompt as named sections with size figures. ``task_id`` selects a
        delegate sub-agent's prompt; empty (default) is the main loop. Token-
        authenticated; fetched on demand when the inspector drawer opens (the
        ~16KB payload is never pushed over SSE). ``ok=False`` before that
        scope's first LLM call."""
        server._require_token(token)
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
        dynamic = _dynamic_context_sections(server.ctx) if not task_id else []
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

    @app.get("/api/debug/prompt/scopes")
    async def debug_prompt_scopes(token: str = Query(...)):
        """Scopes that currently have a captured system prompt — the main loop
        plus any delegate sub-agents — for the inspector's scope chip row."""
        server._require_token(token)
        return {"ok": True, "scopes": server.renderer.prompt_scopes()}

    @app.delete("/api/debug/prompt")
    async def debug_prompt_delete(token: str = Query(...), task_id: str = Query(...)):
        """Drop a sub-agent's captured prompt (inspector ✕ button). Main is
        not deletable (it regenerates every turn)."""
        server._require_token(token)
        removed = server.renderer.delete_prompt_scope(task_id)
        return {"ok": True, "removed": removed}

    @app.get("/api/export/jira/targets")
    async def export_jira_targets(token: str = Query(...)):
        """Configured Jira instance names + base URLs (+ deployment) for the
        export dropdown. Token-authenticated; NEVER returns credentials (none
        are stored server-side). Each target's ``deployment`` is the
        config-pinned value or, when absent, probed from serverInfo so the UI
        pre-selects the right credential fields. Empty list when no Jira is
        configured (the UI then disables the Jira option)."""
        server._require_token(token)
        from agent_cli.config import load_config
        from agent_cli.integrations import jira as jira_mod

        targets = jira_mod.list_targets(load_config())
        for t in targets:
            if not t.get("deployment"):
                t["deployment"] = jira_mod.detect_deployment(t["base_url"])
        return {"ok": True, "targets": targets}

    @app.post("/api/export/html")
    async def export_html(request: Request, token: str = Query(...)):
        """Render selected transcript entries to a self-contained HTML doc and
        return it as a download. Body: ``{title?, entries: [...]}``. Read-only,
        so token-auth (no controller check) — any authenticated viewer may
        export what they can see."""
        server._require_token(token)
        from agent_cli.integrations import export as export_mod

        try:
            body = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        entries = body.get("entries")
        if not isinstance(entries, list):
            raise HTTPException(status_code=400, detail="entries must be a list")
        title = body.get("title") or ""
        doc = export_mod.entries_to_html(entries, title=str(title))
        return Response(
            content=doc,
            media_type="text/html; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="agent-cli-export.html"'
            },
        )

    @app.post("/api/export/jira")
    async def export_jira(request: Request, token: str = Query(...)):
        """Post selected transcript entries as ONE Jira comment, AS THE
        FRONTEND USER. Body: ``{target?, base_url?, issue_key, deployment?,
        entries: [...], auth: {user, secret}}``. ``base_url`` (optional) lets the
        user point at a URL not in config — works with no config at all, but an
        unconfigured URL must be https. Otherwise the named instance is resolved
        from config. Renders entries to ADF (Cloud) or wiki markup (Server/DC)
        per ``deployment`` and POSTs with the user-supplied credentials, which
        are used ONLY for this request — never logged or persisted. Returns
        ``{ok, url}`` or 400 with the error."""
        server._require_token(token)
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
    async def workspace_tree(token: str = Query(...), path: str = Query("")):
        """List one directory level of the workspace (lazy tree expansion).
        Returns ``{path, entries:[{name, type, size}]}`` — dirs first, then
        files, name-sorted. Read-only, token-auth."""
        server._require_token(token)
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

        entries = []
        for child in sorted(d.iterdir(), key=lambda c: (c.is_file(), c.name.lower())):
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
        return JSONResponse({"path": path, "entries": entries})

    @app.post("/api/workspace/download")
    async def workspace_download(request: Request, token: str = Query(...)):
        """Zip the selected workspace paths and return the archive, deleting
        the temp file after send. Body: ``{paths:[rel...], all?:bool}``. A dir
        is added recursively; a file individually. Read-only, token-auth."""
        server._require_token(token)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        rels = ["."] if body.get("all") else (body.get("paths") or [])
        if not isinstance(rels, list) or not rels:
            raise HTTPException(status_code=400, detail="no paths selected")

        targets = [server._safe_workspace_path(r) for r in rels]
        tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        tmp.close()
        try:
            with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
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
        except Exception:
            os.unlink(tmp.name)
            raise
        name = "workspace" if body.get("all") else server.workspace.name
        return FileResponse(
            tmp.name,
            media_type="application/zip",
            filename=f"{name}.zip",
            background=BackgroundTask(os.unlink, tmp.name),
        )

    @app.post("/api/workspace/upload")
    async def workspace_upload(
        request: Request,
        token: str = Query(...),
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
        server._require_token(token)
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
        # Create the nested dirs (under the already-validated target). Safe:
        # ``_safe_workspace_path`` confirmed ``dest`` resolves under the
        # workspace, so its parents do too.
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        return JSONResponse(
            {
                "name": segments[-1],
                "rel": str(dest.resolve().relative_to(server.workspace)),
                "size": len(body),
                "overwritten": overwritten,
            }
        )

    @app.get("/api/stream")
    async def stream(token: str = Query(...)):
        """SSE event stream. Token-authenticated; multi-viewer (all equal)."""
        server._require_token(token)
        conn = WebConnection(id=secrets.token_hex(8))
        return EventSourceResponse(server.stream_events(conn))

    @app.post("/api/input")
    async def input_endpoint(request: Request, token: str = Query(...)):
        """User input → renderer queue.

        Body shape::

            {"kind": "prompt", "content": "..."}
            {"kind": "confirm", "key": "y", "comment": "..."}
            {"kind": "chat", "content": "..."}

        ``chat`` is the only kind that advances the AgentLoop directly;
        ``prompt`` / ``confirm`` answer an in-flight render call. Every
        authenticated connection may send input (no controller gate).
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
            # Enqueue (no immediate echo): the message shows in the live queue
            # display until it's dequeued — by the worker to START a run, or by
            # the running loop at a turn boundary to INJECT — at which point it
            # is rendered as a conversation card.
            server.enqueue(body.get("conn_id"), content)
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

    @app.post("/api/queue/cancel")
    async def queue_cancel(request: Request, token: str = Query(...)):
        """Cancel a still-pending queued message. Body: ``{conn_id, id}`` —
        only the owner (matching ``conn_id``) may cancel; already-dequeued
        messages can't be cancelled. Returns ``{cancelled: bool}``."""
        server._require_token(token)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        ok = server.cancel_pending(body.get("conn_id"), str(body.get("id", "")))
        return JSONResponse({"cancelled": ok})

    @app.post("/api/nickname")
    async def set_nickname(request: Request, token: str = Query(...)):
        """Set the caller's display nickname. Body: ``{conn_id, name}``. The
        UI pre-fills the assigned fun default; the user edits/confirms."""
        server._require_token(token)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        ok = server.renderer.set_nickname(body.get("conn_id"), body.get("name", ""))
        return JSONResponse({"ok": ok})

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

    @app.post("/api/auto_review")
    async def auto_review(request: Request, token: str = Query(...)):
        """Set the auto-review toggle. Body: ``{enabled: bool}``. When on, the
        worker runs a reviewer agent after each complete and keeps reviewing
        until it accepts (or the toggle goes off)."""
        server._require_token(token)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        server.set_auto_review(bool(body.get("enabled", False)))
        return JSONResponse({"enabled": server.auto_review_enabled()})

    return app


__all__ = ["WebServer", "create_app"]
