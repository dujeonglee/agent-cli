"""EventHub 단독 테스트 (v10.115.0 — ``WebRenderer`` 에서 분리한 연결 허브).

렌더러 없이 허브만: 팬아웃·재생 버퍼·스티키 순서·별명 풀·해제·종료, 그리고
뷰어 변화 콜백. 렌더러 쪽엔 "연결 메서드가 렌더러에 남지 않았다" 가드 하나.
"""

from __future__ import annotations

import json

import pytest

import agent_cli.render.web_hub as hub_mod
from agent_cli.render.web import WebRenderer
from agent_cli.render.web_hub import (
    _CLOSE_SENTINEL,
    _NICKNAMES,
    EventHub,
    WebConnection,
    _JsonReady,
)


def _ready(data: dict) -> _JsonReady:
    r = _JsonReady(data)
    r.json_str = json.dumps(r)
    return r


def _drain(conn: WebConnection) -> list[tuple[str, dict]]:
    out = []
    while not conn.queue.empty():
        out.append(conn.queue.get())
    return out


class TestPublish:
    def test_persistent_only_goes_to_buffer(self):
        hub = EventHub()
        hub.publish("a", _ready({"n": 1}), persistent=True)
        hub.publish("b", _ready({"n": 2}), persistent=False)
        assert [ev for ev, _ in hub.events] == ["a"]
        assert hub.persistent_count == 1

    def test_fans_out_to_every_open_connection(self):
        hub = EventHub()
        c1, c2 = WebConnection(id="c1"), WebConnection(id="c2")
        hub.register(c1)
        hub.register(c2)
        _drain(c1)  # c2 가 붙을 때 받은 viewers
        data = _ready({"n": 1})
        hub.publish("x", data, persistent=False)
        assert _drain(c1) == [("x", data)]
        assert _drain(c2) == [("x", data)]

    def test_closed_connection_is_skipped(self):
        hub = EventHub()
        c1, c2 = WebConnection(id="c1"), WebConnection(id="c2")
        hub.register(c1)
        hub.register(c2)
        _drain(c1)
        c2.closed.set()
        hub.publish("x", _ready({}), persistent=True)
        assert [ev for ev, _ in _drain(c1)] == ["x"]
        assert c2.queue.empty()

    def test_drop_buffered_adjusts_count(self):
        hub = EventHub()
        for i in range(3):
            hub.publish("m", _ready({"i": i}), persistent=True)
        assert hub.drop_buffered(lambda ev, d: d["i"] != 1) == 2
        assert [d["i"] for _, d in hub.events] == [1]
        assert hub.persistent_count == 1
        snap = hub.register(WebConnection(id="c"))
        assert "transcript_truncated" not in [ev for ev, _ in snap]


class TestBufferBound:
    def test_cap_and_truncation_notice_counts_omitted(self, monkeypatch):
        monkeypatch.setattr(hub_mod, "_EVENT_BUFFER_MAX", 4)
        hub = EventHub()
        for i in range(10):
            hub.publish("m", _ready({"i": i}), persistent=True)
        assert len(hub.events) == 4
        assert hub.persistent_count == 10
        snap = hub.register(WebConnection(id="c"))
        assert snap[0] == ("identity", {"conn_id": "c"})
        assert snap[1] == ("transcript_truncated", {"omitted": 6})
        assert [d["i"] for ev, d in snap if ev == "m"] == [6, 7, 8, 9]
        assert snap[-1] == ("snapshot_end", {"events": len(snap) - 1})


