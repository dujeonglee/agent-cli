"""세션 로드(스냅샷 재생) 렌더 성능 — 회귀 가드 + KPI 측정 (v8.42.2).

배경: 보드에서 대화가 쌓인 게시글을 열 때, 프런트가 스냅샷 재생 중
**이벤트마다** 개요 전체를 다시 그리고(``ovRender`` — innerHTML 전면
교체 + 최근 항목 마크다운 재실행) **카드마다** ``scrollTop=scrollHeight``
를 써서 전체 레이아웃을 강제해 메인스레드가 O(N²)로 막혔다(888이벤트
블로킹 1012ms, 4x 스로틀 ~4.3s). v8.42.0 이 두 경로를 rAF 당 1회로
병합해 277ms 로 낮췄다.

**이 파일의 역할 분리 (중요)**:

- ``TestRenderCoalescingRuntime`` — **회귀 가드(단언)**. 벽시계 시간이
  아니라 **하드웨어 무관한 카운팅 불변식**을 단언한다: 스냅샷 N 이벤트를
  재생해도 개요 재작성/스크롤 쓰기는 **프레임 수 수준의 상수**여야 한다.
  느린 CI·빠른 랩톱 어디서도 같은 판정이 나오고, 병합기가 풀리거나
  새 이벤트 훅이 직접 렌더를 부르면 즉시 실패한다. 정적 소스 핀
  (``test_web_server.TestRenderCoalescing``)이 못 잡는 **런타임 경로**
  (다른 이름의 호출·병합기 무력화·새 훅)를 여기서 잡는다.
- ``TestRenderKpi`` — **KPI 측정(비단언)**. 벽시계 블로킹 시간을 재서
  JSON/표로 남긴다. 단언하지 않는 이유: 절대 임계는 머신 성능에 종속돼
  (a) 느린 CI 에서 오탐 (b) 빠른 머신에선 실제 회귀(1012ms)도 임계 아래로
  통과 — 즉 flaky 하면서 동시에 무력하다. 수치는 릴리스 때 기록해
  추세로 관리한다(docs/PERF-KPI.md).

실행: ``AGENT_CLI_BROWSER_TESTS=1 pytest tests/browser/test_render_perf.py``
KPI 까지: ``AGENT_CLI_PERF_KPI=1`` 추가 (docs/PERF-KPI.md 참조).
"""

from __future__ import annotations

import json
import os
import time

import pytest

# ── 계측 스크립트 (페이지 스크립트보다 먼저 실행) ───────────────────
#
# ovRender/scrollToBottom 은 IIFE 클로저 안이라 밖에서 못 잡는다. 대신
# **관측 가능한 효과**를 prototype accessor 에서 정확히 센다:
#   · ovRender  → ``#overview`` 의 innerHTML 전면 교체
#   · scrollToBottom → ``#messages`` 의 scrollTop 쓰기
# MutationObserver 는 마이크로태스크 배칭 때문에 호출 횟수를 과소계상할
# 수 있어 쓰지 않는다(회귀를 놓치는 방향의 오차 = 가드로서 치명적).
_INSTRUMENT = """
window.__perf = { ovWrites: 0, scrollMsgs: 0, scrollOv: 0 };
(function () {
  var ih = Object.getOwnPropertyDescriptor(Element.prototype, "innerHTML");
  Object.defineProperty(Element.prototype, "innerHTML", {
    configurable: true,
    get: ih.get,
    set: function (v) {
      if (this.id === "overview") window.__perf.ovWrites++;
      return ih.set.call(this, v);
    },
  });
  var st = Object.getOwnPropertyDescriptor(Element.prototype, "scrollTop");
  Object.defineProperty(Element.prototype, "scrollTop", {
    configurable: true,
    get: st.get,
    set: function (v) {
      if (this.id === "messages") window.__perf.scrollMsgs++;
      else if (this.id === "overview") window.__perf.scrollOv++;
      return st.set.call(this, v);
    },
  });
})();
"""

# long-task 관측 (KPI 전용 — 계측 오버헤드가 없도록 별도 스크립트)
_LONGTASK = """
window.__lt = [];
try {
  new PerformanceObserver(function (l) {
    for (const e of l.getEntries()) window.__lt.push(e.duration);
  }).observe({ entryTypes: ["longtask"] });
} catch (e) {}
"""

