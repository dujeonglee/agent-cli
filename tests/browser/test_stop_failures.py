"""정지·지우기 버튼이 닿지 못했거나 거절됐을 때 (v10.155.0, 실브라우저).

종전엔 응답을 버렸다 — 입력창 옆 Stop 은 "Stopping…" 으로 잠긴 채 그 런이 스스로 끝날 때까지 남았고(다시 못 누름),
채널 머리 ■/✕ 는 명단이 다시 그릴 때까지 잠겼다. 이제 입력창 아래에 ``Not stopped — <사유>`` (✕ 는
``Not removed``) 가 서고 그 버튼은 다시 누를 수 있다. 상한은 없다(늦게라도 닿으면 적용 — 사용자 결정).
``stopped: false``(돌던 턴 없음)는 실패가 아니다.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

NOTICES_JS = """() => {
  window.__notices = [];
  new MutationObserver(() => {
    const n = document.querySelector('#attach-strip .attach-msg');
    if (n) window.__notices.push(n.textContent);
  }).observe(document.getElementById('attach-strip'),
             {childList: true, subtree: true, characterData: true});
}"""


def _wait(cond, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _notices(page) -> list:
    return page.evaluate("() => window.__notices || []")


def _open(stack, page):
    stack.emit_ready()
    page.goto(stack.url)
    assert stack.wait_connected()
    page.evaluate(NOTICES_JS)


class TestMainStop:
    def _busy(self, stack, page):
        _open(stack, page)
        stack.renderer.worker_busy()
        page.wait_for_selector("#chat-stop:not([hidden])", timeout=8000)

    def test_unreachable_stop_frees_the_button_with_a_reason(self, stack, page):
        self._busy(stack, page)
        page.route("**/api/stop", lambda route: route.abort())
        page.click("#chat-stop")
        assert _wait(
            lambda: "Not stopped — could not reach the server" in _notices(page)
        )
        assert _wait(lambda: page.inner_text("#chat-stop") == "Stop")
        assert not page.is_disabled("#chat-stop")
        # 다시 누를 수 있다 — 이번엔 닿는다(돌던 턴 없음 = stopped:false 는 실패가 아니다)
        page.unroute("**/api/stop")
        before = len(_notices(page))
        with page.expect_response(lambda r: r.url.endswith("/api/stop")) as info:
            page.click("#chat-stop")
        assert info.value.json()["stopped"] is False
        assert page.inner_text("#chat-stop") == "Stopping…"
        assert len(_notices(page)) == before

    def test_a_slow_stop_is_not_cut_off(self, stack, page):
        """상한이 없다(사용자 결정) — 막힌 연결에서 20초가 지나도 끊지 않고 "Stopping…" 그대로, 늦게 닿으면 적용."""
        page.clock.install()
        self._busy(stack, page)
        held: list = []
        page.route("**/api/stop", lambda route: held.append(route))
        page.click("#chat-stop")
        assert _wait(lambda: len(held) == 1)
        page.clock.run_for(25_000)
        assert page.inner_text("#chat-stop") == "Stopping…"
        assert _notices(page) == []
        with page.expect_response(lambda r: r.url.endswith("/api/stop")) as info:
            held[0].continue_()
        assert info.value.ok
        assert _notices(page) == []

    def test_server_error_says_so(self, stack, page):
        self._busy(stack, page)
        page.route("**/api/stop", lambda route: route.fulfill(status=500, body="boom"))
        page.click("#chat-stop")
        assert _wait(lambda: "Not stopped — HTTP 500" in _notices(page))
        assert _wait(lambda: not page.is_disabled("#chat-stop"))


@pytest.fixture
def busy_agent(stack, monkeypatch, tmp_path):
    from agent_cli import render as render_mod
    from agent_cli.subagent.agents_live import AgentRegistry

    monkeypatch.setattr(render_mod, "_renderer", stack.renderer)
    monkeypatch.setattr(render_mod, "get_renderer", lambda: stack.renderer)
    gate, inside = threading.Event(), threading.Event()

    def runner(kind, start, ctx):
        inside.set()
        gate.wait(30)
        return SimpleNamespace(success=True, output="ok"), 0.01

    reg = AgentRegistry(tmp_path / "busy", runtime={"model": "m"}, runner=runner)
    stack.server.agent_registry = reg
    key, _ = reg.spawn(name="alpha")
    assert _wait(lambda: reg.get(key).state == "idle")
    assert reg.request(key, "work", author="user:dj") == ""
    assert inside.wait(5)
    yield SimpleNamespace(reg=reg, key=key, gate=gate)
    gate.set()
    reg.shutdown_all()


def _open_channel(stack, page, key):
    _open(stack, page)
    chip = page.locator(f'.ov-ch[data-key="{key}"]')
    assert _wait(lambda: chip.count() == 1)
    chip.locator(".ov-ch-lb").click()
    assert _wait(lambda: page.locator("#ov-chhead").is_visible())


class TestChannelHead:
    def test_refused_stop_frees_the_button_with_the_servers_reason(
        self, stack, page, busy_agent
    ):
        """실제 409 — 화면은 아직 일하는 중인데 그 런은 이미 끝났다(명단은 늦는다)."""
        reg, key = busy_agent.reg, busy_agent.key
        _open_channel(stack, page, key)
        stop = page.locator('#ov-chhead .chh-btn[data-act="stop"]')
        assert _wait(lambda: stop.count() == 1)
        reg._notify_roster = lambda: None  # 명단이 화면에 늦게 닿는다
        busy_agent.gate.set()
        assert _wait(lambda: reg.get(key).state == "idle")
        stop.click()
        want = f'Not stopped — agent "{key}" is not working on a request'
        assert _wait(lambda: want in _notices(page))
        assert _wait(lambda: not stop.is_disabled())
        del reg._notify_roster

    def test_failed_remove_frees_the_button(self, stack, page, busy_agent):
        _open_channel(stack, page, busy_agent.key)
        kill = page.locator('#ov-chhead .chh-btn[data-act="kill"]')
        assert _wait(lambda: kill.count() == 1)
        page.route(
            "**/api/agent/*/kill", lambda route: route.fulfill(status=500, body="boom")
        )
        kill.click()
        assert _wait(lambda: "Not removed — HTTP 500" in _notices(page))
        assert _wait(lambda: not kill.is_disabled())
        assert busy_agent.reg.get(busy_agent.key).state == "busy"

    def test_stop_all_agents_unreachable(self, stack, page, busy_agent):
        _open(stack, page)
        btn = page.locator("#ov-stop-all")
        assert _wait(lambda: btn.count() == 1)
        page.route("**/api/stop", lambda route: route.abort())
        btn.click()
        assert _wait(
            lambda: "Not stopped — could not reach the server" in _notices(page)
        )


def test_stuck_band_stop_unreachable_frees_the_button(stack, page):
    _open(stack, page)
    stack.renderer.stale_loop({"turns_without_new": 3, "since_turn": 2})
    btn = page.locator("#stuck-band .stuck-stop")
    assert _wait(lambda: btn.count() == 1)
    page.route("**/api/stop", lambda route: route.abort())
    btn.click()
    assert _wait(lambda: "Not stopped — could not reach the server" in _notices(page))
    assert _wait(lambda: btn.inner_text() == "Stop" and not btn.is_disabled())
