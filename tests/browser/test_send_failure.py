"""보내기 실패가 화면에 보인다 — 실브라우저.

입력창의 보내기(main 의 ``/api/input``·에이전트 채널의 ``/api/agent/<key>/input``)는 서버가 받았을
때만 글과 첨부 칩을 비운다. 거절되면 사유(서버의 ``detail``)가 첨부 줄에 한 줄로 서고 글·칩이 남는다
— 다시 보내거나 고칠 수 있다. 종전엔 응답을 보지 않고 비워, 거절된 말이 소리 없이 사라졌다.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

from tests.browser import test_image_attach as _ia

_png_bytes, _wait = _ia._png_bytes, _ia._wait
ws = _ia.ws  # 워크스페이스 = tmp 픽스처 (이미지 첨부 테스트의 것)


def _attach(page):
    page.set_input_files(
        "#attach-input",
        {"name": "shot.png", "mimeType": "image/png", "buffer": _png_bytes()},
    )
    page.wait_for_selector("#attach-strip .attach-chip:not(.uploading)", timeout=8000)
    return page.locator("#attach-strip .attach-chip").first.inner_text()


def _notice(page) -> str:
    page.wait_for_selector("#attach-strip .attach-msg", timeout=8000)
    return page.inner_text("#attach-strip .attach-msg")


def test_refused_main_message_keeps_text_and_chip(stack, page, ws):
    stack.emit_ready()
    page.goto(stack.url)
    page.wait_for_selector("#attach", timeout=8000)
    label = _attach(page)
    path = label.split(" (")[0]
    (ws / path).unlink()  # 칩이 든 파일이 사라졌다 — 서버는 400 "<path>: no such file"
    page.fill("#input", "look at this")
    page.press("#input", "Enter")
    assert _notice(page) == f"Not sent — {path}: no such file"
    assert page.input_value("#input") == "look at this"
    assert page.locator("#attach-strip .attach-chip").count() == 1
    assert stack.server.main_input.pending_count() == 0


def test_refused_agent_channel_message_keeps_text_and_chip(
    stack,
    page,
    ws,
    monkeypatch,
    tmp_path,
):
    """화면은 아직 살아 있는 채널인데 서버에선 그 에이전트가 내려가는 중(정지가 섰다) — 404 의 사유가 보인다."""
    from agent_cli import render as render_mod
    from agent_cli.subagent.agents_live import AgentRegistry

    monkeypatch.setattr(render_mod, "_renderer", stack.renderer)
    monkeypatch.setattr(render_mod, "get_renderer", lambda: stack.renderer)
    gate, inside = threading.Event(), threading.Event()

    def runner(kind, start, ctx):
        inside.set()
        gate.wait(10)
        return SimpleNamespace(success=True, output="ok"), 0.01

    reg = AgentRegistry(tmp_path / "agents", runtime={"model": "m"}, runner=runner)
    stack.server.agent_registry = reg
    try:
        key, _ = reg.spawn(name="alpha")
        assert _wait(lambda: reg.get(key).state == "idle")
        assert reg.request(key, "work", author="user:dj") == ""
        assert inside.wait(10)
        stack.emit_ready()
        page.goto(stack.url)
        chip = page.locator(f'.ov-ch[data-key="{key}"]')
        assert _wait(lambda: chip.count() == 1)
        chip.locator(".ov-ch-lb").click()
        assert _wait(lambda: "on" in (chip.get_attribute("class") or ""))
        _attach(page)
        page.fill("#input", "and this")
        # `kill` 의 첫 걸음과 같다 — 정지를 세운다(워커는 런이 끝나야 내려와 명단이 아직 바뀌지 않는다)
        reg.get(key).stop_event.set()
        page.press("#input", "Enter")
        assert _notice(page) == f'Not sent — agent "{key}" is dead — spawn a new one'
        assert page.input_value("#input") == "and this"
        assert page.locator("#attach-strip .attach-chip").count() == 1
    finally:
        gate.set()
        reg.shutdown_all()


def test_accepted_message_clears_text_and_chip(stack, page, ws):
    stack.emit_ready()
    page.goto(stack.url)
    page.wait_for_selector("#attach", timeout=8000)
    _attach(page)
    page.fill("#input", "what is this?")
    page.press("#input", "Enter")
    assert _wait(lambda: stack.server.main_input.pending_count() == 1)
    assert _wait(lambda: page.input_value("#input") == "")
    assert _wait(lambda: page.locator("#attach-strip .attach-chip").count() == 0)
    assert page.locator("#attach-strip .attach-msg").count() == 0


def test_two_quick_enters_send_once(stack, page, ws):
    """빠른 Enter 두 번 — 첫 보내기가 끝나기 전이면 두 번째는 알리고 받지 않고, 끝난 뒤면 입력창이 비어
    있다. 어느 쪽이든 줄에는 하나만 선다."""
    stack.emit_ready()
    page.goto(stack.url)
    page.wait_for_selector("#input", timeout=8000)
    page.fill("#input", "once")
    page.press("#input", "Enter")
    page.press("#input", "Enter")
    assert _wait(lambda: page.input_value("#input") == "")
    assert stack.server.main_input.pending_count() == 1
    assert stack.server.main_input.take_next()["text"] == "once"


def test_no_response_times_out_keeps_text_and_frees_the_next_send(stack, page, ws):
    """응답이 오지 않는 보내기 — 상한(테스트가 짧게 줄인 ``SEND_LIMIT.ms``)이 지나면 알림이 서고 글·칩이
    남으며 다음 보내기가 다시 된다. 끊긴 요청에 늦게 온 응답은 화면을 건드리지 않는다."""
    stack.emit_ready()
    page.goto(stack.url)
    page.wait_for_selector("#attach", timeout=8000)
    page.evaluate(
        """async () => {
            const m = await import(new URL('static/js/input.js', document.baseURI).href);
            m.SEND_LIMIT.ms = 300;
            window.__notices = [];
            new MutationObserver(() => {
              const n = document.querySelector('#attach-strip .attach-msg');
              if (n) window.__notices.push(n.textContent);
            }).observe(document.getElementById('attach-strip'),
                       {childList: true, subtree: true, characterData: true});
        }"""
    )
    held: list = []
    page.route("**/api/input", lambda route: held.append(route))  # 응답을 주지 않는다
    _attach(page)
    page.fill("#input", "anyone there?")
    page.press("#input", "Enter")
    assert _notice(page) == "Not sent — no response from the server"
    assert page.input_value("#input") == "anyone there?"
    assert page.locator("#attach-strip .attach-chip").count() == 1
    assert len(held) == 1
    # 끊긴 요청에 늦은 응답 — 끊겼으니 페이지에 닿지 않는다
    try:
        held[0].fulfill(status=400, json={"detail": "late"})
    except Exception:
        pass  # 이미 끊긴 요청이면 Playwright 가 거절한다 — 어느 쪽이든 페이지는 못 받는다
    page.unroute("**/api/input")
    page.press("#input", "Enter")  # 다음 보내기가 다시 된다
    assert _wait(lambda: stack.server.main_input.pending_count() == 1)
    assert _wait(lambda: page.input_value("#input") == "")
    assert _wait(lambda: page.locator("#attach-strip .attach-chip").count() == 0)
    assert "Not sent — late" not in page.evaluate("() => window.__notices")
    assert stack.server.main_input.take_next()["text"] == "anyone there?"
