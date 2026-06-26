"""Unit tests for :mod:`agent_cli.web.server`.

Coverage axes:

1. **Auth** — every authenticated endpoint refuses missing / wrong
   tokens; the health probe is open.
2. **SSE stream** — events emitted by the renderer reach the SSE
   subscriber in order; the persistent buffer is replayed on connect.
3. **Multi-viewer** — every connection is equal (all may input / queue);
   a second connection does not end the first's stream.
4. **POST /api/input** — chat / prompt / confirm routes wire through
   to the renderer (chat enqueues onto the pending message queue:
   ``enqueue`` / ``dequeue_blocking`` / ``dequeue_nowait`` / cancel).
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


def _qget(conn, timeout=1.0):
    """Next queued (event, data) skipping the cross-cutting ``viewers``
    count broadcast (put on every connection's queue on join/leave)."""
    while True:
        event, data = conn.queue.get(timeout=timeout)
        if event != "viewers":
            return event, data


def _register(renderer, conn_id="ctrl"):
    """Register a connection and return its id (input tests need a live
    connection so push_user_message / can_prompt work)."""
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
        event, data = _qget(conn)
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
        event, data = _qget(conn)
        assert event == "observation"
        assert data["success"] is False
        assert "exit code: 7" in data["content"]

    def test_slash_sh_no_args_shows_usage(self):
        from agent_cli.web.server import handle_slash_command

        renderer = WebRenderer()
        conn = WebConnection(id="c1")
        renderer.register_connection(conn)

        assert handle_slash_command("/sh", renderer) is True
        event, data = _qget(conn)
        assert event == "observation"
        assert "Usage:" in data["content"]
        assert data["success"] is False

    def test_slash_sh_non_utf8_output_does_not_crash(self):
        # `/sh git show HEAD` etc. can emit non-UTF-8 bytes; text=True would
        # raise UnicodeDecodeError. Must decode with replacement, not crash.
        from agent_cli.web.server import handle_slash_command

        renderer = WebRenderer()
        conn = WebConnection(id="c1")
        renderer.register_connection(conn)

        assert handle_slash_command(r"/sh printf '\xff\xfe hi'", renderer) is True
        event, data = _qget(conn)
        assert event == "observation"
        assert "hi" in data["content"]

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
        event, data = _qget(conn)
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
        _, data = _qget(conn)
        assert "Nothing to compact" in data["content"]

    def test_slash_compact_without_ctx_reports_unavailable(self):
        from agent_cli.web.server import handle_slash_command

        renderer = WebRenderer()
        conn = WebConnection(id="c1")
        renderer.register_connection(conn)

        assert handle_slash_command("/compact", renderer, ctx=None) is True
        _, data = _qget(conn)
        assert data["success"] is False

    def test_slash_help_lists_supported_commands(self):
        from agent_cli.web.server import handle_slash_command

        renderer = WebRenderer()
        conn = WebConnection(id="c1")
        renderer.register_connection(conn)

        assert handle_slash_command("/help", renderer) is True
        event, data = _qget(conn)
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


class _FakeInspectorCtx:
    def __init__(self, messages):
        self._messages = messages

    def get_messages(self):
        return self._messages


class TestPromptInspectorDynamic:
    """Phase A: the Prompt Inspector shows the DYNAMIC context (conversation +
    observations) alongside the static system prompt, reusing the sections
    pipeline (kind=system | dynamic)."""

    def test_dynamic_sections_helper(self):
        from agent_cli.web.server import _dynamic_context_sections

        assert _dynamic_context_sections(None) == []
        ctx = _FakeInspectorCtx(
            [
                {"role": "system", "content": "you are an agent"},  # skipped
                {"role": "user", "content": "[DJ]: analyze the project"},
                {"role": "user", "content": "[read_file]\nfile body..."},
                {"role": "assistant", "content": "## Thought\nok\n## Action\n[...]"},
            ]
        )
        secs = _dynamic_context_sections(ctx)
        assert all(s["kind"] == "dynamic" for s in secs)
        assert len(secs) == 3  # system skipped
        assert any("analyze the project" in s["name"] for s in secs)
        assert all(s["est_tokens"] >= 0 and "text" in s for s in secs)

    def test_endpoint_includes_system_and_dynamic(self):
        renderer = WebRenderer()
        renderer.note_system_prompt([("System Prompt", "you are an agent")], turn=2)
        ctx = _FakeInspectorCtx(
            [
                {"role": "user", "content": "[DJ]: hello"},
                {"role": "assistant", "content": "## Thought\nhi"},
            ]
        )
        server = WebServer(renderer, token="t", ctx=ctx)
        client = TestClient(create_app(server))
        r = client.get("/api/debug/prompt?token=t")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"]
        kinds = [s.get("kind") for s in data["sections"]]
        assert "system" in kinds and "dynamic" in kinds
        # system section first, dynamic after
        assert kinds[0] == "system"
        assert kinds.count("dynamic") == 2

    def test_dynamic_shows_before_any_llm_call_on_resume(self):
        """Part 1: a resumed session has ctx messages but no system snapshot
        yet (the loop captures only on an LLM call). The inspector must still
        show the restored conversation immediately, not 'no LLM call yet'."""
        renderer = WebRenderer()  # NO note_system_prompt → no snapshot
        ctx = _FakeInspectorCtx([{"role": "user", "content": "[DJ]: resumed question"}])
        server = WebServer(renderer, token="t", ctx=ctx)
        client = TestClient(create_app(server))
        data = client.get("/api/debug/prompt?token=t").json()
        assert data["ok"]  # not blocked by the missing system snapshot
        assert all(s["kind"] == "dynamic" for s in data["sections"])
        assert any("resumed question" in s["name"] for s in data["sections"])

    def test_empty_session_no_snapshot_no_messages_is_not_ok(self):
        renderer = WebRenderer()
        server = WebServer(renderer, token="t", ctx=_FakeInspectorCtx([]))
        client = TestClient(create_app(server))
        assert client.get("/api/debug/prompt?token=t").json()["ok"] is False

    def test_startup_capture_populates_system_prompt_before_first_call(self):
        """Part 2: build + capture the system prompt at web startup so the
        inspector shows it before any message (the loop only captures on an
        LLM call)."""
        from agent_cli.providers.capabilities import ModelCapabilities
        from agent_cli.web.server import capture_startup_system_prompt
        from agent_cli.wire_formats import get

        caps = ModelCapabilities(
            context_window=32768,
            max_output_tokens=4096,
            supports_structured_output=True,
            supports_thinking=False,
            thinking_budget=0,
            supports_strict_schema=False,
        )
        renderer = WebRenderer()
        assert renderer.prompt_snapshot() is None  # empty before
        capture_startup_system_prompt(
            renderer,
            capabilities=caps,
            wire_format=get("md_array"),
            session_dir="",
            max_depth=2,
        )
        snap = renderer.prompt_snapshot()
        assert snap is not None and len(snap["sections"]) > 0
        names = " ".join(s["name"] for s in snap["sections"]).lower()
        assert "role" in names or "tool" in names  # real system sections

    def test_subagent_scope_stays_system_only(self):
        renderer = WebRenderer()
        # a delegate sub-agent snapshot under a task_id scope
        import threading

        renderer._thread_to_task[threading.get_ident()] = "t-1"
        renderer.note_system_prompt([("System", "subagent sys")], turn=1)
        renderer._thread_to_task.pop(threading.get_ident(), None)
        ctx = _FakeInspectorCtx([{"role": "user", "content": "should NOT appear"}])
        server = WebServer(renderer, token="t", ctx=ctx)
        client = TestClient(create_app(server))
        r = client.get("/api/debug/prompt?token=t&task_id=t-1")
        data = r.json()
        assert data["ok"]
        assert all(s.get("kind") == "system" for s in data["sections"])


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
        # edit_file is flat-native (one op, no `edits` array) — the action card
        # must NOT count a non-existent `edits` array (that always read 0 →
        # "(0 edits)"); it shows the op/ref instead.
        assert "editCount" not in body

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

    def test_delegate_observation_renders_markdown(self, server_and_client):
        # A delegate observation is a subagent's prose answer → it must be
        # markdown-rendered (escapeAndFormat → .obs-md div), not the raw <pre>
        # the other (monospace) tool outputs use. Guard the branch + style so
        # it can't silently regress to the unreadable raw blob.
        _, _, client = server_and_client
        js = client.get("/static/app.js").text
        assert '=== "delegate"' in js
        assert "obs-md" in js and "escapeAndFormat" in js
        css = client.get("/static/style.css").text
        assert ".obs-body.obs-md" in css

    def test_style_css_is_served(self, server_and_client):
        _, _, client = server_and_client
        resp = client.get("/static/style.css")
        assert resp.status_code == 200
        assert "text/css" in resp.headers["content-type"]

    def test_theme_tokens_and_picker_wired(self, server_and_client):
        """Multi-theme design-token system + a working theme picker. Guards that
        a future edit can't silently (a) reintroduce raw colors in the body,
        (b) drop a curated theme block, or (c) break the picker / FOUC wiring."""
        import re

        _, _, client = server_and_client
        css = client.get("/static/style.css").text
        html = client.get("/").text
        js = client.get("/static/app.js").text

        # :root base (= slate) + every curated theme defines tokens
        assert ":root {" in css
        for theme in ("midnight", "terminal", "amber", "light"):
            assert f':root[data-theme="{theme}"]' in css, f"missing theme: {theme}"
        # borders are translucent hairlines in the dark base (the refined look)
        assert "--border: rgba(255,255,255,.07)" in css
        # body is fully tokenized — no raw hex colors after the token block
        body = css[css.index("* { box-sizing") :]
        assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", body), "raw hex leaked into body"
        # no token resolves to another var() (the self-reference bug)
        tok = css[css.index(":root {") : css.index("* { box-sizing")]
        assert not re.findall(r"--[\w-]+:\s*var\(", tok), "token self-reference"
        # FOUC-prevention applies a saved/default theme before paint
        assert "agentcli_theme" in html and "data-theme" in html
        assert '"amber"' in html  # default theme in the inline script
        # picker: button + menu container + JS that builds it from the theme list
        assert 'id="theme-btn"' in html and 'id="theme-menu"' in html
        assert 'getElementById("theme-menu")' in js and "agentcli_theme" in js
        assert 'id: "midnight"' in js and 'id: "terminal"' in js

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
    def test_chat_message_is_enqueued_not_echoed(self, server_and_client):
        server, renderer, client = server_and_client
        cid = _register(renderer)
        resp = client.post(
            "/api/input?token=testtoken",
            json={"kind": "chat", "content": "hello", "conn_id": cid},
        )
        assert resp.status_code == 200
        # No immediate conversation echo — it sits in the live queue until
        # dequeued (then the worker/loop renders it).
        pending = server.queue_snapshot()
        assert len(pending) == 1
        assert pending[0]["text"] == "hello" and pending[0]["conn_id"] == cid
        # dequeue_blocking returns the item (text + nickname).
        item = server.dequeue_blocking()
        assert item["text"] == "hello"

    def test_prompt_response_goes_to_renderer_input_queue(self, server_and_client):
        _, renderer, client = server_and_client
        cid = _register(renderer)
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
        cid = _register(renderer)
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
        cid = _register(renderer)
        resp = client.post(
            "/api/input?token=testtoken",
            json={"kind": "bogus", "content": "x", "conn_id": cid},
        )
        assert resp.status_code == 400

    def test_any_connection_can_send_input(self, server_and_client):
        # No controller gate: every registered connection may send input.
        _, renderer, client = server_and_client
        renderer.register_connection(WebConnection(id="a"))
        renderer.register_connection(WebConnection(id="b"))
        for conn_id in ("a", "b"):
            resp = client.post(
                "/api/input?token=testtoken",
                json={"kind": "chat", "content": "x", "conn_id": conn_id},
            )
            assert resp.status_code == 200, conn_id

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


class TestMessageQueue:
    """``enqueue`` / ``dequeue_blocking`` / ``dequeue_nowait`` / cancel +
    the ``shutdown`` sentinel that wakes a blocked worker."""

    def test_shutdown_wakes_blocked_worker(self):
        renderer = WebRenderer()
        srv = WebServer(renderer)
        msgs: list = []

        def worker():
            while True:
                m = srv.dequeue_blocking()
                if m is srv.SHUTDOWN:
                    msgs.append("done")
                    break
                msgs.append(m["text"])

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        srv.enqueue("c1", "hello")
        srv.shutdown()
        t.join(timeout=1.0)
        assert msgs == ["hello", "done"]
        assert not t.is_alive()

    def test_enqueue_assigns_nickname_and_id(self):
        renderer = WebRenderer()
        renderer.register_connection(WebConnection(id="c1"))
        srv = WebServer(renderer)
        item = srv.enqueue("c1", "do the thing")
        assert item["text"] == "do the thing"
        assert item["conn_id"] == "c1"
        assert item["nickname"]  # a fun nickname was attached
        assert item["id"]

    def test_dequeue_nowait_fifo_then_none(self):
        srv = WebServer(WebRenderer())
        srv.enqueue("a", "first")
        srv.enqueue("b", "second")
        assert srv.dequeue_nowait()["text"] == "first"
        assert srv.dequeue_nowait()["text"] == "second"
        assert srv.dequeue_nowait() is None  # empty → non-blocking None

    def test_cancel_only_by_owner_and_pending(self):
        srv = WebServer(WebRenderer())
        it = srv.enqueue("owner", "x")
        assert srv.cancel_pending("someone-else", it["id"]) is False  # not owner
        assert len(srv.queue_snapshot()) == 1
        assert srv.cancel_pending("owner", it["id"]) is True
        assert srv.queue_snapshot() == []
        # already gone → cancel is a no-op False
        assert srv.cancel_pending("owner", it["id"]) is False

    def test_enqueue_broadcasts_queue_event(self):
        renderer = WebRenderer()
        conn = WebConnection(id="c1")
        renderer.register_connection(conn)
        srv = WebServer(renderer)
        srv.enqueue("c1", "hello")
        # the `queue` event is broadcast to the live connection
        events = []
        import queue as _q

        while True:
            try:
                ev, data = conn.queue.get_nowait()
            except _q.Empty:
                break
            if ev == "queue":
                events.append(data)
        assert events and events[-1]["pending"][0]["text"] == "hello"

    def test_queue_ui_wired(self, server_and_client):
        _, _, client = server_and_client
        html = client.get("/").text
        for el_id in ("queue-list", "chat-stop"):
            assert f'id="{el_id}"' in html, el_id
        js = client.get("/static/app.js").text
        assert '"queue"' in js  # SSE handler
        assert "/api/queue/cancel" in js


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

    def test_falls_back_when_live_server_holds_port_via_wildcard(self):
        """The real bug: another instance listens on ``0.0.0.0:PORT``; a new
        ``--host <ip>`` run must NOT pick the same PORT. ``SO_REUSEADDR`` alone
        false-positives (a specific-IP bind coexists with the wildcard one on
        macOS/BSD), so two servers fight for the port. A connect-based liveness
        check catches the live listener → fall back to an ephemeral port."""
        import socket as _s

        holder = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
        try:
            holder.bind(("0.0.0.0", 0))  # wildcard — covers all local IPs
            holder.listen(1)
            busy = holder.getsockname()[1]
            # Pick for a SPECIFIC host (127.0.0.1) that the wildcard listener
            # already serves — must detect it's taken and fall back.
            picked = pick_port("127.0.0.1", busy)
            assert picked != busy
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
        # ``viewers`` is a cross-cutting count broadcast that can appear in the
        # snapshot or interleave on join/leave — skip it so sequence asserts
        # stay about the events under test.
        while True:
            ev = await asyncio.wait_for(gen.__anext__(), timeout=timeout)
            if ev.get("event") != "viewers":
                return ev

    async def test_replay_buffer_on_connect(self):
        renderer = WebRenderer()
        server = WebServer(renderer, token="t")
        renderer.final("preconnect answer", turn=1)

        conn = WebConnection(id="c1")
        gen = server.stream_events(conn)
        try:
            # First yielded event is the connection's role (identity).
            assert (await self._next(gen))["event"] == "identity"
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
            assert (await self._next(gen))["event"] == "identity"  # identity first
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
            assert (await self._next(gen))["event"] == "identity"  # drain snapshot

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
        # A second connection joins; the first generator keeps running and
        # generator keeps running + receiving events.
        renderer = WebRenderer()
        server = WebServer(renderer, token="t")

        conn = WebConnection(id="c1")
        gen = server.stream_events(conn)
        try:
            ident = await self._next(gen)  # registers c1
            assert ident["event"] == "identity"
            assert json.loads(ident["data"])["conn_id"] == "c1"

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
            assert (await self._next(gen))["event"] == "identity"  # drain snapshot

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

    # Credentials are no longer stored server-side; deployment is pinned so the
    # targets endpoint doesn't probe the network during tests.
    _CFG = {
        "jira": {
            "instances": {
                "work": {
                    "base_url": "https://work.atlassian.net",
                    "deployment": "cloud",
                },
                "dc": {"base_url": "https://jira.corp", "deployment": "server"},
            },
            "default": "work",
        }
    }

    def test_targets_requires_token(self, server_and_client):
        _, _, client = server_and_client
        assert client.get("/api/export/jira/targets").status_code == 422  # no token
        assert client.get("/api/export/jira/targets?token=wrong").status_code == 401

    def test_targets_lists_instances_with_deployment(self, server_and_client):
        _, _, client = server_and_client
        with patch("agent_cli.config.load_config", return_value=self._CFG):
            r = client.get("/api/export/jira/targets?token=testtoken")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        by_name = {t["name"]: t for t in data["targets"]}
        assert set(by_name) == {"work", "dc"}
        assert by_name["work"]["deployment"] == "cloud"
        assert by_name["dc"]["deployment"] == "server"

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

    def test_jira_export_cloud_posts_adf_as_user(self, server_and_client):
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
                    "auth": {"user": "me@co.com", "secret": "tok"},
                },
            )
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["url"] == "https://work.atlassian.net/browse/PROJ-3"
        assert data["deployment"] == "cloud"
        # ADF posted to v3 with the USER's credentials (not a server account)
        call = post.call_args
        assert call.args[0].endswith("/rest/api/3/issue/PROJ-3/comment")
        assert call.kwargs["auth"] == ("me@co.com", "tok")
        assert call.kwargs["json"]["body"]["type"] == "doc"

    def test_jira_export_server_posts_wiki_as_user(self, server_and_client):
        _, _, client = server_and_client
        with (
            patch("agent_cli.config.load_config", return_value=self._CFG),
            patch("agent_cli.integrations.jira.requests.post") as post,
        ):
            post.return_value = type("R", (), {"status_code": 201, "text": "{}"})()
            r = client.post(
                "/api/export/jira?token=testtoken",
                json={
                    "target": "dc",
                    "issue_key": "DC-9",
                    "entries": [{"kind": "user", "label": "User", "body": "hi"}],
                    "auth": {"user": "alice", "secret": "pw"},
                },
            )
        assert r.status_code == 200
        data = r.json()
        assert data["deployment"] == "server"
        call = post.call_args
        assert call.args[0].endswith("/rest/api/2/issue/DC-9/comment")
        assert call.kwargs["auth"] == ("alice", "pw")
        # v2 body is a wiki-markup STRING, not ADF
        assert isinstance(call.kwargs["json"]["body"], str)
        assert "*User*" in call.kwargs["json"]["body"]

    def test_jira_export_user_supplied_https_url_zero_config(self, server_and_client):
        # No config at all: the user types the base_url in the UI and it works.
        _, _, client = server_and_client
        with (
            patch("agent_cli.config.load_config", return_value={}),
            patch("agent_cli.integrations.jira.requests.post") as post,
        ):
            post.return_value = type("R", (), {"status_code": 201, "text": "{}"})()
            r = client.post(
                "/api/export/jira?token=testtoken",
                json={
                    "base_url": "https://mine.atlassian.net",
                    "issue_key": "X-1",
                    "deployment": "cloud",
                    "entries": [{"kind": "user", "label": "User", "body": "hi"}],
                    "auth": {"user": "me@x.com", "secret": "tok"},
                },
            )
        assert r.status_code == 200
        assert r.json()["url"] == "https://mine.atlassian.net/browse/X-1"
        assert post.call_args.args[0].startswith("https://mine.atlassian.net/rest/")

    def test_jira_export_user_supplied_http_url_ok(self, server_and_client):
        # A user-typed http:// base_url is now allowed (plaintext risk is a UI
        # warning, not a hard block). deployment is given explicitly so
        # detect_deployment does not hit the network.
        _, _, client = server_and_client
        with (
            patch("agent_cli.config.load_config", return_value={}),
            patch("agent_cli.integrations.jira.requests.post") as post,
        ):
            post.return_value = type("R", (), {"status_code": 201, "text": "{}"})()
            r = client.post(
                "/api/export/jira?token=testtoken",
                json={
                    "base_url": "http://insecure.lan",
                    "issue_key": "X-1",
                    "deployment": "cloud",
                    "entries": [{"kind": "user", "label": "User", "body": "hi"}],
                    "auth": {"user": "u", "secret": "s"},
                },
            )
        assert r.status_code == 200
        assert r.json()["url"] == "http://insecure.lan/browse/X-1"
        assert post.call_args.args[0].startswith("http://insecure.lan/rest/")

    def test_jira_export_missing_auth_is_400(self, server_and_client):
        _, _, client = server_and_client
        with patch("agent_cli.config.load_config", return_value=self._CFG):
            r = client.post(
                "/api/export/jira?token=testtoken",
                json={"issue_key": "P-1", "entries": []},
            )
        assert r.status_code == 400
        assert "credentials" in r.json()["detail"].lower()

    def test_jira_export_no_config_is_400(self, server_and_client):
        _, _, client = server_and_client
        with patch("agent_cli.config.load_config", return_value={}):
            r = client.post(
                "/api/export/jira?token=testtoken",
                json={
                    "issue_key": "P-1",
                    "entries": [],
                    "auth": {"user": "u", "secret": "s"},
                },
            )
        assert r.status_code == 400
        assert "No Jira instances" in r.json()["detail"]


