"""🧵 백그라운드 작업 칩 (v10.121.0, docs/complete-while-running/REVIEW-fable.md 후속
리뷰) — 실브라우저.

정적 검사는 JS 에 이름이 있는지까지만 본다. 진짜 `JobRegistry` 의 띄움이 칩으로 뜨는지,
경과가 브라우저에서 흐르는지, ■ → 칩 안의 확인 → Stop 이 진짜 프로세스를 멈추고 그 뷰어의
닉네임이 소유자 메일에 실리는지, 끝나면 칩이 내려가는지는 여기서만 잡힌다.
"""

from __future__ import annotations

import os
import time

import pytest

from agent_cli.jobs import JobRegistry, set_job_registry
from tests.monitor_delivery import RecordingDelivery


def _wait(cond, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture
def jobs(stack, tmp_path):
    reg = JobRegistry(tmp_path)
    reg.deliver = RecordingDelivery()
    reg.on_change = stack.renderer.jobs_changed
    set_job_registry(reg)
    yield reg
    reg.stop_all()
    set_job_registry(None)


class TestJobChips:
    def test_chip_elapsed_confirm_stop_and_hide(self, stack, page, jobs):
        stack.emit_ready()
        page.goto(stack.url)
        chips = page.locator("#job-chips")
        assert chips.is_hidden()
        job = jobs.start("sleep 60", owner="main")
        assert _wait(lambda: chips.is_visible())
        chip = page.locator(f'#job-chips .job-chip[data-id="{job.id}"]')
        assert "🧵 #1" in chip.inner_text() and "sleep 60" in chip.inner_text()
        # 입력창 바로 위 — 🔁 띠 뒤
        order = page.evaluate(
            "() => [...document.querySelector('footer').children].map(e => e.id)"
        )
        assert (
            order.index("stuck-band")
            < order.index("job-chips")
            < order.index("input-area")
        )
        # 경과는 브라우저가 started_at 에서 센다
        first = chip.locator(".job-elapsed").inner_text()
        assert _wait(lambda: chip.locator(".job-elapsed").inner_text() != first, 4)
        # ■ 는 바로 멈추지 않고 칩 안에서 묻는다 — Cancel 은 아무것도 안 한다
        chip.locator(".job-stop").click()
        assert "Stop job #1?" in chip.inner_text()
        chip.locator(".job-cancel").click()
        assert chip.locator(".job-stop").is_visible() and job.running
        chip.locator(".job-stop").click()
        chip.locator(".job-confirm").click()
        assert _wait(lambda: not job.running)
        assert _wait(lambda: not _alive(job.pid))
        assert _wait(lambda: chips.is_hidden())
        # 멈춘 사람 = 이 페이지의 뷰어 닉네임
        names = set(stack.renderer.hub._nicknames.values())
        (report,) = jobs.deliver.reports
        who = report.split("stopped by user ", 1)[1].split(" after ", 1)[0]
        assert who in names and who != "", (who, names)
        assert report.startswith("🧵 job #1 stopped by user ")

    def test_late_viewer_sees_running_jobs_and_agent_owner(self, stack, page, jobs):
        stack.emit_ready()
        job = jobs.start("sleep 60", owner="agent:k")
        page.goto(stack.url)  # 띄운 뒤에 들어온 뷰어 — 스티키 재생
        chip = page.locator(f'#job-chips .job-chip[data-id="{job.id}"]')
        assert _wait(lambda: chip.is_visible())
        assert "agent:k" in chip.inner_text()
        jobs.kill(job)  # 하니스의 정리 — 칩이 내려간다
        assert _wait(lambda: page.locator("#job-chips").is_hidden())