# 스냅샷 버스트 규모. 턴당 4이벤트(user + obs×2 + final) — 실세션의
# 이벤트 혼합(개요 갱신 트리거 2 + 카드만 2)을 그대로 흉내낸다.
_TURNS = 60
_EVENTS_PER_TURN = 4
_TOTAL_EVENTS = _TURNS * _EVENTS_PER_TURN

# 병합 후 허용 상한. 스냅샷은 SSE 로 여러 태스크에 걸쳐 도착하므로
# "프레임 수"만큼은 정당하게 발생한다(실측 1~5회). 상한 20 은 그 변동을
# 넉넉히 흡수하면서, 병합이 풀린 경우(이벤트당 1회 = 120/240회)와는
# 6~12배 격차라 판정이 흔들리지 않는다.
_COALESCED_MAX = 20


def _seed_snapshot(stack, turns: int = _TURNS) -> str:
    """브라우저 접속 **전에** 렌더러로 이벤트를 쌓아 스냅샷 버스트를 만든다.

    접속 시 ``register_connection`` 이 이 버퍼를 통째로 재생하므로,
    보드에서 대화가 쌓인 게시글을 여는 상황과 같은 부하가 된다.
    마지막 응답 텍스트(개요 최신 상태 검증용)를 반환한다."""
    r = stack.renderer
    body = "관찰 본문 " * 40
    last = ""
    for i in range(turns):
        r.push_user_message(f"[Tester]: 요청 {i} 처리해줘", author="Tester")
        r.observation(body, turn=i, tool_name="read_file", success=True)
        r.observation(body, turn=i, tool_name="shell", success=True)
        last = f"응답본문마커{i} " + ("결과 " * 30)
        r.final(last, turn=i)
    return f"응답본문마커{turns - 1}"


