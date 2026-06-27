"""WebRenderer — emits Renderer events as Server-Sent Events to every
connected web client, and waits on POST /api/input for user input.

Architecture::

    AgentLoop (worker thread)              FastAPI / uvicorn (main thread, async)
    ──────────────────────────              ───────────────────────────────────
    renderer.thought(...)
      → _emit("assistant_turn", ...) ─→ event_buffer (persistent)
                                        ↓
                                      conn.queue (per active SSE)
                                        ↓
                                       SSE endpoint pulls and yields

    renderer.prompt_user(...)
      → _emit("input_required") ────→ (same path, SSE pushes form to client)
      → input_queue.get() (blocks worker thread)
                                          ↑
                                      POST /api/input puts here

Multi-viewer, all equal: every authenticated connection is kept in
``_connections``, receives the fan-out, AND may send input / queue messages.
The worker thread is unaware of clients — it just emits, the renderer fans
out to all.

Buffer / replay: ``_event_buffer`` holds every persistent event since
session start (or since session resume on ``--resume <id>``). When a new
client connects, the server first replays the buffer in order, then
forwards live events from its dedicated queue. Transient events
(``stream_chunk``, ``status``, ``spinner``) are not replayed — they are
runtime UX, not state.
"""

from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass, field
from queue import Empty, SimpleQueue
from typing import Any

from agent_cli.render.base import ConfirmOption, Renderer

# Prompt Inspector scope key for the main loop's system prompt. Delegate
# sub-agents are keyed by their ``task_id``; the empty string is the main
# loop (which has no task_id). Kept a constant so the renderer, server, and
# tests agree on the sentinel.
_MAIN_SCOPE = ""

# Fun default nicknames assigned to viewers on connect (browsers can't read
# the client's OS username, so a friendly auto-label is the practical
# identity). A random unused one is picked per connection.
_NICKNAMES = [
    "Sneaky Fox",
    "Witty Otter",
    "Brave Penguin",
    "Sleepy Panda",
    "Curious Cat",
    "Jazzy Llama",
    "Grumpy Walrus",
    "Dizzy Ferret",
    "Mellow Moose",
    "Snappy Crab",
    "Funky Gecko",
    "Loyal Hound",
    "Zesty Quokka",
    "Cosmic Yak",
    "Turbo Sloth",
    "Nifty Newt",
    "Rowdy Raccoon",
    "Bouncy Badger",
    "Spicy Mantis",
    "Chill Capybara",
]


@dataclass
class WebConnection:
    """One active SSE subscriber. Server creates one per HTTP connection."""

    id: str
    queue: SimpleQueue = field(default_factory=SimpleQueue)
    closed: threading.Event = field(default_factory=threading.Event)


