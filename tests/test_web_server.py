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
import logging
import threading
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agent_cli.render.web import WebRenderer
from agent_cli.web.server import (
    WebServer,
    create_app,
    pick_port,
    suppress_incomplete_response_log,
    _IncompleteResponseLogFilter,
)


@pytest.fixture
def server_and_client():
    """Fresh server + renderer + TestClient per test (no shared state)."""
    renderer = WebRenderer()
    server = WebServer(renderer, token="testtoken")
    app = create_app(server)
    client = TestClient(app)
    return server, renderer, client


def _controller(renderer, conn_id="ctrl"):
    """Register a connection as the controller and return its id — input is
    gated to the controller (first-come-keeps-control), so input tests need
    one and must send ``conn_id``."""
    renderer.register_connection(WebConnection(id=conn_id))
    return conn_id


# SSE streaming is tested at the async-generator level — calling
# ``server.stream_events(conn).__anext__()`` directly. The HTTP wire
# format (``event: …\ndata: …\n\n``) is sse-starlette's responsibility
# (library code, not ours to retest); our contract is "the generator
# yields the right event dicts in the right order". Generator-level
# testing has the same coverage as end-to-end HTTP without the
# in-process test client's streaming quirks.


# ── Slash command dispatch (CLI parity) ───────────


