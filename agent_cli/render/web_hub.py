"""EventHub — 웹 뷰어 연결 허브 (v10.115.0, ``WebRenderer`` 에서 분리).

허브가 가진 것: SSE 구독자 목록, 재접속 재생 버퍼(+누적 수), 스티키 레지스트리,
뷰어 별명. 하는 일: 팬아웃, 새 연결의 스냅샷 조립, 연결 해제·종료.

허브가 모르는 것: 이벤트의 모양(``task_id``·``note``·``hidx``·``ts`` 부착과
직렬화는 렌더러 ``_emit`` 이 한 번 해서 넘긴다), 스코프, 재생, ``status.json``.
뷰어 수가 바뀌면 생성 인자 ``on_viewers_changed`` 를 부를 뿐이고 — 렌더러가 거기에
``_publish_status`` 를 건다 — 스티키 변화가 status 를 뒤집는지도 렌더러 쪽 정책이다.

동시성: 허브 상태는 전부 허브의 ``_lock`` 하나 아래. 콜백과 센티널 put 은 락 밖
(콜백은 디스크 I/O 를 한다). 렌더러의 ``_lock`` 과 중첩해 잡는 자리는 없다.
"""

from __future__ import annotations

import random
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from queue import SimpleQueue
from typing import Any

# Replay-buffer bound (persistent events). ~3-4 persistent events per turn →
# roughly a thousand turns of transcript; beyond that a reconnecting client
# gets a ``transcript_truncated`` notice + the most recent window. Live
# delivery is unaffected (connected clients received everything as it
# happened); only the mid-session reconnect replay is windowed. Full history
# survives on disk (history.jsonl → --resume replays from there).
_EVENT_BUFFER_MAX = 5000


class _JsonReady(dict):
    """A fan-out payload dict carrying its own serialized form.

    ``WebRenderer._emit`` serializes each event ONCE at the single fan-out
    point and caches the string here; the SSE generator (one per viewer) sends
    the cached string instead of re-``json.dumps``-ing the same dict per viewer
    per event — and buffer replay on reconnect reuses it too. Still a real
    dict, so tests and in-process consumers read fields normally.
    """

    __slots__ = ("json_str",)


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

# Sentinel event used by ``EventHub.unregister`` / ``shutdown_all`` to wake up
# the SSE generator's executor-thread queue wait. Pattern-matched in
# ``WebServer.stream_events`` as a stream-end signal — never serialised to a
# client.
_CLOSE_SENTINEL: tuple[str, dict] = ("__close__", {})


@dataclass
class WebConnection:
    """One active SSE subscriber. Server creates one per HTTP connection."""

    id: str
    queue: SimpleQueue = field(default_factory=SimpleQueue)
    closed: threading.Event = field(default_factory=threading.Event)