class WebRenderer(Renderer):
    """Renderer for ``agent-cli web``.

    Thread-safe: emit methods are called from the AgentLoop worker
    thread; ``register_connection`` / ``push_user_input`` are called
    from the FastAPI request handlers (async, but the calls themselves
    are synchronous from the renderer's POV). All shared state is
    guarded by ``self._lock``.

    Persistent events flow through ``_event_buffer`` (for replay) and
    every active connection's ``queue``. Transient events skip the
    buffer.
    """

    def __init__(self, *, workspace: str = "") -> None:
        super().__init__()
        self._lock = threading.Lock()
        # event_buffer entries: (event_name, data_dict)
        self._event_buffer: list[tuple[str, dict[str, Any]]] = []
        self._connections: list[WebConnection] = []
        # Worker busy/idle mirror (set by worker_busy/worker_idle) — a plain
        # queryable bool for the idle-reaper, independent of the sticky payload.
        self._worker_busy = False
        # conn_id → fun nickname (assigned on register, shown to all viewers)
        self._nicknames: dict[str, str] = {}
        # Every connection is equal: all receive the fan-out AND may send
        # input / queue messages (no controller/observer split).
        # ``_connections`` is append-order = oldest-first (snapshot fan-out).
        # input queue feeds prompt_user / confirm
        self._input_queue: SimpleQueue = SimpleQueue()
        # Pending assistant emission: thought() arrives before action() /
        # final(), so the thought is held until the second call so we can
        # emit a single ``assistant_turn`` event per LLM emission.
        self._pending_thought: str | None = None
        # Counter of persistent events in the SSE replay buffer.
        self._persistent_count: int = 0
        # Optional workspace path piggybacks on the ``ready`` event so
        # the frontend's top bar can show "provider · model · cwd"
        # without the LLM needing to volunteer the path. Empty string
        # = field omitted from the event entirely.
        self._workspace = workspace
        # Per-scope system-prompt snapshots for the Prompt Inspector
        # (``GET /api/debug/prompt``). Keyed by scope: ``_MAIN_SCOPE`` ("")
        # is the main loop, a ``task_id`` is a delegate sub-agent — resolved
        # from the CALLING thread via ``_thread_to_task`` (the same map
        # ``_emit`` uses), so ``note_system_prompt`` needs no extra context
        # threaded down from the loop. Slot-per-scope semantics like the sticky
        # ``ready`` slot: only the most recent LLM call's prompt per scope
        # is kept (an on-demand view, not a history). Sub-agent snapshots
        # persist after the agent finishes so its prompt stays inspectable
        # post-mortem; the frontend drops one via ``DELETE``.
        self._prompt_snapshots: dict[str, dict[str, Any]] = {}
        # Scope-label metadata (task_id → {"agent", "index"}) captured at
        # ``begin_delegate_task`` so the inspector chip row can name each
        # sub-agent ("explorer·1") without re-deriving it from the timeline.
        self._prompt_scope_labels: dict[str, dict[str, Any]] = {}
        # ── Sticky state registry ───────────────────────────────────
        # A "sticky" state is a single server value that is (a) broadcast live
        # to connected clients AND (b) replayed into each NEW connection's
        # snapshot — so a late / refreshed / second-browser client sees the
        # last value (plain SSE only reaches clients connected at emit time).
        # ``set_sticky(name, event, payload, position=…)`` is the ONE place that
        # does both; ``register_connection`` walks this dict to rebuild the
        # snapshot. ``position`` controls placement: ``ready`` goes FIRST (the
        # top-bar must populate before everything — fixes the stuck
        # "connecting…" state), the rest append. All emit non-persistent (live
        # only — ``_event_buffer`` keeps history, the slot keeps "latest" so the
        # buffer doesn't accumulate one entry per turn). Members:
        #   ready        — session info (model/session/workspace) for the top-bar
        #   worker_state — {busy} send-button gating
        #   token_usage  — per-turn token readout
        #   queue        — pending user-message queue
        #   auto_review  — {enabled} review toggle (shared across all browsers)
        # NOT sticky: viewers (connection-set-derived, not a single latest
        # value) / prompt snapshots (per-scope, on-demand pull).
        # name → {"event", "payload", "position"}.
        self._sticky: dict[str, dict[str, Any]] = {}
        # Per-thread delegate-task routing. Worker threads spawned by
        # ``_run_parallel`` register their ``task_id`` here via
        # ``begin_delegate_task``; ``_emit`` then auto-attaches
        # ``task_id`` to every event the worker fires so the frontend
        # can route into the right collapsible group instead of
        # interleaving on the main timeline.
        self._thread_to_task: dict[int, str] = {}
        # During ``replay_from_history`` (resume), holds the history record's
        # original ``ts`` so replayed cards show when the step ACTUALLY
        # happened, not the resume moment. ``None`` = live path → ``_emit``
        # stamps fresh wall-clock. Set/cleared around the single-threaded seed
        # loop (runs before the worker thread or any SSE client exists).
        self._replay_ts: float | str | None = None

    # ─── Event distribution ─────────────────────────

    def _emit(
        self,
        event: str,
        data: dict[str, Any],
        *,
        persistent: bool,
    ) -> None:
        """Append to buffer (if persistent) and fan out to active connections.

        Auto-attaches ``task_id`` from the per-thread delegate map
        when the emitting thread is a parallel-delegate worker, so
        every downstream event (``assistant_turn``, ``observation``,
        ``stream_chunk``, ``error``, …) carries the routing key the
        frontend uses to keep parallel work visually separated. Skips
        attachment for events that already carry an explicit
        ``task_id`` (the ``delegate_task_*`` lifecycle markers fill
        it in themselves) and for events whose data shape is
        deliberately bare (e.g. ``input_resolved``).
        """
        tid = threading.get_ident()
        task_id = self._thread_to_task.get(tid)
        if task_id is not None and "task_id" not in data:
            data = {**data, "task_id": task_id}
        # Server-stamp emit time once, at the single fan-out point, so every
        # card-producing event (incl. delegate/skill inner cards, which route
        # through here with their ``task_id``) carries a ``ts``. Baked into the
        # persistent buffer too → reconnect replay shows the original time. On
        # resume, ``_replay_ts`` carries the history record's original ts so
        # seeded cards aren't all stamped with the resume moment. Frontend
        # accepts either epoch seconds (live) or ISO string (history) and
        # formats to local time.
        if "ts" not in data:
            data = {
                **data,
                "ts": self._replay_ts if self._replay_ts is not None else time.time(),
            }
        with self._lock:
            if persistent:
                self._event_buffer.append((event, data))
                self._persistent_count += 1
            for conn in self._connections:
                if not conn.closed.is_set():
                    conn.queue.put((event, data))

    def set_sticky(
        self,
        name: str,
        event: str,
        payload: dict[str, Any],
        *,
        position: str = "append",
    ) -> None:
        """Set a sticky state: broadcast ``(event, payload)`` live AND cache it
        so every NEW connection replays it in its snapshot (see ``_sticky``).
        ``position`` ('prepend' | 'append') is where it lands in the snapshot —
        ``ready`` prepends (top-bar first), the rest append. Always live
        (non-persistent): the slot holds the latest, the buffer holds history."""
        with self._lock:
            self._sticky[name] = {
                "event": event,
                "payload": payload,
                "position": position,
            }
        self._emit(event, payload, persistent=False)

    def clear_sticky(self, name: str) -> None:
        """Drop a sticky slot so new connections no longer replay it — e.g.
        once a pending ``input_required`` is answered, a late/reconnecting
        client must NOT be handed the already-resolved prompt."""
        with self._lock:
            self._sticky.pop(name, None)

    def register_connection(self, conn: WebConnection) -> list[tuple[str, dict]]:
        """Add ``conn`` as a subscriber. Every connection is equal — all may
        send input and queue messages (no controller/observer split).

        The returned snapshot starts with an ``identity`` event (so the client
        learns its ``conn_id`` before anything else — used for the viewer
        roster "(you)" mark and queued-message ownership), then the usual
        replay. Caller yields the snapshot, then loops on ``conn.queue`` for
        live events.
        """
        with self._lock:
            self._connections.append(conn)

            snapshot = list(self._event_buffer)
            # Replay sticky state into the new connection's snapshot so a
            # late/refreshed/second-browser client sees the last value of each.
            # ``prepend`` slots (ready → top-bar) go ahead of the buffer;
            # ``append`` slots (worker_state/token_usage/queue/auto_review) after.
            for slot in self._sticky.values():
                entry = (slot["event"], slot["payload"])
                if slot["position"] == "prepend":
                    snapshot.insert(0, entry)
                else:
                    snapshot.append(entry)
            # ``identity`` first: the client needs its conn_id before anything.
            snapshot.insert(0, ("identity", {"conn_id": conn.id}))
            self._assign_nickname_locked(conn.id)
            # Live viewers: the JOINING conn learns the roster via its snapshot
            # (no extra queue event — keeps single-conn queue assertions
            # clean); EXISTING conns learn via their queue.
            payload = self._viewers_payload_locked()
            snapshot.append(("viewers", payload))
            for c in self._connections:
                if c is not conn and not c.closed.is_set():
                    c.queue.put(("viewers", payload))
            return snapshot

    def _assign_nickname_locked(self, conn_id: str) -> None:
        used = set(self._nicknames.values())
        free = [n for n in _NICKNAMES if n not in used]
        if free:
            name = random.choice(free)
        else:  # more viewers than pool — disambiguate with a counter
            name = f"{random.choice(_NICKNAMES)} #{len(self._nicknames) + 1}"
        self._nicknames[conn_id] = name

    def _viewers_payload_locked(self) -> dict:
        """`{count, viewers:[{id, name}]}` for the open connections. The
        client matches its own ``conn_id`` to mark "(you)"."""
        vs = [
            {"id": c.id, "name": self._nicknames.get(c.id, "?")}
            for c in self._connections
            if not c.closed.is_set()
        ]
        return {"count": len(vs), "viewers": vs}

    def _broadcast_viewers_locked(self) -> None:
        """Push the current viewer roster to every remaining client's queue.
        Caller must hold ``self._lock`` — invoked from unregister (the leaver
        is already removed)."""
        payload = self._viewers_payload_locked()
        for c in self._connections:
            if not c.closed.is_set():
                c.queue.put(("viewers", payload))

    def nickname_for(self, conn_id: str | None) -> str:
        """The fun nickname assigned to ``conn_id`` (for queued-message
        attribution), or "?" if unknown."""
        with self._lock:
            return self._nicknames.get(conn_id or "", "?")

    def set_nickname(self, conn_id: str | None, name: str) -> bool:
        """Let a viewer rename itself (the fun default is pre-filled in the
        UI, the user edits/confirms). Trimmed + length-capped; empty rejected.
        Rebroadcasts the roster so everyone sees the new name."""
        name = (name or "").strip()[:24]
        if not conn_id or not name:
            return False
        with self._lock:
            self._nicknames[conn_id] = name
            self._broadcast_viewers_locked()
        return True

    def queue_state(self, pending: list[dict]) -> None:
        """Broadcast the pending user-message queue (real-time). Each item is
        ``{id, nickname, conn_id, text}``; the frontend renders the list and
        shows a cancel control on the viewer's own items. Cached so a
        reconnecting client sees the queue immediately."""
        self.set_sticky("queue", "queue", {"pending": pending})

    def unregister_connection(self, conn: WebConnection) -> None:
        """Drop ``conn`` and wake any pending queue waiter.

        The sentinel put is the symmetric pair of ``register_connection``'s
        list append: register exposes the queue to writers, unregister signals
        readers to stop (else the SSE generator's ``queue.get(timeout=15)``
        leaks its executor thread for up to 15s after disconnect).
        """
        with self._lock:
            if conn in self._connections:
                self._connections.remove(conn)
            self._nicknames.pop(conn.id, None)
            # the leaver is already removed → broadcast the decremented count
            self._broadcast_viewers_locked()
        conn.queue.put(_CLOSE_SENTINEL)

    # ─── Parallel delegate visibility ───────────────

    def begin_delegate_task(
        self,
        *,
        task_id: str,
        index: int,
        agent: str,
        task_text: str,
    ) -> None:
        """Register the current thread as a delegate worker and emit a
        persistent ``delegate_task_start`` event so the frontend opens
        a collapsible group card for this task.

        Persistent (not transient): the start marker pairs with
        ``delegate_task_end`` to bound a card lifetime in the event
        buffer. If a client connects mid-delegate, the replay snapshot
        still draws the card so the user isn't staring at a blank
        view while the worker chugs through.
        """
        tid = threading.get_ident()
        with self._lock:
            self._thread_to_task[tid] = task_id
            # Remember the chip-row label for this scope; the snapshot itself
            # arrives later (first LLM call) keyed by the same task_id.
            self._prompt_scope_labels[task_id] = {"agent": agent, "index": index}
        # Tag this worker thread so a confirm/ask it triggers can name it.
        self.set_thread_agent(agent or f"task #{index + 1}")
        self._emit(
            "delegate_task_start",
            {
                "task_id": task_id,
                "index": index,
                "agent": agent,
                "task_text": task_text,
            },
            persistent=True,
        )

    def end_delegate_task(
        self,
        *,
        task_id: str,
        success: bool,
        duration_s: float,
        error: str = "",
    ) -> None:
        """Unregister the current thread and emit a persistent
        ``delegate_task_end`` event with the final ✓/✗ + duration so
        the frontend caps the card header."""
        tid = threading.get_ident()
        with self._lock:
            self._thread_to_task.pop(tid, None)
        self.set_thread_agent("")  # worker's prompt label no longer applies
        payload = {
            "task_id": task_id,
            "success": success,
            "duration_s": duration_s,
        }
        if error:
            payload["error"] = error
        self._emit("delegate_task_end", payload, persistent=True)

    def set_thread_status(self, status: str) -> None:
        """Forward to the base ``_thread_status`` dict (CLI rich.Live
        polling compatibility) AND emit a transient
        ``delegate_task_status`` event so the frontend's task card
        header can update its live status line."""
        super().set_thread_status(status)
        tid = threading.get_ident()
        task_id = self._thread_to_task.get(tid)
        if task_id is not None:
            self._emit(
                "delegate_task_status",
                {"task_id": task_id, "status": status},
                persistent=False,
            )

    def shutdown_all_connections(self) -> None:
        """Close every active SSE generator (used on shutdown).

        Pushes the ``__close__`` sentinel into each connection's queue
        so the SSE generator's blocking executor-thread ``queue.get``
        wakes up and breaks out of its loop. Idempotent — second call
        finds an empty connection list and no-ops.

        Called from two places: the FastAPI ``shutdown`` lifespan hook
        (uvicorn's own SIGINT path) and ``main.py``'s ``finally`` block
        (belt-and-braces). Either ordering leaves the worker thread
        free to finalise the session.
        """
        with self._lock:
            for conn in self._connections:
                if not conn.closed.is_set():
                    conn.closed.set()
                    conn.queue.put(_CLOSE_SENTINEL)
            self._connections.clear()

    def replay_from_history(self, ctx) -> None:
        """Re-emit persistent events from ``ctx`` so reconnecting
        clients see prior turns.

        Walks the ContextManager's raw cache (already populated by
        ``ContextManager(..., resume=True)``) and translates each
        message back into the live-loop's persistent event sequence:
        ``user_message`` for user input, ``observation`` for tool
        results, ``assistant_turn`` for assistant thought/action/final.

        Transient events (``stream_chunk``, ``status``, ``spinner``)
        are runtime UX only and have no on-disk counterpart, so they
        are not replayed.

        Called once at server startup when ``--resume <id>`` was passed,
        BEFORE the worker thread starts or any SSE client connects.
        """
        for msg in ctx.get_raw_messages():
            # Resumed cards show the step's original time (from the enriched
            # history record), not the resume moment. ``_restore_cache`` loads
            # full records, so ``ts`` survives in the cache; legacy pre-ts
            # sessions yield None → ``_emit`` falls back to wall-clock.
            self._replay_ts = msg.get("ts")
            role = msg.get("role")
            if role == "user":
                # ``tool`` key presence — not truthiness — signals an
                # observation entry. ``_append_observation`` always
                # writes both ``tool`` and ``success`` (empty string
                # ``tool`` for format-retry interventions), so plain
                # user chat turns (no ``tool`` key) route through
                # ``push_user_message`` and tool results / retries
                # route through ``observation()``.
                if "tool" in msg:
                    content = msg.get("content", "")
                    if not isinstance(content, str):
                        content = ""
                    # ``_append_observation`` prefixes ``obs_msg`` with
                    # ``"Observation: "`` for the LLM-facing slot. The
                    # web frontend's observation card already labels
                    # the entry, so strip the prefix to match what a
                    # live ``observation()`` call would emit.
                    prefix = "Observation: "
                    if content.startswith(prefix):
                        content = content[len(prefix) :]
                    self.observation(
                        content,
                        turn=0,
                        tool_name=msg.get("tool", ""),
                        success=msg.get("success", True),
                    )
                else:
                    content = msg.get("content", "")
                    if content:
                        self.push_user_message(content)
            elif role == "assistant":
                thought = msg.get("thought", "") or ""
                ops = msg.get("ops")
                if isinstance(ops, list) and ops:
                    # Both wire formats store every assistant turn — INCLUDING
                    # the terminal ``complete`` — in the ``ops`` shape
                    # (``serialize_terminal_for_history`` / ``serialize_
                    # assistant_for_history``). Emit the thought once (held),
                    # then flush one card per op. Without this branch the whole
                    # assistant side (thought / action / final) is dropped on
                    # resume — only the singular legacy shape was handled.
                    self.thought(thought, turn=0)
                    for op in ops:
                        if isinstance(op, dict):
                            self._replay_assistant_op(
                                op.get("action", "") or "", op.get("action_input", {})
                            )
                elif msg.get("action"):
                    # Legacy singular ``{action, action_input}`` shape — kept for
                    # old history files / the base (non-multi-op) format.
                    self.thought(thought, turn=0)
                    self._replay_assistant_op(
                        msg.get("action", "") or "", msg.get("action_input", {})
                    )
                else:
                    # Raw content-only assistant turn (e.g. a NO_JSON emission
                    # stored verbatim) — render as a final card so the resumed
                    # transcript isn't silently missing it.
                    content = msg.get("content", "")
                    if content:
                        self.final(content, turn=0)
        # Back to live: subsequent events get a fresh wall-clock stamp.
        self._replay_ts = None

    def _replay_assistant_op(self, action: str, action_input) -> None:
        """Emit one assistant op as a replay card — ``final`` for a terminal
        ``complete``, otherwise an ``action`` card. Mirrors the live
        ``render_step`` calls; the caller emits the (held) thought first so the
        first op's card carries it."""
        if action == "complete":
            if isinstance(action_input, dict):
                final_text = action_input.get("result", "") or ""
            else:
                final_text = str(action_input) if action_input else ""
            self.final(final_text, turn=0)
        elif action:
            if isinstance(action_input, dict):
                tool_input = json.dumps(action_input, ensure_ascii=False)
            else:
                tool_input = str(action_input)
            self.action(action, tool_input, turn=0)

    @property
    def persistent_count(self) -> int:
        """Number of persistent events currently in the SSE replay
        buffer. Read by tests to assert which events land in replay.
        """
        with self._lock:
            return self._persistent_count

    # ─── Output methods (Renderer ABC) ──────────────

    def header(
        self,
        provider: str,
        model: str,
        max_turns: int,
        skill_name: str = "",
        skill_args: str = "",
    ) -> None:
        # Nested AgentLoop runs (delegate, skill) also call header()
        # but those would clobber the top-level session info — the
        # frontend's top bar should keep showing "provider · model ·
        # cwd" while a sub-flow runs, not flicker to a skill name and
        # back. Skip non-top-level headers entirely.
        if skill_name or skill_args:
            return
        payload = {
            "provider": provider,
            "model": model,
            "max_turns": max_turns,
            "skill_name": skill_name,
            "skill_args": skill_args,
        }
        if self._workspace:
            payload["workspace"] = self._workspace
        # Sticky + ``prepend`` so the top-bar populates before everything on a
        # client that opens the page mid-session (fixes stuck "connecting…").
        self.set_sticky("ready", "ready", payload, position="prepend")

    def turn_sep(self, turn: int) -> None:
        # No frontend event — turn number rides on each message event.
        pass

    def thought(self, content: str, turn: int) -> None:
        # Record for an interactive-prompt header (who/why behind a confirm
        # or ask in this thread).
        self.note_thought(content)
        # Hold until the matching action / final fires so we can emit
        # a single ``assistant_turn`` event per LLM emission.
        self._pending_thought = content
        # Mirror the CLI behaviour: surface the first line of the
        # thought as the worker's live status so a delegate-task card
        # header shows ``💭 reasoning…`` while the worker is still
        # mid-turn. ``set_thread_status`` is a no-op outside delegate
        # workers (no ``task_id`` in ``_thread_to_task``).
        first_line = content.split("\n", 1)[0] if content else ""
        if first_line:
            self.set_thread_status(f"💭 {first_line}")

    def action(self, tool_name: str, tool_input: str, turn: int) -> None:
        self.note_action(tool_name, tool_input)
        self._emit(
            "assistant_turn",
            {
                "turn": turn,
                "thought": self._pending_thought or "",
                "action": {"tool_name": tool_name, "tool_input": tool_input},
            },
            persistent=True,
        )
        self._pending_thought = None

    def observation(
        self,
        content: str,
        turn: int,
        tool_name: str | None = None,
        success: bool = True,
    ) -> None:
        self._emit(
            "observation",
            {
                "turn": turn,
                "tool_name": tool_name or "",
                "content": content,
                "success": success,
            },
            persistent=True,
        )

    def final(self, content: str, turn: int) -> None:
        self._emit(
            "assistant_turn",
            {
                "turn": turn,
                "thought": self._pending_thought or "",
                "final": content,
            },
            persistent=True,
        )
        self._pending_thought = None

    def error(self, content: str, turn: int) -> None:
        self._emit("error", {"turn": turn, "content": content}, persistent=True)

    def recovery(
        self,
        raw_emission: str,
        intervention_message: str,
        reason: str,
        turn: int,
    ) -> None:
        # Finalize the live streaming card as a failed emission so the next
        # turn's stream starts a fresh card instead of appending to the
        # rejected one. ``raw`` is carried for replay (event_buffer), where
        # no live streaming card exists to close.
        self._emit(
            "failed_turn",
            {"turn": turn, "reason": reason, "raw": raw_emission},
            persistent=True,
        )
        # The intervention we fed back to the model — its own card.
        self.observation(intervention_message, turn, None, success=False)

    def raw(self, text: str, turn: int, verbose: bool) -> None:
        # verbose-only — transient debug stream.
        if verbose:
            self._emit("raw", {"turn": turn, "text": text}, persistent=False)

    def thinking(self, text: str, turn: int) -> None:
        # Reasoning channel — transient, shown in verbose UI.
        self._emit("thinking", {"turn": turn, "text": text}, persistent=False)

    def status(self, state: str, message: str, turn: int = 0) -> None:
        self._emit(
            "status",
            {"state": state, "message": message, "turn": turn},
            persistent=False,
        )

    def compaction(
        self,
        *,
        phase: str,
        old_tokens: int = 0,
        new_tokens: int = 0,
        evicted_count: int = 0,
        reason: str = "",
    ) -> None:
        """Dedicated structured event for the frontend's inline conversation
        line (the generic ``status`` event has no frontend listener). Raw
        figures go over the wire; the frontend formats the line. Transient —
        a compaction is a live timeline marker, not replayed on reconnect.
        """
        self._emit(
            "compaction",
            {
                "phase": phase,
                "old_tokens": old_tokens,
                "new_tokens": new_tokens,
                "evicted_count": evicted_count,
                "reason": reason,
            },
            persistent=False,
        )

    def token_usage(self, stats: dict, turn: int, verbose: bool = False) -> None:
        """Emit the per-turn token usage for the frontend's top-bar
        readout. Raw stats go over the wire (the frontend formats); the
        latest is cached so a refresh repopulates the bar from snapshot.
        """
        self.set_sticky("token_usage", "token_usage", {**stats, "turn": turn})

    def model_detected(
        self, model: str, capabilities, provider: str, saved_path: str
    ) -> None:
        # One-shot info — frontend can toast or log it. Capabilities is a
        # dataclass, expose only the safe public fields.
        self._emit(
            "model_detected",
            {
                "model": model,
                "provider": provider,
                "saved_path": saved_path,
                "context_window": getattr(capabilities, "context_window", 0),
            },
            persistent=False,
        )

    def model_loaded(self, model: str, capabilities) -> None:
        self._emit(
            "model_loaded",
            {
                "model": model,
                "context_window": getattr(capabilities, "context_window", 0),
            },
            persistent=False,
        )

    def context_dump(self, messages: list[dict], turn: int) -> None:
        # Debug-only — verbose dump for developers. Send raw structured
        # form so a future debugging UI can render it.
        self._emit(
            "context_dump",
            {"turn": turn, "messages": messages},
            persistent=False,
        )

    def spinner_start(self, message: str = "") -> None:
        self._emit("spinner", {"state": "start", "message": message}, persistent=False)

    def spinner_stop(self) -> None:
        self._emit("spinner", {"state": "stop"}, persistent=False)

    def worker_busy(self) -> None:
        """Signal that the chat worker just picked up a user message
        and is processing it. Stays busy until the worker returns to
        ``dequeue_blocking`` — through every intermediate LLM turn, tool call,
        and even any ``prompt_user`` / ``confirm`` wait. The frontend
        uses this to disable the chat ``Send`` button so the user
        doesn't queue a second message into an actively-running turn.

        Sticky (see ``set_sticky``) so a refreshed / reconnected client
        gets the correct send-button state immediately via the snapshot
        replay, without waiting for the next transition.
        """
        self._worker_busy = True
        self.set_sticky("worker_state", "worker_state", {"busy": True})

    def worker_idle(self) -> None:
        """Signal that the chat worker is back at the top-level
        ``dequeue_blocking`` and ready to accept the next user message.
        Re-enables the frontend ``Send`` button. See ``worker_busy``
        for the persistence + reconnect semantics."""
        self._worker_busy = False
        self.set_sticky("worker_state", "worker_state", {"busy": False})

    def worker_is_busy(self) -> bool:
        """Whether the chat worker is mid-message (LLM turn / tool / prompt
        wait). Read by the idle-reaper so a running agent with no viewers is
        never reaped mid-task."""
        return self._worker_busy

    def has_live_connections(self) -> bool:
        """Whether at least one browser is still subscribed (not closed).
        The idle-reaper's primary 'someone is here' signal."""
        with self._lock:
            return any(not c.closed.is_set() for c in self._connections)

    def auto_review_state(self, enabled: bool) -> None:
        """Broadcast the auto-review toggle state. Sticky so EVERY browser's
        toggle button reflects the shared server state — toggling on one client
        updates the others live, and a refreshed/new client sees it via the
        snapshot. (The state itself lives on ``WebServer``; this just mirrors it
        to all views.)"""
        self.set_sticky("auto_review", "auto_review", {"enabled": bool(enabled)})

    def note_system_prompt(self, sections: list[tuple[str, str]], turn: int) -> None:
        """Keep the latest system-prompt snapshot for the Prompt Inspector.

        The scope is resolved from the CALLING thread: a delegate worker's
        snapshot lands under its ``task_id`` (the same ``_thread_to_task``
        routing ``_emit`` uses), the main loop under ``_MAIN_SCOPE``. So each
        agent's prompt is inspectable separately without the loop passing any
        identity down.

        Store-only (no SSE emission — the prompt is ~16KB and the inspector
        fetches on demand via ``GET /api/debug/prompt``). Token figures are
        the same chars/4 estimate the context manager budgets with.
        """
        from agent_cli.context.token_estimator import estimate_tokens

        scope = self._thread_to_task.get(threading.get_ident()) or _MAIN_SCOPE
        snapshot = {
            "turn": turn,
            "total_chars": sum(len(t) for _, t in sections)
            + 2 * max(0, len(sections) - 1),
            "est_tokens": sum(estimate_tokens(t) for _, t in sections),
            "sections": [
                {
                    "name": name,
                    "text": text,
                    "chars": len(text),
                    "est_tokens": estimate_tokens(text),
                }
                for name, text in sections
            ],
        }
        with self._lock:
            self._prompt_snapshots[scope] = snapshot

    def prompt_snapshot(self, scope: str = _MAIN_SCOPE) -> dict[str, Any] | None:
        """Latest system-prompt snapshot for ``scope`` (``_MAIN_SCOPE`` = main
        loop, a ``task_id`` = a delegate sub-agent), or None if that scope has
        no captured prompt yet. Public read surface for the debug endpoint."""
        with self._lock:
            return self._prompt_snapshots.get(scope)

    def prompt_scopes(self) -> list[dict[str, Any]]:
        """Scopes that currently have a captured prompt, for the inspector
        chip row. Main first (if present), then sub-agents in capture order.
        Each entry: ``{id, label, turn, est_tokens, main}``."""
        out: list[dict[str, Any]] = []
        with self._lock:
            for scope, snap in self._prompt_snapshots.items():
                is_main = scope == _MAIN_SCOPE
                if is_main:
                    label = "Main"
                else:
                    meta = self._prompt_scope_labels.get(scope, {})
                    agent = meta.get("agent") or "agent"
                    idx = meta.get("index")
                    label = f"{agent}·{idx + 1}" if isinstance(idx, int) else agent
                out.append(
                    {
                        "id": scope,
                        "label": label,
                        "turn": snap.get("turn"),
                        "est_tokens": snap.get("est_tokens"),
                        "main": is_main,
                    }
                )
        # Main pinned first; stable sort keeps sub-agents in insertion order.
        out.sort(key=lambda s: 0 if s["main"] else 1)
        return out

    def delete_prompt_scope(self, scope: str) -> bool:
        """Drop a sub-agent's prompt snapshot (inspector ✕ button). Main is
        not deletable — it regenerates every turn and is the default view.
        Returns True if a snapshot was actually removed."""
        if scope == _MAIN_SCOPE:
            return False
        with self._lock:
            removed = self._prompt_snapshots.pop(scope, None) is not None
            self._prompt_scope_labels.pop(scope, None)
        return removed

    def dispatch_progress(
        self,
        label: str,
        turn: int,
        tool_name: str,
        detail: str = "",
        thought: str = "",
    ) -> None:
        self._emit(
            "dispatch_progress",
            {
                "label": label,
                "turn": turn,
                "tool_name": tool_name,
                "detail": detail,
                "thought": thought,
            },
            persistent=False,
        )

    def stream_chunk(self, text: str) -> None:
        self._emit("stream_chunk", {"text": text}, persistent=False)

    def stream_end(self) -> None:
        self._emit("stream_end", {}, persistent=False)

    def group_start(self, label: str, icon: str = "") -> None:
        self._emit("group_start", {"label": label, "icon": icon}, persistent=False)

    def group_end(
        self, label: str, success: bool = True, duration_s: float = 0
    ) -> None:
        self._emit(
            "group_end",
            {"label": label, "success": success, "duration_s": duration_s},
            persistent=False,
        )

    # ─── Input methods (Renderer ABC) ───────────────

    def prompt_user(
        self,
        prompt: str,
        *,
        default: str = "",
        multiline: bool = True,
        continuation: str = "... ",
        context: str = "",
    ) -> str:
        """Push an ``input_required`` event and block the worker thread
        until POST /api/input arrives with a chat / ask answer.

        ``EOFError`` is raised if ``push_abort()`` was signalled while
        waiting — gives the same propagation semantics as the CLI
        renderer so interactive teardown logic stays consistent.

        ``context`` (e.g. the ``ask`` tool's question block) is
        forwarded as a separate field so the frontend can attach it
        to the input affordance — the user doesn't have to scroll
        back to the assistant card to see what they're answering.
        """

        # Emit + wait run together under ``_guarded_read``'s shared lock so
        # only one ``input_required`` is outstanding at a time — otherwise
        # two concurrent delegate prompts would both block on the single
        # ``_input_queue`` and one answer could satisfy the wrong worker.
        meta = self.prompt_meta()

        def _do() -> str:
            # Sticky (not bare emit): a pending prompt must replay into a
            # late/reconnecting client's snapshot, else the worker waits for an
            # answer the UI never offered (the prompt is invisible). Cleared on
            # resolve so a fresh connection doesn't see the stale question.
            self.set_sticky(
                "input_required",
                "input_required",
                {
                    "kind": "prompt",
                    "prompt": prompt,
                    "multiline": multiline,
                    "continuation": continuation,
                    "context": context,
                    # Who/why: which delegate agent is asking + its
                    # reasoning, so the user can attribute the prompt.
                    "agent": meta["agent"],
                    "reasoning": meta["reasoning"],
                },
            )
            try:
                return self._wait_for_input()
            finally:
                self.clear_sticky("input_required")
                self._emit("input_resolved", {}, persistent=False)

        value = self._guarded_read(_do)
        return value if value else default

    def can_prompt(self) -> bool:
        """We can prompt whenever a browser is connected to answer the
        ``input_required`` event — no TTY needed; the SSE + ``/api/input``
        channel carries it. Returns ``False`` when nothing is connected, so
        an interactive prompt is refused / defaulted with a clear path
        rather than blocking on an answer no one can give."""
        return self.has_live_connections()

    def confirm(
        self,
        prompt: str,
        options: list[ConfirmOption],
        *,
        default_key: str,
    ) -> tuple[str, str]:
        """Push an ``input_required`` event with the option list, block
        until POST /api/input arrives with a ``(key, comment)`` payload.

        On abort, returns ``(default_key, "")`` — matches MinimalRenderer
        so callers see the same "safe default" semantics regardless of
        where the user disconnected.
        """

        # Emit + wait run together under ``_guarded_read``'s shared lock
        # (same serialization as ``prompt_user``) so confirm and ask never
        # have two prompts outstanding on the single ``_input_queue``.
        meta = self.prompt_meta()

        def _do():
            # Sticky like ``prompt_user`` so a reconnecting client replays the
            # pending confirm dialog instead of leaving the worker blocked.
            self.set_sticky(
                "input_required",
                "input_required",
                {
                    "kind": "confirm",
                    "prompt": prompt,
                    "options": [
                        {"key": o.key, "label": o.label, "aliases": list(o.aliases)}
                        for o in options
                    ],
                    "default_key": default_key,
                    # Who/why/what: which delegate agent + its reasoning +
                    # the action it wants to run, surfaced in the dialog.
                    "agent": meta["agent"],
                    "reasoning": meta["reasoning"],
                    "action": meta["action"],
                },
            )
            try:
                return self._wait_for_input()
            finally:
                self.clear_sticky("input_required")
                self._emit("input_resolved", {}, persistent=False)

        try:
            value = self._guarded_read(_do)
        except EOFError:
            # Mirror MinimalRenderer: confirm is "pick or default" — abort
            # collapses to the safe default rather than propagating (the
            # caller passed a default_key precisely so confirm can always
            # answer).
            return (default_key, "")
        if isinstance(value, tuple) and len(value) == 2:
            return value  # type: ignore[return-value]
        # Malformed payload — fall back to default.
        return (default_key, "")

    # ─── External hooks ─────────────────────────────

    def push_user_input(self, kind: str, payload: dict[str, Any]) -> None:
        """Called by FastAPI POST /api/input handler.

        ``kind``: ``"prompt"`` (chat / ask answer) or ``"confirm"``.
        ``payload``: For prompt, ``{"content": "..."}``. For confirm,
        ``{"key": "...", "comment": "..."}``.
        """
        if kind == "confirm":
            self._input_queue.put((payload.get("key", ""), payload.get("comment", "")))
        else:
            self._input_queue.put(payload.get("content", ""))

    def push_user_message(self, content: str) -> None:
        """Echo a user-typed chat message into the persistent event
        stream so the frontend renders it as a card.

        Called by the server's POST /api/input handler for chat
        messages, BEFORE the message is fed to the AgentLoop. Goes
        into the buffer (replayed on reconnect) so the conversation
        renders correctly.
        """
        self._emit(
            "user_message",
            {"content": content},
            persistent=True,
        )

    def push_abort(self) -> None:
        """Unblock a pending ``prompt_user`` / ``confirm`` call by
        injecting a sentinel that the wait helper treats as EOF.
        Called by POST /api/abort.
        """
        self._input_queue.put(_ABORT_SENTINEL)

    # ─── Helpers ────────────────────────────────────

    def _wait_for_input(self) -> Any:
        """Block worker thread until POST /api/input arrives.

        Polls with a small timeout so future interrupt mechanisms can
        slot in without rewriting the wait loop.
        """
        while True:
            try:
                value = self._input_queue.get(timeout=0.5)
            except Empty:
                continue
            if value is _ABORT_SENTINEL:
                raise EOFError("Input aborted by user")
            return value


# Sentinel object used to distinguish "abort" from a legitimate empty
# string input. Module-private — never crosses the renderer boundary.
_ABORT_SENTINEL = object()

# Sentinel event used by ``unregister_connection`` to wake up the SSE
# generator's executor-thread queue wait. Pattern-matched in
# ``WebServer.stream_events`` as a stream-end signal — never serialised
# to a client.
_CLOSE_SENTINEL: tuple[str, dict] = ("__close__", {})
