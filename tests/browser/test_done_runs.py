"""끝난 run 칩 접기 — 실브라우저 (v10.143.0, 사용자 승인 시안 A).

채널 줄에는 main·일하는 에이전트·상주(죽은 상주의 ↻ 포함)만 칩이고, 끝난 run 은 줄 끝 `Done N ▾` 하나로
접힌다. 누르면 최근 순 목록(이름·키·과제 앞부분·끝난 시각, 멈췄으면 stopped) — 항목을 누르면 그 채널이
열린다. 지금 보고 있는 채널이 끝난 run 이면 그 칩은 접히지 않는다.

서버 쪽은 실제 WebRenderer 의 표면(`begin_agent_work`·`end_agent_work`·`agent_roster`)만 쓴다 — 레지스트리가
내는 것과 같은 이벤트다. 기다림은 DOM 상태로만 본다.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

#: 스크린샷을 남길 디렉터리 — 비면(기본) 찍지 않는다. 리뷰 때 ``AGENT_CLI_SHOTS=<dir>`` 로.
SHOTS = os.environ.get("AGENT_CLI_SHOTS", "")


def _wait(cond, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _row(key, *, profile, state, run, name=""):
    """명단 한 줄 — `AgentInstance.snapshot` + `open_questions` 의 모양."""
    return {
        "key": key,
        "profile": profile,
        "name": name,
        "state": state,
        "handled": 1,
        "pending_requests": 0,
        "est_tokens": 0,
        "error": "",
        "ends_after_task": run,
        "parent": "main",
        "task_id": f"r-{key[-3:]}" if run else "",
        "item": "",
        "open_questions": [],
    }


TASKS = [
    "find callers of parse_tokens",
    "check docs/parser.md",
    "run the parser tests",
    "review lexer.py",
    "list TODOs in src/",
    "grep for deprecated APIs",
    "summarize test failures",
]
PROFILES = ["explorer", "code-analyst", "general", "code-reviewer"]


class Room:
    """run 14개 끝·2개 일함·상주 2(한가·바쁨) — 시안의 그 순간."""

    def __init__(self, renderer):
        self.r = renderer
        self.rows: dict[str, dict] = {}
        self.done = [f"agt-d{i:03d}" for i in range(14)]
        self.working = ["agt-w001", "agt-w002"]
        self.residents = ["agt-s001", "agt-s002"]
        self.stopped = {self.done[12]}  # 사람이 ■ 한 run — 목록에 stopped

    def task(self, key):
        return TASKS[int(key[-3:]) % len(TASKS)] + f" ({key})"

    def start_run(self, key, profile):
        self.r.begin_agent_work(
            key=key,
            item=f"r-{key[-3:]}",
            profile=profile,
            message=self.task(key) + "\nsecond line of the task",
            caller_task="",
        )

    def end_run(self, key, *, stopped=False):
        self.r.end_agent_work(
            key=key,
            item=f"r-{key[-3:]}",
            success=not stopped,
            duration_s=1.0,
            stopped=stopped,
        )
        self.rows[key]["state"] = "dead"
        self.publish()

    def build(self):
        for i, k in enumerate(self.done):
            prof = PROFILES[i % len(PROFILES)]
            self.rows[k] = _row(k, profile=prof, state="dead", run=True)
            self.start_run(k, prof)
            self.r.end_agent_work(
                key=k,
                item=f"r-{k[-3:]}",
                success=k not in self.stopped,
                duration_s=1.0,
                stopped=k in self.stopped,
            )
        for k in self.working:
            self.rows[k] = _row(k, profile="explorer", state="busy", run=True)
            self.start_run(k, "explorer")
        self.rows[self.residents[0]] = _row(
            self.residents[0], profile="code-reviewer", state="idle", run=False
        )
        self.rows[self.residents[1]] = _row(
            self.residents[1], profile="code-analyst", state="busy", run=False
        )
        self.publish()
        return self

    def publish(self):
        self.r.agent_roster([dict(v) for v in self.rows.values()])


def _chips(page):
    return page.locator("#ov-channels .ov-ch")


def _done(page):
    return page.locator("#ov-done")


def _menu(page):
    return page.locator("#ov-done-menu")


@pytest.fixture
def page2x(browser):
    """스크린샷용 2배 화면."""
    ctx = browser.new_context(
        viewport={"width": 1100, "height": 750}, device_scale_factor=2
    )
    yield ctx.new_page()
    ctx.close()


def _shot(page, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / name))


class TestDoneRunsFold:
    def test_chips_fold_list_open_and_live_moves(self, stack, page2x):
        page = page2x
        stack.emit_ready()
        room = Room(stack.renderer).build()
        page.goto(stack.url)
        # 칩: main + 일하는 run 2 + 상주 2 — 끝난 14 는 `Done 14 ▾` 하나
        assert _wait(lambda: _done(page).count() == 1)
        assert _chips(page).count() == 5
        for k in room.done:
            assert page.locator(f'.ov-ch[data-key="{k}"]').count() == 0
        assert _done(page).inner_text() == "Done 14 ▾"
        assert _done(page).get_attribute("aria-expanded") == "false"
        _shot(page, "done-1-row.png")

        # 목록: 최근 순 8 줄 + "+6 more…", 이름·키·과제 첫 줄·끝난 시각, 멈춘 것은 stopped
        _done(page).click()
        menu = _menu(page)
        assert _wait(lambda: menu.is_visible())
        assert _done(page).get_attribute("aria-expanded") == "true"
        assert "Finished runs — newest first" in menu.inner_text()
        rows = menu.locator(".ov-done-row")
        assert rows.count() == 8
        assert menu.locator(".ov-done-more").inner_text() == "+6 more…"
        keys = [rows.nth(i).get_attribute("data-key") for i in range(8)]
        assert keys == list(reversed(room.done))[:8]
        first = rows.nth(0)
        assert "agt-d013" in first.inner_text()
        assert room.task("agt-d013") in first.locator(".ov-done-task").inner_text()
        assert "second line" not in first.inner_text()  # 과제 앞부분 — 첫 줄만
        assert first.locator(".ov-done-when").inner_text() == "just now"
        stopped = menu.locator('.ov-done-row[data-key="agt-d012"]')
        assert stopped.locator(".ov-done-tag").inner_text() == "stopped"
        assert first.locator(".ov-done-tag").count() == 0
        box = menu.bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= 1100
        _shot(page, "done-2-menu.png")
        menu.locator(".ov-done-more").click()
        assert _wait(lambda: rows.count() == 14)
        assert menu.locator(".ov-done-more").count() == 0

        # 항목을 누르면 그 채널이 열린다 — 보는 동안 그 칩은 접히지 않는다
        menu.locator('.ov-done-row[data-key="agt-d005"]').click()
        assert _wait(lambda: not menu.is_visible())
        chip = page.locator('.ov-ch[data-key="agt-d005"]')
        assert _wait(lambda: chip.count() == 1)
        assert "on" in chip.get_attribute("class") and "done" in chip.get_attribute(
            "class"
        )
        assert _done(page).inner_text() == "Done 13 ▾"
        block = page.locator('#messages > .card-run[data-task-id="agt-d005#r-005"]')
        assert block.is_visible()
        assert page.locator("#input").is_disabled()
        _shot(page, "done-3-viewing.png")
        page.locator('.ov-ch[data-key="main"]').click()
        assert _wait(lambda: chip.count() == 0)
        assert _done(page).inner_text() == "Done 14 ▾"

        # 보던 run 이 끝나도 칩은 남는다(회색), 다른 채널로 가면 접힌다
        w1 = page.locator('.ov-ch[data-key="agt-w001"]')
        w1.locator(".ov-ch-lb").click()
        assert _wait(lambda: "on" in (w1.get_attribute("class") or ""))
        room.end_run("agt-w001")
        assert _wait(lambda: "done" in (w1.get_attribute("class") or ""))
        assert "on" in w1.get_attribute("class")
        assert _done(page).inner_text() == "Done 14 ▾"
        page.locator('.ov-ch[data-key="main"]').click()
        assert _wait(lambda: w1.count() == 0)
        assert _done(page).inner_text() == "Done 15 ▾"

        # 보지 않던 run 이 끝나는 순간 칩이 접힘으로 옮겨간다 — 열린 목록도 따라 자란다
        _done(page).click()
        assert _wait(lambda: menu.is_visible() and rows.count() == 8)
        room.end_run("agt-w002", stopped=True)
        w2 = page.locator('.ov-ch[data-key="agt-w002"]')
        assert _wait(lambda: w2.count() == 0)
        assert _wait(lambda: _done(page).inner_text() == "Done 16 ▾")
        assert _wait(lambda: rows.nth(0).get_attribute("data-key") == "agt-w002")
        assert rows.nth(0).locator(".ov-done-tag").inner_text() == "stopped"
        assert menu.locator(".ov-done-more").inner_text() == "+8 more…"
        assert _chips(page).count() == 3  # main + 상주 2

        # 새로고침 뒤 같은 모양
        page.reload()
        assert _wait(lambda: _done(page).count() == 1)
        assert _done(page).inner_text() == "Done 16 ▾"
        assert _chips(page).count() == 3
        _done(page).click()
        assert _wait(lambda: menu.is_visible())
        assert rows.nth(0).get_attribute("data-key") == "agt-w002"
        assert room.task("agt-w001") in rows.nth(1).inner_text()

    def test_menu_closes_on_outside_click_and_escape(self, stack, page):
        stack.emit_ready()
        Room(stack.renderer).build()
        page.goto(stack.url)
        assert _wait(lambda: _done(page).count() == 1)
        _done(page).click()
        assert _wait(lambda: _menu(page).is_visible())
        page.locator("#messages").click(position={"x": 5, "y": 5})
        assert _wait(lambda: not _menu(page).is_visible())
        assert _done(page).get_attribute("aria-expanded") == "false"
        _done(page).click()
        assert _wait(lambda: _menu(page).is_visible())
        page.keyboard.press("Escape")
        assert _wait(lambda: not _menu(page).is_visible())
        _done(page).click()  # 다시 누르면 닫힌다
        assert _wait(lambda: _menu(page).is_visible())
        _done(page).click()
        assert _wait(lambda: not _menu(page).is_visible())

    def test_dead_resident_keeps_its_chip_and_no_done_without_finished_runs(
        self, stack, page
    ):
        stack.emit_ready()
        stack.renderer.agent_roster(
            [
                _row("agt-s001", profile="code-reviewer", state="dead", run=False),
                _row("agt-w001", profile="explorer", state="busy", run=True),
            ]
        )
        page.goto(stack.url)
        res = page.locator('.ov-ch[data-key="agt-s001"]')
        assert _wait(lambda: res.count() == 1)
        assert res.locator('.ov-ch-ctl[data-act="resume"]').count() == 1  # ↻ 그대로
        assert _done(page).count() == 0  # 끝난 run 이 없으면 보이지 않는다
        assert _chips(page).count() == 3

    def test_phone_width_menu_stays_on_screen(self, stack, browser):
        ctx = browser.new_context(viewport={"width": 390, "height": 760})
        page = ctx.new_page()
        try:
            stack.emit_ready()
            Room(stack.renderer).build()
            page.goto(stack.url)
            assert _wait(lambda: _done(page).count() == 1)
            _done(page).click()
            menu = _menu(page)
            assert _wait(lambda: menu.is_visible())
            box = menu.bounding_box()
            assert box["x"] >= 0 and box["x"] + box["width"] <= 390
            assert box["y"] >= 0
        finally:
            ctx.close()


class TestDoneRunsResume:
    def test_resume_replay_folds_the_same_and_keeps_end_times(
        self, stack, page2x, tmp_path
    ):
        """지난 세션의 사이드카(`scopes.jsonl`)를 재생해도 같은 규칙 — 끝난 시각은 그때의 것."""
        from agent_cli.render.web import WebRenderer

        page = page2x
        sdir = tmp_path / "sess"
        sdir.mkdir()
        recorder = WebRenderer()
        recorder._scope_log_path = sdir / "scopes.jsonl"
        room = Room(recorder).build()
        # 지난 세션의 시각으로 — 끝난 run 마다 6분씩 거슬러 (가장 최근 것이 6분 전)
        lines = [
            json.loads(x) for x in recorder._scope_log_path.read_text().splitlines()
        ]
        order = {k: i for i, k in enumerate(room.done)}
        now = time.time()
        for rec in lines:
            key = rec["task_id"].split("#", 1)[0]
            if key in order:
                back = (len(room.done) - order[key]) * 360
                rec["ts"] = now - back - (5 if rec["event"] == "scope_start" else 0)
        recorder._scope_log_path.write_text(
            "".join(json.dumps(r) + "\n" for r in lines), encoding="utf-8"
        )

        stack.renderer._scope_log_path = sdir / "scopes.jsonl"
        stack.emit_ready()
        stack.renderer.agent_roster([dict(v) for v in room.rows.values()])

        class _Ctx:
            def get_raw_messages(self):
                return [{"role": "user", "content": "go", "ts": "2020-01-01T00:00:00"}]

        stack.renderer.replay_session(_Ctx())
        page.goto(stack.url)
        assert _wait(lambda: _done(page).count() == 1)
        assert _done(page).inner_text() == "Done 14 ▾"
        assert _chips(page).count() == 5
        _done(page).click()
        menu = _menu(page)
        assert _wait(lambda: menu.is_visible())
        rows = menu.locator(".ov-done-row")
        assert rows.nth(0).get_attribute("data-key") == "agt-d013"
        assert rows.nth(0).locator(".ov-done-when").inner_text() == "6m ago"
        assert rows.nth(1).locator(".ov-done-when").inner_text() == "12m ago"
        assert room.task("agt-d013") in rows.nth(0).inner_text()
        assert (
            menu.locator('.ov-done-row[data-key="agt-d012"] .ov-done-tag').inner_text()
            == "stopped"
        )
        _shot(page, "done-4-resume-menu.png")
        rows.nth(0).click()
        chip = page.locator('.ov-ch[data-key="agt-d013"]')
        assert _wait(lambda: chip.count() == 1 and "on" in chip.get_attribute("class"))
        assert _done(page).inner_text() == "Done 13 ▾"
