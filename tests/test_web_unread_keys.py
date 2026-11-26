"""웹이 싣지만 아무도 읽지 않는 키·사건 (LIST-cleanup2-3c §3) — 지금 값 고정.

프런트(JS)·보드·제품 어디도 읽지 않는 SSE payload 키, 리스너 없는 사건, 읽는 이 없는 저장 칸. 다음 커밋이 지운다.
"""

from __future__ import annotations

import threading

from agent_cli.render.web import WebRenderer
from agent_cli.render.web_hub import WebConnection


def _drain(conn) -> list[tuple[str, dict]]:
    out = []
    while not conn.queue.empty():
        out.append(conn.queue.get_nowait())
    return out


def _renderer():
    r = WebRenderer()
    conn = WebConnection(id="c")
    r.hub.register(conn)
    _drain(conn)
    return r, conn


def test_ready_carries_provider_model_turns_and_skill_fields():
    r, conn = _renderer()
    r.header("openai", "gpt-4o", 10)
    ((event, data),) = _drain(conn)
    assert event == "ready"
    assert {"provider", "model", "max_turns", "skill_name", "skill_args"} <= set(data)


def test_scope_start_carries_index_and_depth():
    r, conn = _renderer()
    r.begin_scope(task_id="t", kind="skill", label="skill:x")
    (data,) = [d for e, d in _drain(conn) if e == "scope_start"]
    assert data["index"] == 0 and data["depth"] == 0
    r.end_scope(task_id="t", kind="skill", success=True)


def test_a_scope_without_label_or_agent_is_named_scope_1():
    r, _conn = _renderer()
    r.begin_scope(task_id="t", kind="run")
    assert r.prompt_meta()["agent"] == "scope #1"
    r.end_scope(task_id="t", kind="run", success=True)


def test_stuck_sticky_carries_fires():
    r, _conn = _renderer()
    r.stale_loop({"turns_without_new": 3, "since_turn": 2, "fires": 1, "asking": False})
    assert r.hub.sticky_payload("stuck")["fires"] == 1


def test_compaction_event_carries_evicted_count():
    r, conn = _renderer()
    r.compaction(phase="start", old_tokens=1000, evicted_count=5)
    (data,) = [d for e, d in _drain(conn) if e == "compaction"]
    assert data["evicted_count"] == 5


def test_prompt_snapshot_carries_totals():
    r, _conn = _renderer()
    r.note_system_prompt([("Role", "you are an agent")], turn=1)
    snap = r.prompt_snapshot()
    assert {"total_chars", "est_tokens"} <= set(snap)


def test_events_without_a_listener_are_emitted():
    r, conn = _renderer()
    r.model_loaded("m", None)
    r.spinner_start("x")
    r.spinner_stop()
    r.stream_reset()
    assert [e for e, _ in _drain(conn)] == [
        "model_loaded",
        "spinner",
        "spinner",
        "stream_reset",
    ]


def test_thread_status_is_kept_while_capturing():
    r, _conn = _renderer()
    r.start_capture()
    r.set_thread_status("busy")
    assert r.get_thread_status(threading.get_ident()) == "busy"
    r.stop_capture()
