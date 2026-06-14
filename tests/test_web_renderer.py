"""Unit tests for :class:`agent_cli.render.web.WebRenderer`.

Coverage axes:

1. **Event distribution** — persistent events go to the buffer + every
   active connection; transient events only to live connections.
2. **Connection lifecycle** — every connection is equal (no takeover);
   the snapshot returned to a new client matches the buffer.
3. **Assistant turn bundling** — ``thought()`` is held and emitted as
   part of the next ``action()`` / ``final()`` so each LLM emission
   produces exactly one persistent ``assistant_turn`` event.
4. **Input flow** — ``prompt_user`` / ``confirm`` block until the
   server pushes input. Abort raises ``EOFError`` from ``prompt_user``
   and returns the safe default from ``confirm``.
"""

from __future__ import annotations

import threading
import time

from agent_cli.render.base import ConfirmOption
from agent_cli.render.web import WebConnection, WebRenderer


def _qget(conn, timeout=0.5):
    """Next queued (event, data) skipping the cross-cutting ``viewers``
    count broadcast (put on existing connections' queues when another
    joins/leaves)."""
    while True:
        event, data = conn.queue.get(timeout=timeout)
        if event != "viewers":
            return event, data


# ── Event distribution ─────────────────────────────


class TestCanPrompt:
    """``can_prompt`` reports whether the dangerous-shell guard can
    actually prompt — for web that means a client is connected to answer
    the ``input_required`` event (no TTY needed)."""

    def test_false_without_connection(self):
        r = WebRenderer()
        assert r.can_prompt() is False

    def test_true_with_open_connection(self):
        r = WebRenderer()
        r.register_connection(WebConnection(id="c1"))
        assert r.can_prompt() is True

    def test_false_when_connection_closed(self):
        r = WebRenderer()
        conn = WebConnection(id="c1")
        r.register_connection(conn)
        conn.closed.set()
        assert r.can_prompt() is False


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


class TestRecovery:
    """recovery() finalizes the rejected emission as its own card, then
    shows the intervention — so the failed response, the intervention, and
    the retry are three distinct cards (not one growing stream blob)."""

    def test_recovery_emits_failed_turn_then_observation(self):
        r = WebRenderer()
        conn = WebConnection(id="c1")
        r.register_connection(conn)

        r.recovery("{bad", "Observation: add an action", "no action", turn=2)

        # 1. failed_turn closes the streaming card (carries raw + reason
        #    for replay where no live stream card exists).
        event, data = conn.queue.get(timeout=0.5)
        assert event == "failed_turn"
        assert data["reason"] == "no action"
        assert data["raw"] == "{bad"
        assert data["turn"] == 2
        # 2. the intervention fed back, as its own observation card.
        event, data = conn.queue.get(timeout=0.5)
        assert event == "observation"
        assert data["content"] == "Observation: add an action"
        assert data["success"] is False
        # Both persistent so a reconnecting client replays them.
        assert r.persistent_count == 2


# ── Connection lifecycle ───────────────────────────


class TestConnectionLifecycle:
    """Register (identity event) + multi-viewer fan-out + replay snapshot."""

    def test_register_returns_role_then_buffer_snapshot(self):
        r = WebRenderer()
        # Emit before any client connects — buffer should hold them
        # for the eventual replay.
        r.final("first", turn=1)
        r.observation("ok", turn=1, tool_name="shell", success=True)

        conn = WebConnection(id="c1")
        snapshot = r.register_connection(conn)

        # First entry is this connection's identity (it learns conn_id before
        # anything else); the rest is the usual buffer replay.
        assert snapshot[0] == ("identity", {"conn_id": "c1"})
        kinds = [event for event, _ in snapshot if event not in ("identity", "viewers")]
        assert kinds == ["assistant_turn", "observation"]

    def test_all_connections_receive_the_fanout(self):
        r = WebRenderer()
        a = WebConnection(id="a")
        snap_a = r.register_connection(a)
        assert snap_a[0] == ("identity", {"conn_id": "a"})

        b = WebConnection(id="b")
        snap_b = r.register_connection(b)
        assert snap_b[0] == ("identity", {"conn_id": "b"})
        assert not a.closed.is_set()
        # A subsequent emit fans out to BOTH (every connection is equal).
        r.final("broadcast", turn=1)
        ea, _ = _qget(a)  # a's queue has a viewers event from b's join
        eb, _ = b.queue.get(timeout=0.5)
        assert ea == "assistant_turn" and eb == "assistant_turn"

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


