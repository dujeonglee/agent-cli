"""Unit tests for :mod:`agent_cli.web.server`.

Coverage axes:

1. **Auth** — every authenticated endpoint refuses missing / wrong
   tokens; the health probe is open.
2. **SSE stream** — events emitted by the renderer reach the SSE
   subscriber in order; the persistent buffer is replayed on connect.
3. **Takeover** — a second SSE connection makes the first receive a
   ``takeover`` event and disconnect.
4. **POST /api/input** — chat / prompt / confirm routes wire through
   to the renderer (and chat additionally feeds the queue exposed by
   ``WebServer.pop_chat``).
5. **POST /api/abort** — releases a blocked ``prompt_user`` /
   ``confirm`` call.

The tests use FastAPI's ``TestClient`` for synchronous HTTP. The SSE
stream is read as raw bytes with line iteration so we don't need a
full SSE client.
"""

from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from agent_cli.render.web import WebRenderer
from agent_cli.web.server import WebServer, create_app, pick_port


@pytest.fixture
def server_and_client():
    """Fresh server + renderer + TestClient per test (no shared state)."""
    renderer = WebRenderer()
    server = WebServer(renderer, token="testtoken")
    app = create_app(server)
    client = TestClient(app)
    return server, renderer, client


# SSE streaming is tested at the async-generator level — calling
# ``server.stream_events(conn).__anext__()`` directly. The HTTP wire
# format (``event: …\ndata: …\n\n``) is sse-starlette's responsibility
# (library code, not ours to retest); our contract is "the generator
# yields the right event dicts in the right order". Generator-level
# testing has the same coverage as end-to-end HTTP without the
# in-process test client's streaming quirks.


# ── Slash command dispatch (CLI parity) ───────────


class TestHandleSlashCommand:
    """``handle_slash_command`` intercepts CLI-parity shortcuts before
    the worker forwards to ``run_loop``. Currently only ``/sh``."""

    def test_non_slash_message_is_passthrough(self):
        from agent_cli.web.server import handle_slash_command

        renderer = WebRenderer()
        assert handle_slash_command("regular chat message", renderer) is False
        assert renderer.persistent_count == 0

    def test_slash_sh_runs_command_and_emits_observation(self):
        from agent_cli.web.server import handle_slash_command

        renderer = WebRenderer()
        conn = WebConnection(id="c1")
        renderer.register_connection(conn)

        handled = handle_slash_command("/sh echo hello-from-shell", renderer)

        assert handled is True
        # ``observation`` event is emitted as a persistent message —
        # frontend renders it as a tool-result card.
        event, data = conn.queue.get(timeout=1.0)
        assert event == "observation"
        assert data["tool_name"] == "sh"
        assert data["success"] is True
        assert "hello-from-shell" in data["content"]

    def test_slash_sh_nonzero_exit_marks_failure(self):
        from agent_cli.web.server import handle_slash_command

        renderer = WebRenderer()
        conn = WebConnection(id="c1")
        renderer.register_connection(conn)

        handled = handle_slash_command("/sh exit 7", renderer)

        assert handled is True
        event, data = conn.queue.get(timeout=1.0)
        assert event == "observation"
        assert data["success"] is False
        assert "exit code: 7" in data["content"]

    def test_slash_sh_no_args_shows_usage(self):
        from agent_cli.web.server import handle_slash_command

        renderer = WebRenderer()
        conn = WebConnection(id="c1")
        renderer.register_connection(conn)

        assert handle_slash_command("/sh", renderer) is True
        event, data = conn.queue.get(timeout=1.0)
        assert event == "observation"
        assert "Usage:" in data["content"]
        assert data["success"] is False

    def test_slash_help_lists_supported_commands(self):
        from agent_cli.web.server import handle_slash_command

        renderer = WebRenderer()
        conn = WebConnection(id="c1")
        renderer.register_connection(conn)

        assert handle_slash_command("/help", renderer) is True
        event, data = conn.queue.get(timeout=1.0)
        assert event == "observation"
        # Must mention each supported command so the user can discover
        # them without leaving the UI.
        assert "/sh" in data["content"]
        assert "/skills" in data["content"]
        assert data["success"] is True

    def test_handle_slash_command_only_owns_web_specific_commands(self):
        """``handle_slash_command`` is the web's stateless layer — owns
        ``/help`` and ``/sh`` only. Everything else (``@<agent>``,
        ``/<skill>``, listings, not-found) is routed through
        ``try_dispatch_agent_or_skill`` so chat REPL and web share a
        single dispatcher. Test pins the surface boundary so a future
        refactor that re-adds duplicate listing logic here fails loudly.
        """
        from agent_cli.web.server import handle_slash_command

        renderer = WebRenderer()
        # These belong to ``try_dispatch_agent_or_skill`` now.
        for msg in (
            "/skills",
            "/clear",
            "/mcp",
            "/optimize ./",
            "@",
            "@agents",
            "@explorer find x",
            "/plan some feature",
        ):
            assert handle_slash_command(msg, renderer) is False, (
                f"handle_slash_command should not catch {msg!r}"
            )


