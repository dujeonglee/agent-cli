"""모델 시점 뷰 (v10.6.0, docs/inspector-model-view §3.1) — 회귀 가드.

챗이 "모델이 지금 무엇을 보고 무엇을 못 보나" 를 그리려면 ContextManager 가
캐시 변화마다 사실 하나를 내야 한다: 빠진 접두사의 경계 ``gone {turn, kind}``
와 압축 요약. 여기서 고정하는 것 — ① 압축 전엔 아무것도 없다 ② 압축 뒤 경계는
**마지막으로 빠진 레코드**(turn·분류)이고 요약·턴 범위·전후 토큰이 실린다
③ FIFO 드롭(압축 꺼짐)도 경계를 낸다, 요약은 없다 ④ fold 는 경계를 바꾸지 않고
개수만 센다 ⑤ resume 은 같은 경계·요약을 복원한다(토큰은 None) ⑥ 변화마다
렌더러에 통지된다 ⑦ web 렌더러는 스코프별 sticky 로 내고 재접속에 재생한다
⑧ resume 재생 카드가 레코드의 turn 을 싣는다(종전 0).
"""

from __future__ import annotations

import json

import pytest

from agent_cli.context.manager import ContextManager
from agent_cli.render import get_renderer, set_renderer
from agent_cli.render.web import WebConnection, WebRenderer


class _CapturingRenderer(WebRenderer):
    """context_view 통지만 모은다 (web 렌더러 위에서 — base 는 추상)."""

    def __init__(self):
        super().__init__()
        self.views: list[dict] = []

    def context_view(self, view: dict) -> None:
        self.views.append(view)


@pytest.fixture
def capture(monkeypatch):
    prev = get_renderer()
    r = _CapturingRenderer()
    set_renderer(r)
    yield r
    set_renderer(prev)


def _turn(ctx, n: int, tool: str = "shell"):
    """한 턴 = assistant op + 관찰 (둘 다 turn n 으로 찍힌다)."""
    ctx.set_turn(n)
    ctx.add(
        {
            "role": "assistant",
            "thought": f"t{n}",
            "ops": [{"action": tool, "action_input": {"cmd": f"c{n}" * 40}}],
        }
    )
    ctx.add({"role": "user", "tool": tool, "success": True, "content": f"out{n} " * 40})


def _ctx(tmp_path, *, compaction=True, budget=100_000):
    ctx = ContextManager(tmp_path / "s", max_context_tokens=budget)
    if compaction:
        ctx.set_compactor(lambda messages: "SUMMARY of earlier turns")
    else:
        ctx.set_compactor(None)
    ctx.add({"role": "system", "content": "sys"})
    ctx.set_turn(0)
    ctx.add({"role": "user", "content": "do the thing"})
    for n in range(1, 7):
        _turn(ctx, n)
    return ctx


