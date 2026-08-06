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

import json
import threading
import time
from typing import ClassVar

from agent_cli.render.base import ConfirmOption
from agent_cli.render.web import WebConnection, WebRenderer
from agent_cli.web.instance_file import read_status_file


def _qget(conn, timeout=0.5):
    """Next queued (event, data) skipping the cross-cutting ``viewers``
    count broadcast (put on existing connections' queues when another
    joins/leaves)."""
    while True:
        event, data = conn.queue.get(timeout=timeout)
        if event != "viewers":
            return event, data


# ── status.json sidecar (board reads it instead of polling /api/health) ──


class TestStatusPublishing:
    """The renderer rewrites ``status.json`` on every viewer/busy/awaiting
    change (Phase 1 of replacing the board's ``/api/health`` polling)."""

    def test_no_write_without_session_dir(self, tmp_path):
        # default renderer (CLI/tests) never touches disk
        r = WebRenderer()
        r.worker_busy()
        assert read_status_file(tmp_path) is None

    def test_busy_toggle_published(self, tmp_path):
        r = WebRenderer(session_dir=str(tmp_path))
        r.worker_busy()
        assert read_status_file(tmp_path)["busy"] is True
        r.worker_idle()
        assert read_status_file(tmp_path)["busy"] is False

    def test_awaiting_toggle_published(self, tmp_path):
        r = WebRenderer(session_dir=str(tmp_path))
        r.set_sticky("input_required", "input_required", {"q": "?"})
        assert read_status_file(tmp_path)["awaiting_input"] is True
        r.clear_sticky("input_required")
        assert read_status_file(tmp_path)["awaiting_input"] is False

    def test_agent_roster_published_as_agents_summary(self, tmp_path):
        """v7.10.0: 상주 에이전트 상태가 status.json 의 additive `agents`
        필드로 — board 가 행에 🤖 칩·"에이전트 작업 중" 상태를 그리는
        데이터 소스. roster sticky 변화가 곧 status 재발행 트리거."""
        r = WebRenderer(session_dir=str(tmp_path))
        r.worker_idle()
        assert "agents" not in (read_status_file(tmp_path) or {})
        r.agent_roster(
            [
                {"key": "agt-1", "profile": "coder", "name": "ui", "state": "busy"},
                {"key": "agt-2", "profile": "reviewer", "name": "", "state": "idle"},
                {"key": "agt-3", "profile": "old", "name": "", "state": "dead"},
            ]
        )
        st = read_status_file(tmp_path)
        assert st["agents"]["alive"] == 2
        assert st["agents"]["working"] == 1
        keys = [a["key"] for a in st["agents"]["list"]]
        assert keys == ["agt-1", "agt-2"]  # dead 제외
        assert st["agents"]["list"][0]["state"] == "busy"

    def test_agents_summary_edges(self):
        """빈/None roster → None(필드 생략), 전원 dead → alive 0."""
        assert WebRenderer._agents_summary_from(None) is None
        assert WebRenderer._agents_summary_from([]) is None
        s = WebRenderer._agents_summary_from(
            [{"key": "a", "profile": "p", "name": "", "state": "dead"}]
        )
        assert s == {"alive": 0, "working": 0, "list": []}

    def test_roster_updates_refresh_status_file(self, tmp_path):
        """상태 전이(working→idle)마다 파일이 최신값으로 재발행 —
        board 가 mtime 감시로 즉시 반영하는 계약의 전제."""
        r = WebRenderer(session_dir=str(tmp_path))
        roster = [{"key": "a", "profile": "coder", "name": "", "state": "busy"}]
        r.agent_roster(roster)
        assert read_status_file(tmp_path)["agents"]["working"] == 1
        r.agent_roster([{**roster[0], "state": "idle"}])
        st = read_status_file(tmp_path)["agents"]
        assert st["working"] == 0 and st["alive"] == 1

    def test_viewers_tracked_on_register_and_unregister(self, tmp_path):
        r = WebRenderer(session_dir=str(tmp_path))
        c1, c2 = WebConnection(id="c1"), WebConnection(id="c2")
        r.register_connection(c1)
        assert read_status_file(tmp_path)["viewers"] == 1
        r.register_connection(c2)
        assert read_status_file(tmp_path)["viewers"] == 2
        r.unregister_connection(c1)
        assert read_status_file(tmp_path)["viewers"] == 1

    def test_unrelated_sticky_does_not_flip_status(self, tmp_path):
        # only worker_state/input_required stickies republish
        r = WebRenderer(session_dir=str(tmp_path))
        r.worker_busy()  # seed a file
        r.set_sticky("token_usage", "token_usage", {"n": 5})
        assert read_status_file(tmp_path)["busy"] is True  # unchanged by token_usage


class TestActiveTurns:
    """A1(v7.29.0): 병렬 모드의 동시 inflight 턴 수. ``TurnRegistry.on_change``
    가 :meth:`WebRenderer.set_active_turns` 로 밀어 넣는 값이며, 소비자가 셋이고
    **각자 다른 질문**에 답한다:

      - sticky ``worker_state.busy`` = 프런트 Send 게이팅 → 병렬에서는 켜면 안
        된다("돌고 있어도 더 보낼 수 있다"가 기능의 요점).
      - :meth:`worker_is_busy` = idle self-reap 판정 → 활성/대기 턴을 **포함해야**
        한다. 아니면 뷰어 없는 세션이 턴 중간에 거둬진다.
      - ``status.json`` = 보드 표시 → 0 이면 필드 생략(직렬 세션 바이트 보존).
    """

    def test_serial_session_never_reports_turns(self, tmp_path):
        """직렬 경로는 이 메서드를 부르지 않는다 — status.json 이 종전 그대로."""
        r = WebRenderer(session_dir=str(tmp_path))
        r.worker_busy()
        assert "active_turns" not in read_status_file(tmp_path)

    def test_active_turns_published_and_omitted_at_zero(self, tmp_path):
        r = WebRenderer(session_dir=str(tmp_path))
        r.set_active_turns(2, 1)
        assert read_status_file(tmp_path)["active_turns"] == 2
        r.set_active_turns(0, 0)
        assert "active_turns" not in read_status_file(tmp_path)

    def test_status_busy_covers_active_and_pending(self, tmp_path):
        """대기분을 빼먹으면 **마지막 턴이 시작되기 전에** 세션이 reap 된다."""
        r = WebRenderer(session_dir=str(tmp_path))
        r.set_active_turns(0, 2)  # cap 에 막혀 아직 아무것도 안 돎
        assert read_status_file(tmp_path)["busy"] is True
        r.set_active_turns(0, 0)
        assert read_status_file(tmp_path)["busy"] is False

    def test_worker_is_busy_counts_turns(self):
        r = WebRenderer()
        assert r.worker_is_busy() is False
        r.set_active_turns(1, 0)
        assert r.worker_is_busy() is True
        r.set_active_turns(0, 3)  # 대기만 남아도 일은 남았다
        assert r.worker_is_busy() is True
        r.set_active_turns(0, 0)
        assert r.worker_is_busy() is False

    def test_worker_busy_flag_is_independent_of_turns(self):
        """직렬 busy 플래그와 병렬 턴 수는 서로를 지우지 않는다."""
        r = WebRenderer()
        r.worker_busy()
        r.set_active_turns(0, 0)
        assert r.worker_is_busy() is True  # 직렬 busy 가 살아 있다
        r.worker_idle()
        assert r.worker_is_busy() is False

    def test_send_gating_stays_open_while_turns_run(self):
        """sticky ``busy`` 는 False 여야 한다 — 켜면 프런트 Send 가 잠겨
        '돌고 있어도 더 보낼 수 있다'는 병렬의 요점이 사라진다."""
        r = WebRenderer()
        conn = WebConnection(id="c1")
        r.register_connection(conn)
        r.set_active_turns(3, 2)
        event, data = _qget(conn)
        assert event == "worker_state"
        assert data["busy"] is False
        assert data["active_turns"] == 3
        assert data["pending_turns"] == 2

    def test_late_viewer_replays_the_turn_counts(self):
        """sticky 슬롯이라 새로고침한 클라이언트도 즉시 현재 수를 본다."""
        r = WebRenderer()
        r.set_active_turns(2, 0)
        snapshot = r.register_connection(WebConnection(id="late"))
        state = [d for e, d in snapshot if e == "worker_state"]
        assert state and state[-1]["active_turns"] == 2

    def test_negative_counts_are_clamped(self):
        r = WebRenderer()
        r.set_active_turns(-5, -2)
        assert r.worker_is_busy() is False


