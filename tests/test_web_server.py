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
from agent_cli.web.server import WebServer, create_app


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
