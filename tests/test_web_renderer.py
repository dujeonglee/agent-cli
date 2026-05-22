"""Unit tests for :class:`agent_cli.render.web.WebRenderer`.

Coverage axes:

1. **Event distribution** — persistent events go to the buffer + every
   active connection; transient events only to live connections.
2. **Connection lifecycle** — registering takes over older connections,
   the snapshot returned to a new client matches the buffer.
3. **Pruning** — ``prune(n)`` shrinks the buffer and notifies clients.
4. **Assistant turn bundling** — ``thought()`` is held and emitted as
   part of the next ``action()`` / ``final()`` so each LLM emission
   produces exactly one persistent ``assistant_turn`` event.
5. **Input flow** — ``prompt_user`` / ``confirm`` block until the
   server pushes input. Abort raises ``EOFError`` from ``prompt_user``
   and returns the safe default from ``confirm``.
"""

from __future__ import annotations

import threading
import time

from agent_cli.render.base import ConfirmOption
from agent_cli.render.web import WebConnection, WebRenderer


# ── Event distribution ─────────────────────────────


class TestEventDistribution:
    """Persistent vs transient routing."""

    def test_persistent_event_lands_in_buffer_and_queue(self):
        r = WebRenderer()
        conn = WebConnection(id="c1")
        r.register_connection(conn)

        r.final("done", turn=1)

        # Buffer keeps it for replay.
        assert r.persistent_count == 1
        # Live connection got it too.
        event, data = conn.queue.get(timeout=0.5)
        assert event == "assistant_turn"
        assert data["final"] == "done"
        assert data["turn"] == 1

    def test_transient_event_skips_buffer(self):
        r = WebRenderer()
        conn = WebConnection(id="c1")
        r.register_connection(conn)

        r.status("running", "thinking…")

        # Reached live connection.
        event, data = conn.queue.get(timeout=0.5)
        assert event == "status"
        assert data["message"] == "thinking…"
        # But buffer stays empty.
        assert r.persistent_count == 0


# ── Connection lifecycle ───────────────────────────


class TestConnectionLifecycle:
    """Register + takeover + replay snapshot."""

    def test_register_returns_existing_buffer_snapshot(self):
        r = WebRenderer()
        # Emit before any client connects — buffer should hold them
        # for the eventual replay.
        r.final("first", turn=1)
        r.observation("ok", turn=1, tool_name="shell", success=True)

        conn = WebConnection(id="c1")
        snapshot = r.register_connection(conn)

        kinds = [event for event, _ in snapshot]
        # final → assistant_turn (with ``final`` payload), observation.
        assert kinds == ["assistant_turn", "observation"]

    def test_second_connection_takes_over_first(self):
        r = WebRenderer()
        a = WebConnection(id="a")
        r.register_connection(a)

        b = WebConnection(id="b")
        r.register_connection(b)

        # ``a`` gets a takeover notice and the closed flag is set.
        event, _ = a.queue.get(timeout=0.5)
        assert event == "takeover"
        assert a.closed.is_set()
        # Subsequent emits only reach ``b``.
        r.final("after takeover", turn=1)
        assert a.queue.empty()
        new_event, _ = b.queue.get(timeout=0.5)
        assert new_event == "assistant_turn"

    def test_unregister_pushes_close_sentinel_and_stops_receiving(self):
        r = WebRenderer()
        conn = WebConnection(id="c1")
        r.register_connection(conn)
        r.unregister_connection(conn)
        r.final("dropped", turn=1)
        # Unregister pushes a ``__close__`` sentinel so the SSE
        # generator's blocking queue.get wakes up promptly. After the
        # sentinel the queue stays empty — subsequent emits skip this
        # connection because it is no longer in the active list.
        first = conn.queue.get(timeout=0.5)
        assert first == ("__close__", {})
        assert conn.queue.empty()


# ── Prune (FIFO sync) ──────────────────────────────