class TestTokenUsage:
    """Per-turn token usage: live emit + latest-cached snapshot replay."""

    _STATS = {"in": 5000, "out": 320, "context_window": 262144, "total_out": 320}

    def test_emits_token_usage_event(self):
        r = WebRenderer()
        conn = WebConnection(id="c1")
        r.register_connection(conn)
        r.token_usage(self._STATS, turn=2)
        event, data = conn.queue.get(timeout=0.5)
        assert event == "token_usage"
        assert data["in"] == 5000
        assert data["context_window"] == 262144
        assert data["turn"] == 2

    def test_token_usage_is_transient_not_buffered(self):
        """Each turn's usage replaces the last — it must not pile up in
        the persistent buffer (only the latest is cached separately)."""
        r = WebRenderer()
        conn = WebConnection(id="c1")
        r.register_connection(conn)
        r.token_usage(self._STATS, turn=1)
        assert r.persistent_count == 0

    def test_latest_token_usage_replayed_on_reconnect(self):
        """A client connecting after a turn sees the latest usage in its
        snapshot, so the top-bar readout isn't blank until the next turn."""
        r = WebRenderer()
        first = WebConnection(id="c1")
        r.register_connection(first)
        r.token_usage(self._STATS, turn=1)
        # New connection — snapshot should carry the usage.
        second = WebConnection(id="c2")
        snapshot = r.register_connection(second)
        assert any(ev == "token_usage" and d.get("in") == 5000 for ev, d in snapshot)


# ── Prune (FIFO sync) ──────────────────────────────


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

    def test_prompt_user_forwards_provenance_fields(self):
        """ask over web carries the delegate agent + reasoning so the user
        can attribute it. Set on the worker thread (mirrors production:
        the delegate worker registers itself and reasons on its own
        thread before prompting)."""
        r = WebRenderer()
        conn = WebConnection(id="c1")
        r.register_connection(conn)

        def worker():
            r.set_thread_agent("explorer")
            r.note_thought("need the user to pick a path")
            r.prompt_user("Your answer: ", multiline=True)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        time.sleep(0.05)
        try:
            event, data = conn.queue.get(timeout=1.0)
            assert event == "input_required"
            assert data["agent"] == "explorer"
            assert "pick a path" in data["reasoning"]
        finally:
            r.push_user_input("prompt", {"content": ""})
            t.join(timeout=2.0)

    def test_confirm_forwards_provenance_fields(self):
        """confirm over web carries agent + reasoning + the action it wants
        to run."""
        from agent_cli.render.base import ConfirmOption

        r = WebRenderer()
        conn = WebConnection(id="c1")
        r.register_connection(conn)

        def worker():
            r.set_thread_agent("explorer")
            r.note_thought("the stale build must go")
            r.note_action("shell", "rm -rf build")
            r.confirm("Allow?", [ConfirmOption(key="y", label="yes")], default_key="n")

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        time.sleep(0.05)
        try:
            event, data = conn.queue.get(timeout=1.0)
            assert event == "input_required"
            assert data["kind"] == "confirm"
            assert data["agent"] == "explorer"
            assert "stale build" in data["reasoning"]
            assert "rm -rf build" in data["action"]
        finally:
            r.push_user_input("confirm", {"key": "n", "comment": ""})
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
        r.header("openai", "gpt-4o", 10)
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
        r.header("openai", "gpt-4o", 10)
        event, data = conn.queue.get(timeout=1.0)
        assert event == "ready"
        assert data["workspace"] == "/Users/me/proj"
        # Existing fields must still be present.
        assert data["provider"] == "openai"
        assert data["model"] == "gpt-4o"

    def test_workspace_omitted_when_empty(self):
        """Empty workspace means we don't know — omit the field so the
        frontend's ``if (d.workspace)`` branch is the single source of
        truth for "show the path or not"."""
        r = WebRenderer()
        conn = WebConnection(id="c")
        r.register_connection(conn)
        r.header("openai", "gpt-4o", 10)
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
        r.header("openai", "gpt-4o", 10)
        # Late client connects.
        conn = WebConnection(id="late")
        snapshot = r.register_connection(conn)
        # ``role`` is index 0 (connection identity); the latest ready must be
        # next so the top-bar renders before any other replayed cards.
        assert snapshot[0][0] == "identity"
        event, data = snapshot[1]
        assert event == "ready"
        assert data["workspace"] == "/proj"
        assert data["model"] == "gpt-4o"

    def test_nested_skill_header_does_not_clobber_session_info(self):
        """A skill's nested AgentLoop also calls ``header()`` with
        ``skill_name`` set. That MUST NOT replace the session-level
        ready — otherwise the top bar would flicker to a skill name
        mid-flow and stay there after the skill finishes."""
        r = WebRenderer(workspace="/proj")
        r.header("openai", "gpt-4o", 10)
        # Nested skill call.
        r.header("openai", "gpt-4o", 10, skill_name="plan")
        # Latest ready in snapshot should still be the top-level one,
        # with NO ``skill_name`` field set on the visible data.
        conn = WebConnection(id="c")
        snapshot = r.register_connection(conn)
        assert snapshot[0][0] == "identity"  # connection identity first
        event, data = snapshot[1]
        assert event == "ready"
        assert data["skill_name"] == ""
        assert data["workspace"] == "/proj"

    def test_repeated_header_does_not_accumulate_in_buffer(self):
        """Chat REPL re-enters AgentLoop on each message, calling
        ``header()`` repeatedly. The slot replaces; the buffer must
        stay empty of ``ready`` so replay snapshots stay small."""
        r = WebRenderer()
        for _ in range(5):
            r.header("openai", "gpt-4o", 10)
        # Drain the live queue side and confirm buffer has no rolling
        # ``ready`` entries (only the slot, which is prepended to
        # snapshot from outside the buffer).
        assert all(ev != "ready" for (ev, _) in r._event_buffer)