# ── Static UI ─────────────────────────────────────


class TestStaticUI:
    """The frontend is served from ``/`` (HTML) + ``/static/*``
    (JS/CSS). The HTML page reads ``?token=…`` from window.location;
    the static assets contain no secrets so they don't gate on auth.
    The SSE / input endpoints behind them stay token-protected.
    """

    def test_index_html_is_served(self, server_and_client):
        _, _, client = server_and_client
        resp = client.get("/")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        body = resp.text
        # Sanity: the page must reference the SSE + input endpoints
        # so the token-from-URL flow can connect.
        assert "/static/app.js" in body
        assert "/static/style.css" in body

    def test_app_js_is_served(self, server_and_client):
        _, _, client = server_and_client
        resp = client.get("/static/app.js")
        assert resp.status_code == 200
        body = resp.text
        # Critical wires must be present.
        assert "EventSource" in body
        assert "/api/stream" in body
        assert "/api/input" in body
        assert "token" in body

    def test_style_css_is_served(self, server_and_client):
        _, _, client = server_and_client
        resp = client.get("/static/style.css")
        assert resp.status_code == 200
        assert "text/css" in resp.headers["content-type"]


# ── Auth ──────────────────────────────────────────


class TestAuth:
    def test_health_is_open(self, server_and_client):
        _, _, client = server_and_client
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_stream_without_token_is_422(self, server_and_client):
        _, _, client = server_and_client
        # FastAPI's required Query param without a value → 422.
        resp = client.get("/api/stream")
        assert resp.status_code == 422

    def test_stream_with_wrong_token_is_401(self, server_and_client):
        _, _, client = server_and_client
        resp = client.get("/api/stream?token=nope")
        assert resp.status_code == 401

    def test_input_with_wrong_token_is_401(self, server_and_client):
        _, _, client = server_and_client
        resp = client.post(
            "/api/input?token=nope",
            json={"kind": "chat", "content": "hi"},
        )
        assert resp.status_code == 401

    def test_abort_with_wrong_token_is_401(self, server_and_client):
        _, _, client = server_and_client
        resp = client.post("/api/abort?token=nope")
        assert resp.status_code == 401


# ── POST /api/input ───────────────────────────────


class TestInputEndpoint:
    def test_chat_message_echoes_to_renderer_and_queue(self, server_and_client):
        server, renderer, client = server_and_client
        resp = client.post(
            "/api/input?token=testtoken",
            json={"kind": "chat", "content": "hello"},
        )
        assert resp.status_code == 200
        # Echoed as persistent ``user_message`` event for replay.
        assert renderer.persistent_count == 1
        # Queued for the worker loop.
        popped = server.pop_chat(timeout=0.5)
        assert popped == "hello"

    def test_prompt_response_goes_to_renderer_input_queue(self, server_and_client):
        _, renderer, client = server_and_client
        result: list[str] = []

        def worker():
            result.append(renderer.prompt_user("Q: "))

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        time.sleep(0.05)

        resp = client.post(
            "/api/input?token=testtoken",
            json={"kind": "prompt", "content": "answer text"},
        )
        assert resp.status_code == 200

        t.join(timeout=2.0)
        assert result == ["answer text"]

    def test_prompt_response_echoes_user_message_for_ui_feedback(
        self, server_and_client
    ):
        """The ask-tool answer flows to the LLM through the renderer's
        input queue (a tool observation, NOT a user message). For the
        UI, however, an empty textarea after submit feels like the
        click did nothing — the server echoes the content as a
        ``user_message`` event so the chat thread shows the user's
        reply right away, ahead of the LLM's next emission."""
        _, renderer, client = server_and_client
        conn = WebConnection(id="c1")
        renderer.register_connection(conn)

        # Detach a worker so the input_queue has a consumer.
        def worker():
            renderer.prompt_user("Q: ")

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        time.sleep(0.05)

        client.post(
            "/api/input?token=testtoken",
            json={"kind": "prompt", "content": "my answer"},
        )
        t.join(timeout=2.0)

        # ``user_message`` echo must arrive on the SSE side too.
        seen_user_message = False
        # Drain a few events from the queue — order matters but other
        # transient events may interleave (e.g. input_resolved).
        for _ in range(5):
            try:
                event, data = conn.queue.get(timeout=0.5)
            except Exception:
                break
            if event == "user_message" and data.get("content") == "my answer":
                seen_user_message = True
                break
        assert seen_user_message

    def test_confirm_response_decodes_key_and_comment(self, server_and_client):
        from agent_cli.render.base import ConfirmOption

        _, renderer, client = server_and_client
        result: list[tuple[str, str]] = []

        def worker():
            result.append(
                renderer.confirm(
                    "?",
                    [
                        ConfirmOption("y", "yes"),
                        ConfirmOption("n", "no"),
                    ],
                    default_key="n",
                )
            )

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        time.sleep(0.05)

        client.post(
            "/api/input?token=testtoken",
            json={"kind": "confirm", "key": "y", "comment": "go ahead"},
        )
        t.join(timeout=2.0)
        assert result == [("y", "go ahead")]

    def test_unknown_kind_is_400(self, server_and_client):
        _, _, client = server_and_client
        resp = client.post(
            "/api/input?token=testtoken",
            json={"kind": "bogus", "content": "x"},
        )
        assert resp.status_code == 400

    def test_invalid_json_is_400(self, server_and_client):
        _, _, client = server_and_client
        resp = client.post(
            "/api/input?token=testtoken",
            content="not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400


class TestWebResumeCli:
    """Integration smoke for the ``agent-cli web --resume <id>`` CLI
    surface. Drives :func:`agent_cli.main.web` directly with all heavy
    deps (uvicorn, AgentLoop) stubbed so we can verify the error path
    for an unknown session without spinning up a server.
    """

    def test_unknown_session_exits_with_code_1(self, tmp_path, monkeypatch):
        """``--resume <bogus>`` aborts before uvicorn is touched."""
        import typer
        from typer.testing import CliRunner

        from agent_cli.main import app

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            app,
            ["web", "--resume", "DOES-NOT-EXIST", "--no-browser"],
            catch_exceptions=False,
        )
        assert result.exit_code == 1
        assert "not found" in result.stdout.lower()
        # Belt-and-braces — typer.Exit subclass not leaking.
        assert not isinstance(result.exception, typer.Exit) or result.exit_code == 1