# ── Event distribution ─────────────────────────────


class TestCanPrompt:
    """v7.8.0: ``can_prompt`` = "답이 도착할 수 **있는** 채널인가"(항상
    True) — "지금 보는 사람이 있나"가 아님. 다중 방 운용에서 사용자가
    다른 방을 보는 사이 ask/confirm 이 즉시 "(no response)"로 포기하던
    것을, 대기 + board "답변 필요" 표시 + 재접속 시 pending 질문
    replay(기존 sticky 기계) 흐름으로 교체. 뷰어 존재 여부는
    ``has_live_connections()`` 가 따로 답한다."""

    def test_true_even_without_connection(self):
        """뷰어 0명이어도 True — ask/confirm 은 대기하고, 늦게 접속한
        클라이언트가 input_required sticky replay 로 질문을 받는다."""
        r = WebRenderer()
        assert r.can_prompt() is True

    def test_true_with_open_connection(self):
        r = WebRenderer()
        r.register_connection(WebConnection(id="c1"))
        assert r.can_prompt() is True

    def test_late_viewer_gets_pending_prompt_and_can_answer(self):
        """핵심 시나리오: 뷰어 0명 상태에서 ask 대기 시작 → 나중에 접속한
        클라이언트의 snapshot 에 pending input_required 가 replay → 답을
        밀어넣으면 worker 가 그 답으로 해제."""
        r = WebRenderer()
        result: list[str] = []

        def worker():
            result.append(r.prompt_user("Q: ", context="Agent asks:\n  질문"))

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        time.sleep(0.2)
        assert t.is_alive(), "뷰어가 없어도 대기해야 한다 (즉시 포기 금지)"

        conn = WebConnection(id="late")
        snapshot = r.register_connection(conn)
        pending = [ev for ev, data in snapshot if ev == "input_required"]
        assert pending, "늦게 온 뷰어의 snapshot 에 pending 질문이 있어야 한다"

        r.push_user_input("prompt", {"content": "늦은 답"})
        t.join(timeout=2.0)
        assert result == ["늦은 답"]


class TestWebInstanceIsActive:
    """--idle-timeout 자가 종료 술어 (v7.10.0 에이전트 가드 포함).
    v7.11.1: 람다였던 조성을 web_instance_is_active 로 추출해 직접 검증."""

    class _R:
        def __init__(self, live=False, busy=False):
            self._live, self._busy = live, busy

        def has_live_connections(self):
            return self._live

        def worker_is_busy(self):
            return self._busy

    class _S:
        def __init__(self, pending=0):
            self._p = pending

        def pending_count(self):
            return self._p

    class _Reg:
        def __init__(self, active):
            self._a = active

        def any_activity(self):
            return self._a

    def test_all_quiet_is_inactive(self):
        from agent_cli.main import web_instance_is_active

        assert web_instance_is_active(self._R(), self._S(), None) is False

    def test_each_signal_alone_is_active(self):
        from agent_cli.main import web_instance_is_active

        assert web_instance_is_active(self._R(live=True), self._S(), None)
        assert web_instance_is_active(self._R(busy=True), self._S(), None)
        assert web_instance_is_active(self._R(), self._S(pending=1), None)
        # 핵심(v7.10.0): main 유휴·무접속이어도 에이전트 활동이면 활성
        assert web_instance_is_active(self._R(), self._S(), self._Reg(True))

    def test_none_registry_is_safe(self):
        """web() 의 registry 는 worker 부트스트랩이 늦게 채우는 nonlocal —
        None 인 초기 창에서 술어가 터지면 reaper 스레드가 죽는다."""
        from agent_cli.main import web_instance_is_active

        assert web_instance_is_active(self._R(), self._S(), None) is False
        assert web_instance_is_active(self._R(), self._S(), self._Reg(False)) is False


class TestDangerousShellWaitsForViewer:
    """v7.8.0: 위험 shell confirm 도 ask 와 동일 — 뷰어가 없으면 즉시
    거부하는 대신 대기하고, 늦게 접속한 사용자의 답으로 해제된다.
    (거부 응답은 모델이 승인 없이 우회하게 만들었음 — 대기=미실행이라
    보수적으로도 안전.)"""

    def test_shell_confirm_waits_and_accepts_late_answer(self, monkeypatch):
        import agent_cli.render as render_mod
        from agent_cli.tools.shell import tool_shell

        r = WebRenderer()
        monkeypatch.setattr(render_mod, "get_renderer", lambda: r)
        results = []

        def worker():
            results.append(tool_shell({"command": "rm -f ./agentcli-test-x"}))

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        time.sleep(0.3)
        assert t.is_alive(), "뷰어 0명이어도 confirm 은 대기해야 한다"

        # 늦게 접속 → snapshot 에 pending confirm replay
        conn = WebConnection(id="late")
        snapshot = r.register_connection(conn)
        kinds = [d.get("kind") for ev, d in snapshot if ev == "input_required"]
        assert "confirm" in kinds

        r.push_user_input("confirm", {"key": "n", "comment": "위험해서 거부"})
        t.join(timeout=3.0)
        assert results and results[0].success is False
        assert "denied" in (results[0].error or "").lower() or "거부" in (
            results[0].error or ""
        )

    def test_confirm_payload_carries_command_and_danger_spans(self, monkeypatch):
        """The confirm event payload includes the raw command and the
        dangerous-token ranges so the web dialog can highlight them — the web
        dialog otherwise never shows the command text."""
        import agent_cli.render as render_mod
        from agent_cli.tools.shell import tool_shell

        r = WebRenderer()
        monkeypatch.setattr(render_mod, "get_renderer", lambda: r)

        def worker():
            tool_shell({"command": "rm -rf ./agentcli-test-x"})

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        time.sleep(0.3)
        try:
            conn = WebConnection(id="late")
            snapshot = r.register_connection(conn)
            payload = next(
                d
                for ev, d in snapshot
                if ev == "input_required" and d.get("kind") == "confirm"
            )
            assert payload["command"] == "rm -rf ./agentcli-test-x"
            assert [tuple(s) for s in payload["danger_spans"]] == [(0, 2)]
        finally:
            r.push_user_input("confirm", {"key": "n", "comment": ""})
            t.join(timeout=3.0)


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


class TestTurnErrorEventName:
    """A turn/tool error must emit under the ``turn_error`` SSE event name,
    NEVER ``error``. The browser's ``EventSource`` dispatches a server-sent
    ``event: error`` message under the same "error" type as a transport
    failure, so naming this event ``error`` would fire the client's
    connection ``onerror`` handler and latch the connection dot red on a
    perfectly healthy stream (and then ``JSON.parse`` a data-less transport
    error). This contract test guards against the name collision reappearing.
    """

    def test_error_emits_turn_error_not_error(self):
        r = WebRenderer()
        conn = WebConnection(id="c1")
        r.register_connection(conn)

        r.error("boom", turn=3)

        event, data = conn.queue.get(timeout=0.5)
        assert event == "turn_error", (
            "turn errors must not reuse the reserved EventSource 'error' type"
        )
        assert event != "error"
        assert data["content"] == "boom"
        assert data["turn"] == 3
        # Persistent so a reconnecting client replays it.
        assert r.persistent_count == 1