class TestDelegateTaskVisibility:
    """Parallel delegate worker threads register themselves via
    ``begin_delegate_task`` so subsequent emits from the worker are
    auto-tagged with ``task_id`` and routed into the right collapsible
    group on the frontend. Tests pin: lifecycle markers, auto-attach,
    status routing, and CLI-renderer compatibility (no-op on base).
    """

    def test_begin_delegate_task_emits_persistent_start_event(self):
        r = WebRenderer()
        conn = WebConnection(id="c")
        r.register_connection(conn)
        r.begin_delegate_task(
            task_id="t-1", index=0, agent="explorer", task_text="find X"
        )
        event, data = conn.queue.get(timeout=1.0)
        assert event == "delegate_task_start"
        assert data == {
            "task_id": "t-1",
            "index": 0,
            "agent": "explorer",
            "task_text": "find X",
        }
        # Persistent so a late-joining client replays the open card.
        assert any(ev == "delegate_task_start" for (ev, _) in r._event_buffer)

    def test_end_delegate_task_emits_persistent_end_event(self):
        r = WebRenderer()
        conn = WebConnection(id="c")
        r.register_connection(conn)
        r.begin_delegate_task(task_id="t-1", index=0, agent="", task_text="t")
        # Drain start.
        conn.queue.get(timeout=1.0)
        r.end_delegate_task(task_id="t-1", success=True, duration_s=4.2)
        event, data = conn.queue.get(timeout=1.0)
        assert event == "delegate_task_end"
        assert data["task_id"] == "t-1"
        assert data["success"] is True
        assert data["duration_s"] == 4.2
        # ``error`` field omitted when empty — matches the schema the
        # frontend's conditional render expects.
        assert "error" not in data

    def test_end_delegate_task_carries_error_when_failed(self):
        r = WebRenderer()
        conn = WebConnection(id="c")
        r.register_connection(conn)
        r.begin_delegate_task(task_id="t-1", index=0, agent="", task_text="t")
        conn.queue.get(timeout=1.0)
        r.end_delegate_task(
            task_id="t-1", success=False, duration_s=1.0, error="timed out"
        )
        _, data = conn.queue.get(timeout=1.0)
        assert data["success"] is False
        assert data["error"] == "timed out"

    def test_emit_auto_attaches_task_id_from_worker_thread(self):
        """Inside a ``begin_delegate_task`` → ``end_delegate_task``
        window the current thread's emits MUST carry ``task_id`` —
        the whole point of routing parallel work into separate
        cards. Outside the window the field MUST be absent."""
        r = WebRenderer()
        conn = WebConnection(id="c")
        r.register_connection(conn)

        # Before begin: no task_id auto-attach.
        r._emit("assistant_turn", {"turn": 1, "thought": "no task"}, persistent=True)
        _, baseline = conn.queue.get(timeout=1.0)
        assert "task_id" not in baseline

        r.begin_delegate_task(task_id="t-7", index=0, agent="", task_text="")
        conn.queue.get(timeout=1.0)  # drain start

        # Within the window: every emit picks up task_id.
        r._emit("observation", {"turn": 1, "content": "obs"}, persistent=True)
        _, mid = conn.queue.get(timeout=1.0)
        assert mid["task_id"] == "t-7"

        r.end_delegate_task(task_id="t-7", success=True, duration_s=0.1)
        conn.queue.get(timeout=1.0)  # drain end

        # After end: back to no task_id.
        r._emit("assistant_turn", {"turn": 2, "final": "done"}, persistent=True)
        _, after = conn.queue.get(timeout=1.0)
        assert "task_id" not in after

    def test_emit_does_not_overwrite_explicit_task_id(self):
        """If a caller passes ``task_id`` in ``data`` explicitly (e.g.
        the ``delegate_task_*`` lifecycle events do this), ``_emit``
        must NOT clobber it with the per-thread map. Without this
        guard the lifecycle markers' ``task_id`` could be replaced
        with a stale one if the calling thread is itself inside a
        nested delegate."""
        r = WebRenderer()
        conn = WebConnection(id="c")
        r.register_connection(conn)

        r.begin_delegate_task(task_id="outer", index=0, agent="", task_text="")
        conn.queue.get(timeout=1.0)  # drain start
        # Now inside outer's thread, emit with an explicit (different)
        # task_id — must survive intact.
        r._emit(
            "delegate_task_start",
            {"task_id": "inner", "index": 1, "agent": "", "task_text": ""},
            persistent=True,
        )
        _, data = conn.queue.get(timeout=1.0)
        assert data["task_id"] == "inner"

    def test_set_thread_status_emits_status_event_when_in_task(self):
        r = WebRenderer()
        conn = WebConnection(id="c")
        r.register_connection(conn)
        r.begin_delegate_task(task_id="t-1", index=0, agent="", task_text="")
        conn.queue.get(timeout=1.0)  # drain start

        r.set_thread_status("reading file...")
        event, data = conn.queue.get(timeout=1.0)
        assert event == "delegate_task_status"
        assert data == {"task_id": "t-1", "status": "reading file..."}

    def test_set_thread_status_silent_outside_delegate(self):
        """No task → no SSE traffic on status updates. The base dict
        write still happens (for rich.Live polling on the CLI side)
        but emitting a frontend event with no card to route to would
        leak data the UI has nowhere to show."""
        r = WebRenderer()
        conn = WebConnection(id="c")
        r.register_connection(conn)
        r.set_thread_status("orphan status")
        assert conn.queue.empty()

    def test_set_thread_status_preserves_base_dict(self):
        """Override must call ``super()`` so the ``_thread_status``
        dict is still populated — CLI's parallel-delegate Live panel
        polls ``get_thread_status`` from main thread and would
        otherwise see empty status. Even on the web renderer this
        invariant has to hold for the CLI-rendered subagent logs."""
        r = WebRenderer()
        r.start_capture()  # base requires capture mode for status write
        try:
            r.set_thread_status("from worker")
        finally:
            r.stop_capture()
        # Status was written into base dict (and stop_capture popped it).
        # Re-create the window and verify the path round-trips.
        r.start_capture()
        r.set_thread_status("again")
        tid = threading.get_ident()
        # ``get_thread_status`` reads from the dict; same thread reads
        # the value it just wrote.
        assert r.get_thread_status(tid) == "again"
        r.stop_capture()


