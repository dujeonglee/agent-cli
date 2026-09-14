"""헤더 칩·팝오버 렌더링 + confirm-stall 경고 — CSS/타이밍 부류 회귀 가드.

칩 사가(v7.1.0)의 실버그 두 개 — 칩 overflow 세로 클리핑, flex 컨테이너
ellipsis 무동작 — 는 레이아웃 엔진 없이는 원리적으로 못 잡는 부류라
실브라우저 층이 유일한 가드다.
"""

from __future__ import annotations

import time


def _wait(cond, timeout=8.0, step=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(step)
    return False


class TestHeaderChips:
    def test_ws_chip_visible_and_copies(self, browser, stack):
        ctx = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
        page = ctx.new_page()
        stack.emit_ready()
        page.goto(stack.url)
        page.wait_for_selector("#chip-ws:not([hidden])", timeout=8000)
        # 칩 내용(📋 아이콘 + 경로 꼬리)이 실제로 보이는 크기로 렌더 —
        # v7.1.0 실버그: 내부 버튼이 칩 overflow 에 세로 클리핑돼 안 보임.
        box = page.locator("#chip-ws").bounding_box()
        assert box and box["height"] >= 14 and box["width"] > 40
        ic = page.locator("#ws-copy-ic").bounding_box()
        assert ic and ic["height"] >= 10  # 아이콘이 클리핑되지 않음
        page.click("#chip-ws")
        # 복사 성공 피드백 (📋 → ✓ 1초 플래시)
        assert _wait(lambda: page.inner_text("#ws-copy-ic").strip() == "✓", timeout=3)
        ctx.close()

    def test_token_usage_shows_in_header(self, stack, page):
        # v8.57.0: 토큰 상세는 헤더에 상시 노출 (구 ctx 게이지 칩·팝오버 폐기).
        page.goto(stack.url)
        stack.renderer.token_usage(
            {"in": 5200, "out": 320, "total_out": 1800, "context_window": 262144},
            turn=1,
        )
        page.wait_for_selector("#token-usage:not([hidden])", timeout=8000)
        # ↑(턴 입력)은 ctx 분자와 동일 값이라 생략, ↓·Σ↓ 는 표시
        txt = page.inner_text("#token-usage")
        assert "ctx" in txt and "↓" in txt and "↑" not in txt
        box = page.locator("#token-usage").bounding_box()
        assert box and box["height"] >= 10 and box["width"] > 40  # 클리핑 없음

    def test_knob_chip_popup_toggles(self, stack, page):
        # 노브 칩 클릭 → 전용 팝업 열림, Escape 로 닫힘 (한 번에 하나).
        # 넓은 뷰포트 — 노브가 보따리로 수납되지 않은 상태를 보장.
        page.set_viewport_size({"width": 1500, "height": 720})
        page.goto(stack.url)
        page.wait_for_selector("#stall-wrap:not([hidden])", timeout=8000)
        page.wait_for_timeout(300)
        assert page.locator("#stall-pop").is_hidden()
        page.click("#stall-chip")
        assert page.locator("#stall-pop").is_visible()
        assert page.locator("#stall-pop #stall-input").is_visible()
        page.keyboard.press("Escape")
        assert _wait(lambda: page.locator("#stall-pop").is_hidden(), timeout=3)
        # v8.57.4: 왼쪽 칩 팝업이 화면 밖으로 잘리지 않는다(오른쪽으로 펼침).
        page.click("#compaction-chip")
        inview = page.evaluate(
            "() => { var b=document.querySelector('#compaction-pop').getBoundingClientRect();"
            " return b.left >= -1 && b.right <= window.innerWidth + 1; }"
        )
        assert inview

    def test_narrow_header_wraps_all_chips_visible(self, stack, page):
        # v8.57.1: 보따리 제거 — 좁은 창에서 헤더가 둘째 줄로 접히고(flex-wrap)
        # 모든 노브 칩이 계속 보인다(숨김/수납 없음).
        page.set_viewport_size({"width": 420, "height": 720})
        page.goto(stack.url)
        page.wait_for_selector("#stall-wrap:not([hidden])", timeout=8000)
        page.wait_for_timeout(200)
        for chip in ("#compaction-chip", "#stall-chip", "#thinking-chip"):
            assert page.locator(chip).is_visible(), chip
        # 줄바꿈이 실제로 일어나 헤더가 한 줄보다 높다
        heights = page.evaluate("""() => {
            var h = document.querySelector('header');
            var maxChild = 0, ch = h.children;
            for (var i=0;i<ch.length;i++){var c=ch[i];if(!c.hidden&&c.offsetParent)maxChild=Math.max(maxChild,c.offsetHeight);}
            return {header: h.clientHeight, maxChild: maxChild};
        }""")
        assert heights["header"] > heights["maxChild"] + 20  # 다중 줄
        # v8.57.2: 우측 그룹(액션·viewers·conn)도 잘리지 않고 화면 안 (margin-left:auto
        # 가 wrap 을 헝클던 문제 방지). confirm/viewers/rename 을 보이게 한 뒤 검사.
        page.evaluate(
            "() => { var c=document.getElementById('confirm-mode-btn'); if(c)c.hidden=false;"
            " var v=document.getElementById('viewers'); if(v)v.textContent='1 viewer - Dizzy Ferret (you)';"
            " var r=document.getElementById('rename-btn'); if(r)r.hidden=false; }"
        )
        page.wait_for_timeout(150)
        offscreen = page.evaluate(
            "() => { var h=document.querySelector('header'), vw=window.innerWidth, bad=[];"
            " Array.from(h.children).forEach(function(c){ if(c.hidden||!c.offsetParent)return;"
            " var r=c.getBoundingClientRect(); if(r.right>vw+1||r.left<-1) bad.push(c.id); }); return bad; }"
        )
        assert offscreen == [], offscreen
        # 좁은 창에서도 칩 클릭 → 팝업 정상
        page.click("#stall-chip")
        assert page.locator("#stall-pop").is_visible()


class TestStallKnobTwoAxes:
    """v8.60.0: ⏳ 노브가 두 축(무진전 한도 × 시도 횟수)을 한 팝업에 담는다.

    칩을 하나 더 늘리지 않은 이유가 레이아웃이므로(헤더는 이미 4칩+토큰
    으로 빠듯하다) 실브라우저 층이 그 판단의 가드다."""

    def test_popup_holds_both_inputs_and_derived_total(self, stack, page):
        page.set_viewport_size({"width": 1500, "height": 720})
        page.goto(stack.url)
        page.wait_for_selector("#stall-wrap:not([hidden])", timeout=8000)
        assert page.inner_text("#stall-badge").strip() == "10m×4"
        page.click("#stall-chip")
        assert page.locator("#stall-pop #stall-input").is_visible()
        assert page.locator("#stall-pop #stall-attempts").is_visible()
        # 곱을 설정하는 자리에서 바로 보여준다 — "3인데 왜 40분?"의 수리.
        assert "최대 40분" in page.inner_text("#stall-derived")
        # 두 입력 + 파생 줄이 팝업 안에 들어가고 화면 밖으로 안 나간다.
        inview = page.evaluate(
            "() => { var b=document.querySelector('#stall-pop').getBoundingClientRect();"
            " return b.left >= -1 && b.right <= window.innerWidth + 1; }"
        )
        assert inview

    def test_changing_attempts_round_trips_to_ctx(self, stack, page):
        """노브 → POST → ctx → 배지/파생값. 왕복이 끊기면 UI 만 바뀌고
        스트림은 옛 값으로 돈다."""
        page.set_viewport_size({"width": 1500, "height": 720})
        page.goto(stack.url)
        page.wait_for_selector("#stall-wrap:not([hidden])", timeout=8000)
        page.click("#stall-chip")
        page.fill("#stall-attempts", "6")
        page.dispatch_event("#stall-attempts", "change")
        assert _wait(lambda: stack.ctx.stream_max_attempts == 6)
        assert _wait(lambda: page.inner_text("#stall-badge").strip() == "10m×6")
        assert "최대 60분" in page.inner_text("#stall-derived")

    def test_changing_limit_keeps_attempts(self, stack, page):
        """한 축을 바꿔도 다른 축은 살아 있어야 한다 — 서로 덮어쓰면
        시도를 고칠 때마다 한도가 기본값으로 되돌아간다."""
        page.set_viewport_size({"width": 1500, "height": 720})
        page.goto(stack.url)
        page.wait_for_selector("#stall-wrap:not([hidden])", timeout=8000)
        page.click("#stall-chip")
        page.fill("#stall-attempts", "6")
        page.dispatch_event("#stall-attempts", "change")
        # 서버 도달이 아니라 **화면 반영**을 기다린다 — 응답의 apply() 가
        # 아직 안 돌았는데 다음 칸을 채우면 그 값이 덮어써질 수 있다.
        assert _wait(lambda: page.inner_text("#stall-badge").strip() == "10m×6")
        assert stack.ctx.stream_max_attempts == 6
        page.fill("#stall-input", "5")
        page.dispatch_event("#stall-input", "change")
        assert _wait(lambda: stack.ctx.stream_idle_timeout_s == 300)
        assert stack.ctx.stream_max_attempts == 6
        assert _wait(lambda: page.inner_text("#stall-badge").strip() == "5m×6")

    def test_zero_limit_disables_attempts_input(self, stack, page):
        """한도 0 = 감지 끔 → 시도 횟수라는 개념이 성립하지 않는다."""
        page.set_viewport_size({"width": 1500, "height": 720})
        page.goto(stack.url)
        page.wait_for_selector("#stall-wrap:not([hidden])", timeout=8000)
        page.click("#stall-chip")
        page.fill("#stall-input", "0")
        page.dispatch_event("#stall-input", "change")
        assert _wait(lambda: page.inner_text("#stall-badge").strip() == "off")
        assert _wait(lambda: page.locator("#stall-attempts").is_disabled())
        assert "꺼져" in page.inner_text("#stall-derived")


class TestStreamStallDisplay:
    """v8.60.0: 무진전 대기·재전송이 웹에 실제로 보인다.

    **원래 버그**: 이 알림은 전부 ``status`` 이벤트로 나갔고 프론트에 그
    리스너가 없어 통째로 드롭됐다 — 40분간 빈 화면 뒤 "LLM call failed".
    엔드-투-엔드로 한 번은 눈으로 확인되어야 하는 부류다."""

    def test_wait_appears_and_updates_in_place(self, stack, page):
        page.goto(stack.url)
        page.wait_for_selector("#stall-wrap:not([hidden])", timeout=8000)
        stack.renderer.stream_stall(
            kind="wait", elapsed_s=90, limit_s=600, attempt=1, attempts=4
        )
        page.wait_for_selector(".stall-line", timeout=8000)
        assert "1:30" in page.inner_text(".stall-line")
        assert "시도 1/4" in page.inner_text(".stall-line")
        # 20틱이 울려도 줄은 하나 — 기록처럼 쌓이면 표면을 덮는다.
        for tick in range(2, 21):
            stack.renderer.stream_stall(
                kind="wait", elapsed_s=tick * 30, limit_s=600, attempt=1, attempts=4
            )
        assert _wait(lambda: "10:00" in page.inner_text(".stall-line"))
        assert page.locator(".stall-line").count() == 1

    def test_resend_is_kept_and_wait_line_is_replaced(self, stack, page):
        page.goto(stack.url)
        page.wait_for_selector("#stall-wrap:not([hidden])", timeout=8000)
        stack.renderer.stream_stall(
            kind="wait", elapsed_s=600, limit_s=600, attempt=1, attempts=4
        )
        page.wait_for_selector(".stall-line", timeout=8000)
        stack.renderer.stream_stall(kind="resend", attempt=2, attempts=4)
        page.wait_for_selector(".stall-resend", timeout=8000)
        assert "시도 2/4" in page.inner_text(".stall-resend")
        assert _wait(lambda: page.locator(".stall-line").count() == 0)

    def test_clear_leaves_nothing_behind(self, stack, page):
        page.goto(stack.url)
        page.wait_for_selector("#stall-wrap:not([hidden])", timeout=8000)
        stack.renderer.stream_stall(
            kind="wait", elapsed_s=120, limit_s=600, attempt=1, attempts=4
        )
        page.wait_for_selector(".stall-line", timeout=8000)
        stack.renderer.stream_stall(kind="clear")
        assert _wait(lambda: page.locator(".stall-line").count() == 0)
        assert page.locator(".stall-resend").count() == 0


class TestConfirmStallWarning:
    def test_warning_appears_when_starved_and_clears_on_recovery(self, browser, stack):
        """origin 당 6연결 고갈 실재현(수용된 잔여 케이스) — 클릭이 갇히면
        3초 뒤 경고, 연결이 풀리면 해결+경고 정리 (v7.2.0 ⓔ)."""
        results: list = []
        stack.start_confirm_loop(results)
        ctx = browser.new_context()
        victim = ctx.new_page()
        victim.goto(stack.url)
        victim.wait_for_selector(".ask-main .confirm-btn", timeout=8000)
        holders = []
        for _ in range(5):
            t = ctx.new_page()
            try:
                t.goto(stack.url, timeout=4000, wait_until="commit")
            except Exception:
                pass  # 풀 경계에서 로드가 밀릴 수 있음 — 보유만 하면 됨
            holders.append(t)
        victim.wait_for_timeout(800)
        victim.click(".ask-main .confirm-btn >> nth=0")
        victim.wait_for_selector(".ask-main .confirm-stall", timeout=8000)
        holders[0].close()  # 슬롯 해방 → 갇힌 POST flush
        assert _wait(lambda: bool(results), timeout=8)
        assert _wait(
            lambda: victim.locator(".ask-main .confirm-stall").count() == 0, timeout=8
        )
        ctx.close()