class TestAgentConversationSurface:
    """🤝 대화창의 kill=정리 / resume=재생 대칭 (5.13): ``agent_message`` 는
    resume 재생용 ``ts`` 를 보존하고, ``clear_agent_conversation`` 은 한
    에이전트의 ``agent_msg`` 를 replay 버퍼에서 걷어내고(``omitted`` 계산이
    어긋나지 않게 count 보정) 라이브 뷰어엔 ``agent_cleared`` 를 보낸다."""

    def _agent_msgs(self, snapshot):
        return [d for (ev, d) in snapshot if ev == "agent_msg"]

    def test_agent_message_ts_preserved_for_replay(self):
        r = WebRenderer()
        r.agent_message(
            key="agt-1", direction="out", author="agt-1", text="x", ts=12345.0
        )
        conn = WebConnection(id="c1")
        snap = r.register_connection(conn)
        msg = self._agent_msgs(snap)[0]
        assert msg["ts"] == 12345.0  # 부활 순간이 아닌 원래 대화 시각

    def test_clear_removes_only_that_key_and_adjusts_count(self):
        r = WebRenderer()
        r.agent_message(key="agt-1", direction="in", author="main", text="a", seq=1)
        r.agent_message(key="agt-1", direction="out", author="agt-1", text="b", seq=1)
        r.agent_message(key="agt-2", direction="in", author="main", text="c", seq=1)
        assert r.persistent_count == 3

        r.clear_agent_conversation("agt-1")

        # 버퍼에서 agt-1 만 제거, agt-2 는 유지. count 도 제거분만큼 보정
        # (안 하면 재접속 snapshot 의 ``transcript_truncated`` omitted 가 부풀음).
        assert r.persistent_count == 1
        conn = WebConnection(id="c1")
        snap = r.register_connection(conn)
        keys = [d.get("key") for d in self._agent_msgs(snap)]
        assert keys == ["agt-2"]
        assert not any(ev == "transcript_truncated" for (ev, _d) in snap)

    def test_clear_emits_agent_cleared_live(self):
        r = WebRenderer()
        conn = WebConnection(id="c1")
        r.register_connection(conn)
        r.agent_message(key="agt-1", direction="in", author="main", text="a", seq=1)

        r.clear_agent_conversation("agt-1")

        events = []
        while not conn.queue.empty():
            events.append(conn.queue.get_nowait())
        cleared = [d for (ev, d) in events if ev == "agent_cleared"]
        assert cleared and cleared[0]["key"] == "agt-1"


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
        assert snapshot[0] == ("identity", {"conn_id": "c1", "readonly": False})
        kinds = [event for event, _ in snapshot if event not in ("identity", "viewers")]
        assert kinds == ["assistant_turn", "observation"]

    def test_pending_prompt_replays_to_reconnecting_client(self):
        # A pending ``ask``/prompt must be sticky: a client that connects WHILE
        # the worker waits (reconnect, second browser, board proxy) replays the
        # prompt — else the worker blocks on an answer the UI never offered.
        r = WebRenderer()
        r.register_connection(WebConnection(id="live"))  # a connection exists
        seen = {}

        def fake_wait():
            late = WebConnection(id="late")
            seen["events"] = [e for e, _ in r.register_connection(late)]
            return "answer"

        r._wait_for_input = fake_wait
        out = r.prompt_user("Q?", context="Agent asks:\n  무엇을?")
        assert out == "answer"
        assert "input_required" in seen["events"]  # replayed while pending

        # Resolved → a freshly connecting client must NOT replay the stale prompt.
        after = [e for e, _ in r.register_connection(WebConnection(id="after"))]
        assert "input_required" not in after

    def test_clear_sticky_removes_slot_from_snapshot(self):
        r = WebRenderer()
        r.set_sticky("input_required", "input_required", {"kind": "prompt"})
        snap = [e for e, _ in r.register_connection(WebConnection(id="a"))]
        assert "input_required" in snap
        r.clear_sticky("input_required")
        snap2 = [e for e, _ in r.register_connection(WebConnection(id="b"))]
        assert "input_required" not in snap2

    def test_all_connections_receive_the_fanout(self):
        r = WebRenderer()
        a = WebConnection(id="a")
        snap_a = r.register_connection(a)
        assert snap_a[0] == ("identity", {"conn_id": "a", "readonly": False})

        b = WebConnection(id="b")
        snap_b = r.register_connection(b)
        assert snap_b[0] == ("identity", {"conn_id": "b", "readonly": False})
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

    _STATS: ClassVar[dict] = {
        "in": 5000,
        "out": 320,
        "context_window": 262144,
        "total_out": 320,
    }

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


class TestCompaction:
    """Context-compaction lifecycle → dedicated structured SSE event the
    frontend renders as an inline conversation line."""

    def test_emits_structured_compaction_event(self):
        r = WebRenderer()
        conn = WebConnection(id="c1")
        r.register_connection(conn)
        r.compaction(phase="done", old_tokens=12400, new_tokens=5100, evicted_count=8)
        event, data = conn.queue.get(timeout=0.5)
        assert event == "compaction"
        assert data["phase"] == "done"
        assert data["old_tokens"] == 12400
        assert data["new_tokens"] == 5100
        assert data["evicted_count"] == 8

    def test_compaction_is_transient_not_buffered(self):
        """A compaction is a live timeline marker — not replayed on reconnect."""
        r = WebRenderer()
        conn = WebConnection(id="c1")
        r.register_connection(conn)
        r.compaction(phase="start", old_tokens=12400, evicted_count=8)
        assert r.persistent_count == 0

    def test_warning_phase_carries_reason(self):
        r = WebRenderer()
        conn = WebConnection(id="c1")
        r.register_connection(conn)
        r.compaction(phase="warning", reason="provider down")
        event, data = conn.queue.get(timeout=0.5)
        assert event == "compaction"
        assert data["phase"] == "warning"
        assert data["reason"] == "provider down"


class _FakeResumeCtx:
    """ctx stub for replay_from_history — returns preloaded raw messages."""

    def __init__(self, messages):
        self._messages = messages

    def get_raw_messages(self):
        return self._messages