class TestRendererBaseDelegateTaskNoOp:
    """``MinimalRenderer`` (and any future CLI-only renderer) must
    inherit the base no-op lifecycle methods so ``delegate.py`` can
    call them unconditionally without branching on renderer type."""

    def test_minimal_renderer_begin_end_are_no_ops(self):
        from agent_cli.render.minimal import MinimalRenderer
        from io import StringIO
        from rich.console import Console

        r = MinimalRenderer(Console(file=StringIO(), force_terminal=False))
        # Should not raise — no-op implementations on base.
        r.begin_delegate_task(task_id="t", index=0, agent="a", task_text="t")
        r.end_delegate_task(task_id="t", success=True, duration_s=1.0)
        r.end_delegate_task(task_id="t", success=False, duration_s=2.0, error="x")


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
        r.header("openai", "gpt-4o", 10)
        r.replay_from_history(ctx)

        conn = WebConnection(id="late")
        snapshot = r.register_connection(conn)
        names = [e for e, _ in snapshot]
        # role first (connection identity), ready next (header replay), then
        # the replayed conversation events.
        assert names[0] == "identity"
        assert names[1] == "ready"
        assert "user_message" in names
        assert "assistant_turn" in names


class TestWorkerStateEmit:
    """``worker_busy`` / ``worker_idle`` are the chat worker's
    transitions between accepting messages and running them. The
    frontend uses them to gate the chat ``Send`` button so a second
    message can't queue into an in-flight turn.

    Live behaviour: both methods emit a ``worker_state`` event with
    ``{busy: True/False}`` payload to every active connection.
    Both also update ``_latest_worker_state`` so reconnecting clients
    see the right thing (covered by ``TestWorkerStateReconnect``).
    """

    def test_worker_busy_emits_busy_true(self):
        r = WebRenderer()
        conn = WebConnection(id="c")
        r.register_connection(conn)
        r.worker_busy()
        event, data = conn.queue.get(timeout=1.0)
        assert event == "worker_state"
        assert data == {"busy": True}

    def test_worker_idle_emits_busy_false(self):
        r = WebRenderer()
        conn = WebConnection(id="c")
        r.register_connection(conn)
        r.worker_idle()
        event, data = conn.queue.get(timeout=1.0)
        assert event == "worker_state"
        assert data == {"busy": False}

    def test_worker_state_event_is_transient_not_buffered(self):
        # Per-turn transitions would balloon the persistent buffer if
        # buffered — we keep only the latest in a slot. Confirm the
        # event itself never lands in ``_event_buffer``.
        r = WebRenderer()
        for _ in range(3):
            r.worker_busy()
            r.worker_idle()
        buffered = [ev for (ev, _) in r._event_buffer]
        assert "worker_state" not in buffered

    def test_latest_worker_state_replaces_not_accumulates(self):
        # Every transition overwrites the slot — there should only
        # ever be one ``_latest_worker_state`` value, reflecting the
        # most recent call.
        r = WebRenderer()
        r.worker_busy()
        r.worker_idle()
        r.worker_busy()
        assert r._latest_worker_state == ("worker_state", {"busy": True})
        r.worker_idle()
        assert r._latest_worker_state == ("worker_state", {"busy": False})