class TestContextView:
    def test_nothing_gone_before_compaction(self, tmp_path):
        ctx = _ctx(tmp_path)
        assert ctx.context_view() == {
            "gone": None,
            "summary": None,
            "compactions": 0,
            "folded_nudges": 0,
        }

    def test_compaction_sets_boundary_and_summary(self, tmp_path, capture):
        ctx = _ctx(tmp_path)
        before, after = ctx.compact_now()
        assert after < before
        view = ctx.context_view()
        # 경계 = 캐시에 남은 첫 **동적** 레코드의 서수 (맨 앞은 system 앵커, hidx 0)
        first_kept = next(
            h
            for h, m in zip(ctx._cache_hidx, ctx.get_raw_messages())
            if m.get("role") != "system"
        )
        assert first_kept > 1  # 턴 0 의 질문도 빠졌다 (앵커 아님)
        assert view["gone"] == {"hidx": first_kept}
        records = [json.loads(ln) for ln in ctx.history_path.read_text().splitlines()]
        s = view["summary"]
        assert s["text"] == "SUMMARY of earlier turns"
        assert s["turns"] == [records[1]["turn"], records[first_kept - 1]["turn"]]
        assert s["before_tokens"] == before and s["after_tokens"] == after
        assert view["compactions"] == 1 and view["folded_nudges"] == 0
        # 렌더러 통지 — 마지막 통지가 현재 뷰와 같다
        assert capture.views and capture.views[-1] == view

    def test_kept_records_are_at_or_after_the_boundary(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.compact_now()
        g = ctx.context_view()["gone"]
        kept = [
            h
            for h, m in zip(ctx._cache_hidx, ctx.get_raw_messages())
            if m.get("role") != "system"
        ]
        assert kept and min(kept) == g["hidx"]
        assert all(h >= g["hidx"] for h in kept)

    def test_second_compaction_moves_the_boundary_forward(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.compact_now()
        g1 = ctx.context_view()["gone"]
        for n in range(7, 13):
            _turn(ctx, n)
        ctx.compact_now()
        g2 = ctx.context_view()["gone"]
        assert g2["hidx"] > g1["hidx"]
        assert ctx.context_view()["compactions"] == 2

    def test_fifo_drop_sets_boundary_without_summary(self, tmp_path, capture):
        ctx = _ctx(tmp_path, compaction=False)
        n_before = len(ctx.get_raw_messages())
        ctx.ensure_within(ctx._cache_tokens // 2)
        assert len(ctx.get_raw_messages()) < n_before
        view = ctx.context_view()
        assert view["gone"] is not None and view["summary"] is None
        assert capture.views[-1] == view

    def test_fold_counts_without_moving_the_boundary(self, tmp_path, capture):
        ctx = _ctx(tmp_path)
        ctx.compact_now()
        g = ctx.context_view()["gone"]
        ctx.add({"role": "assistant", "content": "broken"})
        ctx.add(
            {
                "role": "user",
                "tool": "",
                "success": False,
                "recovery": "format",
                "nudge": {"reason": "no_json", "prior": "broken"},
            }
        )
        assert ctx.fold_resolved_interventions(assume_tail_resolved=True) == 2
        view = ctx.context_view()
        assert view["gone"] == g and view["folded_nudges"] == 2
        assert capture.views[-1] == view

    def test_resume_restores_boundary_and_summary(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.compact_now()
        live = ctx.context_view()
        resumed = ContextManager(
            tmp_path / "s", max_context_tokens=100_000, resume=True
        )
        view = resumed.context_view()
        assert view["gone"] == live["gone"]
        assert view["summary"]["text"] == live["summary"]["text"]
        assert view["summary"]["turns"] == live["summary"]["turns"]
        assert view["summary"]["before_tokens"] is None  # 실측은 그 런의 것
        assert view["compactions"] == 1

    def test_in_memory_context_has_no_history(self, tmp_path):
        ctx = ContextManager(tmp_path / "s", max_context_tokens=1000)
        assert ctx.context_view()["gone"] is None

    def test_cli_renderer_ignores_the_view(self, tmp_path):
        from agent_cli.render import render_context_view

        render_context_view({"gone": None})  # base no-op — 예외 없음


class TestWebRendererContextView:
    def test_main_scope_sticky_and_replay(self):
        r = WebRenderer()
        c1 = WebConnection(id="c1")
        r.register_connection(c1)
        view = {"gone": {"hidx": 9}, "summary": None}
        r.context_view(view)
        ev, data = c1.queue.get_nowait()
        assert ev == "ctx_view" and data["gone"] == view["gone"]
        assert "task_id" not in data  # main
        c2 = WebConnection(id="c2")
        snapshot = r.register_connection(c2)
        assert any(e == "ctx_view" and d["gone"] == view["gone"] for e, d in snapshot)

    def test_latest_view_replaces_the_sticky(self):
        r = WebRenderer()
        r.context_view({"gone": {"hidx": 1}, "summary": None})
        r.context_view({"gone": {"hidx": 5}, "summary": None})
        snapshot = r.register_connection(WebConnection(id="c"))
        views = [d for e, d in snapshot if e == "ctx_view"]
        assert len(views) == 1 and views[0]["gone"]["hidx"] == 5

    def test_scoped_view_carries_task_id(self):
        r = WebRenderer()
        sid = "t-inline-1"
        r.begin_scope(task_id=sid, kind="run", label="x", agent="a")
        try:
            r.context_view({"gone": None, "summary": None})
        finally:
            r.end_scope(task_id=sid)
        snapshot = r.register_connection(WebConnection(id="c"))
        views = [d for e, d in snapshot if e == "ctx_view"]
        assert views and views[-1]["task_id"] == sid


class TestReplayCarriesTurnsAndOrdinals:
    def _events(self, r, records):
        conn = WebConnection(id="c")
        r.register_connection(conn)
        for i, rec in enumerate(records):
            r._replay_record(rec, hidx=i)
        out = []
        while not conn.queue.empty():
            out.append(conn.queue.get_nowait())
        return out

    def test_observation_and_assistant_turns_and_ordinals(self):
        r = WebRenderer()
        evs = self._events(
            r,
            [
                {"role": "user", "content": "q", "turn": 0},
                {
                    "role": "assistant",
                    "thought": "t",
                    "ops": [
                        {"action": "shell", "action_input": {"cmd": "ls"}},
                        {"action": "read_file", "action_input": {"path": "a"}},
                    ],
                    "turn": 3,
                },
                {
                    "role": "user",
                    "tool": "shell",
                    "success": True,
                    "content": "ok",
                    "turn": 3,
                },
                {
                    "role": "assistant",
                    "ops": [{"action": "complete", "action_input": {"result": "done"}}],
                    "turn": 4,
                },
            ],
        )
        by = {}
        for e, d in evs:
            by.setdefault(e, []).append(d)
        assert by["user_message"][0]["hidx"] == 0
        # 다중 op → 이벤트 둘, 둘 다 같은 레코드 서수 (카드 하나로 묶인다)
        assert [d["turn"] for d in by["assistant_turn"]] == [3, 3, 4]
        assert [d["hidx"] for d in by["assistant_turn"]] == [1, 1, 3]
        assert by["observation"][0]["turn"] == 3 and by["observation"][0]["hidx"] == 2

    def test_live_user_message_hidx_is_optional(self):
        r = WebRenderer()
        conn = WebConnection(id="c")
        r.register_connection(conn)
        r.push_user_message("hi")
        r.push_user_message("hi", hidx=7)
        first = conn.queue.get_nowait()[1]
        second = conn.queue.get_nowait()[1]
        assert "hidx" not in first and second["hidx"] == 7

    def test_hidx_does_not_leak_to_a_later_card(self):
        # 한 카드가 소비한 서수는 다음 카드에 남지 않는다
        r = WebRenderer()
        conn = WebConnection(id="c")
        r.register_connection(conn)
        r.push_user_message("a", hidx=3)
        r.push_user_message("b")
        conn.queue.get_nowait()
        assert "hidx" not in conn.queue.get_nowait()[1]


class TestLiveDispatchOrdinals:
    """라이브 경로: 행동 카드는 자기 assistant 레코드(레코드보다 먼저 그려짐),
    관찰 카드는 자기 관찰 레코드의 서수를 싣는다."""

    def test_action_and_observation_point_at_their_records(self, tmp_path, monkeypatch):
        from agent_cli.dialects import get as get_dialect
        from agent_cli.loop import LoopConfig, LoopState, ToolBridge, TurnDispatcher
        from agent_cli.recovery.observability import TurnRecorder

        prev = get_renderer()
        r = WebRenderer()
        set_renderer(r)
        try:
            conn = WebConnection(id="c")
            r.register_connection(conn)
            wf = get_dialect("json_fc")
            ctx = ContextManager(tmp_path / "s", max_context_tokens=100_000, dialect=wf)
            ctx.add({"role": "user", "content": "q"})
            cfg = LoopConfig(tools_list=["read_file", "complete"], dialect=wf)
            st = LoopState(query="q")
            d = TurnDispatcher(
                cfg,
                st,
                ctx=ctx,
                tools=ToolBridge(cfg, st, ctx, None),
                recorder=TurnRecorder(session_dir=None, enabled=False),
            )
            target = tmp_path / "f.txt"
            target.write_text("hello\n")
            # (shell `echo` 는 echo-as-final 로 접히므로 read_file 로 관찰을 만든다)
            d._handle_text_path('[{"action":"read_file","path":"' + str(target) + '"}]')
        finally:
            set_renderer(prev)
        evs = []
        while not conn.queue.empty():
            evs.append(conn.queue.get_nowait())
        by = {e: d for e, d in evs if e in ("assistant_turn", "observation")}
        records = [json.loads(ln) for ln in ctx.history_path.read_text().splitlines()]
        assert (
            records[1]["role"] == "assistant" and records[2].get("tool") == "read_file"
        )
        assert by["assistant_turn"]["hidx"] == 1
        assert by["observation"]["hidx"] == 2


class TestStaticWiring:
    def test_frontend_has_model_view(self):
        from pathlib import Path

        import agent_cli.web as web_pkg

        root = Path(web_pkg.__file__).parent / "static"
        js = (root / "app.js").read_text(encoding="utf-8")
        css = (root / "style.css").read_text(encoding="utf-8")
        assert 'addEventListener("ctx_view"' in js
        assert "ctx-gone" in js and "ctx-summary" in js
        assert "dataset.hidx" in js and "gone.hidx" in js
        assert "ctxApplyToCard(cardEl" in js  # finishCard 가 새 카드에도 적용
        assert ".card.ctx-gone" in css and ".card.ctx-summary" in css


class TestSnapshotEnd:
    """v10.8.1: 스냅샷의 마지막 항목은 ``snapshot_end`` — 클라이언트가 그때까지
    타임라인을 숨기고 한 번에 맨 아래로 점프한다."""

    def test_snapshot_ends_with_marker_and_count(self):
        r = WebRenderer()
        r.push_user_message("a")
        r.push_user_message("b")
        snapshot = r.register_connection(WebConnection(id="c"))
        assert snapshot[-1][0] == "snapshot_end"
        assert snapshot[-1][1] == {"events": len(snapshot) - 1}
        assert sum(1 for e, _ in snapshot if e == "snapshot_end") == 1

    def test_marker_is_not_a_live_event(self):
        r = WebRenderer()
        c1 = WebConnection(id="c1")
        r.register_connection(c1)
        r.register_connection(WebConnection(id="c2"))  # 두 번째 접속
        live = []
        while not c1.queue.empty():
            live.append(c1.queue.get_nowait()[0])
        assert "snapshot_end" not in live  # 기존 접속은 viewers 갱신만 받는다


class TestAbsorbedAgentMessage:
    def test_absorbed_into_rides_on_the_payload(self):
        r = WebRenderer()
        c = WebConnection(id="c")
        r.register_connection(c)
        r.agent_message(
            key="agt-1",
            direction="absorbed",
            author="main",
            text="t",
            seq=2,
            to="agt-1",
            absorbed_into=1,
        )
        ev, data = c.queue.get_nowait()
        assert ev == "agent_msg"
        assert data["direction"] == "absorbed" and data["absorbed_into"] == 1
        r.agent_message(
            key="agt-1", direction="in", author="main", text="t", seq=3, to="agt-1"
        )
        assert "absorbed_into" not in c.queue.get_nowait()[1]