class TestPrune:
    """``prune()`` drops oldest persistent events + emits a notice."""

    def test_prune_shrinks_buffer_and_notifies(self):
        r = WebRenderer()
        conn = WebConnection(id="c1")
        r.register_connection(conn)

        r.final("a", turn=1)
        r.observation("a-result", turn=1, tool_name="shell")
        r.final("b", turn=2)
        # Drain the queue first so we only see what prune emits next.
        for _ in range(3):
            conn.queue.get(timeout=0.5)

        r.prune(2)

        assert r.persistent_count == 1
        event, data = conn.queue.get(timeout=0.5)
        assert event == "prune"
        assert data["drop"] == 2

    def test_prune_zero_is_noop(self):
        r = WebRenderer()
        conn = WebConnection(id="c1")
        r.register_connection(conn)
        r.final("a", turn=1)
        conn.queue.get(timeout=0.5)

        r.prune(0)

        # No extra event landed — buffer untouched.
        assert r.persistent_count == 1
        assert conn.queue.empty()

    def test_prune_larger_than_buffer_clamps(self):
        r = WebRenderer()
        r.final("a", turn=1)
        r.prune(100)
        assert r.persistent_count == 0


# ── Assistant turn bundling ────────────────────────


class TestAssistantTurnBundling:
    """``thought()`` is held until ``action()`` / ``final()``."""

    def test_thought_plus_action_emit_one_event(self):
        r = WebRenderer()
        conn = WebConnection(id="c1")
        r.register_connection(conn)

        r.thought("I should read the file", turn=1)
        r.action("read_file", '{"path":"x.py"}', turn=1)

        event, data = conn.queue.get(timeout=0.5)
        assert event == "assistant_turn"
        assert data["thought"] == "I should read the file"
        assert data["action"]["tool_name"] == "read_file"
        assert data["turn"] == 1
        # Only one persistent event — thought did NOT fire on its own.
        assert r.persistent_count == 1

    def test_thought_plus_final_emit_one_event(self):
        r = WebRenderer()
        conn = WebConnection(id="c1")
        r.register_connection(conn)

        r.thought("confirmed", turn=2)
        r.final("the answer is 42", turn=2)

        event, data = conn.queue.get(timeout=0.5)
        assert event == "assistant_turn"
        assert data["thought"] == "confirmed"
        assert data["final"] == "the answer is 42"
        assert r.persistent_count == 1


# ── User message echo ──────────────────────────────


class TestUserMessageEcho:
    def test_push_user_message_appends_persistent_event(self):
        r = WebRenderer()
        conn = WebConnection(id="c1")
        r.register_connection(conn)

        r.push_user_message("hello world")

        event, data = conn.queue.get(timeout=0.5)
        assert event == "user_message"
        assert data["content"] == "hello world"
        assert r.persistent_count == 1


# ── Input flow ─────────────────────────────────────


class TestPromptUserInput:
    def test_prompt_user_blocks_until_input_pushed(self):
        r = WebRenderer()
        result: list[str] = []

        def worker():
            result.append(r.prompt_user("Q: ", multiline=False))

        t = threading.Thread(target=worker, daemon=True)
        t.start()

        # Give the worker a moment to enter the wait — then push input.
        # Polling loop avoids a race on slow CI without arbitrary sleeps.
        deadline = time.time() + 2.0
        while (
            t.is_alive() and not r._input_queue.qsize() == 0 and time.time() < deadline
        ):
            time.sleep(0.05)

        r.push_user_input("prompt", {"content": "hello"})
        t.join(timeout=2.0)
        assert not t.is_alive()
        assert result == ["hello"]

    def test_prompt_user_forwards_context_field_to_event(self):
        """``context`` kwarg (used by the ``ask`` tool to ship its
        question list alongside the input affordance) must land on the
        ``input_required`` event so the frontend can render it next to
        the ANSWERING badge."""
        r = WebRenderer()
        conn = WebConnection(id="c1")
        r.register_connection(conn)

        def worker():
            r.prompt_user(
                "Your answer: ",
                multiline=True,
                context="Agent asks:\n  1. What's your name?",
            )

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        time.sleep(0.05)
        try:
            event, data = conn.queue.get(timeout=1.0)
            assert event == "input_required"
            assert data["kind"] == "prompt"
            assert data["context"] == "Agent asks:\n  1. What's your name?"
        finally:
            r.push_user_input("prompt", {"content": ""})
            t.join(timeout=2.0)

    def test_prompt_user_returns_default_on_empty(self):
        r = WebRenderer()
        result: list[str] = []

        def worker():
            result.append(r.prompt_user("Q: ", default="fallback"))

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        time.sleep(0.05)
        r.push_user_input("prompt", {"content": ""})
        t.join(timeout=2.0)
        assert result == ["fallback"]

    def test_prompt_user_abort_raises_eof(self):
        r = WebRenderer()
        exc: list[BaseException] = []

        def worker():
            try:
                r.prompt_user("Q: ")
            except BaseException as e:
                exc.append(e)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        time.sleep(0.05)
        r.push_abort()
        t.join(timeout=2.0)
        assert exc and isinstance(exc[0], EOFError)