class TestCardTimestamps:
    """Every event is server-stamped with ``ts`` at the single fan-out point
    (covers delegate/skill inner cards too); resume replay carries the
    history record's original ts instead of a fresh stamp."""

    def test_live_event_stamped_with_epoch_ts(self):
        r = WebRenderer()
        conn = WebConnection(id="c1")
        r.register_connection(conn)
        r.push_user_message("hello")
        event, data = conn.queue.get(timeout=0.5)
        assert event == "user_message"
        assert isinstance(data["ts"], float)  # epoch seconds (live path)

    def test_resume_replay_preserves_history_ts(self):
        """A resumed card must show when the step ACTUALLY happened — the
        history record's ISO ts — not the resume moment."""
        r = WebRenderer()
        conn = WebConnection(id="c1")
        r.register_connection(conn)
        iso = "2026-06-17T14:32:05.123456+00:00"
        ctx = _FakeResumeCtx([{"role": "user", "content": "hi", "ts": iso}])
        r.replay_from_history(ctx)
        event, data = conn.queue.get(timeout=0.5)
        assert event == "user_message"
        assert data["ts"] == iso  # original history ts, passed through verbatim

    def test_replay_ts_reset_after_seed(self):
        """After the seed loop, live events fall back to fresh wall-clock."""
        r = WebRenderer()
        conn = WebConnection(id="c1")
        r.register_connection(conn)
        ctx = _FakeResumeCtx(
            [{"role": "user", "content": "old", "ts": "2026-06-17T14:32:05+00:00"}]
        )
        r.replay_from_history(ctx)
        conn.queue.get(timeout=0.5)  # drain the replayed card
        r.push_user_message("new live message")
        _, data = conn.queue.get(timeout=0.5)
        assert isinstance(data["ts"], float)  # back to live epoch stamp

    def test_legacy_message_without_ts_falls_back_to_wallclock(self):
        """A pre-ts history record (no ``ts``) still gets a usable stamp."""
        r = WebRenderer()
        conn = WebConnection(id="c1")
        r.register_connection(conn)
        ctx = _FakeResumeCtx([{"role": "user", "content": "ancient"}])
        r.replay_from_history(ctx)
        _, data = conn.queue.get(timeout=0.5)
        assert isinstance(data["ts"], float)


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
        while t.is_alive() and r._input_queue.qsize() != 0 and time.time() < deadline:
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
    options: ClassVar[list] = [
        ConfirmOption(key="y", label="yes", aliases=("yes",)),
        ConfirmOption(key="n", label="no", aliases=("no",)),
    ]

    def test_confirm_drains_stale_queue_before_announcing(self):
        """announce(sticky emit) 전에 큐에 있던 답변은 정의상 stale —
        (이전 프롬프트가 해결된 뒤 도착한 잔여 클릭 등) 새 confirm 이
        그걸 즉시 소비해 자동 응답되면 안 된다 (v7.2.0 ⓓ drain)."""
        r = WebRenderer()
        r._input_queue.put(("y", "stale click"))
        result: list[tuple[str, str]] = []

        def worker():
            result.append(r.confirm("?", self.options, default_key="n"))

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        time.sleep(0.2)
        # stale 답변으로 즉시 리턴했으면 안 된다 — 아직 대기 중이어야.
        assert t.is_alive(), f"confirm consumed a stale answer: {result}"
        r.push_user_input("confirm", {"key": "n", "comment": "fresh"})
        t.join(timeout=2.0)
        assert result == [("n", "fresh")]

    def test_prompt_user_drains_stale_queue_before_announcing(self):
        r = WebRenderer()
        r._input_queue.put("stale text")
        result: list[str] = []

        def worker():
            result.append(r.prompt_user("Q: "))

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        time.sleep(0.2)
        assert t.is_alive(), f"prompt_user consumed a stale answer: {result}"
        r.push_user_input("prompt", {"content": "fresh"})
        t.join(timeout=2.0)
        assert result == ["fresh"]

    def test_awaiting_input_kind_reflects_pending_prompt(self):
        """서버 /api/input 게이트가 읽는 표면: 대기 중인 프롬프트의 kind
        (없으면 None)."""
        r = WebRenderer()
        assert r.awaiting_input_kind() is None
        r.set_sticky("input_required", "input_required", {"kind": "confirm"})
        assert r.awaiting_input_kind() == "confirm"
        r.clear_sticky("input_required")
        assert r.awaiting_input_kind() is None

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
        r.begin_scope(task_id="t-1", index=0, agent="explorer", label="find X")
        event, data = conn.queue.get(timeout=1.0)
        assert event == "scope_start"
        data.pop("ts", None)  # server-stamped emit time — not under test here
        assert data == {
            "task_id": "t-1",
            "kind": "run",
            "index": 0,
            "agent": "explorer",
            "label": "find X",
            # Nesting is on the wire: top-level scope → no parent, depth 0.
            "parent": "",
            "depth": 0,
        }
        # Persistent so a late-joining client replays the open card.
        assert any(ev == "scope_start" for (ev, _) in r._event_buffer)

    def test_end_delegate_task_emits_persistent_end_event(self):
        r = WebRenderer()
        conn = WebConnection(id="c")
        r.register_connection(conn)
        r.begin_scope(task_id="t-1", index=0, agent="", label="t")
        # Drain start.
        conn.queue.get(timeout=1.0)
        r.end_scope(task_id="t-1", success=True, duration_s=4.2)
        event, data = conn.queue.get(timeout=1.0)
        assert event == "scope_end"
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
        r.begin_scope(task_id="t-1", index=0, agent="", label="t")
        conn.queue.get(timeout=1.0)
        r.end_scope(task_id="t-1", success=False, duration_s=1.0, error="timed out")
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

        r.begin_scope(task_id="t-7", index=0, agent="", label="")
        conn.queue.get(timeout=1.0)  # drain start

        # Within the window: every emit picks up task_id.
        r._emit("observation", {"turn": 1, "content": "obs"}, persistent=True)
        _, mid = conn.queue.get(timeout=1.0)
        assert mid["task_id"] == "t-7"

        r.end_scope(task_id="t-7", success=True, duration_s=0.1)
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

        r.begin_scope(task_id="outer", index=0, agent="", label="")
        conn.queue.get(timeout=1.0)  # drain start
        # Now inside outer's thread, emit with an explicit (different)
        # task_id — must survive intact.
        r._emit(
            "scope_start",
            {"task_id": "inner", "index": 1, "agent": "", "label": ""},
            persistent=True,
        )
        _, data = conn.queue.get(timeout=1.0)
        assert data["task_id"] == "inner"

    def test_set_thread_status_emits_status_event_when_in_task(self):
        r = WebRenderer()
        conn = WebConnection(id="c")
        r.register_connection(conn)
        r.begin_scope(task_id="t-1", index=0, agent="", label="")
        conn.queue.get(timeout=1.0)  # drain start

        r.set_thread_status("reading file...")
        event, data = conn.queue.get(timeout=1.0)
        assert event == "scope_status"
        data.pop("ts", None)  # server-stamped emit time — not under test here
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
        from io import StringIO

        from rich.console import Console

        from agent_cli.render.minimal import MinimalRenderer

        r = MinimalRenderer(Console(file=StringIO(), force_terminal=False))
        # Should not raise — no-op implementations on base.
        r.begin_scope(task_id="t", index=0, agent="a", label="t")
        r.end_scope(task_id="t", success=True, duration_s=1.0)
        r.end_scope(task_id="t", success=False, duration_s=2.0, error="x")


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
        assert list(r._event_buffer) == []

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

    # ── Real ``ops`` shape (what both wire formats actually serialize) ──
    # The tests above use the legacy singular ``{action, action_input}`` shape
    # (kept working for old history files). json_fc / react store EVERY
    # assistant turn — including terminal ``complete`` — in the ``ops`` shape,
    # so these guard the path that real resumes exercise.

    def test_replays_ops_complete_as_final(self, tmp_path):
        # Regression: a terminal complete in ops shape must render its final
        # answer (previously dropped → blank assistant side on resume).
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(tmp_path / "s1", max_context_tokens=100_000)
        ctx.add(
            {
                "role": "assistant",
                "thought": "wrapping up",
                "ops": [
                    {"action": "complete", "action_input": {"result": "the answer"}}
                ],
            }
        )
        r = WebRenderer()
        r.replay_from_history(ctx)

        events = [(e, d) for e, d in r._event_buffer]
        assert [e for e, _ in events] == ["assistant_turn"]
        turn = events[0][1]
        assert turn["final"] == "the answer"
        assert turn["thought"] == "wrapping up"

    def test_replays_ops_action_call(self, tmp_path):
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(tmp_path / "s1", max_context_tokens=100_000)
        ctx.add(
            {
                "role": "assistant",
                "thought": "read it",
                "ops": [{"action": "read_file", "action_input": {"path": "y.py"}}],
            }
        )
        r = WebRenderer()
        r.replay_from_history(ctx)

        names = [e for e, _ in r._event_buffer]
        assert names == ["assistant_turn"]
        data = r._event_buffer[0][1]
        assert data["thought"] == "read it"
        assert data["action"]["tool_name"] == "read_file"
        assert json.loads(data["action"]["tool_input"]) == {"path": "y.py"}

    def test_replays_multi_op_turn_one_card_per_op(self, tmp_path):
        # A multi-op turn emits one card per op; the thought rides the first.
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(tmp_path / "s1", max_context_tokens=100_000)
        ctx.add(
            {
                "role": "assistant",
                "thought": "two reads",
                "ops": [
                    {"action": "read_file", "action_input": {"path": "a"}},
                    {"action": "read_file", "action_input": {"path": "b"}},
                ],
            }
        )
        r = WebRenderer()
        r.replay_from_history(ctx)

        cards = [d for e, d in r._event_buffer if e == "assistant_turn"]
        assert len(cards) == 2
        assert cards[0]["thought"] == "two reads"
        assert cards[1]["thought"] == ""  # thought already flushed on first op
        assert json.loads(cards[0]["action"]["tool_input"]) == {"path": "a"}
        assert json.loads(cards[1]["action"]["tool_input"]) == {"path": "b"}

    def test_replays_content_only_assistant_as_final(self, tmp_path):
        # A raw content-only assistant entry (e.g. a NO_JSON emission stored
        # verbatim) renders as a final card so the transcript isn't lost.
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(tmp_path / "s1", max_context_tokens=100_000)
        ctx.add({"role": "assistant", "content": "raw leftover text"})
        r = WebRenderer()
        r.replay_from_history(ctx)

        events = [(e, d) for e, d in r._event_buffer]
        assert [e for e, _ in events] == ["assistant_turn"]
        assert events[0][1]["final"] == "raw leftover text"


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
        data.pop("ts", None)  # server-stamped emit time — not under test here
        assert data == {"busy": True}

    def test_worker_idle_emits_busy_false(self):
        r = WebRenderer()
        conn = WebConnection(id="c")
        r.register_connection(conn)
        r.worker_idle()
        event, data = conn.queue.get(timeout=1.0)
        assert event == "worker_state"
        data.pop("ts", None)  # server-stamped emit time — not under test here
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
        assert r._sticky["worker_state"]["payload"] == {"busy": True}
        r.worker_idle()
        assert r._sticky["worker_state"]["payload"] == {"busy": False}


