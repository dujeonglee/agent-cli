"""모델 시점 틀 (v10.17.0) — 실브라우저 e2e.

화면은 위에서 아래로 모델이 받는 순서다: 맨 위 "맨 처음 받는 것", 가운데 대화,
맨 아래 "매 턴 끝에 붙는 것". 🔍 드로어는 없다. 컨텍스트에서 빠진 카드는 한
묶음으로 접힌다. main 과 인라인 카드(agent/skill)가 같은 부품을 쓴다.
"""

from __future__ import annotations

import threading
import time

from tests.browser.test_resume_replay import _history

_SECTIONS = [
    ("Role", "You are an agent."),
    ("Available Tools", "- read_file\n- shell"),
    ("Hook: lint", "## lint\nrun ruff"),
]
_SUMMARY = {
    "text": "사용자는 gomoku.html 리뷰를 요청했다.",
    "files": ["gomoku.html"],
    "turns": [1, 2],
    "before_tokens": 18200,
    "after_tokens": 700,
}


class _Ctx:
    def __init__(self, msgs):
        self._msgs = msgs

    def get_raw_messages(self):
        return self._msgs

    def cache_ordinals(self):
        return list(range(len(self._msgs)))


def _tail(turn: int, extra: str = "") -> list[tuple[str, str]]:
    return [
        ("Standing Rules (per-turn tail)", "## Task Guidelines\n- do x"),
        (
            "Session State (per-turn tail)",
            f"turn {turn}/30\n\n## Outstanding user requests\n1. foo{extra}",
        ),
    ]


def _history7():
    return _history() + [
        {"role": "user", "content": "이제 고쳐줘", "ts": "2026-01-15T10:01:00"},
        {
            "role": "assistant",
            "thought": "고친다",
            "ops": [{"action": "edit_file", "action_input": {"path": "g.html"}}],
            "ts": "2026-01-15T10:01:05",
        },
        {
            "role": "user",
            "content": "Observation: ok",
            "tool": "edit_file",
            "success": True,
            "ts": "2026-01-15T10:01:06",
        },
    ]


def _seed_main(stack, *, gone: int | None = 4):
    r = stack.renderer
    stack.emit_ready()
    r.replay_from_history(_Ctx(_history7()))
    r.note_system_prompt(_SECTIONS, 4, grammar=(False, "root ::= x"), tail=_tail(4))
    r.note_system_prompt(
        _SECTIONS, 5, grammar=(False, "root ::= x"), tail=_tail(5, "\n2. bar")
    )
    if gone is not None:
        r.context_view({"gone": {"hidx": gone}, "summary": _SUMMARY, "compactions": 1})


def _run_inline(stack, sid="t1", *, view=None):
    r = stack.renderer

    def body():
        r.begin_scope(task_id=sid, kind="run", index=0, agent="tester", label="테스트")
        r.note_system_prompt(
            [("Role", "sub agent")],
            2,
            tail=[("Session State (per-turn tail)", "turn 2/20")],
        )
        r.observation("2 failed", turn=1, tool_name="shell", success=False)
        if view is not None:
            r.context_view(view)
        r.end_scope(task_id=sid, kind="run", success=True, duration_s=1.2)

    t = threading.Thread(target=body)
    t.start()
    t.join()


def _open(page, stack):
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(stack.url)
    page.wait_for_selector("#messages > .ctx-top .ctx-bar", timeout=8000)
    page.wait_for_function(
        "() => /턴 \\d+\\/30/.test(document.querySelector('#ctx-tail .ctx-bar')"
        "?.textContent || '')",
        timeout=8000,
    )
    return errors


def test_frame_stands_where_the_drawer_was(stack, page):
    _seed_main(stack)
    errors = _open(page, stack)
    facts = page.evaluate(
        """() => ({
            drawer: !!document.querySelector('#inspector'),
            button: !!document.querySelector('#insp-main-btn, .task-inspect'),
            first: document.querySelector('#messages').firstElementChild.className,
            top: document.querySelector('#messages > .ctx-top .ctx-bar').textContent,
            tail: document.querySelector('#ctx-tail .ctx-bar').textContent,
            budget: document.querySelector('#ctx-budget').hidden,
            summaryCards: document.querySelectorAll('#messages > .card.ctx-summary').length,
        })"""
    )
    assert facts["drawer"] is False and facts["button"] is False
    assert facts["first"].startswith("ctx-top")
    assert "시스템 프롬프트 3개 섹션" in facts["top"]
    assert "훅 섹션 1" in facts["top"] and "디코딩 문법" in facts["top"]
    assert "압축 요약 턴 1–2" in facts["top"]  # 요약은 맨 위 카드에 산다
    assert facts["summaryCards"] == 0
    assert "턴 5/30" in facts["tail"] and "Outstanding user requests" in facts["tail"]
    assert "+2" in facts["tail"] and "vs 턴 4" in facts["tail"]  # 직전 턴과의 diff
    assert facts["budget"] is False
    assert errors == []


