"""장부 P4 — 사람에게 보이는 장부 (v10.119.0, docs/ledger/DESIGN.md §15).

- `Ledger.snapshot(turn)`: 꼬리와 같은 출처·같은 머리말로 만든 화면용 사본. 빚 먼저,
  요청 마지막(꼬리의 닫는 순서). ``hidx`` 는 장부가 모르므로 언제나 ``None``.
- 웹: 스티키 ``open_items`` 하나 — 늦은 접속은 스냅샷으로, 연결된 뷰어는 라이브로.
  같은 payload 는 두 번 보내지 않는다. ``hidx`` 는 렌더러가 원문 레코드 서수로 채운다.
  `_flips_status` 라 status.json·/api/health 에 요약이 선다.
- CLI: 턴 경계마다 ``open: u-2 n-1`` 한 줄(대괄호 없음 — rich 태그 삼킴 사고 재발 방지).
- 루프: main 런만 보낸다. 모델 입력(꼬리)은 그대로 — 꼬리 golden 이 지킨다.
"""

from __future__ import annotations

import io
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from rich.console import Console

from agent_cli.context.manager import ContextManager
from agent_cli.loop.ledger import NAG_BUDGET, Ledger
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.render.minimal import MinimalRenderer
from agent_cli.render.web import WebRenderer
from agent_cli.render.web_hub import WebConnection
from agent_cli.web.instance_file import read_status_file
from agent_cli.web.server import WebServer, create_app
from tests.loop_ports import make_ports
from tests.loopkit import make_loop


class _Port:
    def __init__(self, debts=()):
        self.list = list(debts)
        self.nonblocking = True

    def debts(self):
        return list(self.list)


_REPLY = {"kind": "reply", "to": "agent:agt-a", "id": "r-4", "text": "검토해 줘"}
_QUESTION = {"kind": "answer", "to": "agt-b", "id": "q-7a3f", "text": "할인 상한은?"}


def _events(conn) -> list[tuple[str, dict]]:
    out = []
    while not conn.queue.empty():
        out.append(conn.queue.get_nowait())
    return out


def _snap(*items, left=NAG_BUDGET):
    return {"items": list(items), "budget": {"left": left, "total": NAG_BUDGET}}


def _item(iid, **kw):
    base = {
        "id": iid,
        "source": "user",
        "who": "dj",
        "head": "A",
        "closer": "complete",
        "turns": 0,
        "hidx": None,
    }
    base.update(kw)
    return base


# ── Ledger.snapshot ─────────────────────────────────────────


class TestSnapshot:
    def test_empty_ledger(self):
        assert Ledger().snapshot(3) == {
            "items": [],
            "budget": {"left": NAG_BUDGET, "total": NAG_BUDGET},
        }

    def test_request_and_mail_carry_source_closer_head_and_turns(self):
        lg = Ledger()
        lg.add_request("u-1", "dj", "첫 줄\n둘째 줄", turn=2)
        lg.add_request("n-2", "mail:job", "x" * 300, turn=5)
        items = lg.snapshot(7)["items"]
        assert items[0] == _item(
            "u-1", head="첫 줄 둘째 줄", turns=5
        )  # 꼬리와 같은 한 줄 머리말
        assert items[1]["source"] == "mail" and items[1]["who"] == "mail:job"
        assert items[1]["closer"] == "complete" and items[1]["turns"] == 2
        assert items[1]["head"] == "x" * 120

    def test_turn_defaults_to_zero_for_callers_without_it(self):
        lg = Ledger()
        lg.add_request("u-1", "", "A")
        assert lg.snapshot(4)["items"][0]["turns"] == 4

    def test_request_dicts_keep_their_shape(self):
        """오른 턴은 항목 dict 에 넣지 않는다 — 최종 카드 ``requests``·`park_requests`
        로 그대로 흘러간다."""
        lg = Ledger()
        lg.add_request("u-1", "dj", "A", turn=3)
        assert lg.requests == [{"id": "u-1", "author": "dj", "text": "A"}]

    def test_debts_come_first_like_the_tail(self):
        lg = Ledger()
        lg.bind_questions(_Port([_REPLY, _QUESTION]))
        lg.add_request("u-1", "dj", "A")
        items = lg.snapshot(1)["items"]
        assert [i["id"] for i in items] == ["r-4", "q-7a3f", "u-1"]
        r, q = items[0], items[1]
        assert (r["source"], r["who"], r["closer"], r["turns"]) == (
            "debt",
            "agt-a",
            "reply",
            None,
        )
        assert r["head"] == "검토해 줘"
        assert (q["source"], q["closer"]) == ("question", "answer")

    def test_budget_follows_spend_and_settle_closes(self):
        lg = Ledger()
        lg.add_request("u-1", "dj", "A")
        lg.add_request("u-2", "dj", "B")
        lg.spend()
        lg.settle({"u-1"})
        snap = lg.snapshot(1)
        assert [i["id"] for i in snap["items"]] == ["u-2"]
        assert snap["budget"] == {"left": NAG_BUDGET - 1, "total": NAG_BUDGET}

    def test_tail_is_untouched_by_the_turn_stamp(self):
        """화면용 필드는 꼬리에 안 나간다 — 같은 장부의 꼬리는 턴과 무관하다."""
        a, b = Ledger(), Ledger()
        a.add_request("u-1", "dj", "A")
        b.add_request("u-1", "dj", "A", turn=9)
        assert a.block() == b.block() and "turn" not in a.block()


