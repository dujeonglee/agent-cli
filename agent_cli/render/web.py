"""WebRenderer — emits Renderer events as Server-Sent Events to the
single active web client, and waits on POST /api/input for user input.

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

Single active client (takeover model): when a new SSE connection arrives,
the server marks the old connection closed and pushes a ``takeover`` event
into its queue so it disconnects cleanly. The worker thread is unaware of
client comings and goings — it just emits, the renderer fans out.

FIFO sync: the renderer counts persistent message events emitted. After
each turn the server compares this count to ``ContextManager._cache``'s
non-system message count; any drop is broadcast as a ``prune`` event so
the frontend trims the same prefix.

Buffer / replay: ``_event_buffer`` holds every persistent event since
session start (or since session resume on ``--resume <id>``). When a new
client connects, the server first replays the buffer in order, then
forwards live events from its dedicated queue. Transient events
(``stream_chunk``, ``status``, ``spinner``) are not replayed — they are
runtime UX, not state.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from queue import Empty, SimpleQueue
from typing import Any

from agent_cli.render.base import ConfirmOption, Renderer


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
        # input queue feeds prompt_user / confirm
        self._input_queue: SimpleQueue = SimpleQueue()
        # Pending assistant emission: thought() arrives before action() /
        # final(), so the thought is held until the second call so we can
        # emit a single ``assistant_turn`` event per LLM emission.
        self._pending_thought: str | None = None
        self._pending_turn: int | None = None
        # Counter for FIFO sync — server compares to context cache size
        # to determine prune drop count.
        self._persistent_count: int = 0
        # Optional workspace path piggybacks on the ``ready`` event so
        # the frontend's top bar can show "provider · model · cwd"
        # without the LLM needing to volunteer the path. Empty string
        # = field omitted from the event entirely.
        self._workspace = workspace
        # Session-info "ready" event lives in its own slot — NOT in
        # ``_event_buffer`` — so two semantics stay clean:
        # (1) new SSE connections always see the latest ready in
        #     their replay snapshot (prepended below in
        #     ``register_connection``) — fixes the "connecting…"
        #     stuck state when a client opens the page before the
        #     first chat turn.
        # (2) chat REPL re-enters AgentLoop on every user message,
        #     calling header() again. A slot avoids the buffer
        #     accumulating one ready per turn.
        self._latest_ready: tuple[str, dict[str, Any]] | None = None
        # Worker busy/idle visibility for the frontend send-button
        # gating. The chat worker thread emits ``worker_state`` on every
        # transition: ``busy=True`` right after popping a user message,
        # ``busy=False`` right before blocking on the next pop. Keeping
        # only the *latest* state in a slot (rather than appending to
        # the persistent buffer) means:
        # (1) a refreshed/reconnecting client sees the current state
        #     immediately via the snapshot prepend below, even if the
        #     worker has been busy for 30s and won't emit anything new
        #     until completion;
        # (2) the buffer doesn't accumulate one entry per turn.
        # Event payload: ``{"busy": bool}``. Server-side
        # ``_worker_loop`` is the sole writer.
        self._latest_worker_state: tuple[str, dict[str, Any]] | None = None
        # Latest per-turn token usage, cached (like worker_state) so a
        # refresh/reconnect repopulates the top-bar token readout from
        # the snapshot instead of waiting for the next turn. Emitted
        # non-persistent (live) so the buffer doesn't accumulate one
        # entry per turn — only the latest matters.
        self._latest_token_usage: tuple[str, dict[str, Any]] | None = None
        # Per-thread delegate-task routing. Worker threads spawned by
        # ``_run_parallel`` register their ``task_id`` here via
        # ``begin_delegate_task``; ``_emit`` then auto-attaches
        # ``task_id`` to every event the worker fires so the frontend
        # can route into the right collapsible group instead of
        # interleaving on the main timeline.
        self._thread_to_task: dict[int, str] = {}

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
        deliberately bare (e.g. ``input_resolved``, ``takeover``).
        """
        tid = threading.get_ident()
        task_id = self._thread_to_task.get(tid)
        if task_id is not None and "task_id" not in data:
            data = {**data, "task_id": task_id}
        with self._lock:
            if persistent:
                self._event_buffer.append((event, data))
                self._persistent_count += 1
            for conn in self._connections:
                if not conn.closed.is_set():
                    conn.queue.put((event, data))

    def register_connection(self, conn: WebConnection) -> list[tuple[str, dict]]:
        """Mark ``conn`` as the active subscriber, take over any others.

        Returns the persistent event buffer snapshot for replay. Caller
        is expected to yield the snapshot first, then loop on
        ``conn.queue`` for live events.
        """
        with self._lock:
            for old in self._connections:
                if not old.closed.is_set():
                    old.closed.set()
                    old.queue.put(("takeover", {}))
            self._connections = [conn]
            snapshot = list(self._event_buffer)
            # Prepend the latest session-info ``ready`` so a client
            # that opens the page mid-session (or before any chat)
            # populates its top bar immediately instead of staying
            # on "connecting…" until the first emission.
            if self._latest_ready is not None:
                snapshot.insert(0, self._latest_ready)
            # Also prepend the latest worker_state so a refresh or
            # reconnect lands with the correct send-button state even
            # if the worker is mid-turn (no new event would otherwise
            # fire until completion). Goes after ``ready`` so the
            # top-bar renders first, then the input affordance settles.
            if self._latest_worker_state is not None:
                snapshot.append(self._latest_worker_state)
            # Latest token usage so the top-bar readout survives a
            # refresh/reconnect instead of blanking until the next turn.
            if self._latest_token_usage is not None:
                snapshot.append(self._latest_token_usage)
            return snapshot

    def unregister_connection(self, conn: WebConnection) -> None:
        """Drop ``conn`` from the active list and signal any pending
        queue waiter to wake up.

        The sentinel put is the symmetric pair of ``register_connection``'s
        list append: register exposes the queue to writers, unregister
        signals readers to stop. Without it the SSE generator's
        executor-thread ``queue.get(timeout=15)`` would block until the
        keep-alive timer expires, leaking that thread for up to 15s
        after the client disconnects.
        """
        with self._lock:
            if conn in self._connections:
                self._connections.remove(conn)
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
        """Close every active SSE generator without sending takeover.

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
                action = msg.get("action", "") or ""
                action_input = msg.get("action_input", {})
                if action == "complete":
                    if isinstance(action_input, dict):
                        final_text = action_input.get("result", "") or ""
                    else:
                        final_text = str(action_input) if action_input else ""
                    self.thought(thought, turn=0)
                    self.final(final_text, turn=0)
                elif action:
                    self.thought(thought, turn=0)
                    if isinstance(action_input, dict):
                        tool_input = json.dumps(action_input, ensure_ascii=False)
                    else:
                        tool_input = str(action_input)
                    self.action(action, tool_input, turn=0)

    def prune(self, drop: int) -> None:
        """Drop the ``drop`` oldest persistent events from the buffer and
        notify clients so they trim the same prefix. No-op if ``drop`` is
        zero or larger than the current buffer."""
        if drop <= 0:
            return
        with self._lock:
            if drop > len(self._event_buffer):
                drop = len(self._event_buffer)
            self._event_buffer = self._event_buffer[drop:]
            self._persistent_count -= drop
        self._emit("prune", {"drop": drop}, persistent=False)

    @property
    def persistent_count(self) -> int:
        """Number of persistent events currently in the buffer.

        Server uses this to compute FIFO prune deltas vs. the live
        ``ContextManager`` cache.
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
        # Live broadcast to active connections; snapshot replay for
        # future connections happens via ``_latest_ready`` in
        # ``register_connection``.
        with self._lock:
            self._latest_ready = ("ready", payload)
        self._emit("ready", payload, persistent=False)

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
        self._pending_turn = turn
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
        self._pending_turn = None

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
        self._pending_turn = None

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

    def token_usage(self, stats: dict, turn: int, verbose: bool = False) -> None:
        """Emit the per-turn token usage for the frontend's top-bar
        readout. Raw stats go over the wire (the frontend formats); the
        latest is cached so a refresh repopulates the bar from snapshot.
        """
        payload = {**stats, "turn": turn}
        with self._lock:
            self._latest_token_usage = ("token_usage", payload)
        self._emit("token_usage", payload, persistent=False)

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
        ``pop_chat`` — through every intermediate LLM turn, tool call,
        and even any ``prompt_user`` / ``confirm`` wait. The frontend
        uses this to disable the chat ``Send`` button so the user
        doesn't queue a second message into an actively-running turn.

        Latest state is cached in ``_latest_worker_state`` so a
        refreshed / reconnected client gets the correct send-button
        state immediately via the snapshot replay, without waiting
        for the next transition.
        """
        payload = {"busy": True}
        with self._lock:
            self._latest_worker_state = ("worker_state", payload)
        self._emit("worker_state", payload, persistent=False)

    def worker_idle(self) -> None:
        """Signal that the chat worker is back at the top-level
        ``pop_chat`` and ready to accept the next user message.
        Re-enables the frontend ``Send`` button. See ``worker_busy``
        for the persistence + reconnect semantics."""
        payload = {"busy": False}
        with self._lock:
            self._latest_worker_state = ("worker_state", payload)
        self._emit("worker_state", payload, persistent=False)

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
        renderer so chat REPL teardown logic stays consistent.

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
            self._emit(
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
                persistent=False,
            )
            try:
                return self._wait_for_input()
            finally:
                self._emit("input_resolved", {}, persistent=False)

        value = self._guarded_read(_do)
        return value if value else default

    def can_prompt(self) -> bool:
        """We can prompt whenever a browser is connected to answer the
        ``input_required`` event — no TTY needed; the SSE + ``/api/input``
        channel carries it. Returns ``False`` when nothing is connected, so
        an interactive prompt is refused / defaulted with a clear path
        rather than blocking on an answer no one can give."""
        with self._lock:
            return any(not c.closed.is_set() for c in self._connections)

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
            self._emit(
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
                persistent=False,
            )
            try:
                return self._wait_for_input()
            finally:
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