def test_expanding_shows_what_the_model_received(stack, page):
    _seed_main(stack)
    _open(page, stack)
    page.click("#messages > .ctx-top .ctx-bar")
    page.click("#ctx-tail .ctx-bar")
    opened = page.evaluate(
        """() => ({
            sys: [...document.querySelectorAll('#messages > .ctx-top .insp-system .insp-name')]
                   .map(e => e.firstChild.textContent),
            grammar: !!document.querySelector('#messages > .ctx-top .insp-grammar'),
            summary: document.querySelector('#messages > .ctx-top .ctx-summary')?.textContent,
            tailSecs: document.querySelectorAll('#ctx-tail .insp-tail').length,
            diffIns: document.querySelectorAll('#ctx-tail .insp-diff .ins').length,
        })"""
    )
    assert opened["sys"] == ["Role", "Available Tools", "Hook: lint"]
    assert opened["grammar"] is True
    assert "gomoku.html 리뷰를 요청했다" in opened["summary"]
    assert opened["tailSecs"] == 2 and opened["diffIns"] == 2
    # 다음 턴이 와도 펼친 상태와 사용자가 연 섹션은 그대로다
    page.click("#messages > .ctx-top .insp-system summary")
    stack.renderer.note_system_prompt(
        _SECTIONS, 6, grammar=(False, "root ::= x"), tail=_tail(6, "\n2. bar")
    )
    stack.renderer.token_usage({"in": 10, "out": 1, "context_window": 100}, 6)
    page.wait_for_function(
        "() => /턴 6\\/30/.test(document.querySelector('#ctx-tail .ctx-bar').textContent)",
        timeout=8000,
    )
    kept = page.evaluate(
        """() => ({
            topOpen: !!document.querySelector('#messages > .ctx-top .ctx-body'),
            secOpen: document.querySelector('#messages > .ctx-top .insp-system').open,
        })"""
    )
    assert kept == {"topOpen": True, "secOpen": True}


def test_cards_out_of_context_fold_into_one_group(stack, page):
    _seed_main(stack)
    _open(page, stack)
    state = lambda: page.evaluate(
        """() => ({
            rows: document.querySelectorAll('#messages > .ctx-fold').length,
            text: document.querySelector('#messages > .ctx-fold')?.textContent || '',
            gone: document.querySelectorAll('#messages > .card.ctx-gone').length,
            shown: [...document.querySelectorAll('#messages > .card.ctx-gone')]
                     .filter(e => e.offsetParent !== null).length,
            live: [...document.querySelectorAll('#messages > .card:not(.ctx-gone)')]
                     .filter(e => e.offsetParent !== null).length,
            next: document.querySelector('#messages > .ctx-fold')
                    ?.nextElementSibling.classList.contains('ctx-gone'),
        })"""
    )
    s = state()
    assert s["rows"] == 1 and "카드 3개" in s["text"]
    assert s["gone"] == 3 and s["shown"] == 0  # 기본 접힘
    assert s["live"] == 2 and s["next"] is True  # 묶음은 빠진 카드 바로 앞
    page.click("#messages > .ctx-fold")
    s = state()
    assert s["shown"] == 3  # 펼치면 흐린 카드
    page.click("#messages > .ctx-fold")
    assert state()["shown"] == 0


def test_no_group_without_compaction(stack, page):
    _seed_main(stack, gone=None)
    _open(page, stack)
    assert (
        page.evaluate(
            "() => document.querySelectorAll('#messages > .ctx-fold, .ctx-folded').length"
        )
        == 0
    )
    assert "압축 요약" not in page.inner_text("#messages > .ctx-top .ctx-bar")


def test_inline_card_has_the_same_three_parts(stack, page):
    _seed_main(stack, gone=None)
    _run_inline(stack)
    requests: list[str] = []
    page.on(
        "request",
        lambda r: requests.append(r.url) if "api/debug/prompt" in r.url else None,
    )
    errors = _open(page, stack)
    page.wait_for_selector(".card-task-group", timeout=8000)
    time.sleep(0.4)
    # 접힌 카드는 틀을 가져오지 않는다 — 재생이 카드를 수백 개 만들어도 요청 0
    assert not [u for u in requests if "task_id=t1" in u]
    hidden = page.evaluate(
        """() => [...document.querySelectorAll('.card-task-group > .ctx-top,'
              + ' .card-task-group > .ctx-tail')].filter(e => e.offsetParent).length"""
    )
    assert hidden == 0
    page.click(".card-task-group .task-header")
    page.wait_for_function(
        "() => /턴 2\\/20/.test(document.querySelector('.card-task-group > .ctx-tail')"
        "?.textContent || '')",
        timeout=8000,
    )
    card = page.evaluate(
        """() => {
            const c = document.querySelector('.card-task-group');
            return {
                order: [...c.children].map(e => e.className.split(' ')[0]),
                top: c.querySelector(':scope > .ctx-top .ctx-bar').textContent,
                inner: c.querySelectorAll(':scope > .task-body > .card').length,
                copy: c.querySelectorAll(':scope > .task-body > .card > .card-copy').length,
            };
        }"""
    )
    assert card["order"][:4] == ["task-header", "ctx-top", "task-body", "ctx-tail"]
    assert "시스템 프롬프트 1개 섹션" in card["top"]
    # 카드 안에 놓인 카드도 마무리 배관(복사 버튼)을 끝까지 탄다 — 종전엔
    # appendToTimeline 이 여기서 ReferenceError 로 끊겼다.
    assert card["inner"] == 1 and card["copy"] == 1
    assert errors == []