# ── 웹 렌더러: 스티키 ───────────────────────────────────────


class TestWebSticky:
    def test_live_then_late_join_snapshot(self):
        r = WebRenderer()
        conn = WebConnection(id="c1")
        r.hub.register(conn)
        r.open_items(_snap(_item("u-1")))
        live = [d for ev, d in _events(conn) if ev == "open_items"]
        assert len(live) == 1 and live[0]["items"][0]["id"] == "u-1"
        late = r.hub.register(WebConnection(id="c2"))
        replayed = [d for ev, d in late if ev == "open_items"]
        assert replayed == [r.hub.sticky_payload("open_items")]
        assert replayed[0]["budget"] == {"left": NAG_BUDGET, "total": NAG_BUDGET}
        # 스티키는 버퍼에 쌓이지 않는다(최신 하나)
        assert all(ev != "open_items" for ev, _d in r.hub.events)

    def test_same_payload_is_not_sent_twice(self):
        r = WebRenderer()
        conn = WebConnection(id="c1")
        r.hub.register(conn)
        r.open_items(_snap(_item("u-1")))
        r.open_items(_snap(_item("u-1")))
        r.open_items(_snap(_item("u-1", turns=1)))
        live = [d for ev, d in _events(conn) if ev == "open_items"]
        assert [d["items"][0]["turns"] for d in live] == [0, 1]

    def test_empty_before_any_strip_sends_nothing_but_closing_does(self):
        r = WebRenderer()
        conn = WebConnection(id="c1")
        r.hub.register(conn)
        r.open_items(_snap())
        assert not [ev for ev, _d in _events(conn) if ev == "open_items"]
        r.open_items(_snap(_item("u-1")))
        r.open_items(_snap())  # 닫힘 — 띠가 영수증을 그리고 숨는다
        live = [d for ev, d in _events(conn) if ev == "open_items"]
        assert [len(d["items"]) for d in live] == [1, 0]

    def test_hidx_is_filled_from_the_request_record(self):
        r = WebRenderer()
        r.note_request("u-1", 12)
        r.open_items(_snap(_item("u-1"), _item("n-2", source="mail")))
        items = r.hub.sticky_payload("open_items")["items"]
        assert [i["hidx"] for i in items] == [12, None]  # 모르면 None

    def test_replay_maps_request_id_to_hidx(self):
        """resume: user 레코드와 메일 관찰 레코드의 ``request_id`` → 그 서수."""
        r = WebRenderer()
        r._replay_record(
            {"role": "user", "content": "[dj]: A", "author": "dj", "request_id": "u-1"},
            hidx=3,
        )
        r._replay_record(
            {
                "role": "user",
                "content": "Observation: mail",
                "tool": "agent",
                "success": True,
                "request_id": "n-2",
            },
            hidx=5,
        )
        r.open_items(_snap(_item("u-1"), _item("n-2", source="mail")))
        items = r.hub.sticky_payload("open_items")["items"]
        assert [i["hidx"] for i in items] == [3, 5]


