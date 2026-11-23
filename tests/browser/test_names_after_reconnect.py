"""에이전트 이름표는 라이브든 다시 접속이든 같은 글로 그려진다.

프런트는 이름표를 그 줄을 그릴 때 한 번 로스터에서 푼다(`peerLabel`·`agentJumpChip`). 그래서 서버는 로스터를 그것으로
그릴 줄보다 먼저 보낸다 — 다시 접속한 화면이 받는 스냅샷에서도(`WebRenderer.agent_roster`). 종전엔 스냅샷에서 로스터가
줄 뒤에 와, 다시 접속하면 이름이 키에서 지은 별명으로 바뀌고 `agent` 도구 줄의 점프 칩이 서지 않았다.

이름표가 서는 자리 다섯을 한 장면에서 본다: 런 블록 머리 줄의 보낸 이, ⏳ 대기 줄의 보낸 이, ⚠ 폴백 줄의 받는 이,
`message` 도구 줄의 받는 이, `agent` 도구 줄의 점프 칩. 진짜 레지스트리·진짜 웹 렌더러·헤드리스 크롬.
"""

from __future__ import annotations

import json
import threading

import agent_cli.render as render_mod
from agent_cli.render import render_step
from agent_cli.subagent.agents_live import AgentRegistry
from tests.browser.conftest import WebStack
from tests.browser.test_channel_jump import _chip, _wait
from tests.test_agents_live import _FakeLoopResult

LABELS = """() => {
  const t = (sel) => Array.from(document.querySelectorAll(sel)).map((e) => e.textContent.trim());
  return {
    head_who: t("#messages .card-run .run-head .run-item .who"),
    queued_peer: t("#messages > .card-queued .peer"),
    fallback_peer: t("#messages .card-fallback .peer"),
    tool_peer: t("#messages .card-assistant .peer:not(.can-jump)"),
    jump_chip: t("#messages .peer.can-jump"),
  };
}"""


def _use(monkeypatch, renderer):
    monkeypatch.setattr(render_mod, "_renderer", renderer)
    monkeypatch.setattr(render_mod, "get_renderer", lambda: renderer)


def _scene(tmp_path):
    """상주 둘. helper 는 도구 줄 둘(`message`·`agent`)을 내고 요청 하나를 흡수한 뒤 회신 없이 끝난다(폴백 둘).
    peer 의 런은 문에서 멈춘다 — 그 뒤에 온 요청은 ⏳ 줄로 남는다."""
    absorb, door = threading.Event(), threading.Event()
    box: dict = {}

    def runner(kind, start, ctx):
        if kind.tm.key != box.get("helper"):
            door.wait(10)
            return _FakeLoopResult(output="done"), 0.01
        to_peer = json.dumps({"to": box["peer"], "text": "hi"})
        render_step("action", "", 1, tool_name="message", tool_input=to_peer)
        render_step("observation", "sent", 1, tool_name="message")
        about_peer = json.dumps({"mode": "status", "key": box["peer"]})
        render_step("action", "", 2, tool_name="agent", tool_input=about_peer)
        render_step("observation", "ok", 2, tool_name="agent")
        absorb.wait(10)
        box["reg"].absorb_pending(kind.tm)
        return _FakeLoopResult(output="ran out of turns"), 0.01

    reg = box["reg"] = AgentRegistry(
        tmp_path, runtime={"model": "m", "timeout": 5}, runner=runner
    )
    return reg, box, absorb, door


def _read(page, key, peer):
    """두 채널의 이름표 — helper 채널(머리 줄·폴백·도구 줄·점프 칩)과 peer 채널(머리 줄·⏳ 줄)."""
    _chip(page, key).click()
    assert _wait(lambda: page.locator("#messages .card-fallback").count() == 2)
    assert _wait(lambda: page.locator("#messages .peer.can-jump").count() == 1)
    helper = page.evaluate(LABELS)
    _chip(page, peer).click()
    last = page.locator('#messages > .card-queued[data-item="r-904"]')
    assert _wait(lambda: last.count() == 1 and last.is_visible())
    return helper, page.evaluate(LABELS)["queued_peer"]