def test_ended_inline_scope_view_stays_in_its_card(stack, page):
    """끝난 인라인 스코프의 sticky 뷰가 재접속 스냅샷에서 main 의 뷰를 덮으면
    main 의 묶음이 사라진다 — 뷰는 자기 카드에 머문다."""
    _seed_main(stack)
    _run_inline(stack, view={"gone": None, "summary": None})
    _open(page, stack)
    page.wait_for_selector("#messages > .ctx-fold", timeout=8000)
    assert (
        page.evaluate(
            "() => document.querySelectorAll('#messages > .card.ctx-gone').length"
        )
        == 3
    )
    assert "압축 요약" in page.inner_text("#messages > .ctx-top .ctx-bar")


def test_copy_all_keeps_the_summary_and_folded_cards(stack, page):
    _seed_main(stack)
    _open(page, stack)
    md = page.evaluate("() => window.__timelineMarkdown()")
    assert md.startswith("> ⊙ **압축 요약** · 턴 1–2")
    assert "gomoku.html 을 리뷰해줘" in md  # 접힌 카드도 대화의 일부다


def _main_calls_one_shot_agent(stack):
    """라이브 순서 그대로: main 턴 1 `agent` → 서브에이전트 턴 1 `shell` · 관찰 ·
    턴 2 최종답 → 스코프 종료 → main 의 `agent` 관찰 → main 턴 2 최종답.
    턴 번호는 루프마다 1 부터라 main 의 턴 1 과 서브에이전트의 턴 1 이 겹친다."""
    r = stack.renderer
    r.thought("서브에이전트를 실행한다", turn=1)
    r.action("agent", '{"mode": "run", "task": "count lines"}', turn=1)

    def sub():
        r.begin_scope(task_id="d1", kind="run", index=0, agent="", label="agent")
        r.thought("wc 로 센다", turn=1)
        r.action("shell", '{"command": "wc -l notes.txt"}', turn=1)
        r.observation("3 notes.txt", turn=1, tool_name="shell", success=True)
        r.final("3", turn=2)
        r.end_scope(task_id="d1", kind="run", success=True, duration_s=1.0)

    t = threading.Thread(target=sub)
    t.start()
    t.join()
    r.observation("STATUS: success", turn=1, tool_name="agent", success=True)
    r.final("3줄입니다", turn=2)


def test_sub_agent_steps_stay_in_its_own_card(stack, page):
    """서브에이전트의 스텝이 main 의 스텝 카드로 들어가면 안 된다 — 종전엔 스텝
    병합의 열쇠가 채널이라 "같은 턴의 두 번째 op" 로 보였다(라이브만)."""
    stack.emit_ready()
    _main_calls_one_shot_agent(stack)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(stack.url)
    page.wait_for_selector("#messages > .card-assistant .final", timeout=8000)
    got = page.evaluate(
        """() => {
            const tools = el => [...el.querySelectorAll('.row.act .k')].map(e => e.textContent);
            const steps = [...document.querySelectorAll('#messages > .card-assistant.step')];
            const g = document.querySelector('.card-task-group > .task-body');
            return {
                mainTools: steps.map(tools),
                mainObs: steps.map(s => s.querySelectorAll('.row.ok, .row.bad').length),
                orphanObs: document.querySelectorAll('#messages > .card-observation').length,
                subCards: [...g.children].filter(c => c.classList.contains('card'))
                            .map(c => c.classList.contains('step') ? 'step' : 'final'),
                subTools: [...g.querySelectorAll(':scope > .card.step')].map(tools),
                subObs: g.querySelectorAll(':scope > .card.step .row.ok').length,
            };
        }"""
    )
    assert got["mainTools"] == [["agent"]], got  # main 의 카드에는 자기 행동만
    assert got["mainObs"] == [1] and got["orphanObs"] == 0, got  # 관찰은 제 짝에
    assert got["subCards"] == ["step", "final"], (
        got
    )  # 서브에이전트의 스텝은 자기 카드에
    assert got["subTools"] == [["shell"]] and got["subObs"] == 1, got
    assert errors == []