class TestStatusSummary:
    def test_status_json_carries_the_summary(self, tmp_path):
        r = WebRenderer(session_dir=str(tmp_path))
        r.open_items(
            _snap(
                _item("r-4", source="debt", turns=None),
                _item("u-1", turns=7),
                _item("n-2", source="mail", turns=2),
                left=6,
            )
        )
        assert read_status_file(tmp_path)["open_items"] == {
            "count": 3,
            "ids": ["r-4", "u-1", "n-2"],
            "oldest_turns": 7,
            "budget_left": 6,
        }

    def test_status_json_omits_it_when_nothing_is_open(self, tmp_path):
        r = WebRenderer(session_dir=str(tmp_path))
        r.open_items(_snap(_item("u-1")))
        r.open_items(_snap())
        assert "open_items" not in read_status_file(tmp_path)

    def test_health_carries_the_same_summary(self):
        r = WebRenderer()
        client = TestClient(create_app(WebServer(r, token="t", ctx=None)))
        assert client.get("/api/health").json()["open_items"] is None
        r.open_items(_snap(_item("u-1", turns=None)))
        assert client.get("/api/health").json()["open_items"] == {
            "count": 1,
            "ids": ["u-1"],
            "oldest_turns": None,
            "budget_left": NAG_BUDGET,
        }


# ── CLI 한 줄 ───────────────────────────────────────────────


class TestCliLine:
    def _r(self):
        buf = io.StringIO()
        return MinimalRenderer(Console(file=buf, force_terminal=False, width=120)), buf

    def test_one_line_ids_without_brackets(self):
        r, buf = self._r()
        r.open_items(_snap(_item("u-2"), _item("n-1"), _item("r-1")))
        assert buf.getvalue().strip() == "open: u-2 n-1 r-1"

    def test_same_snapshot_and_empty_print_nothing(self):
        r, buf = self._r()
        r.open_items(_snap())
        assert buf.getvalue() == ""
        r.open_items(_snap(_item("u-1")))
        r.open_items(_snap(_item("u-1")))
        assert buf.getvalue().count("open:") == 1
        r.open_items(_snap(_item("u-1", turns=1)))  # 턴이 지나면 다음 줄
        assert buf.getvalue().count("open:") == 2


# ── 루프: 부르는 자리와 거름 ───────────────────────────────


def _loop(tmp_path, *, requests=(), main=True):
    q = [dict(it) for it in requests]
    return make_loop(
        query="",
        provider=MagicMock(),
        capabilities=ModelCapabilities(
            context_window=32768, max_output_tokens=4096, supports_thinking=False
        ),
        model="m",
        ctx=ContextManager(Path(tmp_path) / "s", max_context_tokens=30_000),
        ports=make_ports(
            # 입력 큐가 있는 종류 = 사람 요청을 받는 main (`tracks_run_authors`)
            dequeue_user_message=(lambda: q.pop(0) if q else None) if main else None
        ),
    )


@pytest.fixture
def renderer(monkeypatch):
    rec = MagicMock()
    from agent_cli import render

    monkeypatch.setattr(render, "get_renderer", lambda: rec)
    return rec


