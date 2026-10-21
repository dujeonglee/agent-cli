"""틀의 Tail 갱신 시점과 "이 호출 뒤에 닫힌 항목" (v10.63.0).

- 스냅샷이 찍히는 순간(호출 직전) 작은 ``prompt_snapshot`` 이벤트가 나간다 — 종전엔
  응답이 끝난 뒤(``token_usage``)에야 틀을 다시 가져와 생성 중엔 직전 호출의 꼬리가
  보였다.
- 런이 끝나면(어떻게 끝나든) 마지막 스냅샷의 ``open_items`` 중 이제 닫힌 id 가
  ``closed_after`` 로 붙는다. 꼬리 본문은 그대로 — 모델이 본 적 없는 꼬리를 지어내지
  않는다. `complete` 로 답한 요청이 Tail 에 열린 채 남아 미응답처럼 보이던 문제.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from agent_cli.render.web import WebConnection, WebRenderer
from agent_cli.web.server import WebServer, create_app
from tests.loop_ports import TEST_PORTS

_TAIL = [("Session State (per-turn tail)", 'turn 2/30\n     [u-1] "A"\n     [u-2] "B"')]


def _events(conn) -> list[tuple[str, dict]]:
    out = []
    while not conn.queue.empty():
        out.append(conn.queue.get_nowait())
    return out


def _prompt(renderer, task_id: str = "") -> dict:
    server = WebServer(renderer, token="t", ctx=None)
    q = f"&task_id={task_id}" if task_id else ""
    return TestClient(create_app(server)).get(f"/api/debug/prompt?token=t{q}").json()


class TestSnapshotEvent:
    def test_each_snapshot_announces_its_scope_at_call_start(self):
        r = WebRenderer()
        conn = WebConnection(id="c1")
        r.register_connection(conn)
        r.note_system_prompt([("S", "x")], turn=1, tail=_TAIL, open_items=["u-1"])
        snaps = [d for ev, d in _events(conn) if ev == "prompt_snapshot"]
        assert snaps and snaps[0]["scope"] == ""
        # 일시 이벤트 — 새로고침 재생에 쌓이지 않는다
        assert all(ev != "prompt_snapshot" for ev, _d in r._event_buffer)


class TestClosedAfter:
    def test_items_closed_after_the_call_are_listed_and_tail_is_untouched(self):
        r = WebRenderer()
        r.note_system_prompt(
            [("S", "x")], turn=2, tail=_TAIL, open_items=["u-1", "u-2"]
        )
        conn = WebConnection(id="c1")
        r.register_connection(conn)
        r.note_items_closed_after(["u-2"])
        d = _prompt(r)
        assert d["closed_after"] == ["u-1"]
        tail = [s["text"] for s in d["sections"] if s["kind"] == "tail"]
        assert tail == [_TAIL[0][1]]  # 꼬리는 그 호출에 실린 그대로
        assert any(ev == "prompt_snapshot" for ev, _d in _events(conn))

    def test_nothing_closed_sends_nothing(self):
        r = WebRenderer()
        r.note_system_prompt([("S", "x")], turn=2, tail=_TAIL, open_items=["u-1"])
        conn = WebConnection(id="c1")
        r.register_connection(conn)
        r.note_items_closed_after(["u-1"])
        assert _prompt(r)["closed_after"] == []
        assert not any(ev == "prompt_snapshot" for ev, _d in _events(conn))

    def test_no_snapshot_yet_is_a_noop(self):
        r = WebRenderer()
        r.note_items_closed_after([])
        assert r.prompt_snapshot() is None

    def test_next_call_clears_the_mark(self):
        r = WebRenderer()
        r.note_system_prompt([("S", "x")], turn=2, tail=_TAIL, open_items=["u-1"])
        r.note_items_closed_after([])
        r.note_system_prompt([("S", "x")], turn=3, tail=_TAIL, open_items=[])
        assert _prompt(r)["closed_after"] == []

    def test_scoped_by_calling_thread(self):
        """인라인 스코프의 런 끝은 그 스코프의 스냅샷에만 붙는다."""
        r = WebRenderer()
        r.note_system_prompt([("S", "main")], turn=1, tail=_TAIL, open_items=["u-1"])
        r.begin_scope(task_id="t-1", kind="skill", label="review", agent="reviewer")
        try:
            r.note_system_prompt([("S", "sub")], turn=1, tail=_TAIL, open_items=["n-1"])
            r.note_items_closed_after([])
        finally:
            r.end_scope(task_id="t-1")
        assert _prompt(r, "t-1")["closed_after"] == ["n-1"]
        assert _prompt(r)["closed_after"] == []

    def test_mark_survives_resume(self, tmp_path):
        r = WebRenderer(session_dir=str(tmp_path))
        r.note_system_prompt([("S", "x")], turn=2, tail=_TAIL, open_items=["u-1"])
        r.note_items_closed_after([])
        main = json.loads((tmp_path / "prompt_views" / "main.json").read_text())
        assert main["snapshot"]["closed_after"] == ["u-1"]
        again = WebRenderer(session_dir=str(tmp_path))
        assert again.restore_prompt_views() is True
        assert _prompt(again)["closed_after"] == ["u-1"]


class TestLoopWiring:
    def test_snapshot_carries_open_ids_and_run_end_reports_what_is_left(self):
        """실제 런: 호출 직전 스냅샷에 열린 id, `complete` 로 답한 뒤 런 끝에 빈 목록."""
        from agent_cli.dialects import get as get_dialect
        from agent_cli.loop import AgentLoop
        from agent_cli.providers.base import LLMResponse
        from agent_cli.providers.capabilities import ModelCapabilities

        loop = None

        def _answer(*_a, **_k):
            ids = loop._state.ledger.request_ids()
            return LLMResponse(
                content=json.dumps(
                    {"action": "complete", "result": "ok", "answers": ids}
                )
            )

        provider = MagicMock()
        provider.call.side_effect = _answer
        loop = AgentLoop(
            ports=TEST_PORTS,
            query="Q",
            query_request_id="u-1",
            provider=provider,
            capabilities=ModelCapabilities(
                context_window=32768, max_output_tokens=4096, supports_thinking=False
            ),
            model="m",
            dialect=get_dialect("json_fc"),
        )
        with (
            patch("agent_cli.loop.llm.render_system_prompt_snapshot") as snap,
            patch("agent_cli.loop.core.render_items_closed_after") as after,
        ):
            loop.run()
        opened = snap.call_args.kwargs["open_items"]
        assert opened == ["u-1"]
        after.assert_called_once_with([])