class TestShutdownSentinel:
    """``WebServer.shutdown()`` pushes ``SHUTDOWN`` onto the chat
    queue so a worker thread blocked in ``pop_chat`` wakes up and
    breaks. Identity comparison (``is``) keeps a user-typed message
    from colliding with the sentinel even if its value happened to
    look like the same string."""

    def test_shutdown_wakes_worker_with_sentinel(self):
        renderer = WebRenderer()
        srv = WebServer(renderer)
        msgs: list = []

        def worker():
            while True:
                m = srv.pop_chat()
                if m is srv.SHUTDOWN:
                    msgs.append("done")
                    break
                msgs.append(m)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        srv.push_chat("hello")
        srv.shutdown()
        t.join(timeout=1.0)
        assert msgs == ["hello", "done"]
        assert not t.is_alive()

    def test_shutdown_is_identity_sentinel(self):
        """``SHUTDOWN`` must be a unique sentinel — ``is`` compares
        identity so even a chat message that stringifies the same
        cannot accidentally be mistaken for shutdown."""
        renderer = WebRenderer()
        srv = WebServer(renderer)
        srv.push_chat("SHUTDOWN")  # user types the word
        item = srv.pop_chat(timeout=0.5)
        assert item is not srv.SHUTDOWN
        assert item == "SHUTDOWN"


class TestAbortEndpoint:
    def test_abort_releases_blocked_prompt_user(self, server_and_client):
        _, renderer, client = server_and_client
        exc: list[BaseException] = []

        def worker():
            try:
                renderer.prompt_user("Q: ")
            except BaseException as e:
                exc.append(e)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        time.sleep(0.05)

        resp = client.post("/api/abort?token=testtoken")
        assert resp.status_code == 200

        t.join(timeout=2.0)
        assert exc and isinstance(exc[0], EOFError)


# ── pick_port (8080-preferred, OS-fallback) ───────