class TestConfirmInput:
    options = [
        ConfirmOption(key="y", label="yes", aliases=("yes",)),
        ConfirmOption(key="n", label="no", aliases=("no",)),
    ]

    def test_confirm_returns_pushed_value(self):
        r = WebRenderer()
        result: list[tuple[str, str]] = []

        def worker():
            result.append(r.confirm("?", self.options, default_key="n"))

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        time.sleep(0.05)
        r.push_user_input("confirm", {"key": "y", "comment": "go"})
        t.join(timeout=2.0)
        assert result == [("y", "go")]

    def test_confirm_abort_returns_default(self):
        r = WebRenderer()
        result: list[tuple[str, str]] = []

        def worker():
            result.append(r.confirm("?", self.options, default_key="n"))

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        time.sleep(0.05)
        r.push_abort()
        t.join(timeout=2.0)
        assert result == [("n", "")]


# ── Sanity: Renderer ABC conformance ───────────────


class TestAbcConformance:
    def test_can_instantiate_and_is_renderer(self):
        from agent_cli.render.base import Renderer

        r = WebRenderer()
        # If any @abstractmethod was left unimplemented, instantiation
        # would already have raised TypeError.
        assert isinstance(r, Renderer)
        # Smoke pass over a handful of abstract methods.
        r.header("ollama", "qwen3:32b", 10)
        r.turn_sep(1)
        r.status("info", "noted")


class TestHeaderWorkspace:
    """Workspace path rides on the ``ready`` event so the frontend's
    top bar can disambiguate which checkout an agent-cli session is
    bound to. Test pins the wire shape — both presence (when supplied
    at construction) and absence (when not) — so a frontend that reads
    ``d.workspace`` never sees a dangling field."""

    def test_workspace_included_when_provided(self):
        r = WebRenderer(workspace="/Users/me/proj")
        conn = WebConnection(id="c")
        r.register_connection(conn)
        r.header("ollama", "qwen3:32b", 10)
        event, data = conn.queue.get(timeout=1.0)
        assert event == "ready"
        assert data["workspace"] == "/Users/me/proj"
        # Existing fields must still be present.
        assert data["provider"] == "ollama"
        assert data["model"] == "qwen3:32b"

    def test_workspace_omitted_when_empty(self):
        """Empty workspace means we don't know — omit the field so the
        frontend's ``if (d.workspace)`` branch is the single source of
        truth for "show the path or not"."""
        r = WebRenderer()
        conn = WebConnection(id="c")
        r.register_connection(conn)
        r.header("ollama", "qwen3:32b", 10)
        event, data = conn.queue.get(timeout=1.0)
        assert event == "ready"
        assert "workspace" not in data

    def test_ready_replays_in_snapshot_for_late_clients(self):
        """A client that connects AFTER ``header()`` fired still gets
        the ready event via the snapshot prepend — fixes the
        "connecting…" stuck state when the browser opens before the
        first chat turn."""
        r = WebRenderer(workspace="/proj")
        # Header fires BEFORE any connection registers.
        r.header("ollama", "qwen3:32b", 10)
        # Late client connects.
        conn = WebConnection(id="late")
        snapshot = r.register_connection(conn)
        # The latest ready must be first in the snapshot so the
        # top-bar renders before any other replayed cards.
        assert snapshot, "snapshot must contain the ready event"
        event, data = snapshot[0]
        assert event == "ready"
        assert data["workspace"] == "/proj"
        assert data["model"] == "qwen3:32b"

    def test_nested_skill_header_does_not_clobber_session_info(self):
        """A skill's nested AgentLoop also calls ``header()`` with
        ``skill_name`` set. That MUST NOT replace the session-level
        ready — otherwise the top bar would flicker to a skill name
        mid-flow and stay there after the skill finishes."""
        r = WebRenderer(workspace="/proj")
        r.header("ollama", "qwen3:32b", 10)
        # Nested skill call.
        r.header("ollama", "qwen3:32b", 10, skill_name="plan")
        # Latest ready in snapshot should still be the top-level one,
        # with NO ``skill_name`` field set on the visible data.
        conn = WebConnection(id="c")
        snapshot = r.register_connection(conn)
        event, data = snapshot[0]
        assert event == "ready"
        assert data["skill_name"] == ""
        assert data["workspace"] == "/proj"

    def test_repeated_header_does_not_accumulate_in_buffer(self):
        """Chat REPL re-enters AgentLoop on each message, calling
        ``header()`` repeatedly. The slot replaces; the buffer must
        stay empty of ``ready`` so replay snapshots stay small."""
        r = WebRenderer()
        for _ in range(5):
            r.header("ollama", "qwen3:32b", 10)
        # Drain the live queue side and confirm buffer has no rolling
        # ``ready`` entries (only the slot, which is prepended to
        # snapshot from outside the buffer).
        assert all(ev != "ready" for (ev, _) in r._event_buffer)


