"""Resume 재생이 **브라우저에 카드로** 그려지는지 — 실브라우저 e2e.

서버 측(`replay_from_history` 가 버퍼에 이벤트를 쌓는가)은 유닛
(`test_web_renderer.py::TestReplayFromHistory`)이 이미 고정한다. 여기서 막는
것은 그 다음 구간이다: **접속 시 snapshot replay 가 실제 DOM 카드가 되는가.**

이 층에 테스트가 없어서 "resume 하면 카드가 하나도 안 뜬다"는 제보가 왔을 때
회귀인지 아닌지 판정할 근거가 없었다(조사 결과 회귀 아님 — 세션 프로세스가
idle-timeout 으로 죽어 upstream 이 없던 케이스였다). 그 판정을 자동화한다.
"""

from __future__ import annotations

import time


def _history():
    """실제 history.jsonl 모양 — user / assistant(ops) / 관찰(user+tool 키).

    ★관찰 레코드는 role="user" + ``tool`` 키다(role="tool" 이 아니다). 이
    모양이 틀리면 재생이 조용히 관찰 카드를 빼먹으므로 계약으로 고정한다.
    """
    return [
        {
            "role": "user",
            "content": "gomoku.html 을 리뷰해줘",
            "ts": "2026-01-15T10:00:00",
        },
        {
            "role": "assistant",
            "thought": "파일을 먼저 읽는다",
            "ops": [{"action": "read_file", "action_input": {"path": "gomoku.html"}}],
            "ts": "2026-01-15T10:00:05",
        },
        {
            "role": "user",
            "content": "Observation: (파일 내용)",
            "tool": "read_file",
            "success": True,
            "ts": "2026-01-15T10:00:06",
        },
        {
            "role": "assistant",
            "thought": "정리 완료",
            "ops": [{"action": "complete", "action_input": {"result": "리뷰 5건"}}],
            "ts": "2026-01-15T10:00:20",
        },
    ]


class _Ctx:
    def __init__(self, msgs):
        self._msgs = msgs

    def get_raw_messages(self):
        return self._msgs


def test_resumed_turns_render_as_cards(stack, page):
    # 프로덕션 순서: 워커/클라이언트 이전에 재생 → 접속 snapshot 으로 전달.
    stack.emit_ready()
    stack.renderer.replay_from_history(_Ctx(_history()))
    page.goto(stack.url)
    page.wait_for_selector("#messages .card", timeout=8000)
    counts = page.evaluate(
        """() => ({
            total: document.querySelectorAll('#messages > .card').length,
            user: document.querySelectorAll('#messages .card-user').length,
            assistant: document.querySelectorAll('#messages .card-assistant').length,
            obs: document.querySelectorAll('#messages .card-observation').length,
        })"""
    )
    assert counts["user"] == 1, counts
    assert counts["assistant"] == 2, counts
    assert counts["obs"] == 1, counts
    assert counts["total"] == 4, counts


def test_resumed_cards_show_original_time_not_now(stack, page):
    """재생 카드는 resume 시각이 아니라 원래 발생 시각을 달아야 한다
    (`_replay_ts` 경로).

    ★히스토리 날짜는 **오늘이 아닌 과거**여야 이 테스트가 무언가를 증명한다 —
    오늘 날짜를 쓰면 `_replay_ts` 를 버려도 wall-clock 폴백이 같은 값을 내서
    구별이 안 된다(첫 버전이 그래서 뮤테이션에 안 물렸다)."""
    stack.emit_ready()
    stack.renderer.replay_from_history(_Ctx(_history()))
    page.goto(stack.url)
    page.wait_for_selector("#messages .card", timeout=8000)
    stamps = page.evaluate(
        """() => [...document.querySelectorAll('#messages .card-time, #messages .card-ts')]
                   .map(e => e.textContent.trim()).filter(Boolean)"""
    )
    assert stamps, "카드에 시각 표시가 없다"
    assert any("260115" in s or "26-01-15" in s for s in stamps), stamps
    # 그리고 오늘 날짜(=resume 시각 폴백)로 찍히지 않았어야 한다.
    # (`time.strftime` = 로컬 시각 — 프런트 카드 스탬프와 같은 기준)
    today = time.strftime("%y%m%d")
    assert not any(today in s for s in stamps), (today, stamps)


def test_replayed_scope_bars_do_not_rebuild_timeline_cards(stack, page):
    """resume 의 scope 재생(`replay:true`)은 **스윔레인만** 복구한다 — 타임라인
    접이 카드를 다시 만들면 내부 턴이 flat 으로 재생되므로 빈 껍데기가 된다."""
    stack.emit_ready()
    stack.renderer.replay_from_history(_Ctx(_history()))
    r = stack.renderer
    for event, data in (
        (
            "scope_start",
            {
                "task_id": "sk1",
                "kind": "skill",
                "label": "orchestrate",
                "parent": "",
                "depth": 0,
                "ts": 1000.0,
                "replay": True,
            },
        ),
        (
            "scope_end",
            {
                "task_id": "sk1",
                "kind": "skill",
                "success": True,
                "ts": 1030.0,
                "replay": True,
            },
        ),
    ):
        r._emit(event, data, persistent=True)
    page.goto(stack.url)
    page.wait_for_selector("#messages .card", timeout=8000)
    time.sleep(0.5)
    # 스윔레인 막대는 복구되고
    assert page.locator("#team-view .tv-scope-skill").count() >= 1
    # 타임라인엔 그 scope 의 카드가 없어야 한다 (flat 재생분만 남음)
    assert page.locator('.card-task-group[data-task-id="sk1"]').count() == 0
    assert page.locator("#messages > .card").count() == 4