class TestWorkspaceDownload:
    """Workspace file tree + zip download. Token-auth, read-only. The
    workspace root is overridden to a tmp dir so tests are isolated and
    don't zip the whole repo."""

    @staticmethod
    def _setup(server, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.c").write_text("int main(){}\n")
        (tmp_path / "src" / "util.c").write_text("// util\n")
        (tmp_path / "README.md").write_text("# hi\n")
        (tmp_path / ".agent-cli").mkdir()
        (tmp_path / ".agent-cli" / "x.json").write_text("{}")
        server.workspace = tmp_path.resolve()

    def test_tree_requires_token(self, server_and_client):
        _, _, client = server_and_client
        assert client.get("/api/workspace/tree").status_code == 422
        assert client.get("/api/workspace/tree?token=wrong").status_code == 401

    def test_tree_lists_root_dirs_first(self, server_and_client, tmp_path):
        server, _, client = server_and_client
        self._setup(server, tmp_path)
        r = client.get("/api/workspace/tree?token=testtoken")
        assert r.status_code == 200
        entries = r.json()["entries"]
        names = [e["name"] for e in entries]
        # dirs first (sorted), then files; .agent-cli IS shown (no exclusions)
        assert names == [".agent-cli", "src", "README.md"]
        src = next(e for e in entries if e["name"] == "src")
        assert src["type"] == "dir" and src["rel"] == "src"
        # directories report a recursive size too (not None)
        assert src["size"] > 0

    def test_tree_lazy_subdir(self, server_and_client, tmp_path):
        server, _, client = server_and_client
        self._setup(server, tmp_path)
        r = client.get("/api/workspace/tree?token=testtoken&path=src")
        assert {e["name"] for e in r.json()["entries"]} == {"main.c", "util.c"}

    def test_tree_rejects_traversal(self, server_and_client, tmp_path):
        server, _, client = server_and_client
        self._setup(server, tmp_path)
        assert (
            client.get("/api/workspace/tree?token=testtoken&path=../etc").status_code
            == 400
        )

    def test_download_selected_dir_recursive(self, server_and_client, tmp_path):
        import io
        import zipfile

        server, _, client = server_and_client
        self._setup(server, tmp_path)
        r = client.post(
            "/api/workspace/download?token=testtoken", json={"paths": ["src"]}
        )
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        assert set(zf.namelist()) == {"src/main.c", "src/util.c"}

    def test_download_single_file(self, server_and_client, tmp_path):
        import io
        import zipfile

        server, _, client = server_and_client
        self._setup(server, tmp_path)
        r = client.post(
            "/api/workspace/download?token=testtoken", json={"paths": ["README.md"]}
        )
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        assert zf.namelist() == ["README.md"]

    def test_download_all_includes_everything(self, server_and_client, tmp_path):
        import io
        import zipfile

        server, _, client = server_and_client
        self._setup(server, tmp_path)
        r = client.post("/api/workspace/download?token=testtoken", json={"all": True})
        names = set(zipfile.ZipFile(io.BytesIO(r.content)).namelist())
        # "전부 표시" decision: .agent-cli is included on All
        assert "src/main.c" in names and "README.md" in names
        assert ".agent-cli/x.json" in names

    def test_download_empty_selection_400(self, server_and_client, tmp_path):
        server, _, client = server_and_client
        self._setup(server, tmp_path)
        assert (
            client.post(
                "/api/workspace/download?token=testtoken", json={"paths": []}
            ).status_code
            == 400
        )

    def test_download_rejects_traversal(self, server_and_client, tmp_path):
        server, _, client = server_and_client
        self._setup(server, tmp_path)
        r = client.post(
            "/api/workspace/download?token=testtoken", json={"paths": ["../secret"]}
        )
        assert r.status_code == 400

    def test_files_ui_wired(self, server_and_client):
        """One 📁 drawer now serves both download (select → zip) and upload
        (drop into the drawer → uploads to the clicked dir)."""
        _, _, client = server_and_client
        html = client.get("/").text
        # single 📁 button replaces the old 📥/📎 pair
        for el_id in (
            "files-btn",
            "download-drawer",
            "dl-tree",
            "dl-download",
            "ul-drop",  # upload dropzone folded into the same drawer
            "ul-target",  # active upload-target indicator
        ):
            assert f'id="{el_id}"' in html, el_id
        assert 'id="download-btn"' not in html and 'id="upload-btn"' not in html
        # directory upload: folder-pick button + recursive drop walk
        assert 'id="ul-pick-dir"' in html
        assert "webkitdirectory" in html
        js = client.get("/static/app.js").text
        assert "/api/workspace/tree" in js
        assert "/api/workspace/download" in js
        assert "/api/workspace/upload" in js  # upload merged in
        assert "webkitGetAsEntry" in js  # recursive directory drop walk
        # root is a tree row (the upload-target model is uniform: any row,
        # incl. root, is selectable). The old ✕ / re-click toggle are gone.
        assert "makeRootRow" in js
        assert "ul-target-clear" not in js
        assert "uploadDir === entry.rel" not in js
        # "whole workspace" download is now the root row's checkbox, not a
        # separate "All" control.
        assert 'id="dl-all"' not in html
        assert "all-selected" in js  # root-checkbox dims the rest
        # open() must clear a prior whole-workspace dim (root checkbox), or a
        # reopened drawer stays greyed out (regression)
        assert 'classList.remove("all-selected")' in js
        css = client.get("/static/style.css").text
        assert "#download-drawer" in css
        assert ".dl-row.target" in css  # upload-target highlight


class TestWorkspaceUpload:
    """Workspace file upload (📎). Token-auth, WRITE — so the guards are
    stricter than download: filename basename-only, target dir under the
    workspace, a size cap, overwrite reported. Raw request body = file bytes
    (no python-multipart dependency)."""

    @staticmethod
    def _setup(server, tmp_path):
        (tmp_path / "src").mkdir()
        server.workspace = tmp_path.resolve()

    def _post(self, client, body, *, name, path="", token="testtoken"):
        q = f"/api/workspace/upload?token={token}&name={name}"
        if path:
            q += f"&path={path}"
        return client.post(q, content=body)

    def test_requires_token(self, server_and_client, tmp_path):
        server, _, client = server_and_client
        self._setup(server, tmp_path)
        assert self._post(client, b"x", name="a.txt", token="wrong").status_code == 401

    def test_uploads_to_root(self, server_and_client, tmp_path):
        server, _, client = server_and_client
        self._setup(server, tmp_path)
        r = self._post(client, b"hello\n", name="note.txt")
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "note.txt"
        assert body["rel"] == "note.txt"
        assert body["overwritten"] is False
        assert (tmp_path / "note.txt").read_bytes() == b"hello\n"

    def test_uploads_to_subdir(self, server_and_client, tmp_path):
        server, _, client = server_and_client
        self._setup(server, tmp_path)
        r = self._post(client, b"int main(){}\n", name="m.c", path="src")
        assert r.status_code == 200
        assert r.json()["rel"] == "src/m.c"
        assert (tmp_path / "src" / "m.c").exists()

    def test_overwrite_reported(self, server_and_client, tmp_path):
        server, _, client = server_and_client
        self._setup(server, tmp_path)
        (tmp_path / "a.txt").write_text("old")
        r = self._post(client, b"new", name="a.txt")
        assert r.json()["overwritten"] is True
        assert (tmp_path / "a.txt").read_bytes() == b"new"

    def test_rejects_filename_traversal(self, server_and_client, tmp_path):
        server, _, client = server_and_client
        self._setup(server, tmp_path)
        # ``..`` segment in the name escapes — rejected (dir-upload allows "/"
        # but not traversal).
        assert self._post(client, b"x", name="../escape.txt").status_code == 400
        assert not (tmp_path.parent / "escape.txt").exists()

    def test_directory_upload_nested_path(self, server_and_client, tmp_path):
        """Directory upload: ``name`` may be a relative path with ``/`` — the
        nested dirs are created under the (existing) target ``path``."""
        server, _, client = server_and_client
        self._setup(server, tmp_path)
        r = self._post(client, b"int main(){}\n", name="mydir/sub/a.c", path="src")
        assert r.status_code == 200
        assert r.json()["rel"] == "src/mydir/sub/a.c"
        assert (
            tmp_path / "src" / "mydir" / "sub" / "a.c"
        ).read_bytes() == b"int main(){}\n"

    def test_directory_upload_creates_under_root(self, server_and_client, tmp_path):
        server, _, client = server_and_client
        self._setup(server, tmp_path)
        r = self._post(client, b"y", name="pkg/mod.py")  # no path → root
        assert r.status_code == 200
        assert (tmp_path / "pkg" / "mod.py").exists()

    def test_rejects_traversal_inside_nested_name(self, server_and_client, tmp_path):
        server, _, client = server_and_client
        self._setup(server, tmp_path)
        # a ``..`` segment anywhere in the relative path must be rejected
        assert self._post(client, b"x", name="a/../../etc/passwd").status_code == 400
        assert not (tmp_path.parent / "etc").exists()
        # absolute path rejected
        assert self._post(client, b"x", name="/etc/passwd").status_code == 400

    def test_rejects_dir_traversal(self, server_and_client, tmp_path):
        server, _, client = server_and_client
        self._setup(server, tmp_path)
        assert self._post(client, b"x", name="a.txt", path="../etc").status_code == 400

    def test_rejects_empty_or_dotted_name(self, server_and_client, tmp_path):
        server, _, client = server_and_client
        self._setup(server, tmp_path)
        assert self._post(client, b"x", name="").status_code in (400, 422)
        assert self._post(client, b"x", name="..").status_code == 400
        assert self._post(client, b"x", name=".").status_code == 400

    def test_rejects_oversize(self, server_and_client, tmp_path):
        from agent_cli.web.server import _MAX_UPLOAD_BYTES

        server, _, client = server_and_client
        self._setup(server, tmp_path)
        big = b"x" * (_MAX_UPLOAD_BYTES + 1)
        assert self._post(client, big, name="big.bin").status_code == 413

    def test_rejects_nonexistent_target_dir(self, server_and_client, tmp_path):
        server, _, client = server_and_client
        self._setup(server, tmp_path)
        # target dir must exist (we don't silently mkdir arbitrary trees)
        assert self._post(client, b"x", name="a.txt", path="nope").status_code == 400

    # NOTE: the upload frontend (📁 merged drawer) is asserted in
    # TestWorkspaceDownload::test_files_ui_wired — upload + download share one
    # drawer now, so the asset wiring lives in one place.


class TestViewerCount:
    """Viewer roster (count + fun nicknames) broadcast on join/leave + UI."""

    @staticmethod
    def _viewer_events(conn):
        import queue as _q

        out = []
        while True:
            try:
                ev, data = conn.queue.get_nowait()
            except _q.Empty:
                break
            if ev == "viewers":
                out.append(data)
        return out

    @staticmethod
    def _snap_viewers(snapshot):
        for ev, data in snapshot:
            if ev == "viewers":
                return data
        return None

    def test_broadcast_on_join_and_leave(self):
        from agent_cli.render.web import _NICKNAMES

        r = WebRenderer()
        a = WebConnection(id="a")
        # joining conn learns the roster via its snapshot (not its queue)
        snap_a = r.register_connection(a)
        va = self._snap_viewers(snap_a)
        assert va["count"] == 1
        assert va["viewers"][0]["id"] == "a"
        assert va["viewers"][0]["name"] in _NICKNAMES  # fun default assigned
        assert self._viewer_events(a) == []  # nothing on its own queue

        b = WebConnection(id="b")
        snap_b = r.register_connection(b)
        assert self._snap_viewers(snap_b)["count"] == 2
        evs = self._viewer_events(a)  # existing conn learns via queue
        assert evs[-1]["count"] == 2
        names = {v["name"] for v in evs[-1]["viewers"]}
        assert len(names) == 2  # distinct nicknames while pool not exhausted

        r.unregister_connection(b)
        assert self._viewer_events(a)[-1]["count"] == 1  # decremented on leave

    def test_viewers_ui_wired(self, server_and_client):
        _, _, client = server_and_client
        assert 'id="viewers"' in client.get("/").text
        assert '"viewers"' in client.get("/static/app.js").text


class TestNickname:
    """User-set nickname (fun default pre-filled, editable on first connect)."""

    def test_set_nickname_updates_roster(self, server_and_client):
        _, renderer, client = server_and_client
        from agent_cli.render.web import WebConnection

        renderer.register_connection(WebConnection(id="c1"))
        resp = client.post(
            "/api/nickname?token=testtoken",
            json={"conn_id": "c1", "name": "  Captain Code  "},
        )
        assert resp.status_code == 200 and resp.json()["ok"] is True
        assert renderer.nickname_for("c1") == "Captain Code"  # trimmed

    def test_empty_name_rejected(self, server_and_client):
        _, renderer, client = server_and_client
        from agent_cli.render.web import WebConnection

        renderer.register_connection(WebConnection(id="c1"))
        before = renderer.nickname_for("c1")
        resp = client.post(
            "/api/nickname?token=testtoken", json={"conn_id": "c1", "name": "   "}
        )
        assert resp.json()["ok"] is False
        assert renderer.nickname_for("c1") == before  # unchanged

    def test_nickname_requires_token(self, server_and_client):
        _, _, client = server_and_client
        assert client.post("/api/nickname?token=nope", json={}).status_code == 401

    def test_nickname_ui_wired(self, server_and_client):
        _, _, client = server_and_client
        html = client.get("/").text
        for el_id in ("name-bar", "nb-input", "nb-set"):
            assert f'id="{el_id}"' in html, el_id
        assert "/api/nickname" in client.get("/static/app.js").text


class TestAutoReviewToggle:
    """POST /api/auto_review flips the worker-read toggle; default off."""

    def test_default_off(self, server_and_client):
        server, _, _ = server_and_client
        assert server.auto_review_enabled() is False

    def test_toggle_on_and_off(self, server_and_client):
        server, _, client = server_and_client
        r = client.post("/api/auto_review?token=testtoken", json={"enabled": True})
        assert r.status_code == 200
        assert r.json() == {"enabled": True}
        assert server.auto_review_enabled() is True

        r = client.post("/api/auto_review?token=testtoken", json={"enabled": False})
        assert r.json() == {"enabled": False}
        assert server.auto_review_enabled() is False

    def test_requires_token(self, server_and_client):
        _, _, client = server_and_client
        r = client.post("/api/auto_review?token=wrong", json={"enabled": True})
        assert r.status_code in (401, 403)

    def test_set_auto_review_coerces_bool(self, server_and_client):
        server, _, _ = server_and_client
        server.set_auto_review(1)
        assert server.auto_review_enabled() is True
        server.set_auto_review(0)
        assert server.auto_review_enabled() is False

    def test_set_auto_review_broadcasts_to_renderer(self, server_and_client):
        """Toggle change is broadcast (sticky) so every browser's button
        syncs — not just the one that POSTed."""
        from agent_cli.render.web import WebConnection

        server, renderer, _ = server_and_client
        c = WebConnection(id="c")
        renderer.register_connection(c)
        while not c.queue.empty():
            c.queue.get_nowait()
        server.set_auto_review(True)
        seen = []
        while not c.queue.empty():
            seen.append(c.queue.get_nowait())
        assert any(e == "auto_review" and d.get("enabled") is True for e, d in seen)
        # and a reconnecting client sees it in the snapshot
        snap = renderer.register_connection(WebConnection(id="late"))
        assert any(e == "auto_review" for e, _ in snap)