class TestShutdownAllConnections:
    """``shutdown_all_connections`` is called on the graceful shutdown
    path (uvicorn lifespan hook + main.py finally). It must wake up
    every blocking SSE consumer by pushing the close sentinel so the
    generator's ``queue.get`` returns immediately rather than waiting
    out the 15s keep-alive timer."""

    def test_pushes_close_sentinel_to_every_active_connection(self):
        from agent_cli.render.web import _CLOSE_SENTINEL

        r = WebRenderer()
        a = WebConnection(id="a")
        # Two connections — registering ``b`` would take over ``a``
        # via the existing single-active-client model, so we register
        # one at a time and validate the active set was closed.
        r.register_connection(a)
        r.shutdown_all_connections()

        # Active connection got the sentinel.
        item = a.queue.get(timeout=0.5)
        assert item == _CLOSE_SENTINEL
        assert a.closed.is_set()
        # Subsequent emits do not reach a (it's been removed from
        # the connections list).
        r.final("after-shutdown", turn=1)
        assert a.queue.empty()

    def test_is_idempotent(self):
        r = WebRenderer()
        conn = WebConnection(id="c1")
        r.register_connection(conn)
        r.shutdown_all_connections()
        # Second call should not raise and should leave the connection
        # list empty.
        r.shutdown_all_connections()
        assert r._connections == []


