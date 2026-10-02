"""⧉ 복사 (v10.8.0, export 대체) — 카드 마크다운의 **형식** 계약.

클립보드 자체는 헤드리스에서 읽기 어려우므로 복사 버튼이 쓰는 같은 함수
(`window.__cardMarkdown` / `window.__timelineMarkdown`)의 출력을 고정한다:
사용자 말풍선 · 최종답(마크다운 소스 그대로) · 스텝 카드(💭 → ⚡ 입력 JSON 펜스 →
✓ 관찰 펜스) · 전체 복사(보이는 카드를 순서대로 이어 붙임) · 카드마다 ⧉ 가 있다.
"""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.browser


def _wait(cond, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _cards_md(page):
    return page.evaluate(
        "Array.from(document.querySelectorAll('#messages > .card')).map(c => window.__cardMarkdown(c))"
    )


class TestCardMarkdown:
    def test_user_step_and_final(self, stack, page):
        r = stack.renderer
        page.goto(stack.url)
        page.wait_for_selector("#messages", timeout=8000)
        stack.emit_ready()
        r.push_user_message("[DJ]: 파일 좀 봐줘", author="DJ", hidx=0)
        r.thought("먼저 읽는다.", turn=1)
        r.action("read_file", '{"path": "a.py"}', turn=1)
        r.observation("print('hi')\n", turn=1, tool_name="read_file", success=True)
        r.thought("", turn=2)
        r.final("## 결과\n\n- `a.py` 는 **한 줄**입니다.", turn=2)
        assert _wait(lambda: page.locator("#messages > .card").count() >= 3)
        md = _cards_md(page)
        assert md[0] == "**👤 DJ**\n\n파일 좀 봐줘"
        assert md[1] == (
            "💭 먼저 읽는다.\n\n"
            '⚡ `read_file`\n```json\n{\n  "path": "a.py"\n}\n```\n\n'
            "✓ `read_file`\n```\nprint('hi')\n```"
        )
        # 최종답은 모델이 쓴 마크다운 소스 그대로 (렌더 결과가 아니다)
        assert md[2] == "## 결과\n\n- `a.py` 는 **한 줄**입니다."
        # 전체 복사 = 보이는 카드를 빈 줄로 이어 붙인 것
        whole = page.evaluate("window.__timelineMarkdown()")
        assert whole == "\n\n".join(md)
        # 카드마다 ⧉
        assert page.locator("#messages > .card > .card-copy").count() == 3
        assert page.locator("#copy-all-btn").count() == 1

    def test_multi_op_turn_and_failed_observation(self, stack, page):
        r = stack.renderer
        page.goto(stack.url)
        page.wait_for_selector("#messages", timeout=8000)
        stack.emit_ready()
        r.thought("둘 다 실행", turn=1)
        r.action("shell", '{"command": "ls"}', turn=1)
        r.action("shell", '{"command": "pwd"}', turn=1)
        r.observation(
            "[1/2] shell — OK\n[2/2] shell — FAILED: boom",
            turn=1,
            tool_name="shell",
            success=False,
        )
        assert _wait(lambda: page.locator("#messages > .card").count() >= 1)
        md = _cards_md(page)[0]
        assert md.count("⚡ `shell`") == 2  # 같은 턴의 두 op 가 한 카드에
        assert "✗ `shell`\n```\n[1/2] shell — OK\n[2/2] shell — FAILED: boom\n```" in md

    def test_fence_grows_past_backticks_in_content(self, stack, page):
        r = stack.renderer
        page.goto(stack.url)
        page.wait_for_selector("#messages", timeout=8000)
        stack.emit_ready()
        r.action("read_file", '{"path": "README.md"}', turn=1)
        r.observation(
            "```python\nx = 1\n```\n", turn=1, tool_name="read_file", success=True
        )
        assert _wait(lambda: page.locator("#messages > .card").count() >= 1)
        md = _cards_md(page)[0]
        assert "````\n```python\nx = 1\n```\n````" in md  # 본문의 ``` 보다 긴 울타리

    def test_export_surface_is_gone(self, stack, page):
        page.goto(stack.url)
        page.wait_for_selector("#messages", timeout=8000)
        assert page.locator("#export-btn, #export-bar").count() == 0