class TestWorkerStateReconnect:
    """The user's explicit requirement: send-button gating must
    survive a page refresh or a fresh SSE connection mid-turn. The
    server holds the latest ``worker_state`` in a slot (parallel to
    ``_latest_ready``) and prepends it to the snapshot every new
    connection receives.
    """

    def test_late_client_sees_busy_state_in_snapshot(self):
        # Simulate: worker pops a message and starts processing.
        # No client is connected at that moment. Then the user
        # refreshes / a fresh client connects. The snapshot the new
        # client receives MUST include worker_state busy=True so the
        # send button immediately disables — even though no new
        # event will fire until the turn finishes.
        r = WebRenderer()
        r.worker_busy()
        conn = WebConnection(id="reconnect")
        snapshot = r.register_connection(conn)
        names = [e for e, _ in snapshot]
        assert "worker_state" in names
        data = next(d for e, d in snapshot if e == "worker_state")
        assert data == {"busy": True}

    def test_late_client_sees_idle_state_in_snapshot(self):
        # Symmetric: worker has finished a turn and is waiting in
        # dequeue. New client must see worker_state busy=False so
        # the send button enables on connect.
        r = WebRenderer()
        r.worker_busy()
        r.worker_idle()
        conn = WebConnection(id="fresh")
        snapshot = r.register_connection(conn)
        data = next(d for e, d in snapshot if e == "worker_state")
        assert data == {"busy": False}

    def test_no_worker_state_in_snapshot_before_any_transition(self):
        # On the very first SSE connection of a brand-new session
        # the worker hasn't emitted anything yet. The snapshot
        # should NOT include a synthetic worker_state event —
        # frontend defaults to ``workerBusy = false`` and that
        # matches the actual server state.
        r = WebRenderer()
        conn = WebConnection(id="first")
        snapshot = r.register_connection(conn)
        names = [e for e, _ in snapshot]
        assert "worker_state" not in names

    def test_snapshot_reflects_latest_transition_across_many(self):
        # Pop / process / pop loop runs many times. Each refresh
        # should reflect ONLY the most recent state — not a trail
        # of every transition the worker ever did.
        r = WebRenderer()
        for _ in range(5):
            r.worker_busy()
            r.worker_idle()
        r.worker_busy()  # ends busy

        conn = WebConnection(id="late")
        snapshot = r.register_connection(conn)
        worker_states = [d for e, d in snapshot if e == "worker_state"]
        # Exactly one — the slot, not the history.
        assert len(worker_states) == 1
        assert worker_states[0] == {"busy": True}

    def test_busy_state_replays_for_a_second_viewer(self):
        # Multi-viewer model: a second client joins (all equal, no takeover).
        # It must still see the current busy state in its replay —
        # joining is not a state reset, and the first client stays connected.
        r = WebRenderer()
        old = WebConnection(id="old")
        r.register_connection(old)
        r.worker_busy()
        ev, _ = old.queue.get(timeout=1.0)
        assert ev == "worker_state"

        # Second client joins (observer).
        new = WebConnection(id="new")
        snapshot = r.register_connection(new)

        # The first client is NOT closed (no takeover).
        assert not old.closed.is_set()
        # The new viewer sees the busy state in its replay.
        names = [e for e, _ in snapshot]
        assert "worker_state" in names
        data = next(d for e, d in snapshot if e == "worker_state")
        assert data == {"busy": True}

    def test_busy_state_persists_after_unregister_and_reconnect(self):
        # The classic "user closed the tab and reopened it" path.
        # ``unregister_connection`` drops the active SSE but does
        # NOT clear server-side state. The next ``register`` must
        # still hand back the current worker_state in snapshot.
        r = WebRenderer()
        c1 = WebConnection(id="c1")
        r.register_connection(c1)
        r.worker_busy()
        r.unregister_connection(c1)

        c2 = WebConnection(id="c2")
        snapshot = r.register_connection(c2)
        data = next(d for e, d in snapshot if e == "worker_state")
        assert data == {"busy": True}

    def test_worker_state_ordering_in_snapshot(self):
        # ``ready`` must come first so the top bar renders before
        # any other affordance settles, and ``worker_state`` lands
        # at the end so the send-button gating is applied AFTER
        # all replayed messages are on screen — matches the
        # implementation's snapshot composition.
        r = WebRenderer(workspace="/proj")
        r.header("openai", "gpt-4o", 10)
        r.thought("thinking", 1)
        r.final("done", 1)  # closes assistant_turn for turn 1
        r.worker_idle()

        conn = WebConnection(id="late")
        snapshot = r.register_connection(conn)
        # the viewer-count event is positionally irrelevant — filter it so the
        # ready-first / worker_state-trails invariant stays the assertion
        names = [e for e, _ in snapshot if e != "viewers"]
        # role leads (connection identity), then ready.
        assert names[0] == "identity"
        assert names[1] == "ready"
        # worker_state trails.
        assert names[-1] == "worker_state"