class TestSticky:
    def test_prepend_and_append_order_in_snapshot(self):
        hub = EventHub()
        hub.publish("hist", _ready({}), persistent=True)
        hub.set_sticky("a", "ev_a", {"v": "a"}, live=_ready({"v": "a"}))
        hub.set_sticky(
            "ready", "ready", {"v": "r"}, live=_ready({"v": "r"}), position="prepend"
        )
        hub.set_sticky("b", "ev_b", {"v": "b"}, live=_ready({"v": "b"}))
        snap = hub.register(WebConnection(id="c"))
        assert [ev for ev, _ in snap] == [
            "identity",
            "ready",
            "hist",
            "ev_a",
            "ev_b",
            "viewers",
            "snapshot_end",
        ]

    def test_set_sticky_broadcasts_live_shape_and_stores_raw_payload(self):
        hub = EventHub()
        c = WebConnection(id="c")
        hub.register(c)
        live = _ready({"v": 1, "ts": 5.0})
        hub.set_sticky("s", "ev", {"v": 1}, live=live)
        assert _drain(c) == [("ev", live)]
        assert hub.sticky_payload("s") == {"v": 1}
        assert len(hub.events) == 0  # 스티키는 비영속

    def test_clear_and_items(self):
        hub = EventHub()
        hub.set_sticky("knob:a", "knob", {"n": "a"}, live=_ready({}))
        hub.set_sticky("knob:b", "knob", {"n": "b"}, live=_ready({}))
        hub.set_sticky("other", "o", {}, live=_ready({}))
        assert hub.sticky_items("knob:") == [
            ("knob:a", {"n": "a"}),
            ("knob:b", {"n": "b"}),
        ]
        hub.clear_sticky("knob:a")
        hub.clear_sticky("missing")  # 없는 슬롯은 조용히
        assert hub.sticky_payload("knob:a") is None
        assert [n for n, _ in hub.sticky_items()] == ["knob:b", "other"]


class TestNicknames:
    def test_pool_exhaustion_disambiguates_with_counter(self):
        hub = EventHub()
        n = len(_NICKNAMES)
        for i in range(n + 1):
            hub.register(WebConnection(id=f"c{i}"))
        names = [hub.nickname_for(f"c{i}") for i in range(n + 1)]
        assert sorted(names[:n]) == sorted(_NICKNAMES)
        assert names[n].endswith(f" #{n + 1}")
        assert names[n].rsplit(" #", 1)[0] in _NICKNAMES

    def test_set_nickname_trims_rebroadcasts_and_rejects_empty(self):
        hub = EventHub()
        c = WebConnection(id="c")
        hub.register(c)
        assert hub.set_nickname("c", "  " + "x" * 40) is True
        assert hub.nickname_for("c") == "x" * 24
        (ev, payload), *_ = _drain(c)
        assert ev == "viewers" and payload["viewers"][0]["name"] == "x" * 24
        assert hub.set_nickname("c", "   ") is False
        assert hub.set_nickname(None, "bob") is False
        assert hub.nickname_for("unknown") == "?"


class TestLifecycle:
    def test_unregister_puts_sentinel_and_rebroadcasts_viewers(self):
        hub = EventHub()
        a, b = WebConnection(id="a"), WebConnection(id="b")
        hub.register(a)
        hub.register(b)
        _drain(a)
        hub.unregister(b)
        assert _drain(b) == [_CLOSE_SENTINEL]
        (ev, payload), *rest = _drain(a)
        assert ev == "viewers" and payload["count"] == 1 and not rest
        assert hub.viewer_count() == 1
        assert [c.id for c in hub.connections] == ["a"]

    def test_on_viewers_changed_once_per_register_and_unregister(self):
        calls: list[int] = []
        hub = EventHub(on_viewers_changed=lambda: calls.append(1))
        c = WebConnection(id="c")
        hub.register(c)
        assert len(calls) == 1
        hub.unregister(c)
        assert len(calls) == 2
        hub.publish("x", _ready({}), persistent=True)
        hub.set_sticky("s", "s", {}, live=_ready({}))
        assert len(calls) == 2  # 팬아웃·스티키는 뷰어 변화가 아니다

    def test_shutdown_all_is_idempotent(self):
        hub = EventHub()
        c = WebConnection(id="c")
        hub.register(c)
        hub.shutdown_all()
        assert c.closed.is_set()
        assert _drain(c) == [_CLOSE_SENTINEL]
        hub.shutdown_all()
        assert c.queue.empty()
        assert hub.connections == []
        assert hub.has_live_connections() is False


@pytest.mark.parametrize(
    "name",
    [
        "register_connection",
        "unregister_connection",
        "nickname_for",
        "set_nickname",
        "viewer_count",
        "has_live_connections",
        "shutdown_all_connections",
        "persistent_count",
    ],
)
def test_renderer_has_no_connection_methods(name):
    """연결 메서드는 ``renderer.hub`` 에만 있다 — 위임 한 줄짜리를 남기지 않는다."""
    assert not hasattr(WebRenderer, name)
