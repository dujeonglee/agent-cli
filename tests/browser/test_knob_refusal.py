"""노브 칩의 거부 사유 (vNEXT, 실브라우저).

서버는 거부(미등록 모델 등)를 200 ``{ok: false, error}`` 로 돌려준다. 종전엔 클라이언트가 그것을 null 로 뭉개 화면은
말없이 되돌아갔다. 이제 되돌리면서 그 칩의 설명 줄에 ``Not changed — <사유>`` 를 쓴다. 설명 줄이 없는 머리
토글은 입력창 아래 줄에.
"""

from __future__ import annotations

import time


def _wait(cond, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_refused_model_reverts_and_says_why(stack, page):
    stack.emit_ready()
    page.set_viewport_size({"width": 1500, "height": 720})
    page.goto(stack.url)
    page.wait_for_selector("#knob-model-wrap:not([hidden])", timeout=8000)
    page.click("#knob-model-chip")
    with page.expect_response(lambda r: "/api/knobs/model" in r.url) as info:
        page.evaluate(
            "() => { var s=document.getElementById('knob-model');"
            " var o=document.createElement('option'); o.value='nope'; o.textContent='nope';"
            " s.appendChild(o); s.value='nope'; s.dispatchEvent(new Event('change', {bubbles:true})); }"
        )
    body = info.value.json()
    assert body["ok"] is False and body["error"]
    note = page.locator("#knob-model-note")
    assert _wait(
        lambda: (
            note.is_visible() and note.inner_text() == "Not changed — " + body["error"]
        )
    )
    assert (
        page.evaluate("() => document.getElementById('knob-model').value")
        == "Test-Model-8bit"
    )
    assert stack.binding.model == "Test-Model-8bit"


def test_unreachable_header_toggle_says_so_below_the_input(stack, page):
    stack.emit_ready()
    page.set_viewport_size({"width": 1500, "height": 720})
    page.goto(stack.url)
    toggle = page.locator("#knobs-header button").first
    assert _wait(lambda: toggle.count() == 1 and toggle.is_visible())
    page.route("**/api/knobs/*", lambda route: route.abort())
    toggle.click()
    page.wait_for_selector("#attach-strip .attach-msg", timeout=8000)
    assert page.inner_text("#attach-strip .attach-msg") == (
        "Not changed — could not reach the server"
    )