class TestWorkerLoopIntegration:
    """End-to-end: main.py's chat worker thread must emit
    ``worker_idle`` immediately before every ``dequeue_blocking``
    call and ``worker_busy`` immediately after popping. The SHUTDOWN
    sentinel must NOT trigger a busy flip — that would race the
    connection teardown.

    These tests exercise the renderer + server together (no main.py
    import) by mimicking the worker_loop's pop/process pattern with
    a minimal helper.
    """

    def _worker_pop_process(self, server, renderer, message):
        """One iteration of main.py's _worker_loop, abstracted to
        avoid importing typer / setting up the whole CLI. The wiring
        we're testing is the renderer+server contract; the loop
        body itself is uninteresting for this test."""
        renderer.worker_idle()
        item = server.dequeue_blocking()
        if item is server.SHUTDOWN:
            return False
        assert item["text"] == message
        renderer.worker_busy()
        return True

    def test_full_cycle_emits_idle_then_busy(self):
        from agent_cli.web.server import WebServer

        r = WebRenderer()
        s = WebServer(r, token="t")
        conn = WebConnection(id="c")
        r.register_connection(conn)

        # Caller enqueues a message before the worker pops, then the
        # worker emits idle → dequeue → busy. The frontend sees
        # idle (transient) and busy (transient) in order.
        s.enqueue("c", "hello")
        ran = self._worker_pop_process(s, r, "hello")
        assert ran is True

        events = []
        while True:
            try:
                events.append(conn.queue.get(timeout=0.2))
            except Exception:
                break
        names = [ev for ev, _ in events]
        assert "worker_state" in names
        states = [d["busy"] for ev, d in events if ev == "worker_state"]
        # idle (False) then busy (True), in that order.
        assert states == [False, True]

    def test_shutdown_does_not_flip_to_busy(self):
        from agent_cli.web.server import WebServer

        r = WebRenderer()
        s = WebServer(r, token="t")
        conn = WebConnection(id="c")
        r.register_connection(conn)

        # SHUTDOWN must skip the busy flip — busy after shutdown is
        # nonsensical and the connections are tearing down anyway.
        r.worker_idle()
        s.shutdown()
        item = s.dequeue_blocking()
        assert item is s.SHUTDOWN
        # Latest state should still be idle.
        assert r._latest_worker_state == ("worker_state", {"busy": False})


