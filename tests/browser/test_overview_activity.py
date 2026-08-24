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