def test_agent_names_read_the_same_after_reconnecting(
    stack, page, monkeypatch, tmp_path
):
    _use(monkeypatch, stack.renderer)
    reg, box, absorb, door = _scene(tmp_path)
    try:
        stack.emit_ready()
        key, _ = reg.spawn(name="helper")
        peer, _ = reg.spawn(name="peer")
        box["helper"], box["peer"] = key, peer
        assert _wait(lambda: reg.get(key).state == reg.get(peer).state == "idle")
        page.goto(stack.url)
        assert _wait(lambda: _chip(page, peer).count() > 0)

        from_peer, from_helper = f"agent:{peer}", f"agent:{key}"
        assert reg.request(key, "TASK-A", author=from_peer, item_id="r-901") == ""
        assert _wait(lambda: reg.get(key).state == "busy")
        assert reg.request(key, "TASK-B", author=from_peer, item_id="r-902") == ""
        absorb.set()
        assert _wait(lambda: reg.get(key).state == "idle")
        # helper 의 폴백 회신이 peer 의 런을 열었고 그 런은 문에서 멈춘다 — 뒤에 온 요청은 ⏳ 줄로 남는다
        assert _wait(lambda: reg.get(peer).state == "busy")
        assert reg.request(peer, "TASK-D", author=from_helper, item_id="r-904") == ""

        helper, queued = _read(page, key, peer)
        assert helper["fallback_peer"][0].endswith(" peer")
        assert helper["tool_peer"][0].endswith(" peer")
        assert helper["jump_chip"][0].endswith(" peer")
        assert all(w.endswith(" peer") for w in helper["head_who"][:2])
        assert queued and all(q.endswith(" helper") for q in queued)

        page.reload()
        assert _wait(lambda: _chip(page, peer).count() > 0)
        assert _read(page, key, peer) == (helper, queued)
    finally:
        absorb.set()
        door.set()
        reg.shutdown_all()


def test_agent_names_read_the_same_after_the_session_restarts(
    stack, browser, page, monkeypatch, tmp_path
):
    """프로세스를 다시 띄운 뒤(새 렌더러·새 레지스트리가 같은 세션 폴더를 되살린다) — 창의 줄은 `conversation.jsonl`
    재생으로 버퍼에 들고, 첫 접속의 스냅샷이 그것을 그린다. 이름은 앞 세션에서 라이브로 본 것과 같다."""
    _use(monkeypatch, stack.renderer)
    reg, box, absorb, door = _scene(tmp_path)
    again = None
    try:
        stack.emit_ready()
        key, _ = reg.spawn(name="helper")
        peer, _ = reg.spawn(name="peer")
        box["helper"], box["peer"] = key, peer
        assert _wait(lambda: reg.get(key).state == reg.get(peer).state == "idle")
        page.goto(stack.url)
        assert _wait(lambda: _chip(page, peer).count() > 0)
        assert reg.request(peer, "TASK-C", author=f"agent:{key}", item_id="r-903") == ""
        assert _wait(lambda: reg.get(peer).state == "busy")
        assert reg.request(peer, "TASK-D", author=f"agent:{key}", item_id="r-904") == ""
        _chip(page, peer).click()
        rows = "#messages > .card-queued:visible .peer"
        assert _wait(lambda: page.locator(rows).count() == 1)
        live = page.locator(rows).all_inner_texts()
        assert live[0].endswith(" helper")

        # 새 프로세스: 앞 레지스트리를 정리하지 않은 채(강제 종료와 같다) 새 스택이 같은 폴더를 되살린다
        again = WebStack()
        again.start()
        _use(monkeypatch, again.renderer)
        again.emit_ready()
        reg2 = AgentRegistry(tmp_path, runtime={"model": "m", "timeout": 5})
        assert reg2.restore() == 2
        try:
            fresh = browser.new_context().new_page()
            fresh.goto(again.url)
            assert _wait(lambda: _chip(fresh, peer).count() > 0)
            _chip(fresh, peer).click()
            assert _wait(lambda: fresh.locator(rows).count() >= 1)
            assert set(fresh.locator(rows).all_inner_texts()) == set(live)
        finally:
            reg2.shutdown_all()
    finally:
        absorb.set()
        door.set()
        reg.shutdown_all()
        if again is not None:
            again.stop()
