"""웹이 싣지 않는 키·사건 (LIST-cleanup2-3c §3) — 프런트(JS)·보드·제품 어디도 읽지 않던 SSE payload 키, 리스너 없는
사건, 읽는 이 없는 저장 칸을 지웠다. 그 자리가 비어 있음을 고정한다."""

from __future__ import annotations

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


def test_scope_start_has_no_index_or_depth():
    r, conn = _renderer()
    r.begin_scope(task_id="t", kind="skill", label="skill:x")
    (data,) = [d for e, d in _drain(conn) if e == "scope_start"]
    assert "index" not in data and "depth" not in data
    r.end_scope(task_id="t", kind="skill", success=True)


def test_a_scope_without_label_or_agent_is_named_scope():
    r, _conn = _renderer()
    r.begin_scope(task_id="t", kind="run")
    assert r.prompt_meta()["agent"] == "scope"
    r.end_scope(task_id="t", kind="run", success=True)


def test_stuck_sticky_has_no_fires():
    r, _conn = _renderer()
    r.stale_loop({"turns_without_new": 3, "since_turn": 2, "fires": 1, "asking": False})
    assert "fires" not in r.hub.sticky_payload("stuck")


def test_compaction_event_has_no_evicted_count():
    r, conn = _renderer()
    r.compaction(phase="start", old_tokens=1000, evicted_count=5)
    (data,) = [d for e, d in _drain(conn) if e == "compaction"]
    assert "evicted_count" not in data


def test_prompt_snapshot_has_no_totals():
    r, _conn = _renderer()
    r.note_system_prompt([("Role", "you are an agent")], turn=1)
    snap = r.prompt_snapshot()
    assert "total_chars" not in snap and "est_tokens" not in snap


def test_events_without_a_listener_are_not_emitted():
    r, conn = _renderer()
    r.spinner_start("x")
    r.spinner_stop()
    r.stream_reset()
    assert _drain(conn) == []


def test_the_base_renderer_keeps_no_thread_status():
    r, _conn = _renderer()
    assert not hasattr(r, "get_thread_status") and not hasattr(r, "_thread_status")