class TestPickPort:
    """``pick_port`` decides which port the web server binds to.

    Earlier the CLI hard-coded 8080 in the URL printed to the operator
    even though uvicorn might fail to bind it; explicit ``--port`` still
    binds exactly that, but the no-flag default now tries 8080 first
    and falls back to whatever the OS gives us if it's busy.
    """

    def test_returns_preferred_when_free(self):
        # Probe with port 0 to discover *some* port that's currently
        # free, then ask pick_port to prefer it — should hand back the
        # same number unchanged.
        import socket as _s

        with _s.socket(_s.AF_INET, _s.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            free = probe.getsockname()[1]
        assert pick_port("127.0.0.1", free) == free

    def test_falls_back_when_preferred_busy(self):
        # Hold a port for the duration of the call so pick_port's bind
        # probe sees EADDRINUSE and must fall through to (host, 0).
        import socket as _s

        holder = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
        try:
            holder.bind(("127.0.0.1", 0))
            holder.listen(1)
            busy = holder.getsockname()[1]
            picked = pick_port("127.0.0.1", busy)
            assert picked != busy
            # Sanity: an ephemeral port number is positive and bindable.
            assert picked > 0
        finally:
            holder.close()


# ── SSE stream (async, ASGITransport) ─────────────


@pytest.mark.asyncio
class TestStreamGenerator:
    """Drives ``WebServer.stream_events`` directly so cancellation and
    yielding are deterministic.

    Each test instantiates its own renderer + server (no shared state)
    and uses an ``asyncio.wait_for`` deadline on every ``__anext__``
    so a missing event surfaces as a test failure rather than a hang.
    """

    async def _next(self, gen, timeout: float = 1.0) -> dict:
        return await asyncio.wait_for(gen.__anext__(), timeout=timeout)

    async def test_replay_buffer_on_connect(self):
        renderer = WebRenderer()
        server = WebServer(renderer, token="t")
        renderer.final("preconnect answer", turn=1)

        conn = WebConnection(id="c1")
        gen = server.stream_events(conn)
        try:
            event_dict = await self._next(gen)
            assert event_dict["event"] == "assistant_turn"
            data = json.loads(event_dict["data"])
            assert data["final"] == "preconnect answer"
            assert data["turn"] == 1
        finally:
            await gen.aclose()

    async def test_replay_preserves_order(self):
        renderer = WebRenderer()
        server = WebServer(renderer, token="t")
        # Three persistent events queued before connection — order
        # must be preserved on replay.
        renderer.final("first", turn=1)
        renderer.observation("ok", turn=1, tool_name="shell", success=True)
        renderer.final("second", turn=2)

        conn = WebConnection(id="c1")
        gen = server.stream_events(conn)
        try:
            first = await self._next(gen)
            second = await self._next(gen)
            third = await self._next(gen)
            assert json.loads(first["data"])["final"] == "first"
            assert second["event"] == "observation"
            assert json.loads(third["data"])["final"] == "second"
        finally:
            await gen.aclose()

    async def test_live_emit_after_connect_reaches_subscriber(self):
        renderer = WebRenderer()
        server = WebServer(renderer, token="t")

        conn = WebConnection(id="c1")
        gen = server.stream_events(conn)
        try:
            # Emit a moment after starting to consume so the live loop
            # (not the snapshot replay) carries the event.
            async def emit_after():
                await asyncio.sleep(0.05)
                renderer.final("live answer", turn=1)

            emit_task = asyncio.create_task(emit_after())
            try:
                event_dict = await self._next(gen, timeout=2.0)
                assert event_dict["event"] == "assistant_turn"
                assert json.loads(event_dict["data"])["final"] == "live answer"
            finally:
                await emit_task
        finally:
            await gen.aclose()

    async def test_takeover_ends_generator(self):
        renderer = WebRenderer()
        server = WebServer(renderer, token="t")

        conn = WebConnection(id="c1")
        gen = server.stream_events(conn)
        try:
            # Start consuming so the connection is registered.
            consume_task = asyncio.create_task(self._next(gen, timeout=2.0))
            # Give the generator a chance to register before takeover.
            await asyncio.sleep(0.05)

            # Trigger takeover by registering a fresh connection.
            renderer.register_connection(WebConnection(id="c2"))

            event_dict = await consume_task
            assert event_dict["event"] == "takeover"

            # The generator should now be exhausted (it broke out of
            # the loop after yielding ``takeover``).
            with pytest.raises(StopAsyncIteration):
                await self._next(gen, timeout=1.0)
        finally:
            await gen.aclose()

    async def test_close_sentinel_ends_generator_silently(self):
        """``unregister_connection`` pushes the sentinel — generator
        terminates without yielding any extra event to the client."""
        renderer = WebRenderer()
        server = WebServer(renderer, token="t")

        conn = WebConnection(id="c1")
        gen = server.stream_events(conn)
        try:
            consume_task = asyncio.create_task(self._next(gen, timeout=2.0))
            await asyncio.sleep(0.05)

            # Push a real event AND then immediately unregister so
            # the generator sees one event followed by the sentinel.
            renderer.final("byebye", turn=1)
            renderer.unregister_connection(conn)

            event_dict = await consume_task
            assert event_dict["event"] == "assistant_turn"

            with pytest.raises(StopAsyncIteration):
                await self._next(gen, timeout=1.0)
        finally:
            await gen.aclose()


# Imports needed by the async tests — defined here so other test
# classes don't pay the import cost when running selectively.
import asyncio  # noqa: E402

from agent_cli.render.web import WebConnection  # noqa: E402