# ── Prompt Inspector per-agent scopes ──────────────


def _note_in_delegate_scope(r, *, task_id, index, agent, sections, turn):
    """Capture a system-prompt snapshot AS a delegate worker would: run
    ``begin_delegate_task`` + ``note_system_prompt`` on a fresh thread so the
    renderer's thread→task routing resolves the scope to ``task_id`` (exactly
    the path a parallel-delegate worker takes)."""

    def worker():
        r.begin_delegate_task(task_id=task_id, index=index, agent=agent, task_text="t")
        r.note_system_prompt(sections, turn=turn)

    th = threading.Thread(target=worker)
    th.start()
    th.join(timeout=2.0)


class TestPromptInspectorScopes:
    """``note_system_prompt`` is scoped by the calling thread: the main loop
    lands under ``_MAIN_SCOPE``, each delegate worker under its ``task_id``
    (resolved from ``_thread_to_task``). So the inspector can show each
    agent's prompt separately, and sub-agent prompts survive the agent
    finishing (post-mortem inspection)."""

    def test_main_thread_snapshot_is_main_scope(self):
        r = WebRenderer()
        r.note_system_prompt([("Role", "main role")], turn=3)
        snap = r.prompt_snapshot()  # default = main scope
        assert snap is not None
        assert snap["turn"] == 3
        assert snap["sections"][0]["text"] == "main role"

    def test_delegate_thread_snapshot_keyed_by_task_id(self):
        r = WebRenderer()
        _note_in_delegate_scope(
            r,
            task_id="task-A",
            index=0,
            agent="explorer",
            sections=[("Role", "explorer role")],
            turn=1,
        )
        # Agent scope holds the agent's prompt...
        agent_snap = r.prompt_snapshot("task-A")
        assert agent_snap is not None
        assert agent_snap["sections"][0]["text"] == "explorer role"
        # ...and the main scope is untouched (no main LLM call happened).
        assert r.prompt_snapshot() is None

    def test_scopes_are_isolated_main_vs_agents(self):
        r = WebRenderer()
        r.note_system_prompt([("Role", "main")], turn=5)
        _note_in_delegate_scope(
            r,
            task_id="task-A",
            index=0,
            agent="explorer",
            sections=[("Role", "A")],
            turn=1,
        )
        _note_in_delegate_scope(
            r,
            task_id="task-B",
            index=1,
            agent="coder",
            sections=[("Role", "B")],
            turn=1,
        )
        assert r.prompt_snapshot()["sections"][0]["text"] == "main"
        assert r.prompt_snapshot("task-A")["sections"][0]["text"] == "A"
        assert r.prompt_snapshot("task-B")["sections"][0]["text"] == "B"

    def test_scopes_lists_main_first_then_agents_with_labels(self):
        r = WebRenderer()
        _note_in_delegate_scope(
            r,
            task_id="task-A",
            index=0,
            agent="explorer",
            sections=[("Role", "A")],
            turn=2,
        )
        r.note_system_prompt([("Role", "main")], turn=9)
        _note_in_delegate_scope(
            r,
            task_id="task-B",
            index=1,
            agent="coder",
            sections=[("Role", "B")],
            turn=4,
        )
        scopes = r.prompt_scopes()
        # Main pinned first regardless of capture order.
        assert scopes[0]["id"] == ""
        assert scopes[0]["label"] == "Main"
        assert scopes[0]["main"] is True
        rest = {s["id"]: s for s in scopes[1:]}
        assert rest["task-A"]["label"] == "explorer·1"  # index+1, 1-based
        assert rest["task-B"]["label"] == "coder·2"
        assert rest["task-A"]["turn"] == 2
        assert all(s["main"] is False for s in scopes[1:])

    def test_scopes_excludes_agents_without_a_captured_prompt(self):
        # delegate_task_start registers a label, but no LLM call yet → no chip.
        r = WebRenderer()

        def worker():
            r.begin_delegate_task(
                task_id="task-A", index=0, agent="explorer", task_text="t"
            )

        th = threading.Thread(target=worker)
        th.start()
        th.join(timeout=2.0)
        assert r.prompt_scopes() == []

    def test_delete_drops_agent_scope(self):
        r = WebRenderer()
        _note_in_delegate_scope(
            r,
            task_id="task-A",
            index=0,
            agent="explorer",
            sections=[("Role", "A")],
            turn=1,
        )
        assert r.delete_prompt_scope("task-A") is True
        assert r.prompt_snapshot("task-A") is None
        assert r.prompt_scopes() == []
        # Idempotent: deleting again is a no-op False.
        assert r.delete_prompt_scope("task-A") is False

    def test_main_scope_is_not_deletable(self):
        r = WebRenderer()
        r.note_system_prompt([("Role", "main")], turn=1)
        assert r.delete_prompt_scope("") is False
        assert r.prompt_snapshot() is not None

    def test_agent_snapshot_survives_task_end(self):
        r = WebRenderer()

        def worker():
            r.begin_delegate_task(
                task_id="task-A", index=0, agent="explorer", task_text="t"
            )
            r.note_system_prompt([("Role", "A")], turn=1)
            r.end_delegate_task(task_id="task-A", success=True, duration_s=0.1)

        th = threading.Thread(target=worker)
        th.start()
        th.join(timeout=2.0)
        # The agent finished, but its prompt stays inspectable post-mortem.
        assert r.prompt_snapshot("task-A") is not None
        labels = {s["id"]: s["label"] for s in r.prompt_scopes()}
        assert labels.get("task-A") == "explorer·1"