class TestWorkerStateReconnect:
    """The user's explicit requirement: send-button gating must
    survive a page refresh or a fresh SSE connection mid-turn. The
    server holds the latest ``worker_state`` as sticky state (like
    ``ready``) and replays it into the snapshot every new
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
        assert r._sticky["worker_state"]["payload"] == {"busy": False}


# ── Prompt Inspector per-agent scopes ──────────────


def _note_in_delegate_scope(r, *, task_id, index, agent, sections, turn):
    """Capture a system-prompt snapshot AS a delegate worker would: run
    ``begin_delegate_task`` + ``note_system_prompt`` on a fresh thread so the
    renderer's thread→task routing resolves the scope to ``task_id`` (exactly
    the path a parallel-delegate worker takes)."""

    def worker():
        r.begin_scope(task_id=task_id, index=index, agent=agent, label="t")
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
            r.begin_scope(task_id="task-A", index=0, agent="explorer", label="t")

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
            r.begin_scope(task_id="task-A", index=0, agent="explorer", label="t")
            r.note_system_prompt([("Role", "A")], turn=1)
            r.end_scope(task_id="task-A", success=True, duration_s=0.1)

        th = threading.Thread(target=worker)
        th.start()
        th.join(timeout=2.0)
        # The agent finished, but its prompt stays inspectable post-mortem.
        assert r.prompt_snapshot("task-A") is not None
        labels = {s["id"]: s["label"] for s in r.prompt_scopes()}
        assert labels.get("task-A") == "explorer·1"


# ── Nickname mid-session change ────────────────────


class TestSetNickname:
    """``set_nickname`` is callable at any time (the ✎ rename entry point in
    the UI re-invokes the same path), and each call re-broadcasts the roster
    so every client sees the updated name."""

    def test_set_nickname_rebroadcasts_updated_roster(self):
        r = WebRenderer()
        c = WebConnection(id="c1")
        r.register_connection(c)

        # First set, then a mid-session change — both return True and each
        # re-broadcasts the roster.
        assert r.set_nickname("c1", "Alice") is True
        assert r.set_nickname("c1", "Bob") is True

        # Drain the queue; the last viewers event must carry the new name.
        last = None
        while True:
            try:
                ev, data = c.queue.get_nowait()
            except Exception:
                break
            if ev == "viewers":
                last = data
        assert last is not None
        names = [v["name"] for v in last["viewers"]]
        assert "Bob" in names and "Alice" not in names


class TestStickyState:
    """Sticky state = a single server value broadcast live AND replayed into
    each new connection's snapshot (so a late/refreshed client sees the last
    value). ready/worker_state/token_usage/queue all share this.
    Pins the snapshot ORDER invariant (ready prepends; others append) across
    the set_sticky refactor."""

    def _events(self, snapshot):
        return [e for e, _ in snapshot]

    def test_ready_prepends_others_append(self):
        r = WebRenderer()
        r.header(provider="openai", model="m", max_turns=0)  # ready
        r.worker_busy()  # worker_state
        r.token_usage({"in": 10, "out": 5}, turn=1)  # token_usage
        r.queue_state([{"id": "1", "nickname": "n", "text": "t"}])  # queue
        snap = r.register_connection(WebConnection(id="c1"))
        ev = self._events(snap)
        # identity first, then ready (prepended), worker/token/queue appended
        assert ev[0] == "identity"
        assert ev[1] == "ready"  # ready prepends ahead of appended slots
        assert "worker_state" in ev and "token_usage" in ev and "queue" in ev
        # all appended-after the buffer, before viewers tail
        assert ev.index("worker_state") > ev.index("ready")

    def test_latest_value_wins(self):
        r = WebRenderer()
        r.worker_busy()
        r.worker_idle()  # later value
        snap = r.register_connection(WebConnection(id="c1"))
        ws = [d for e, d in snap if e == "worker_state"]
        assert len(ws) == 1 and ws[0] == {"busy": False}  # only the latest

    def test_reconnect_sees_state(self):
        r = WebRenderer()
        r.token_usage({"in": 42, "out": 1}, turn=3)
        # a brand-new connection (reconnect/refresh) must see the cached value
        snap = r.register_connection(WebConnection(id="late"))
        tok = [d for e, d in snap if e == "token_usage"]
        assert tok and tok[0]["in"] == 42


class TestDirectivesDirtyFlag:
    def test_mark_consume_clears(self):
        from agent_cli.render.web import WebRenderer

        r = WebRenderer()
        assert r.consume_directives_dirty() is False
        r.mark_directives_dirty()
        assert r.consume_directives_dirty() is True
        assert r.consume_directives_dirty() is False  # cleared after read

    def test_render_module_dispatch(self):
        from agent_cli import render
        from agent_cli.render.web import WebRenderer

        prev = render.get_renderer()
        try:
            r = WebRenderer()
            render.set_renderer(r)
            r.mark_directives_dirty()
            assert render.consume_directives_reload() is True
            assert render.consume_directives_reload() is False
        finally:
            render.set_renderer(prev)

    def test_applied_broadcast_reaches_viewers(self):
        # update-when-applied: notify_directives_applied → SSE event so open
        # inspectors re-fetch the prompt view at the moment it takes effect.
        from agent_cli import render
        from agent_cli.render.web import WebConnection, WebRenderer

        prev = render.get_renderer()
        try:
            r = WebRenderer()
            render.set_renderer(r)
            conn = WebConnection(id="c1")
            r.register_connection(conn)
            render.notify_directives_applied()
            assert _qget(conn)[0] == "directives_changed"
        finally:
            render.set_renderer(prev)

    def test_memory_applied_broadcast_reaches_viewers(self):
        # A `memory` op → notify_memory_applied → SSE event so open inspectors
        # re-fetch the prompt view (## Session Memory index) live.
        from agent_cli import render
        from agent_cli.render.web import WebConnection, WebRenderer

        prev = render.get_renderer()
        try:
            r = WebRenderer()
            render.set_renderer(r)
            conn = WebConnection(id="c1")
            r.register_connection(conn)
            render.notify_memory_applied()
            assert _qget(conn)[0] == "memory_changed"
        finally:
            render.set_renderer(prev)


# ── Bounded replay buffer + one-shot serialization (B2/B3/B4 web bundle) ──


class TestBoundedEventBuffer:
    """``_event_buffer`` is a ``deque(maxlen=_EVENT_BUFFER_MAX)`` — long
    sessions stop growing memory, and a reconnecting client gets a
    ``transcript_truncated`` notice ahead of the windowed replay."""

    def _small_buffer_renderer(self, monkeypatch, maxlen):
        import agent_cli.render.web as web_mod

        monkeypatch.setattr(web_mod, "_EVENT_BUFFER_MAX", maxlen)
        return WebRenderer()

    def test_buffer_capped_and_truncation_notice(self, monkeypatch):
        r = self._small_buffer_renderer(monkeypatch, maxlen=5)
        for i in range(8):
            r.push_user_message(f"m{i}")  # persistent event per call
        assert len(r._event_buffer) == 5  # oldest 3 fell off
        assert r.persistent_count == 8  # total ever emitted survives

        conn = WebConnection(id="c1")
        snapshot = r.register_connection(conn)
        events = [e for e, _ in snapshot]
        assert "transcript_truncated" in events
        idx = events.index("transcript_truncated")
        payload = snapshot[idx][1]
        assert payload["omitted"] == 3
        # identity always first; notice precedes the replayed window
        assert events[0] == "identity"
        assert idx < events.index("user_message")
        # windowed replay holds ONLY the newest 5
        replayed = [d for e, d in snapshot if e == "user_message"]
        assert [d["content"] for d in replayed] == [f"m{i}" for i in range(3, 8)]

    def test_no_notice_under_cap(self, monkeypatch):
        r = self._small_buffer_renderer(monkeypatch, maxlen=5)
        r.push_user_message("only one")
        conn = WebConnection(id="c1")
        snapshot = r.register_connection(conn)
        assert "transcript_truncated" not in [e for e, _ in snapshot]


class TestEmitSerializesOnce:
    """``_emit`` dumps each payload ONCE and caches it (``json_str``); the
    SSE generator and reconnect replay reuse the cached string instead of
    re-serializing per viewer per event. Payloads stay real dicts."""

    def test_queue_payload_carries_cached_json(self):
        r = WebRenderer()
        conn = WebConnection(id="c1")
        r.register_connection(conn)
        r.push_user_message("hello")
        event, data = _qget(conn)
        assert event == "user_message"
        assert isinstance(data, dict) and data["content"] == "hello"
        cached = getattr(data, "json_str", None)
        assert isinstance(cached, str)
        assert json.loads(cached) == dict(data)  # cache matches the dict

    def test_buffer_replay_reuses_cached_json(self):
        r = WebRenderer()
        r.push_user_message("hello")
        conn = WebConnection(id="c1")
        snapshot = r.register_connection(conn)
        replayed = [d for e, d in snapshot if e == "user_message"]
        assert len(replayed) == 1
        assert isinstance(getattr(replayed[0], "json_str", None), str)

    def test_synthetic_snapshot_entries_are_plain_dicts(self):
        # identity / viewers are per-connection synthetics — no cache needed;
        # the server falls back to json.dumps for them (a handful per connect).
        r = WebRenderer()
        conn = WebConnection(id="c1")
        snapshot = r.register_connection(conn)
        ident = next(d for e, d in snapshot if e == "identity")
        assert getattr(ident, "json_str", None) is None


# ── v4.52.0: 스코프 스택 + 서브-스코프 동적 컨텍스트 ─────────────────


class TestPromptScopeStack:
    def _ctx(self, tmp_path, marker):
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(tmp_path / marker, max_context_tokens=10_000)
        ctx.add({"role": "user", "content": f"질문-{marker}"})
        return ctx

    def test_skill_scope_no_longer_clobbers_main(self):
        # 회귀 가드: skill(호출자 스레드 중첩)의 시스템 스냅샷이 main 을
        # 덮던 동작 — 스코프 push 후에는 자기 스코프에 저장된다.
        r = WebRenderer()
        r.note_system_prompt([("Base", "MAIN PROMPT")], turn=1)
        r.begin_prompt_scope("skill-plan-abc", label="skill:plan")
        r.note_system_prompt([("Base", "SKILL PROMPT")], turn=1)
        r.end_prompt_scope("skill-plan-abc")
        assert "MAIN PROMPT" in r.prompt_snapshot("")["sections"][0]["text"]
        assert (
            "SKILL PROMPT" in r.prompt_snapshot("skill-plan-abc")["sections"][0]["text"]
        )
        # 칩 목록에 skill 라벨 등장
        labels = [sc["label"] for sc in r.prompt_scopes()]
        assert "skill:plan" in labels

    def test_nested_delegate_then_skill_resolves_top(self):
        r = WebRenderer()
        r.begin_scope(task_id="delegate-1-x", index=0, agent="explorer", label="t")
        r.begin_prompt_scope("skill-opt-1", label="skill:opt")
        r.note_system_prompt([("Base", "NESTED")], turn=1)
        r.end_prompt_scope("skill-opt-1")
        r.note_system_prompt([("Base", "AGENT")], turn=1)
        r.end_scope(task_id="delegate-1-x", success=True, duration_s=0.1)
        assert "NESTED" in r.prompt_snapshot("skill-opt-1")["sections"][0]["text"]
        assert "AGENT" in r.prompt_snapshot("delegate-1-x")["sections"][0]["text"]

    def test_scope_ctx_live_then_frozen(self, tmp_path):
        r = WebRenderer()
        ctx = self._ctx(tmp_path, "s1")
        r.begin_prompt_scope("skill-x-1", label="skill:x")
        r.note_scope_ctx(ctx)
        live = r.scope_dynamic_sections("skill-x-1")
        assert live and any("질문-s1" in sec.get("text", "") for sec in live)
        # live 반영: ctx 에 추가되면 즉시 보임
        ctx.add({"role": "user", "content": "추가-관찰"})
        assert any(
            "추가-관찰" in sec.get("text", "")
            for sec in r.scope_dynamic_sections("skill-x-1")
        )
        r.end_prompt_scope("skill-x-1")
        # 고정 스냅샷으로 전환(live 참조 해제) — 여전히 조회 가능
        assert "skill-x-1" not in r._scope_ctxs
        frozen = r.scope_dynamic_sections("skill-x-1")
        assert any("추가-관찰" in sec.get("text", "") for sec in frozen)

    def test_main_scope_ctx_is_ignored(self, tmp_path):
        r = WebRenderer()
        r.note_scope_ctx(self._ctx(tmp_path, "m"))  # 스코프 없음 = main
        assert r._scope_ctxs == {}

    def test_delete_scope_cleans_dynamic_stores(self, tmp_path):
        r = WebRenderer()
        r.begin_prompt_scope("skill-y-1", label="skill:y")
        r.note_scope_ctx(self._ctx(tmp_path, "y"))
        r.note_system_prompt([("Base", "Y")], turn=1)
        r.end_prompt_scope("skill-y-1")
        assert r.delete_prompt_scope("skill-y-1") is True
        assert r.scope_dynamic_sections("skill-y-1") == []
        assert r.prompt_snapshot("skill-y-1") is None


class TestTeammateWork:
    """teammate P1: 요청별 SSE 라우팅(begin/end_agent_work)은 delegate
    카드 이벤트를 재사용하되 프롬프트 스코프는 건드리지 않는다 — 스코프는
    worker 의 begin_prompt_scope(key) 상시 소유."""

    def test_work_emits_delegate_card_events(self):
        r = WebRenderer()
        r.begin_agent_work(key="agt-1", seq=2, profile="researcher", message="dig")
        r.end_agent_work(key="agt-1", seq=2, success=True, duration_s=0.3)
        names = [e for e, _ in r._event_buffer]
        assert "scope_start" in names and "scope_end" in names
        start = next(d for e, d in r._event_buffer if e == "scope_start")
        assert start["task_id"] == "agt-1#2"
        assert "researcher" in start["agent"]
        end = next(d for e, d in r._event_buffer if e == "scope_end")
        assert end["task_id"] == "agt-1#2" and end["success"] is True

    def test_work_routes_thread_without_touching_prompt_scope(self, tmp_path):
        import threading

        r = WebRenderer()
        tid = threading.get_ident()
        # worker 가 상시 스코프를 이미 보유한 상태를 재현
        r.begin_prompt_scope("agt-9", label="teammate:anon")
        r.begin_agent_work(key="agt-9", seq=1, profile="", message="m")
        assert r._thread_to_task[tid] == "agt-9#1"  # SSE 는 요청 카드로
        r.note_system_prompt([("Base", "TM PROMPT")], turn=1)
        r.end_agent_work(key="agt-9", seq=1, success=True, duration_s=0.1)
        assert tid not in r._thread_to_task
        # 시스템 스냅샷은 요청 카드가 아니라 상시 teammate 스코프에 쌓였다
        assert "TM PROMPT" in r.prompt_snapshot("agt-9")["sections"][0]["text"]
        assert r.prompt_snapshot("agt-9#1") is None
        # 스코프는 end_agent_work 이후에도 살아 있다 (kill 때만 고정)
        labels = [sc["label"] for sc in r.prompt_scopes()]
        assert "teammate:anon" in labels
        r.end_prompt_scope("agt-9")


class TestTeammateWindowEvents:
    """P4: roster 는 sticky(재접속 복원), 대화 메시지는 persistent(replay)."""

    def test_roster_sticky_replayed_on_reconnect(self):
        # sticky 계약: register_connection 의 snapshot 에 최신 roster 포함.
        r = WebRenderer()
        r.agent_roster([{"key": "agt-1", "profile": "res", "state": "idle"}])
        r.agent_roster([{"key": "agt-1", "profile": "res", "state": "busy"}])
        snap = r.register_connection(WebConnection(id="late"))
        roster_evs = [d for (e, d) in snap if e == "agent_roster"]
        assert len(roster_evs) == 1  # latest wins (슬롯 1개)
        assert roster_evs[0]["roster"][0]["state"] == "busy"

    def test_message_is_persistent(self):
        r = WebRenderer()
        r.agent_message(key="agt-1", direction="out", author="agt-1", text="hi", seq=1)
        names = [e for e, _ in r._event_buffer]
        assert "agent_msg" in names
        data = next(d for e, d in r._event_buffer if e == "agent_msg")
        assert data["direction"] == "out" and data["text"] == "hi"


class TestScopePersistenceAndReplay:
    """Team-swimlane resume: scope_start/scope_end have no other on-disk
    source (unlike agent_msg→conversation.jsonl), so they are logged to a
    ``scopes.jsonl`` sidecar and re-emitted on resume to restore the
    activity bars. Live emissions log; replays must not re-append."""

    def test_scope_events_logged_to_sidecar(self, tmp_path):
        r = WebRenderer(session_dir=str(tmp_path))
        r.begin_scope(task_id="sk1", kind="skill", label="plan")
        r.end_scope(task_id="sk1", kind="skill", success=True, duration_s=1.0)
        path = tmp_path / "scopes.jsonl"
        assert path.exists()
        recs = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
        assert [x["event"] for x in recs] == ["scope_start", "scope_end"]
        assert recs[0]["task_id"] == "sk1" and recs[0]["kind"] == "skill"
        assert "ts" in recs[0]  # timestamp captured for original-time replay

    def test_agent_work_scopes_logged(self, tmp_path):
        r = WebRenderer(session_dir=str(tmp_path))
        r.begin_agent_work(key="w1", seq=0, profile="code-writer", message="do")
        r.end_agent_work(key="w1", seq=0, success=True, duration_s=0.5)
        recs = [
            json.loads(x)
            for x in (tmp_path / "scopes.jsonl").read_text().splitlines()
            if x.strip()
        ]
        assert [x["event"] for x in recs] == ["scope_start", "scope_end"]
        assert recs[0]["task_id"] == "w1#0"

    def test_no_sidecar_without_session_dir(self):
        r = WebRenderer()  # no session_dir → logging off, no crash
        assert r._scope_log_path is None
        r.begin_scope(task_id="sk1", kind="skill", label="plan")  # must not raise

    def test_replay_reemits_with_original_ts_and_flag(self, tmp_path):
        r1 = WebRenderer(session_dir=str(tmp_path))
        r1.begin_agent_work(key="w1", seq=0, profile="code-writer", message="do")
        r1.end_agent_work(key="w1", seq=0, success=True, duration_s=0.5)
        original_ts = next(
            json.loads(x)
            for x in (tmp_path / "scopes.jsonl").read_text().splitlines()
            if x.strip()
        )["ts"]

        # Fresh process (empty buffer) → replay restores the scope events.
        r2 = WebRenderer(session_dir=str(tmp_path))
        r2.replay_scopes()
        starts = [d for e, d in r2._event_buffer if e == "scope_start"]
        assert starts and starts[0]["task_id"] == "w1#0"
        assert starts[0]["replay"] is True  # frontend skips timeline card
        assert starts[0]["ts"] == original_ts  # original time, not resume moment

    def test_replay_noop_without_sidecar(self, tmp_path):
        r = WebRenderer(session_dir=str(tmp_path))  # no scopes.jsonl yet
        r.replay_scopes()
        assert len(r._event_buffer) == 0  # pre-feature/fresh session → unchanged

    def test_replay_synthesizes_end_for_open_scope(self, tmp_path):
        # Process died mid-scope: scope_start logged, no scope_end.
        r1 = WebRenderer(session_dir=str(tmp_path))
        r1.begin_scope(task_id="sk1", kind="skill", label="plan")
        r2 = WebRenderer(session_dir=str(tmp_path))
        r2.replay_scopes()
        evs = [e for e, _ in r2._event_buffer]
        assert evs.count("scope_start") == 1
        assert evs.count("scope_end") == 1  # synthesized so bar isn't perpetual

    def test_replay_does_not_reappend_to_sidecar(self, tmp_path):
        r1 = WebRenderer(session_dir=str(tmp_path))
        r1.begin_scope(task_id="sk1", kind="skill", label="plan")
        r1.end_scope(task_id="sk1", kind="skill")
        path = tmp_path / "scopes.jsonl"
        n_before = len([x for x in path.read_text().splitlines() if x.strip()])
        r2 = WebRenderer(session_dir=str(tmp_path))
        r2.replay_scopes()
        n_after = len([x for x in path.read_text().splitlines() if x.strip()])
        assert n_after == n_before  # replay reads, never re-logs


class TestScopeNesting:
    """``scope_start`` carries the ENCLOSING scope + nesting depth, so both web
    surfaces can show containment (swimlane slot, nested timeline card). Before
    this, a nested skill was drawn at the same x/width as its parent — i.e.
    invisible — and its card landed at the timeline root as a sibling.

    Parent comes off the calling thread's scope stack; a cross-thread worker
    passes it explicitly (its own stack is empty). Depth is always derived from
    the parent's depth, never from the caller.
    """

    @staticmethod
    def _starts(r):
        return [d for e, d in r._event_buffer if e == "scope_start"]

    def test_same_thread_nesting_derives_parent_and_depth(self):
        r = WebRenderer()
        r.begin_scope(task_id="outer", kind="skill", label="skill:plan")
        r.begin_scope(task_id="inner", kind="skill", label="skill:create-team")
        r.begin_scope(task_id="run", kind="run", label="agent:explorer")
        outer, inner, run = self._starts(r)
        assert (outer["parent"], outer["depth"]) == ("", 0)
        assert (inner["parent"], inner["depth"]) == ("outer", 1)
        assert (run["parent"], run["depth"]) == ("inner", 2)

    def test_sequential_siblings_share_depth(self):
        """Skill/run block their caller, so a second child opened AFTER the
        first closed is a sibling at the same depth — not a grandchild."""
        r = WebRenderer()
        r.begin_scope(task_id="outer", kind="skill", label="skill:plan")
        r.begin_scope(task_id="a", kind="run", label="first")
        r.end_scope(task_id="a", kind="run")
        r.begin_scope(task_id="b", kind="run", label="second")
        a, b = self._starts(r)[1:]
        assert (a["parent"], a["depth"]) == ("outer", 1)
        assert (b["parent"], b["depth"]) == ("outer", 1)

    def test_explicit_parent_overrides_thread_stack(self):
        """A parallel worker runs on its own (empty-stack) thread, so the
        spawning thread hands the parent over."""
        r = WebRenderer()
        r.begin_scope(task_id="sk", kind="skill", label="skill:plan")
        depths = {}

        def worker():
            r.begin_scope(task_id="w", kind="run", label="task", parent="sk")
            depths["w"] = self._starts(r)[-1]

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        assert (depths["w"]["parent"], depths["w"]["depth"]) == ("sk", 1)

    def test_worker_thread_without_parent_is_top_level(self):
        """No parent passed and nothing on the worker's own stack → the scope
        is top-level. (Pre-nesting behaviour, unchanged.)"""
        r = WebRenderer()
        r.begin_scope(task_id="sk", kind="skill", label="skill:plan")
        seen = {}

        def worker():
            r.begin_scope(task_id="w", kind="run", label="task")
            seen["w"] = self._starts(r)[-1]

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        assert (seen["w"]["parent"], seen["w"]["depth"]) == ("", 0)

    def test_current_scope_reports_innermost_then_unwinds(self):
        r = WebRenderer()
        assert r.current_scope() == ""
        r.begin_scope(task_id="outer", kind="skill", label="o")
        assert r.current_scope() == "outer"
        r.begin_scope(task_id="inner", kind="skill", label="i")
        assert r.current_scope() == "inner"
        r.end_scope(task_id="inner", kind="skill")
        assert r.current_scope() == "outer"
        r.end_scope(task_id="outer", kind="skill")
        assert r.current_scope() == ""

    def test_current_scope_is_per_thread(self):
        r = WebRenderer()
        r.begin_scope(task_id="sk", kind="skill", label="o")
        seen = {}

        def worker():
            seen["v"] = r.current_scope()

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        assert seen["v"] == ""  # another thread's scope must not leak in
        assert r.current_scope() == "sk"

    def test_unknown_explicit_parent_still_counts_as_nested(self):
        """Defensive: an id whose scope already ended (or was never opened)
        must not collapse the child onto main's slot — that would draw it over
        an unrelated bar. One level of nesting is the safe reading."""
        r = WebRenderer()
        r.begin_scope(task_id="w", kind="run", label="t", parent="ghost")
        start = self._starts(r)[-1]
        assert (start["parent"], start["depth"]) == ("ghost", 1)

    def test_depth_entry_released_on_end(self):
        r = WebRenderer()
        r.begin_scope(task_id="sk", kind="skill", label="o")
        assert "sk" in r._scope_depths
        r.end_scope(task_id="sk", kind="skill")
        assert "sk" not in r._scope_depths  # no per-scope leak over a session

    def test_reopened_sibling_id_gets_fresh_depth(self):
        """Depth must come from the CURRENT parent chain, not a stale entry."""
        r = WebRenderer()
        r.begin_scope(task_id="outer", kind="skill", label="o")
        r.begin_scope(task_id="x", kind="run", label="t")
        r.end_scope(task_id="x", kind="run")
        r.end_scope(task_id="outer", kind="skill")
        r.begin_scope(task_id="x", kind="run", label="t again")
        again = self._starts(r)[-1]
        assert (again["parent"], again["depth"]) == ("", 0)

    def test_teammate_work_is_not_nested(self):
        """A resident teammate's request goes to that agent's OWN lane (routed
        by the ``key#seq`` id), so it is never a child of the requesting
        scope — but it still carries the fields for one event shape."""
        r = WebRenderer()
        r.begin_scope(task_id="sk", kind="skill", label="skill:plan")
        r.begin_agent_work(key="w1", seq=0, profile="code-writer", message="do")
        work = self._starts(r)[-1]
        assert work["task_id"] == "w1#0"
        assert (work["parent"], work["depth"]) == ("", 0)

    def test_explicit_ts_survives_to_event_and_sidecar(self, tmp_path):
        """A parallel batch shares ONE start time so the swimlane puts its
        workers on a single row (a fork) instead of a staircase. The emit point
        must not overwrite a caller-supplied ts — live event AND resume sidecar."""
        r = WebRenderer(session_dir=str(tmp_path))
        r.begin_scope(task_id="w0", kind="run", label="a", parent="sk", ts=1234.5)
        r.begin_scope(task_id="w1", kind="run", label="b", parent="sk", ts=1234.5)
        starts = self._starts(r)
        assert [s["ts"] for s in starts] == [1234.5, 1234.5]
        recs = [
            json.loads(x)
            for x in (tmp_path / "scopes.jsonl").read_text().splitlines()
            if x.strip()
        ]
        assert [x["ts"] for x in recs if x["event"] == "scope_start"] == [
            1234.5,
            1234.5,
        ]

    def test_omitted_ts_is_server_stamped(self):
        r = WebRenderer()
        r.begin_scope(task_id="sk", kind="skill", label="o")
        assert self._starts(r)[-1]["ts"] > 0  # emit-time stamp, as before


# ── seq cursor + incremental reconnect replay ────────────────────────


class TestSeqCursor:
    """Every persistent event is stamped with a monotonic ``seq`` at the
    buffer-load point, and ``register_connection(after=…)`` replays only what
    a reconnecting client missed. Transient events are unstamped — a client's
    cursor must stay pinned to the last event replay can actually reproduce."""

    def test_persistent_events_are_stamped_gapless(self):
        r = WebRenderer()
        for i in range(4):
            r.push_user_message(f"m{i}")
        assert [d.seq for _, d in r._event_buffer] == [1, 2, 3, 4]
        assert r.last_seq == 4

    def test_transient_events_carry_no_seq(self):
        r = WebRenderer()
        conn = WebConnection(id="c1")
        r.register_connection(conn)
        r.stream_chunk("tok")
        event, data = _qget(conn)
        assert event == "stream_chunk"
        assert getattr(data, "seq", None) is None
        assert r.last_seq == 0  # transient does not advance the cursor

    def test_live_delivery_carries_the_same_seq_as_the_buffer(self):
        # The buffered entry and the fan-out payload are ONE object, so a
        # viewer that stayed connected and a viewer replaying later agree on
        # the cursor of a given event.
        r = WebRenderer()
        conn = WebConnection(id="c1")
        r.register_connection(conn)
        r.push_user_message("hello")
        _, live = _qget(conn)
        buffered = r._event_buffer[-1][1]
        assert live.seq == buffered.seq == 1

    def test_incremental_replay_returns_only_newer_events(self):
        r = WebRenderer()
        for i in range(5):
            r.push_user_message(f"m{i}")
        snap = r.register_connection(WebConnection(id="c1"), after=2)
        replayed = [d for e, d in snap if e == "user_message"]
        assert [d["content"] for d in replayed] == ["m2", "m3", "m4"]
        assert [d.seq for d in replayed] == [3, 4, 5]
        assert "replay_reset" not in [e for e, _ in snap]
        assert "transcript_truncated" not in [e for e, _ in snap]

    def test_incremental_replay_keeps_identity_and_sticky(self):
        # Latest-value state is replayed on BOTH paths: while disconnected the
        # client may have missed the newest worker_state / queue / ready.
        r = WebRenderer()
        r.push_user_message("m0")
        r.worker_busy()
        r.queue_state([{"id": "1", "nickname": "n", "conn_id": "x", "text": "t"}])
        snap = r.register_connection(WebConnection(id="c1"), after=1)
        events = [e for e, _ in snap]
        assert events[0] == "identity"
        assert "worker_state" in events and "queue" in events and "viewers" in events

    def test_boundary_cursor_replays_the_whole_buffer(self):
        r = WebRenderer()
        for i in range(3):
            r.push_user_message(f"m{i}")
        snap = r.register_connection(WebConnection(id="c1"), after=0)
        assert [d["content"] for e, d in snap if e == "user_message"] == [
            "m0",
            "m1",
            "m2",
        ]
        assert "replay_reset" not in [e for e, _ in snap]

    def test_attribution_survives_incremental_replay(self):
        # A replayed event must keep the fields the frontend routes on —
        # otherwise a resumed transcript loses which turn a card belongs to.
        r = WebRenderer()
        r.push_user_message("m0")
        r.begin_scope(task_id="t7", kind="run", label="code-analyst")
        try:
            r.final("from the sub-agent", turn=3)
        finally:
            r.end_scope(task_id="t7", kind="run")
        snap = r.register_connection(WebConnection(id="c1"), after=2)
        card = next(d for e, d in snap if e == "assistant_turn")
        assert card["task_id"] == "t7"
        assert card["turn"] == 3

    def test_no_cursor_replays_everything_without_reset(self):
        r = WebRenderer()
        r.push_user_message("m0")
        snap = r.register_connection(WebConnection(id="c1"))
        assert "replay_reset" not in [e for e, _ in snap]
        assert [d["content"] for e, d in snap if e == "user_message"] == ["m0"]

    def test_trimmed_cursor_falls_back_to_full_snapshot(self, monkeypatch):
        import agent_cli.render.web as web_mod

        monkeypatch.setattr(web_mod, "_EVENT_BUFFER_MAX", 3)
        r = WebRenderer()
        for i in range(6):
            r.push_user_message(f"m{i}")  # seq 1..6, only 4..6 survive
        snap = r.register_connection(WebConnection(id="c1"), after=1)
        events = [e for e, _ in snap]
        assert events[0] == "identity"
        assert events[1] == "replay_reset"  # ahead of every replayed event
        assert "transcript_truncated" in events
        assert [d["content"] for e, d in snap if e == "user_message"] == [
            "m3",
            "m4",
            "m5",
        ]

    def test_cursor_ahead_of_us_falls_back(self):
        # A cursor we never issued (client from a previous process whose epoch
        # somehow parsed, or a fabricated header) must not silently replay
        # nothing — that would leave the client staring at a stale transcript.
        r = WebRenderer()
        r.push_user_message("m0")
        snap = r.register_connection(WebConnection(id="c1"), after=99)
        assert "replay_reset" in [e for e, _ in snap]
        assert [d["content"] for e, d in snap if e == "user_message"] == ["m0"]

    def test_foreign_cursor_falls_back_on_an_empty_buffer(self):
        # Restart-into-empty-session: nothing to replay, but the client still
        # has the OLD session on screen and must be told to drop it.
        r = WebRenderer()
        snap = r.register_connection(WebConnection(id="c1"), after=-1)
        assert "replay_reset" in [e for e, _ in snap]

    def test_two_connections_observe_the_same_order(self):
        # Concurrent emitters (the parallel contract's per-turn threads) must
        # not be able to hand out seq in one order and buffer in another.
        r = WebRenderer()
        a, b = WebConnection(id="a"), WebConnection(id="b")
        r.register_connection(a)
        r.register_connection(b)

        def emit(tag):
            for i in range(25):
                r.push_user_message(f"{tag}{i}")

        threads = [threading.Thread(target=emit, args=(t,)) for t in ("x", "y")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        def drain(conn):
            out = []
            while True:
                try:
                    event, data = conn.queue.get(timeout=0.2)
                except Exception:
                    return out
                if event == "user_message":
                    out.append((data.seq, data["content"]))

        seen_a, seen_b = drain(a), drain(b)
        assert len(seen_a) == 50
        assert seen_a == seen_b  # same (seq, event) sequence for both viewers
        assert [s for s, _ in seen_a] == sorted(s for s, _ in seen_a)
        assert [s for s, _ in seen_a] == [d.seq for _, d in r._event_buffer]


class TestParseCursor:
    """Wire cursor → replay position. The epoch guard is what makes a restart
    (or ``--resume``) force a clean reload instead of splicing a new session's
    events onto the old transcript."""

    def test_none_and_blank_mean_no_cursor(self):
        r = WebRenderer()
        assert r.parse_cursor(None) is None
        assert r.parse_cursor("") is None
        assert r.parse_cursor("   ") is None

    def test_epoch_qualified_cursor(self):
        r = WebRenderer()
        assert r.parse_cursor(f"{r.stream_epoch}:12") == 12

    def test_bare_cursor_is_read_as_this_epoch(self):
        r = WebRenderer()
        assert r.parse_cursor("7") == 7

    def test_foreign_epoch_is_refused(self):
        r = WebRenderer()
        other = WebRenderer()
        assert r.stream_epoch != other.stream_epoch
        assert r.parse_cursor(f"{other.stream_epoch}:12") == -1

    def test_garbage_is_refused(self):
        r = WebRenderer()
        assert r.parse_cursor("not-a-number") == -1
        assert r.parse_cursor(f"{r.stream_epoch}:-3") == -1