class EventHub:
    """연결·재생 버퍼·스티키·별명을 가진 팬아웃 허브.

    Every connection is equal: all receive the fan-out AND may send input /
    queue messages (no controller/observer split). ``_connections`` is
    append-order = oldest-first (snapshot fan-out).
    """

    def __init__(self, *, on_viewers_changed: Callable[[], None] | None = None) -> None:
        self._lock = threading.Lock()
        self._connections: list[WebConnection] = []
        # (event_name, data) — bounded deque; the oldest events fall off past
        # _EVENT_BUFFER_MAX (reconnect replay is windowed, disk history is
        # complete). _persistent_count keeps the TOTAL ever published so
        # register can report the gap.
        self._event_buffer: deque[tuple[str, dict[str, Any]]] = deque(
            maxlen=_EVENT_BUFFER_MAX
        )
        self._persistent_count = 0
        # ── Sticky state registry ───────────────────────────────────
        # A "sticky" state is a single server value that is (a) broadcast live
        # to connected clients AND (b) replayed into each NEW connection's
        # snapshot — so a late / refreshed / second-browser client sees the
        # last value (plain SSE only reaches clients connected at emit time).
        # ``set_sticky`` does both; ``register`` walks this dict to rebuild the
        # snapshot. ``position``: ``ready`` goes FIRST (the top-bar must
        # populate before everything), the rest append. Live only — the buffer
        # keeps history, the slot keeps "latest". NOT sticky: viewers
        # (connection-set-derived) / prompt snapshots (per-scope, on-demand).
        # name → {"event", "payload", "position"}.
        self._sticky: dict[str, dict[str, Any]] = {}
        # conn_id → fun nickname (assigned on register, shown to all viewers)
        self._nicknames: dict[str, str] = {}
        self._on_viewers_changed = on_viewers_changed

    # ─── 팬아웃 ───────────────────────────────────────────

    def _publish_locked(self, event: str, data: dict, persistent: bool) -> None:
        if persistent:
            self._event_buffer.append((event, data))
            self._persistent_count += 1
        for conn in self._connections:
            if not conn.closed.is_set():
                conn.queue.put((event, data))

    def publish(self, event: str, data: _JsonReady, *, persistent: bool) -> None:
        """``(event, data)`` 를 열린 연결 전부의 큐에 넣고, ``persistent`` 면 재생
        버퍼에도 쌓는다. ``data`` 는 호출자가 모양을 다 만들고 직렬화까지 한 것."""
        with self._lock:
            self._publish_locked(event, data, persistent)

    def drop_buffered(self, predicate: Callable[[str, dict], bool]) -> int:
        """재생 버퍼에서 ``predicate(event, data)`` 가 참인 항목을 빼고 그 수를
        돌려준다. 누적 수도 같은 만큼 줄여 ``transcript_truncated`` 의 omitted 계산이
        어긋나지 않게 한다 (teammate kill 정리 — ``clear_agent_conversation``)."""
        with self._lock:
            kept: deque[tuple[str, dict[str, Any]]] = deque(
                maxlen=self._event_buffer.maxlen
            )
            removed = 0
            for event, data in self._event_buffer:
                if predicate(event, data):
                    removed += 1
                    continue
                kept.append((event, data))
            self._event_buffer = kept
            self._persistent_count -= removed
        return removed

    # ─── 스티키 ───────────────────────────────────────────

    def set_sticky(
        self,
        name: str,
        event: str,
        payload: dict[str, Any],
        *,
        live: _JsonReady,
        position: str = "append",
    ) -> None:
        """슬롯 ``name`` 을 ``(event, payload)`` 로 두고 ``live`` 를 라이브로 보낸다
        (비영속). 슬롯엔 날 ``payload`` 가 남아 새 연결의 스냅샷에 재생되고, 라이브
        쪽은 렌더러가 모양을 만든 ``live`` 다. ``position`` ('prepend' | 'append') 은
        스냅샷 안의 자리. 저장과 팬아웃은 한 락 아래서 한다."""
        with self._lock:
            self._sticky[name] = {
                "event": event,
                "payload": payload,
                "position": position,
            }
            self._publish_locked(event, live, False)

    def clear_sticky(self, name: str) -> None:
        """Drop a sticky slot so new connections no longer replay it — e.g.
        once a pending ``input_required`` is answered, a late/reconnecting
        client must NOT be handed the already-resolved prompt."""
        with self._lock:
            self._sticky.pop(name, None)

    def sticky_payload(self, name: str) -> dict[str, Any] | None:
        """슬롯 ``name`` 의 payload (없으면 None)."""
        with self._lock:
            slot = self._sticky.get(name)
            return slot["payload"] if slot is not None else None

    def sticky_items(self, prefix: str = "") -> list[tuple[str, dict[str, Any]]]:
        """``prefix`` 로 시작하는 슬롯들의 ``(name, payload)`` — 락 아래 한 번에 뜬
        사본(슬롯 순서 = 처음 설정된 순서)."""
        with self._lock:
            return [
                (name, slot["payload"])
                for name, slot in self._sticky.items()
                if name.startswith(prefix)
            ]

    # ─── 연결 ─────────────────────────────────────────────

    def register(self, conn: WebConnection) -> list[tuple[str, dict]]:
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
            # Bounded buffer: when older events have been dropped, tell the
            # joining client HOW MANY are missing (rendered as a small notice
            # card) — the windowed replay must not read as the full story.
            omitted = self._persistent_count - len(snapshot)
            if omitted > 0:
                snapshot.insert(0, ("transcript_truncated", {"omitted": omitted}))
            # Replay sticky state into the new connection's snapshot so a
            # late/refreshed/second-browser client sees the last value of each.
            # ``prepend`` slots (ready → top-bar) go ahead of the buffer;
            # ``append`` slots (worker_state/token_usage/queue) after.
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
            # 스냅샷의 끝 (v10.8.1): 클라이언트는 여기까지 타임라인을 숨긴 채 재생하고
            # 한 번에 맨 아래로 점프한 뒤 보여 준다 — 열 때마다 카드가 쌓이며
            # 내려가는 스크롤이 보이지 않는다. 그 다음부터는 라이브 이벤트다.
            snapshot.append(("snapshot_end", {"events": len(snapshot)}))
        self._viewers_changed()  # 락 밖: 렌더러 콜백은 디스크 I/O 를 한다
        return snapshot

    def unregister(self, conn: WebConnection) -> None:
        """Drop ``conn`` and wake any pending queue waiter.

        The sentinel put is the symmetric pair of ``register``'s list append:
        register exposes the queue to writers, unregister signals readers to
        stop (else the SSE generator's ``queue.get(timeout=15)`` leaks its
        executor thread for up to 15s after disconnect).
        """
        with self._lock:
            if conn in self._connections:
                self._connections.remove(conn)
            self._nicknames.pop(conn.id, None)
            # the leaver is already removed → broadcast the decremented count
            self._broadcast_viewers_locked()
        self._viewers_changed()  # 락 밖: 렌더러 콜백은 디스크 I/O 를 한다
        conn.queue.put(_CLOSE_SENTINEL)

    def shutdown_all(self) -> None:
        """Close every active SSE generator (used on shutdown).

        Pushes the ``__close__`` sentinel into each connection's queue so the
        SSE generator's blocking executor-thread ``queue.get`` wakes up and
        breaks out of its loop. Idempotent — second call finds an empty
        connection list and no-ops.

        Called from two places: the FastAPI ``shutdown`` lifespan hook
        (uvicorn's own SIGINT path) and ``main.py``'s ``finally`` block
        (belt-and-braces). Either ordering leaves the worker thread free to
        finalise the session.
        """
        with self._lock:
            for conn in self._connections:
                if not conn.closed.is_set():
                    conn.closed.set()
                    conn.queue.put(_CLOSE_SENTINEL)
            self._connections.clear()

    def _viewers_changed(self) -> None:
        if self._on_viewers_changed is not None:
            self._on_viewers_changed()

    # ─── 별명·뷰어 ────────────────────────────────────────

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
        Caller must hold ``self._lock``."""
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

    def viewer_count(self) -> int:
        """Number of live (not-closed) browser subscribers — the same predicate
        as :meth:`has_live_connections`, as a count. Exposed via /api/health so
        a front controller (e.g. a board) can tell 'someone is watching' from
        'nobody here' before disrupting the session."""
        with self._lock:
            return sum(1 for c in self._connections if not c.closed.is_set())

    def has_live_connections(self) -> bool:
        """Whether at least one browser is still subscribed (not closed).
        The idle-reaper's primary 'someone is here' signal."""
        with self._lock:
            return any(not c.closed.is_set() for c in self._connections)

    # ─── 테스트용 읽기 ────────────────────────────────────

    @property
    def events(self) -> deque[tuple[str, dict[str, Any]]]:
        """재생 버퍼 그대로 (``(event, data)`` 의 deque)."""
        return self._event_buffer

    @property
    def persistent_count(self) -> int:
        """지금까지 publish 된 영속 이벤트의 누적 수 (버퍼에서 밀려난 것 포함)."""
        with self._lock:
            return self._persistent_count

    @property
    def connections(self) -> list[WebConnection]:
        """등록된 연결 목록의 사본."""
        with self._lock:
            return list(self._connections)
