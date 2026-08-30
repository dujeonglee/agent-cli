"""개요 활동 스트립("도구 N회")의 수명 — 실사고 회귀 (v8.42.4).

증상: LLM 응답이 Invalid JSON 이면 개요의 툴 사용 카드가 사라졌다가 다음
툴콜부터 다시 생김.

원인: ``failed_turn`` 이벤트에서 활동 스트립(``ovAct``)을 비웠다. 그런데
``failed_turn`` 은 서버의 ``WebRenderer.recovery()`` **한 곳에서만** 나오고,
그건 "포맷 복구 후 같은 런이 재시도한다"는 뜻이지 런 종료가 아니다. 런
종료 정리는 ``worker_state`` idle 이 이미 소유하고 있었으므로 중복이자
오작동이었다(누적 카운트까지 0 으로 리셋됐다).

계약: 복구 중에도 스트립은 유지 · 카운트 누적 보존 · 런 종료(idle)에만 정리.
"""

from __future__ import annotations

import json
import time


def _wait(cond, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _strip(page):
    return page.locator(".ov-act-strip")


def _count_text(page):
    n = page.locator(".ov-act-strip .ov-act-n")
    return n.inner_text().strip() if n.count() else ""


class TestOverviewActivityStrip:
    def test_survives_invalid_json_recovery(self, stack, page):
        """Invalid JSON 복구(failed_turn) 후에도 스트립이 유지되고 누적
        카운트가 리셋되지 않는다 — 사용자 보고 지점."""
        r = stack.renderer
        r.header("openai", "Test-Model", max_turns=0)
        page.goto(stack.url)  # 기본 뷰 = 개요
        page.wait_for_selector("#overview", timeout=8000)

        r.action("read_file", json.dumps({"path": "/tmp/a.py"}), 1)
        assert _wait(lambda: _strip(page).count() > 0), "도구 호출 후 스트립 미표시"
        assert _count_text(page) == "도구 1회"

        # LLM 이 Invalid JSON 을 뱉음 → 서버가 복구 개입을 되먹임
        r.recovery(
            "{ broken json",
            "형식이 잘못되었습니다. 다시 emit 하세요.",
            "invalid JSON",
            1,
        )
        # 복구는 **재시도**다 — 스트립이 사라지면 회귀
        assert _wait(lambda: page.locator(".card-failed").count() > 0), (
            "실패 카드 미표시"
        )
        assert _strip(page).count() > 0, "복구 후 활동 스트립이 사라짐(회귀)"
        assert _count_text(page) == "도구 1회", "복구 후 누적 카운트가 리셋됨(회귀)"

        # 재시도 성공 → 카운트는 이어서 누적 (0 부터 다시 세지 않는다)
        r.action("shell", json.dumps({"command": "ls -la"}), 2)
        assert _wait(lambda: _count_text(page) == "도구 2회"), (
            f"카운트 누적 실패: {_count_text(page)!r}"
        )

    def test_cleared_on_run_end(self, stack, page):
        """런 종료(worker idle)에는 정리된다 — 복구 때 안 지운다고 해서
        영구히 남지는 않는다(안전망 유지)."""
        r = stack.renderer
        r.header("openai", "Test-Model", max_turns=0)
        page.goto(stack.url)
        page.wait_for_selector("#overview", timeout=8000)

        r.action("read_file", json.dumps({"path": "/tmp/a.py"}), 1)
        assert _wait(lambda: _strip(page).count() > 0)

        r.worker_idle()
        assert _wait(lambda: _strip(page).count() == 0), "런 종료 후에도 스트립 잔존"


class TestOverviewStableDuringStreaming:
    """LLM 발화 중 개요 카드의 버튼이 눌리지 않던 실사고 회귀 (v8.47.0).

    원인: ``ovRender()`` 가 ``$overview.innerHTML`` 를 통째로 갈아끼우는데,
    ``stream_chunk`` 마다(rAF 병합 → 초당 ~60회) 그게 호출됐다. 실제로 바뀌는
    것은 맨 아래 활동 스트립 한 줄뿐인데도 위쪽 응답 블록이 전부 파괴·재생성됐다.

    click 은 mousedown 과 mouseup 이 **같은 엘리먼트**에 떨어져야 발생하므로,
    노드가 그 사이에 사라지면 브라우저는 click 을 아예 만들지 않는다 —
    ``$overview`` 위임으로도 못 살린다. 계약: 스트리밍 중 목록 DOM 은 불변.
    """

    def _seed_response(self, stack, page):
        r = stack.renderer
        r.header("openai", "Test-Model", max_turns=0)
        page.goto(stack.url)
        page.wait_for_selector("#overview", timeout=8000)
        r.final("응답 본문입니다", 1)
        assert _wait(lambda: page.locator(".ov-block.resp .ov-copy").count() > 0), (
            "개요 응답 블록 미표시"
        )
        return r

    def _stream(self, r, n=12):
        for i in range(n):
            r.stream_chunk(f"chunk-{i} ")
            time.sleep(0.02)

    def test_response_block_is_not_recreated_by_streaming(self, stack, page):
        r = self._seed_response(stack, page)
        # 노드 동일성 표식 — 재생성되면 사라진다
        page.evaluate(
            "document.querySelector('.ov-block.resp').dataset.probe = 'keep-me'"
        )
        self._stream(r)
        assert _wait(lambda: _strip(page).count() > 0), "스트립 미표시"
        probe = page.evaluate(
            "(document.querySelector('.ov-block.resp') || {dataset:{}}).dataset.probe"
        )
        assert probe == "keep-me", "스트리밍이 응답 블록을 재생성함(회귀)"

    def test_copy_button_is_clickable_mid_stream(self, stack, page):
        """사용자가 보고한 증상 그대로 — 발화 중에 눌러도 동작해야 한다.

        헤드리스에는 클립보드 권한이 없어 ``navigator.clipboard.writeText`` 가
        reject 되므로(그러면 앱이 조용히 무시한다) 성공 플래시로는 판정할 수
        없다. 대신 **click 이벤트가 발생하는지** 를 직접 본다 — 그게 정확히
        깨졌던 지점이다: click 은 mousedown 과 mouseup 이 같은 엘리먼트에
        떨어져야 나오는데, 매 프레임 재생성되면 브라우저가 만들지 않는다.
        """
        r = self._seed_response(stack, page)
        page.evaluate(
            "window.__ovClicks = 0;"
            "document.querySelector('.ov-block.resp .ov-copy')"
            ".addEventListener('click', function(){ window.__ovClicks++; })"
        )
        self._stream(r)
        assert _wait(lambda: _strip(page).count() > 0)
        # 스트리밍이 계속되는 도중에 누른다 (한 프레임 뒤 재생성되던 구간)
        btn = page.locator(".ov-block.resp .ov-copy").first
        btn.click()
        r.stream_chunk("more ")
        btn.click()
        assert page.evaluate("window.__ovClicks") == 2, (
            "발화 중 복사 버튼에 click 이 발생하지 않음 — 노드가 재생성되고 있다"
        )

    def test_open_full_conversation_button_is_clickable_mid_stream(self, stack, page):
        """[▤ 전체 대화] 도 같은 원인으로 죽었다. 다만 이 테스트는 버그를
        재현하지는 않는다(재현하려면 mousedown~mouseup 사이에 정확히 rAF
        재렌더가 끼어야 해서 타이밍 의존이라 flaky 하다) — 외과적 갱신 경로가
        두 번째 버튼까지 살려두는지 확인하는 **전진 가드**다. 실제 재현은 위의
        복사 버튼 테스트가 담당한다(구버전에서 실패 확인됨)."""
        r = self._seed_response(stack, page)
        self._stream(r)
        assert _wait(lambda: _strip(page).count() > 0)
        assert not page.evaluate("document.body.classList.contains('drawer-open')"), (
            "사전조건: 전문 드로어가 닫혀 있어야 한다"
        )
        page.locator(".ov-block.resp .ov-open").first.click()
        # ovOpenTimeline → setViewMode("detail") → setDrawer(true) → body.drawer-open
        assert _wait(
            lambda: page.evaluate("document.body.classList.contains('drawer-open')"),
            timeout=3.0,
        ), "발화 중 [전체 대화] 클릭이 처리되지 않음"

    def test_list_dom_survives_streaming(self, stack, page):
        """펼쳐둔 <details> 나 선택 상태처럼 목록 DOM 에 붙은 것이 매 프레임
        리셋되면 안 된다."""
        r = self._seed_response(stack, page)
        page.evaluate(
            "document.querySelector('.ov-block.resp')"
            ".insertAdjacentHTML('beforeend','<details class=probe-d open></details>')"
        )
        self._stream(r)
        assert _wait(lambda: _strip(page).count() > 0)
        assert page.locator(".probe-d").count() == 1, "스트리밍이 목록 DOM 을 날림"

    def test_pulse_element_survives_so_its_animation_keeps_running(self, stack, page):
        """.ov-pulse 는 1.1s 무한 CSS 애니메이션 — 매 프레임 새로 만들면 t=0
        에서 영원히 재시작해 멎은 것처럼 보인다(사용자가 말한 '반짝임')."""
        r = self._seed_response(stack, page)
        r.stream_chunk("start ")
        assert _wait(lambda: page.locator(".ov-pulse").count() > 0)
        page.evaluate("document.querySelector('.ov-pulse').dataset.probe = 'same'")
        self._stream(r)
        probe = page.evaluate(
            "(document.querySelector('.ov-pulse') || {dataset:{}}).dataset.probe"
        )
        assert probe == "same", "펄스 재생성됨 — 애니메이션이 매 프레임 리셋된다"

    def test_strip_content_still_updates(self, stack, page):
        """외과적 갱신이라고 해서 스트립이 굳으면 안 된다."""
        r = self._seed_response(stack, page)
        r.action("read_file", json.dumps({"path": "/tmp/a.py"}), 1)
        assert _wait(lambda: _count_text(page) == "도구 1회")
        r.action("shell", json.dumps({"command": "ls"}), 1)
        assert _wait(lambda: _count_text(page) == "도구 2회"), (
            f"스트립이 갱신되지 않음: {_count_text(page)!r}"
        )

    def test_new_entry_still_triggers_a_full_render(self, stack, page):
        """엔트리가 실제로 늘면 전체 렌더가 돌아야 한다(외과적 경로가 목록
        갱신까지 삼키면 안 됨)."""
        r = self._seed_response(stack, page)
        self._stream(r)
        r.final("두 번째 응답", 2)
        assert _wait(lambda: page.locator(".ov-block.resp").count() == 2), (
            "새 응답이 개요에 반영되지 않음"
        )