class TestReplayFromHistory:
    """``replay_from_history`` is the engine behind ``web --resume``:
    it walks a resumed ContextManager's raw cache and re-emits the
    same persistent events the live loop would have produced, so a
    fresh SSE client sees the prior conversation in the snapshot."""

    def test_replays_user_and_assistant_complete(self, tmp_path):
        from agent_cli.context.manager import ContextManager

        session_dir = tmp_path / ".agent-cli" / "sessions" / "s1"
        ctx = ContextManager(session_dir, max_context_tokens=100_000)
        ctx.add({"role": "user", "content": "hi"})
        ctx.add(
            {
                "role": "assistant",
                "thought": "respond friendly",
                "action": "complete",
                "action_input": {"result": "hello"},
            }
        )

        r = WebRenderer(workspace=str(tmp_path))
        r.replay_from_history(ctx)

        # Persistent events landed in the buffer for snapshot replay.
        events = [(ev, data) for (ev, data) in r._event_buffer]
        names = [e for e, _ in events]
        assert "user_message" in names
        assert "assistant_turn" in names
        # The assistant turn carries the final result text.
        turn = next(d for e, d in events if e == "assistant_turn")
        assert turn["final"] == "hello"
        assert turn["thought"] == "respond friendly"

    def test_replays_tool_observation(self, tmp_path):
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(tmp_path / "s1", max_context_tokens=100_000)
        ctx.add(
            {
                "role": "user",
                "tool": "shell",
                "success": True,
                "content": "hello-from-shell",
            }
        )
        r = WebRenderer()
        r.replay_from_history(ctx)

        names = [e for e, _ in r._event_buffer]
        assert "observation" in names
        data = next(d for e, d in r._event_buffer if e == "observation")
        assert data["tool_name"] == "shell"
        assert data["content"] == "hello-from-shell"
        assert data["success"] is True

    def test_replay_strips_observation_prefix(self, tmp_path):
        """``_append_observation`` writes content prefixed with
        ``"Observation: "`` (LLM-facing form). The frontend's tool-result
        card already labels the entry, so replay must strip the prefix
        — otherwise the user sees ``Observation: Observation: ...``
        once the live observation card's own framing is added."""
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(tmp_path / "s1", max_context_tokens=100_000)
        ctx.add(
            {
                "role": "user",
                "tool": "write_file",
                "success": True,
                "content": "Observation: File saved: /tmp/x.txt (12 bytes)",
            }
        )
        r = WebRenderer()
        r.replay_from_history(ctx)

        data = next(d for e, d in r._event_buffer if e == "observation")
        assert data["content"] == "File saved: /tmp/x.txt (12 bytes)"

    def test_replay_preserves_failure_status(self, tmp_path):
        """A failed tool result stored with ``success=False`` must
        re-emit with the same ✗ shape — otherwise the user can't tell
        on resume which historical steps failed."""
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(tmp_path / "s1", max_context_tokens=100_000)
        ctx.add(
            {
                "role": "user",
                "tool": "edit_file",
                "success": False,
                "content": "Observation: ERROR: file not found",
            }
        )
        r = WebRenderer()
        r.replay_from_history(ctx)

        data = next(d for e, d in r._event_buffer if e == "observation")
        assert data["success"] is False
        assert data["tool_name"] == "edit_file"
        assert data["content"] == "ERROR: file not found"

    def test_replay_routes_empty_tool_through_observation(self, tmp_path):
        """Format-retry interventions are stored with ``tool=""`` (no
        specific tool fired). The ``tool`` *key* presence — not its
        truthiness — must drive the routing, so the entry still
        renders as an observation card (✗ visible) instead of being
        misclassified as a user chat turn."""
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(tmp_path / "s1", max_context_tokens=100_000)
        ctx.add(
            {
                "role": "user",
                "tool": "",
                "success": False,
                "content": "Observation: thought field is required.",
            }
        )
        r = WebRenderer()
        r.replay_from_history(ctx)

        names = [e for e, _ in r._event_buffer]
        assert names == ["observation"]
        data = r._event_buffer[0][1]
        assert data["tool_name"] == ""
        assert data["success"] is False

    def test_replay_routes_plain_user_message(self, tmp_path):
        """A user chat turn (no ``tool`` key at all) must route through
        ``push_user_message`` so it renders as the right-aligned blue
        bubble, not a tool-result card. This is the bug the schema
        change closes — observations used to be indistinguishable from
        chat turns on disk."""
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(tmp_path / "s1", max_context_tokens=100_000)
        ctx.add({"role": "user", "content": "this is a real chat turn"})

        r = WebRenderer()
        r.replay_from_history(ctx)

        names = [e for e, _ in r._event_buffer]
        assert names == ["user_message"]
        assert r._event_buffer[0][1]["content"] == "this is a real chat turn"

    def test_replays_assistant_action_call(self, tmp_path):
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(tmp_path / "s1", max_context_tokens=100_000)
        ctx.add(
            {
                "role": "assistant",
                "thought": "I should read the file",
                "action": "read_file",
                "action_input": {"path": "x.py"},
            }
        )
        r = WebRenderer()
        r.replay_from_history(ctx)

        names = [e for e, _ in r._event_buffer]
        assert names == ["assistant_turn"]
        data = r._event_buffer[0][1]
        assert data["thought"] == "I should read the file"
        assert data["action"]["tool_name"] == "read_file"
        # action_input is wire-format JSON so the frontend can render
        # the same way the live path emits.
        import json as _json

        parsed = _json.loads(data["action"]["tool_input"])
        assert parsed == {"path": "x.py"}

    def test_skips_empty_user_message(self, tmp_path):
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(tmp_path / "s1", max_context_tokens=100_000)
        ctx.add({"role": "user", "content": ""})
        r = WebRenderer()
        r.replay_from_history(ctx)
        assert r._event_buffer == []

    def test_snapshot_includes_replayed_events_for_first_connection(self, tmp_path):
        """After resume + replay, the FIRST SSE client connecting must
        receive the prior events via the snapshot — that's how the
        ``--resume`` flow renders past conversation in a fresh
        browser tab."""
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(tmp_path / "s1", max_context_tokens=100_000)
        ctx.add({"role": "user", "content": "earlier question"})
        ctx.add(
            {
                "role": "assistant",
                "thought": "",
                "action": "complete",
                "action_input": {"result": "earlier answer"},
            }
        )

        r = WebRenderer(workspace=str(tmp_path))
        r.header("ollama", "qwen3:32b", 10)
        r.replay_from_history(ctx)

        conn = WebConnection(id="late")
        snapshot = r.register_connection(conn)
        names = [e for e, _ in snapshot]
        # ready first (header replay), then the replayed events.
        assert names[0] == "ready"
        assert "user_message" in names
        assert "assistant_turn" in names