class TestLoopCallSites:
    def test_injected_request_notes_its_record_and_shows_at_the_boundary(
        self, tmp_path, renderer
    ):
        from agent_cli.loop.dispatch import show_open_items

        lp = _loop(tmp_path, requests=[{"id": "u-1", "nickname": "dj", "text": "A"}])
        lp._intake()
        rid, hidx = renderer.note_request.call_args.args
        assert rid == "u-1" and hidx == lp.ctx.last_ordinal
        show_open_items(lp._config, lp._state)
        snap = renderer.open_items.call_args.args[0]
        assert [i["id"] for i in snap["items"]] == ["u-1"]

    def test_complete_shows_the_settled_ledger(self, tmp_path, renderer, monkeypatch):
        import agent_cli.loop.dispatch as d

        monkeypatch.setattr(d, "render_step", lambda *a, **k: None)
        lp = _loop(tmp_path, requests=[{"id": "u-1", "nickname": "dj", "text": "A"}])
        lp._intake()
        renderer.open_items.reset_mock()
        turn = types.SimpleNamespace(
            thought="", record={"role": "assistant", "content": "raw"}
        )
        op = types.SimpleNamespace(
            action="complete", action_input={"result": "done", "answers": ["u-1"]}
        )
        lp._dispatch._op_complete("raw", turn, op, {})
        assert renderer.open_items.call_args.args[0]["items"] == []

    def test_non_main_loops_show_nothing(self, tmp_path, renderer):
        from agent_cli.loop.dispatch import show_open_items

        lp = _loop(tmp_path, main=False)
        lp._state.ledger.add_request("u-1", "dj", "A")
        show_open_items(lp._config, lp._state)
        lp._note_request_record("u-1")
        assert not renderer.open_items.called and not renderer.note_request.called

    def test_run_shows_at_start_each_boundary_and_end(self, tmp_path, renderer):
        """런 시작 핀 뒤 · 턴 경계(흡수 뒤) · 런 끝 — 턴 하나짜리 런이면 셋."""
        from agent_cli.tools.result import ToolResult

        lp = _loop(tmp_path, requests=[{"id": "u-1", "nickname": "dj", "text": "A"}])
        lp._execute_turn = lambda: ToolResult(True, output="x")
        lp.run()
        snaps = [c.args[0] for c in renderer.open_items.call_args_list]
        assert [[i["id"] for i in s["items"]] for s in snaps] == [[], ["u-1"], ["u-1"]]


# ── 웹 소스 (실제 그리기는 tests/browser/test_open_items.py) ──────


class TestWebSource:
    def test_ledger_module_draws_the_strip_and_jumps_by_event(self):
        from tests.web_static import js_text

        js = js_text("ledger")
        assert 'es.addEventListener("open_items"' in js
        assert '"agentcli:jump"' in js and "detail: { hidx: it.hidx }" in js
        assert '"📋 open items · the model owes these"' in js
        assert '"budget " + oiBudget.left + "/" + oiBudget.total' in js
        assert '"oi-budget-out"' in js and '"✓ closed"' in js
        # 순환 밖 잎 — dom·sse 만
        imports = [ln for ln in js.splitlines() if ln.startswith("import ")]
        assert imports == [
            'import { el } from "./dom.js";',
            'import { es } from "./sse.js";',
        ]

    def test_closed_receipt_is_not_struck_through(self):
        """v10.119.1: 닫힘 영수증은 배경·✓ closed 글자로만 — 제목 취소선 규칙이 없다."""
        from tests.web_static import STATIC

        css = (STATIC / "style.css").read_text(encoding="utf-8")
        assert ".oi-item.oi-closed {" in css  # 행 배경은 남는다
        assert not [
            ln for ln in css.splitlines() if "oi-closed" in ln and "line-through" in ln
        ]

    def test_channels_listens_for_the_jump(self):
        from tests.web_static import js_text

        assert 'document.addEventListener("agentcli:jump"' in js_text("channels")

    def test_strip_sits_between_tray_and_queue(self):
        from tests.web_static import STATIC

        html = (STATIC / "index.html").read_text(encoding="utf-8")
        tray = html.index('id="ask-tray"')
        strip = html.index('<div id="open-items" hidden></div>')
        queue = html.index('id="queue-list"')
        assert tray < strip < queue

    def test_queue_rows_carry_the_same_id_chip(self):
        from tests.web_static import js_fn_body, js_text

        body = js_fn_body(js_text("roster"), "qRender")
        assert body.count('el("span", ["oi-id"]') == 2  # 대기 행·영수증 행
        assert '"⏳ [" + it.nickname + "] " + it.text' in body  # 글은 그대로