def _wait_settled(page, expected_cards: int, timeout: float = 30.0) -> None:
    """카드 수가 목표치에 도달하고 rAF 병합분이 flush 될 때까지 대기."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        n = page.evaluate("document.querySelectorAll('#messages > *').length")
        if n >= expected_cards:
            break
        time.sleep(0.05)
    # 마지막 rAF 콜백까지 확실히 실행 (2 프레임 여유)
    page.evaluate(
        "new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"
    )


class TestRenderCoalescingRuntime:
    """스냅샷 재생 시 렌더/스크롤이 **이벤트 수에 비례하지 않는다**는
    런타임 불변식 — v8.42.0 회귀 가드."""

    def test_snapshot_replay_coalesces_render_and_scroll(self, stack, page):
        _seed_snapshot(stack)
        stack.emit_ready()
        page.add_init_script(_INSTRUMENT)
        page.goto(stack.url)
        _wait_settled(page, _TOTAL_EVENTS)

        perf = page.evaluate("window.__perf")
        cards = page.evaluate("document.querySelectorAll('#messages > *').length")
        # 여유(headroom) 가시화 — ``-s`` 로 실행 시 상한 대비 실측이 보인다.
        print(
            f"\n[coalescing] events={_TOTAL_EVENTS} → ovWrites={perf['ovWrites']} "
            f"scrollMsgs={perf['scrollMsgs']} (상한 {_COALESCED_MAX}, "
            f"병합 전 기준 ≈{_TOTAL_EVENTS // 2}/{_TOTAL_EVENTS})"
        )

        # 1) 모든 이벤트가 실제로 재생됐다 (부하가 없었던 게 아님을 확정 —
        #    이 단언이 없으면 "아무것도 안 그려서 빠름"이 통과해버린다).
        assert cards == _TOTAL_EVENTS, f"카드 {cards} != 이벤트 {_TOTAL_EVENTS}"

        # 2) 개요 전체 재작성은 프레임 수준 상수 (병합 풀리면 ≈120회)
        assert perf["ovWrites"] <= _COALESCED_MAX, (
            f"개요 재작성 {perf['ovWrites']}회 — 이벤트당 재렌더로 회귀했을 "
            f"가능성(스냅샷 {_TOTAL_EVENTS}이벤트, 상한 {_COALESCED_MAX})"
        )

        # 3) 타임라인 스크롤 쓰기(강제 레이아웃)도 상수 (병합 풀리면 ≈240회)
        assert perf["scrollMsgs"] <= _COALESCED_MAX, (
            f"scrollTop 쓰기 {perf['scrollMsgs']}회 — 카드마다 강제 레이아웃으로 "
            f"회귀했을 가능성(상한 {_COALESCED_MAX})"
        )

    def test_coalescing_preserves_final_state(self, stack, page):
        """병합이 **마지막 렌더를 삼키지 않는다** — 개요는 최신 응답을
        보여주고 타임라인은 바닥에 붙어 있어야 한다(병합의 기능 계약)."""
        expected_last = _seed_snapshot(stack)
        stack.emit_ready()
        page.add_init_script(_INSTRUMENT)
        page.goto(stack.url)
        _wait_settled(page, _TOTAL_EVENTS)

        # 개요에 최신 응답이 반영됨 (rAF 마지막 1회가 최신 상태를 그린다)
        ov_text = page.inner_text("#overview")
        assert expected_last in ov_text, "개요가 최신 응답을 반영하지 않음"

        # 타임라인은 자동 스크롤로 바닥 (앱의 임계 50px 와 동일 기준)
        dist = page.evaluate(
            "(() => { const m = document.getElementById('messages');"
            " return m.scrollHeight - m.scrollTop - m.clientHeight; })()"
        )
        assert dist <= 50, f"자동 스크롤이 바닥에 도달하지 않음 (여유 {dist}px)"

    def test_live_stream_does_not_render_per_event(self, stack, page):
        """접속 **후** 라이브로 도착하는 이벤트도 병합된다 — 스냅샷 경로만
        고치고 라이브 경로를 놓치는 반쪽 회귀 방지."""
        stack.emit_ready()
        page.add_init_script(_INSTRUMENT)
        page.goto(stack.url)
        page.wait_for_selector("#overview", timeout=8000)
        # 접속 후 카운터를 0 으로 리셋하고 라이브 버스트를 쏜다
        page.evaluate("window.__perf.ovWrites = 0; window.__perf.scrollMsgs = 0;")
        before_cards = page.evaluate(
            "document.querySelectorAll('#messages > *').length"
        )
        _seed_snapshot(stack, turns=30)
        _wait_settled(page, before_cards + 30 * _EVENTS_PER_TURN)

        perf = page.evaluate("window.__perf")
        assert perf["ovWrites"] <= _COALESCED_MAX, (
            f"라이브 경로 개요 재작성 {perf['ovWrites']}회 — 병합 미적용"
        )
        assert perf["scrollMsgs"] <= _COALESCED_MAX, (
            f"라이브 경로 scrollTop 쓰기 {perf['scrollMsgs']}회 — 병합 미적용"
        )


@pytest.mark.skipif(
    os.environ.get("AGENT_CLI_PERF_KPI") != "1",
    reason="KPI 측정은 AGENT_CLI_PERF_KPI=1 에서만 (릴리스 전 수동 실행)",
)
class TestRenderKpi:
    """KPI 측정 — 단언하지 않고 수치를 남긴다 (docs/PERF-KPI.md 참조).

    출력: ``perf-kpi.json`` (cwd) + stdout 표. ``-s`` 로 실행해야 표가 보인다.
    """

    @pytest.mark.parametrize("turns", [30, 90, 150])
    def test_measure_snapshot_load(self, stack, page, turns, record_property):
        _seed_snapshot(stack, turns=turns)
        stack.emit_ready()
        page.add_init_script(_LONGTASK)  # 계측 오버헤드 최소 (카운터 미주입)
        t0 = time.time()
        page.goto(stack.url)
        _wait_settled(page, turns * _EVENTS_PER_TURN)
        elapsed_ms = round((time.time() - t0) * 1000)
        long_tasks = page.evaluate("window.__lt") or []
        blocking_ms = round(sum(long_tasks))
        worst_ms = round(max(long_tasks, default=0))
        events = turns * _EVENTS_PER_TURN

        row = {
            "events": events,
            "cards": page.evaluate("document.querySelectorAll('#messages > *').length"),
            "blocking_ms": blocking_ms,
            "worst_long_task_ms": worst_ms,
            "load_to_settled_ms": elapsed_ms,
        }
        for k, v in row.items():
            record_property(k, v)
        print(
            f"\n[KPI] events={events:>4}  blocking={blocking_ms:>5}ms  "
            f"worst={worst_ms:>4}ms  settled={elapsed_ms:>5}ms"
        )

        path = "perf-kpi.json"
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {"rows": []}
        data["rows"] = [r for r in data["rows"] if r["events"] != events] + [row]
        data["rows"].sort(key=lambda r: r["events"])
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
