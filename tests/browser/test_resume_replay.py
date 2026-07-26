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


def _wait(cond, timeout=8.0, step=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(step)
    return False


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


def test_replayed_scope_rebuilds_both_bar_and_card(stack, page):
    """resume 의 scope 재생은 **스윔레인 막대 + 타임라인 카드** 둘 다 복구한다.

    v7.21~7.27 은 카드를 건너뛰었다("내부 턴이 flat 재생이라 빈 껍데기") — 그
    결과 resume 후 skill/agent 카드가 0 이고 막대 클릭이 무동작이었다. v7.28.0
    부터 resume 은 시각 순서 단일 스트림이라 카드가 자기 턴보다 먼저 열리므로
    껍데기가 아니다(턴이 없으면 카드 안에 사유를 적는다)."""
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
    # 스윔레인 막대 + 타임라인 카드 둘 다
    assert page.locator("#team-view .tv-scope-skill").count() >= 1
    assert page.locator('.card-task-group[data-task-id="sk1"]').count() == 1
    # 이 히스토리엔 task_id 가 없으니 카드는 비어 있고 사유가 적힌다.
    assert (
        page.locator('.card-task-group[data-task-id="sk1"] .task-empty-note').count()
        == 1
    )


class TestResumeRebuildsScopeCards:
    """resume 후 skill/agent 카드와 클릭-네비 (v7.28.0).

    v7.21.0 은 재생 scope 를 **막대만** 복구했다(카드는 "빈 껍데기 방지"로 스킵).
    결과: resume 하면 flat 카드만 남고 **scope 카드 0** → 스윔레인이 존재하지
    않는 카드로의 네비게이션을 광고했다(라이브 실측: 막대 23개, 클릭 시
    scrollTop 불변). 수리=①history 레코드에 `task_id` ②resume 을 **시각 순서
    단일 스트림**으로 재생(카드가 자기 턴보다 먼저 열림) ③대상 없는 막대 클릭엔
    안내 표시.
    """

    @staticmethod
    def _sidecar(tmp_path, *, t0=100.0, t1=200.0):
        import json

        (tmp_path / "scopes.jsonl").write_text(
            json.dumps(
                {
                    "event": "scope_start",
                    "task_id": "sk1",
                    "kind": "skill",
                    "label": "orchestrate",
                    "parent": "",
                    "depth": 0,
                    "ts": t0,
                }
            )
            + "\n"
            + json.dumps(
                {
                    "event": "scope_end",
                    "task_id": "sk1",
                    "kind": "skill",
                    "success": True,
                    "duration_s": t1 - t0,
                    "ts": t1,
                }
            )
            + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _iso(epoch):
        import datetime as dt

        return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat()

    def _records(self, *, with_task_id):
        base = [
            {"role": "user", "content": "오목 게임 만들어줘", "ts": self._iso(95.0)},
            {
                "role": "assistant",
                "thought": "스킬 안에서 계획한다",
                "ops": [{"action": "read_file", "action_input": {"path": "plan.md"}}],
                "ts": self._iso(120.0),
            },
            {
                "role": "user",
                "content": "Observation: 계획 3단계",
                "tool": "read_file",
                "success": True,
                "ts": self._iso(150.0),
            },
        ]
        if with_task_id:
            for r in base[1:]:
                r["task_id"] = "sk1"
        return base

    def _drive(self, stack, tmp_path, *, with_task_id):
        self._sidecar(tmp_path)
        stack.renderer._scope_log_path = tmp_path / "scopes.jsonl"
        stack.emit_ready()
        stack.renderer.replay_session(_Ctx(self._records(with_task_id=with_task_id)))

    def test_scope_card_is_rebuilt_with_its_turns_inside(self, stack, page, tmp_path):
        self._drive(stack, tmp_path, with_task_id=True)
        page.goto(stack.url)
        page.wait_for_selector('.card-task-group[data-task-id="sk1"]', timeout=8000)
        card = page.locator('.card-task-group[data-task-id="sk1"]')
        assert card.count() == 1
        # 턴이 카드 **안에** 들어가야 한다 (루트 형제가 아니라).
        inner = page.locator('.card-task-group[data-task-id="sk1"] .task-body .card')
        assert inner.count() >= 2, inner.count()
        # 카드는 닫힌 상태(✓ + duration)로 복구된다.
        assert "task-ok" in (card.get_attribute("class") or "")
        # 루트에는 스코프 밖 user 카드 + scope 카드만.
        assert page.locator("#messages > .card").count() == 2

    def test_swimlane_click_navigates_to_the_rebuilt_card(self, stack, page, tmp_path):
        self._drive(stack, tmp_path, with_task_id=True)
        page.goto(stack.url)
        page.wait_for_selector("#team-view .tv-scope-skill", timeout=8000)
        page.wait_for_selector('.card-task-group[data-task-id="sk1"]', timeout=8000)
        page.locator("#team-view .tv-scope-skill").first.click()
        card = page.locator('.card-task-group[data-task-id="sk1"]')
        assert _wait(lambda: "tv-nav-hl" in (card.get_attribute("class") or ""))
        assert page.locator("#team-view .tv-nav-miss:not([hidden])").count() == 0

    def test_old_session_card_explains_why_it_is_empty(self, stack, page, tmp_path):
        """task_id 없는 구 세션: 카드는 복구되지만 내용이 없다 → 사유를 밝힌다."""
        self._drive(stack, tmp_path, with_task_id=False)
        page.goto(stack.url)
        page.wait_for_selector('.card-task-group[data-task-id="sk1"]', timeout=8000)
        note = page.locator(
            '.card-task-group[data-task-id="sk1"] .task-body .task-empty-note'
        )
        assert note.count() == 1
        assert "히스토리에 없습니다" in (note.inner_text() or "")

    def test_bar_without_a_card_shows_a_notice(self, stack, page, tmp_path):
        """ⓒ 대상 없는 막대 클릭 = 조용한 무동작 금지."""
        self._sidecar(tmp_path)
        stack.renderer._scope_log_path = tmp_path / "scopes.jsonl"
        stack.emit_ready()
        # 막대만 복구 (카드 생성 이벤트 없이 스윔레인만) — 옛 동작 재현
        for _ts, event, data in stack.renderer._scope_replay_events():
            if event == "scope_start":
                page_data = dict(data)
                stack.renderer._emit("agent_roster", {"roster": []}, persistent=False)
                stack.renderer._emit(
                    "scope_status",
                    {"task_id": page_data["task_id"], "status": ""},
                    persistent=False,
                )
        page.goto(stack.url)
        page.evaluate(
            """() => {
                const d = {task_id: 'ghost', kind: 'skill', label: 'gone', ts: 100,
                          parent: '', depth: 0, replay: true};
                if (window.TeamView) TeamView.ingest('scope_start', d);
                if (window.TeamView) TeamView.ingest('scope_end',
                    {task_id: 'ghost', kind: 'skill', success: true, ts: 200, replay: true});
                document.getElementById('view-toggle').hidden = false;
                TeamView.setActive(true);
            }"""
        )
        page.wait_for_selector(
            "#team-view [data-task-id='ghost']", state="attached", timeout=8000
        )
        page.locator("#team-view [data-task-id='ghost']").first.click()
        assert _wait(lambda: page.locator(".tv-nav-miss:not([hidden])").count() == 1)
        assert "카드가 없습니다" in page.locator(".tv-nav-miss").inner_text()