class TestIncompleteResponseLogFilter:
    """The Ctrl+C cosmetic-noise filter for uvicorn's SSE shutdown log."""

    def test_drops_the_incomplete_response_message(self):
        f = _IncompleteResponseLogFilter()
        rec = logging.LogRecord(
            "uvicorn.error",
            logging.ERROR,
            __file__,
            0,
            "ASGI callable returned without completing response.",
            None,
            None,
        )
        assert f.filter(rec) is False

    def test_keeps_other_messages(self):
        f = _IncompleteResponseLogFilter()
        rec = logging.LogRecord(
            "uvicorn.error",
            logging.ERROR,
            __file__,
            0,
            "Some other real error",
            None,
            None,
        )
        assert f.filter(rec) is True

    def test_suppress_is_idempotent(self):
        logger = logging.getLogger("uvicorn.error")
        before = len(
            [f for f in logger.filters if isinstance(f, _IncompleteResponseLogFilter)]
        )
        try:
            suppress_incomplete_response_log()
            suppress_incomplete_response_log()
            count = len(
                [
                    f
                    for f in logger.filters
                    if isinstance(f, _IncompleteResponseLogFilter)
                ]
            )
            assert count == 1  # not stacked
        finally:
            # Clean up so the global logger isn't left mutated for other tests.
            logger.filters = [
                f
                for f in logger.filters
                if not isinstance(f, _IncompleteResponseLogFilter)
            ]
        assert before == 0 or before == 1


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

    def test_slash_compact_reports_before_after(self):
        from unittest.mock import MagicMock
        from agent_cli.web.server import handle_slash_command

        renderer = WebRenderer()
        conn = WebConnection(id="c1")
        renderer.register_connection(conn)
        ctx = MagicMock()
        ctx.compact_now.return_value = (5000, 2000)

        handled = handle_slash_command("/compact", renderer, ctx=ctx)

        assert handled is True
        ctx.compact_now.assert_called_once()
        event, data = conn.queue.get(timeout=1.0)
        assert event == "observation"
        assert data["tool_name"] == "compact"
        assert data["success"] is True
        assert "5,000" in data["content"] and "2,000" in data["content"]

    def test_slash_compact_no_change(self):
        from unittest.mock import MagicMock
        from agent_cli.web.server import handle_slash_command

        renderer = WebRenderer()
        conn = WebConnection(id="c1")
        renderer.register_connection(conn)
        ctx = MagicMock()
        ctx.compact_now.return_value = (3000, 3000)
        ctx.max_context_tokens = 100000

        handle_slash_command("/compact", renderer, ctx=ctx)
        _, data = conn.queue.get(timeout=1.0)
        assert "Nothing to compact" in data["content"]

    def test_slash_compact_without_ctx_reports_unavailable(self):
        from agent_cli.web.server import handle_slash_command

        renderer = WebRenderer()
        conn = WebConnection(id="c1")
        renderer.register_connection(conn)

        assert handle_slash_command("/compact", renderer, ctx=None) is True
        _, data = conn.queue.get(timeout=1.0)
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
        ``try_dispatch_agent_or_skill`` so ``run`` and web share a
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

    def test_export_ui_wired(self, server_and_client):
        # Frontend↔backend contract guard for the Export feature: the page has
        # the export controls and app.js hits the export endpoints. Keeps the
        # JS (untested by an engine here) from silently drifting off the
        # server endpoints, which ARE tested in TestExportEndpoints.
        _, _, client = server_and_client
        html = client.get("/").text
        for el_id in ("export-btn", "export-bar", "export-all", "export-jira-form"):
            assert f'id="{el_id}"' in html, el_id
        js = client.get("/static/app.js").text
        assert "/api/export/html" in js
        assert "/api/export/jira" in js
        assert "/api/export/jira/targets" in js
        # The action bar uses the `hidden` attribute to show/hide, but its
        # `display:flex` ID rule outweighs the UA `[hidden]` style — so an
        # explicit `#export-bar[hidden] { display:none }` is REQUIRED or ✕
        # never hides the bar. Guard it (regression: it shipped missing once).
        css = client.get("/static/style.css").text
        assert "#export-bar[hidden]" in css
        assert "#export-jira-form[hidden]" in css

    def test_style_css_is_served(self, server_and_client):
        _, _, client = server_and_client
        resp = client.get("/static/style.css")
        assert resp.status_code == 200
        assert "text/css" in resp.headers["content-type"]

    def test_pre_code_does_not_let_inline_code_bleed_through(self, server_and_client):
        # ``<pre class="code"><code>...</code></pre>`` is how the markdown
        # pipeline wraps fenced blocks. ``.card code`` styles inline
        # backticks with a LIGHT background — without a reset it plates
        # over the dark slab the parent ``pre.code`` paints, and the
        # ``#e5e7eb`` text inherited from ``pre.code`` becomes nearly
        # invisible (light-on-light). This was the user-reported "gray
        # text on white" leak in markdown fenced blocks.
        _, _, client = server_and_client
        css = client.get("/static/style.css").text
        # The reset rule MUST exist and MUST kill the inline background
        # when the code element is nested in pre.code.
        assert ".card pre.code code" in css, (
            "missing reset rule: .card code would override pre.code's "
            "dark slab and hide the text"
        )
        # And the reset must zero out the background — anything other
        # than transparent / inherit would re-introduce the same bug
        # with a different value.
        idx = css.find(".card pre.code code")
        block = css[idx : idx + 200]
        assert "background: transparent" in block, block

    def test_static_responses_set_no_cache(self, server_and_client):
        # Editable installs ship live source for /static/*; without an
        # explicit Cache-Control header the browser falls back to
        # heuristic caching and stale CSS / JS lingers across edits.
        # All three frontend endpoints must carry no-cache so a server
        # restart is the only revalidation the operator has to perform.
        _, _, client = server_and_client
        for path in ("/", "/static/app.js", "/static/style.css"):
            resp = client.get(path)
            assert resp.status_code == 200, path
            cc = resp.headers.get("cache-control", "")
            assert "no-cache" in cc, f"{path}: missing no-cache (got {cc!r})"


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
        cid = _controller(renderer)
        resp = client.post(
            "/api/input?token=testtoken",
            json={"kind": "chat", "content": "hello", "conn_id": cid},
        )
        assert resp.status_code == 200
        # Echoed as persistent ``user_message`` event for replay.
        assert renderer.persistent_count == 1
        # Queued for the worker loop.
        popped = server.pop_chat(timeout=0.5)
        assert popped == "hello"

    def test_prompt_response_goes_to_renderer_input_queue(self, server_and_client):
        _, renderer, client = server_and_client
        cid = _controller(renderer)
        result: list[str] = []

        def worker():
            result.append(renderer.prompt_user("Q: "))

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        time.sleep(0.05)

        resp = client.post(
            "/api/input?token=testtoken",
            json={"kind": "prompt", "content": "answer text", "conn_id": cid},
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
            json={"kind": "prompt", "content": "my answer", "conn_id": "c1"},
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
        cid = _controller(renderer)
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
            json={"kind": "confirm", "key": "y", "comment": "go ahead", "conn_id": cid},
        )
        t.join(timeout=2.0)
        assert result == [("y", "go ahead")]

    def test_unknown_kind_is_400(self, server_and_client):
        _, renderer, client = server_and_client
        cid = _controller(renderer)
        resp = client.post(
            "/api/input?token=testtoken",
            json={"kind": "bogus", "content": "x", "conn_id": cid},
        )
        assert resp.status_code == 400

    def test_non_controller_input_is_403(self, server_and_client):
        # An observer (or missing/forged conn_id) cannot send input.
        _, renderer, client = server_and_client
        _controller(renderer, "ctrl")
        renderer.register_connection(WebConnection(id="obs"))  # observer
        for conn_id in ("obs", "nope", None):
            resp = client.post(
                "/api/input?token=testtoken",
                json={"kind": "chat", "content": "x", "conn_id": conn_id},
            )
            assert resp.status_code == 403, conn_id

    def test_invalid_json_is_400(self, server_and_client):
        _, _, client = server_and_client
        resp = client.post(
            "/api/input?token=testtoken",
            content="not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400


class TestControlEndpoints:
    """POST /api/request-control + /api/respond-control — the handoff flow."""

    def _has_event(self, conn, name, n=5):
        for _ in range(n):
            try:
                ev, _data = conn.queue.get(timeout=0.5)
            except Exception:
                return False
            if ev == name:
                return True
        return False

    def test_request_then_grant_transfers_control(self, server_and_client):
        _, renderer, client = server_and_client
        a = WebConnection(id="a")
        renderer.register_connection(a)  # controller
        renderer.register_connection(WebConnection(id="b"))  # observer

        assert (
            client.post(
                "/api/request-control?token=testtoken", json={"conn_id": "b"}
            ).status_code
            == 200
        )
        assert self._has_event(a, "control_request")  # controller notified
        assert (
            client.post(
                "/api/respond-control?token=testtoken",
                json={"conn_id": "a", "requester_id": "b", "grant": True},
            ).status_code
            == 200
        )
        assert renderer.is_controller("b")

    def test_respond_from_non_controller_is_403(self, server_and_client):
        _, renderer, client = server_and_client
        renderer.register_connection(WebConnection(id="a"))  # controller
        renderer.register_connection(WebConnection(id="b"))  # observer
        # b (observer) forges a grant → 403, control unchanged.
        resp = client.post(
            "/api/respond-control?token=testtoken",
            json={"conn_id": "b", "requester_id": "b", "grant": True},
        )
        assert resp.status_code == 403
        assert renderer.is_controller("a")

    def test_request_control_requires_conn_id(self, server_and_client):
        _, _, client = server_and_client
        resp = client.post("/api/request-control?token=testtoken", json={})
        assert resp.status_code == 400

    def test_control_endpoints_require_token(self, server_and_client):
        _, _, client = server_and_client
        assert (
            client.post(
                "/api/request-control?token=nope", json={"conn_id": "x"}
            ).status_code
            == 401
        )
        assert (
            client.post("/api/respond-control?token=nope", json={}).status_code == 401
        )


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


class TestStopEndpoint:
    """POST /api/stop → server.trigger_stop → the turn's stop_event."""

    def test_stop_wrong_token_is_401(self, server_and_client):
        _, _, client = server_and_client
        resp = client.post("/api/stop?token=nope")
        assert resp.status_code == 401

    def test_stop_no_active_turn_returns_false(self, server_and_client):
        _, _, client = server_and_client
        resp = client.post("/api/stop?token=testtoken")
        assert resp.status_code == 200
        assert resp.json()["stopped"] is False

    def test_stop_sets_registered_handle(self, server_and_client):
        server, _, client = server_and_client
        ev = threading.Event()
        server.set_stop_handle(ev)
        resp = client.post("/api/stop?token=testtoken")
        assert resp.status_code == 200
        assert resp.json()["stopped"] is True
        assert ev.is_set()

    def test_set_stop_handle_none_clears(self, server_and_client):
        server, _, client = server_and_client
        ev = threading.Event()
        server.set_stop_handle(ev)
        server.set_stop_handle(None)
        resp = client.post("/api/stop?token=testtoken")
        assert resp.json()["stopped"] is False
        assert not ev.is_set()

    def test_trigger_stop_unit(self, server_and_client):
        server, _, _ = server_and_client
        assert server.trigger_stop() is False  # no handle registered
        ev = threading.Event()
        server.set_stop_handle(ev)
        assert server.trigger_stop() is True
        assert ev.is_set()


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
            # First yielded event is the connection's role (identity).
            assert (await self._next(gen))["event"] == "role"
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
            assert (await self._next(gen))["event"] == "role"  # identity first
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
            assert (await self._next(gen))["event"] == "role"  # drain snapshot

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

    async def test_second_connection_does_not_end_first(self):
        # No takeover: a second connection joins as an observer and the first
        # generator keeps running + receiving events.
        renderer = WebRenderer()
        server = WebServer(renderer, token="t")

        conn = WebConnection(id="c1")
        gen = server.stream_events(conn)
        try:
            role = await self._next(gen)  # registers c1 as controller
            assert role["event"] == "role"
            assert json.loads(role["data"])["role"] == "controller"

            # A fresh connection joins — must NOT end this generator.
            renderer.register_connection(WebConnection(id="c2"))

            # c1 still receives a subsequent live emit.
            renderer.final("still here", turn=1)
            ev = await self._next(gen, timeout=2.0)
            assert ev["event"] == "assistant_turn"
            assert json.loads(ev["data"])["final"] == "still here"
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
            assert (await self._next(gen))["event"] == "role"  # drain snapshot

            # Push a real event AND then immediately unregister so
            # the generator sees one event followed by the sentinel.
            renderer.final("byebye", turn=1)
            renderer.unregister_connection(conn)

            event_dict = await self._next(gen, timeout=2.0)
            assert event_dict["event"] == "assistant_turn"

            with pytest.raises(StopAsyncIteration):
                await self._next(gen, timeout=1.0)
        finally:
            await gen.aclose()


# Imports needed by the async tests — defined here so other test
# classes don't pay the import cost when running selectively.
import asyncio  # noqa: E402

from agent_cli.render.web import WebConnection  # noqa: E402


# ── Prompt Inspector (GET /api/debug/prompt) ───────


class TestDebugPromptEndpoint:
    """The Prompt Inspector reads the renderer's latest system-prompt
    snapshot. Flow: loop → render_system_prompt_snapshot → WebRenderer
    (store-only slot) → token-authenticated GET. The payload must carry
    per-section names + size figures so the UI never re-parses the joined
    prompt string (whose section bodies are full of `##` headings)."""

    def test_requires_token(self, server_and_client):
        _, _, client = server_and_client
        assert client.get("/api/debug/prompt").status_code == 422  # missing param
        r = client.get("/api/debug/prompt?token=wrong")
        assert r.status_code == 401

    def test_before_first_call_reports_unavailable(self, server_and_client):
        _, _, client = server_and_client
        r = client.get("/api/debug/prompt?token=testtoken")
        assert r.status_code == 200
        assert r.json()["ok"] is False

    def test_snapshot_round_trips_with_sizes(self, server_and_client):
        _, renderer, client = server_and_client
        sections = [
            ("Role", "## Role\nYou are an agent."),
            ("Available Tools", "- read_file: ...\n  Input JSON: {...}"),
        ]
        renderer.note_system_prompt(sections, turn=7)
        body = client.get("/api/debug/prompt?token=testtoken").json()
        assert body["ok"] is True
        assert body["turn"] == 7
        names = [s["name"] for s in body["sections"]]
        assert names == ["Role", "Available Tools"]
        for s in body["sections"]:
            assert s["chars"] == len(s["text"])
            assert s["est_tokens"] > 0
        # totals are consistent with the per-section figures + join overhead
        assert body["total_chars"] == sum(s["chars"] for s in body["sections"]) + 2
        assert body["est_tokens"] == sum(s["est_tokens"] for s in body["sections"])

    def test_latest_snapshot_wins(self, server_and_client):
        _, renderer, client = server_and_client
        renderer.note_system_prompt([("Role", "old")], turn=1)
        renderer.note_system_prompt([("Role", "new")], turn=2)
        body = client.get("/api/debug/prompt?token=testtoken").json()
        assert body["turn"] == 2
        assert body["sections"][0]["text"] == "new"


def _note_agent_scope(renderer, *, task_id, index, agent, sections, turn):
    """Populate an agent-scoped snapshot via the real thread→task routing
    (a delegate worker captures its prompt on its own thread)."""

    def worker():
        renderer.begin_delegate_task(
            task_id=task_id, index=index, agent=agent, task_text="t"
        )
        renderer.note_system_prompt(sections, turn=turn)

    th = threading.Thread(target=worker)
    th.start()
    th.join(timeout=2.0)


class TestDebugPromptScopedEndpoints:
    """The inspector can target a delegate sub-agent via ``?task_id=`` and
    list/delete scopes. Main (no task_id) and each agent are isolated; agent
    snapshots persist post-mortem; main is not deletable."""

    def test_task_id_selects_agent_scope(self, server_and_client):
        _, renderer, client = server_and_client
        renderer.note_system_prompt([("Role", "main role")], turn=1)
        _note_agent_scope(
            renderer,
            task_id="task-A",
            index=0,
            agent="explorer",
            sections=[("Role", "explorer role")],
            turn=2,
        )
        # No task_id → main.
        main = client.get("/api/debug/prompt?token=testtoken").json()
        assert main["ok"] is True
        assert main["sections"][0]["text"] == "main role"
        # task_id → that agent.
        agent = client.get("/api/debug/prompt?token=testtoken&task_id=task-A").json()
        assert agent["ok"] is True
        assert agent["task_id"] == "task-A"
        assert agent["sections"][0]["text"] == "explorer role"

    def test_unknown_agent_scope_reports_agent_specific_reason(self, server_and_client):
        _, _, client = server_and_client
        body = client.get("/api/debug/prompt?token=testtoken&task_id=ghost").json()
        assert body["ok"] is False
        assert "agent" in body["reason"]

    def test_scopes_lists_main_and_agents(self, server_and_client):
        _, renderer, client = server_and_client
        renderer.note_system_prompt([("Role", "main")], turn=1)
        _note_agent_scope(
            renderer,
            task_id="task-A",
            index=0,
            agent="explorer",
            sections=[("Role", "A")],
            turn=1,
        )
        body = client.get("/api/debug/prompt/scopes?token=testtoken").json()
        assert body["ok"] is True
        ids = [s["id"] for s in body["scopes"]]
        assert ids[0] == ""  # main pinned first
        labels = {s["id"]: s["label"] for s in body["scopes"]}
        assert labels[""] == "Main"
        assert labels["task-A"] == "explorer·1"

    def test_scopes_requires_token(self, server_and_client):
        _, _, client = server_and_client
        assert (
            client.get("/api/debug/prompt/scopes").status_code == 422
        )  # missing param
        assert client.get("/api/debug/prompt/scopes?token=wrong").status_code == 401

    def test_delete_drops_agent_scope(self, server_and_client):
        _, renderer, client = server_and_client
        _note_agent_scope(
            renderer,
            task_id="task-A",
            index=0,
            agent="explorer",
            sections=[("Role", "A")],
            turn=1,
        )
        r = client.delete("/api/debug/prompt?token=testtoken&task_id=task-A")
        assert r.status_code == 200
        assert r.json() == {"ok": True, "removed": True}
        # Gone afterwards.
        gone = client.get("/api/debug/prompt?token=testtoken&task_id=task-A").json()
        assert gone["ok"] is False

    def test_delete_main_is_rejected_noop(self, server_and_client):
        _, renderer, client = server_and_client
        renderer.note_system_prompt([("Role", "main")], turn=1)
        r = client.delete("/api/debug/prompt?token=testtoken&task_id=")
        assert r.json()["removed"] is False
        # Main still present.
        assert client.get("/api/debug/prompt?token=testtoken").json()["ok"] is True

    def test_delete_requires_token_and_task_id(self, server_and_client):
        _, _, client = server_and_client
        # Missing token AND task_id → 422 (both are required query params).
        assert client.delete("/api/debug/prompt").status_code == 422
        assert (
            client.delete("/api/debug/prompt?token=wrong&task_id=x").status_code == 401
        )


class TestExportEndpoints:
    """Export feature endpoints: HTML download + Jira comment + targets.

    Token-authenticated and read-only (no controller gate). Jira config /
    HTTP POST are patched so these run without a live (paid) Jira."""

    _CFG = {
        "jira": {
            "instances": {
                "work": {
                    "base_url": "https://work.atlassian.net",
                    "email": "me@co.com",
                    "api_token": "tok-w",
                }
            },
            "default": "work",
        }
    }

    def test_targets_requires_token(self, server_and_client):
        _, _, client = server_and_client
        assert client.get("/api/export/jira/targets").status_code == 422  # no token
        assert client.get("/api/export/jira/targets?token=wrong").status_code == 401

    def test_targets_lists_instances_without_tokens(self, server_and_client):
        _, _, client = server_and_client
        with patch("agent_cli.config.load_config", return_value=self._CFG):
            r = client.get("/api/export/jira/targets?token=testtoken")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        names = {t["name"] for t in data["targets"]}
        assert names == {"work"}
        assert "tok-w" not in r.text  # token never leaves the server

    def test_html_export_returns_attachment(self, server_and_client):
        _, _, client = server_and_client
        r = client.post(
            "/api/export/html?token=testtoken",
            json={
                "title": "S",
                "entries": [{"kind": "user", "label": "User", "body": "hi there"}],
            },
        )
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "attachment" in r.headers["content-disposition"]
        assert "hi there" in r.text and "<!doctype html>" in r.text

    def test_html_export_requires_token_and_list(self, server_and_client):
        _, _, client = server_and_client
        assert client.post("/api/export/html", json={}).status_code == 422
        r = client.post("/api/export/html?token=testtoken", json={"entries": "x"})
        assert r.status_code == 400

    def test_jira_export_success(self, server_and_client):
        _, _, client = server_and_client
        with (
            patch("agent_cli.config.load_config", return_value=self._CFG),
            patch("agent_cli.integrations.jira.requests.post") as post,
        ):
            post.return_value = type("R", (), {"status_code": 201, "text": "{}"})()
            r = client.post(
                "/api/export/jira?token=testtoken",
                json={
                    "issue_key": "PROJ-3",
                    "entries": [{"kind": "user", "label": "User", "body": "hi"}],
                },
            )
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["url"] == "https://work.atlassian.net/browse/PROJ-3"
        # the ADF body was posted to the right URL
        assert post.call_args.args[0].endswith("/rest/api/3/issue/PROJ-3/comment")

    def test_jira_export_no_config_is_400(self, server_and_client):
        _, _, client = server_and_client
        with patch("agent_cli.config.load_config", return_value={}):
            r = client.post(
                "/api/export/jira?token=testtoken",
                json={"issue_key": "P-1", "entries": []},
            )
        assert r.status_code == 400
        assert "Jira" in r.json()["detail"]
