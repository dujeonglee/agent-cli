"""정지 조작 — 실브라우저 (docs/agent-engine/DESIGN.md §2.3, v10.142.0 시안 탭 1·2·4).

실제 레지스트리·실제 WebRenderer·실제 서버 API 에 가짜 엔진(정지가 설 때까지 막는다)을 꽂는다.

1. 입력창 옆 정지 버튼은 하나: main 만 바쁘면 Stop(▾ 없음), main·에이전트 모두 바쁘면 Stop ▾ — ▾ 메뉴 셋
   (Stop main · Stop all agents · Stop everything), main 이 한가하고 에이전트만 일하면 Send 그대로 + 채널 줄 끝 ■ all.
2. 채널 머리: 고른 에이전트의 ■ Stop run · ✕ Remove. run 의 ■ → 회색 칩(머리 없음), 상주의 ■ → 남는다.
"""

from __future__ import annotations

import threading
import time

import pytest

from agent_cli.tools.result import ToolResult


def _wait(cond, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _hold_engine(kind, start, ctx):
    """정지가 설 때까지 막는 런 — 멈추면 interrupt."""
    while not start.stop_event.is_set():
        time.sleep(0.01)
    return ToolResult(False, error="Interrupted by user")


@pytest.fixture
def live(stack, monkeypatch):
    from agent_cli import render as render_mod
    from agent_cli.session_slots import get_agent_registry, set_agent_registry

    monkeypatch.setattr(render_mod, "_renderer", stack.renderer)
    monkeypatch.setattr(render_mod, "get_renderer", lambda: stack.renderer)
    monkeypatch.setattr("agent_cli.loop.run.run_loop", _hold_engine)
    reg = stack.server.agent_registry
    reg.runtime = {"binding": stack.binding, "max_turns": 5}
    prev = get_agent_registry()
    set_agent_registry(reg)
    yield reg
    reg.shutdown_all()
    set_agent_registry(prev)


def _working(reg, *, run=False):
    key, err = reg.spawn(
        parent="main", ends_after_task=run, task_summary="find callers"
    )
    assert not err, err
    tm = reg.get(key)
    assert _wait(lambda: tm.state == "idle")
    assert not reg.request(
        key, "find every caller of parse_tokens", item_id=f"r-{key[-3:]}"
    )
    assert _wait(lambda: tm.state == "busy")
    return tm


def _open(stack, page):
    stack.emit_ready()
    page.goto(stack.url)
    page.wait_for_selector("#ov-channels", state="attached")


class TestStopButton:
    def test_main_only_busy_is_a_plain_stop(self, stack, page, live):
        _open(stack, page)
        stack.renderer.worker_busy()
        assert _wait(lambda: page.locator("#chat-stop").is_visible())
        assert page.locator("#chat-stop").inner_text() == "Stop"
        assert not page.locator("#chat-stop-more").is_visible()
        assert page.locator("#ov-stop-all").count() == 0

    def test_main_and_agents_busy_is_stop_with_a_scope_menu(self, stack, page, live):
        tm = _working(live)
        stop = threading.Event()
        stack.server.set_stop_handle(stop)
        _open(stack, page)
        stack.renderer.worker_busy()
        more = page.locator("#chat-stop-more")
        assert _wait(lambda: more.is_visible())
        assert page.locator("#chat-stop").is_visible()
        assert page.locator("#ov-stop-all").count() == 0  # main 이 바쁘면 ■ all 은 없다
        menu = page.locator("#stop-menu")
        assert not menu.is_visible()
        more.click()
        assert _wait(lambda: menu.is_visible())
        items = menu.locator("[data-scope]")
        assert [items.nth(i).get_attribute("data-scope") for i in range(3)] == [
            "main",
            "agents",
            "all",
        ]
        text = menu.inner_text()
        for label in ("Stop main", "Stop all agents", "Stop everything"):
            assert label in text
        menu.locator('[data-scope="agents"]').click()
        assert _wait(lambda: not menu.is_visible())
        # 모든 에이전트의 런이 멈추고 main 은 계속 — 상주는 남는다
        assert _wait(lambda: tm.state == "idle" and live._pending)
        assert not stop.is_set()
        assert _wait(lambda: not more.is_visible())  # 일하는 에이전트가 없다 — Stop 만
        assert page.locator("#chat-stop").is_visible()
        stack.server.set_stop_handle(None)

    def test_stop_everything_stops_main_too(self, stack, page, live):
        tm = _working(live, run=True)
        stop = threading.Event()
        stack.server.set_stop_handle(stop)
        _open(stack, page)
        stack.renderer.worker_busy()
        more = page.locator("#chat-stop-more")
        assert _wait(lambda: more.is_visible())
        more.click()
        page.locator('#stop-menu [data-scope="all"]').click()
        assert _wait(lambda: stop.is_set() and tm.state == "dead")
        # Stop everything 의 정지 회신은 main 을 깨우지 않는다 — 다음 입력 런에 함께 (사용자 결정)
        assert _wait(lambda: [m.get("wakes_main") for m in live._pending] == [False])
        stack.server.set_stop_handle(None)

    def test_main_idle_agents_working_is_send_and_stop_all(self, stack, page, live):
        tm = _working(live)
        _open(stack, page)
        stack.renderer.worker_idle()
        all_btn = page.locator("#ov-stop-all")
        assert _wait(lambda: all_btn.is_visible())
        assert all_btn.inner_text() == "■ all"
        assert page.locator("#send").is_visible()
        assert not page.locator("#chat-stop").is_visible()
        all_btn.click()
        assert _wait(lambda: tm.state == "idle" and live._pending)
        assert _wait(lambda: page.locator("#ov-stop-all").count() == 0)


class TestChannelHead:
    def test_resident_head_and_its_stop_keeps_the_agent(self, stack, page, live):
        tm = _working(live)
        _open(stack, page)
        chip = page.locator(f'.ov-ch[data-key="{tm.key}"]')
        assert _wait(lambda: chip.count() == 1)
        assert chip.locator(".ov-ch-ctl").count() == 0  # 칩엔 ✕ 가 없다 — 머리에
        chip.locator(".ov-ch-lb").click()
        head = page.locator("#ov-chhead")
        assert _wait(lambda: head.is_visible())
        assert "spawn" in head.inner_text() and "from main" in head.inner_text()
        stop_btn = head.locator('.chh-btn[data-act="stop"]')
        assert stop_btn.inner_text() == "■ Stop run" and stop_btn.is_enabled()
        assert head.locator('.chh-btn[data-act="kill"]').inner_text() == "✕ Remove"
        stop_btn.click()
        assert _wait(lambda: tm.state == "idle" and live._pending)
        assert tm.stop_event.is_set() is False  # 상주는 남는다
        assert _wait(lambda: head.locator('.chh-btn[data-act="stop"]').is_disabled())

    def test_run_stop_turns_the_chip_grey(self, stack, page, live):
        tm = _working(live, run=True)
        _open(stack, page)
        chip = page.locator(f'.ov-ch[data-key="{tm.key}"]')
        assert _wait(lambda: chip.count() == 1)
        chip.locator(".ov-ch-lb").click()
        head = page.locator("#ov-chhead")
        assert _wait(lambda: head.is_visible())
        assert head.inner_text().startswith(tm.key) or "run" in head.inner_text()
        head.locator('.chh-btn[data-act="stop"]').click()
        assert _wait(lambda: "done" in (chip.get_attribute("class") or ""))
        assert "dead" not in chip.get_attribute("class")  # 지운 줄이 아니라 회색
        assert _wait(lambda: not head.is_visible())  # 끝난 run — ■/✕ 없음

    def test_remove_from_the_head(self, stack, page, live):
        tm = _working(live)
        _open(stack, page)
        chip = page.locator(f'.ov-ch[data-key="{tm.key}"]')
        assert _wait(lambda: chip.count() == 1)
        chip.locator(".ov-ch-lb").click()
        page.locator('#ov-chhead .chh-btn[data-act="kill"]').click()
        assert _wait(lambda: tm.state == "dead")
        assert _wait(lambda: "dead" in (chip.get_attribute("class") or ""))
        assert [m.get("mark") for m in live._pending] == ["stopped by user"]
