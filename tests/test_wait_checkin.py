"""`wait` 중간 깨우기 (v10.79.0, docs/wait/DESIGN.md §6).

고정하는 계약: 받아들여진 `wait` 가 깨지 않으면 5분 뒤 하니스가 깨운다 · 같은 대상
집합을 다시 기다리면 10·20·40·60·60분, 집합이 바뀌면 5분 · 런이 시작되면 타이머가
풀린다(입력 없는 빈 깨우기 런은 풀지 않는다) · 알림은 사실 진술("still running")이고
장부 항목이 되지 않으며 영속되지 않는다 · 작업 줄엔 로그 변화(P(a)), monitor 줄엔
남은 시간, 에이전트 줄엔 레지스트리가 아는 상태 · 예약만 기다리면 걸지 않는다 ·
`kill -- -<pid>` 가 네 자리에서 같다.
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import MagicMock

import pytest

from agent_cli.constants import CHECKIN_STEPS_S, format_duration
from agent_cli.dialects import get as get_dialect
from agent_cli.jobs import JobRegistry
from agent_cli.loop.core import mail_item
from agent_cli.monitor import MonitorRegistry, build
from agent_cli.prompts.system_prompt import build_live_agents_section
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.session_slots import (
    get_agent_registry,
    set_agent_registry,
    set_job_registry,
    set_monitor_registry,
)
from agent_cli.subagent.agent_mail import build_reply_record
from agent_cli.subagent.agents_live import AgentInstance
from agent_cli.wake import Mailbox
from tests.loop_ports import make_ports, queue_inbox
from tests.loopkit import make_loop
from tests.mail_support import absorbed_mail, agent_wake_lines, checkin_ids
from tests.monitor_delivery import RecordingDelivery
from tests.test_agents_live import make_registry, make_runner, wait_until


class FakeTimer:
    """`threading.Timer` 자리 — 시작·취소만 기록하고 스스로 발화하지 않는다.
    테스트가 `fire_checkin` 을 직접 부른다."""

    made: ClassVar[list[FakeTimer]] = []

    def __init__(self, interval, fn, args=()):
        self.interval, self.fn, self.args = interval, fn, args
        self.started = self.cancelled = False
        self.daemon = False
        FakeTimer.made.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


@pytest.fixture
def jobs(tmp_path):
    r = JobRegistry(tmp_path)
    r.deliver = lambda *a, **k: ""
    set_job_registry(r)
    yield r
    r.stop_all()
    set_job_registry(None)


@pytest.fixture
def reg(tmp_path, monkeypatch):
    from agent_cli.session_slots import set_schedule_registry

    set_schedule_registry(None)
    FakeTimer.made.clear()
    r = make_registry(tmp_path)
    r.checkin_timer_factory = FakeTimer
    set_agent_registry(r)
    yield r
    set_agent_registry(None)
    r.shutdown_all()


@pytest.fixture
def mons():
    r = MonitorRegistry()
    r.deliver = RecordingDelivery()
    r.stop()
    set_monitor_registry(r)
    yield r
    set_monitor_registry(None)
    r.stop()


def _armed(reg, owner="main"):
    return reg._checkin_armed.get(owner)


def _mail(reg):
    return [m for m in absorbed_mail(reg) if m.get("kind") == "checkin"]


# ── 걸고 풀기 ────────────────────────────────────────────────


class TestArmAndFire:
    def test_arm_uses_the_targets_it_is_given(self, reg):
        """체크인 대상은 받아들일 때 읽은 것을 넘겨받는다(5c-1 4b) — 거는 순간에 다시 묻지 않는다: 지금은 아무것도
        돌지 않아도 넘겨받은 대상으로 건다."""
        ids = frozenset({"job:1"})
        assert reg.next_checkin_s("main", ids) == 300
        assert reg.arm_checkin("main", ids) == 300
        assert _armed(reg).ids == ids
        reg.run_started("main")

    def test_wait_arms_five_minutes_and_fires_a_fact_statement(self, reg, jobs):
        job = jobs.start("sleep 30", owner="main")
        assert reg.next_checkin_s("main", checkin_ids("main")) == 300
        assert (
            reg.arm_checkin("main", checkin_ids("main")) == 300
        )  # 제자리 `wait` 가 기다리기 시작할 때 건다(`Mailbox.arm_checkin`)
        ck = _armed(reg)
        assert ck is not None and ck.interval_s == 300 and ck.step == 0
        assert ck.ids == {"job:1"} and ck.log_marks == {1: job.header_bytes}
        timer = FakeTimer.made[-1]
        assert timer.started and timer.daemon and timer.interval == 300

        text = reg.fire_checkin("main")
        lines = text.splitlines()
        assert lines[0] == "⏰ Check-in after 5m — still running:"
        assert lines[1].startswith("job #1 · ") and "`sleep 30`" in lines[1]
        assert lines[2] == (
            "  log: .agent-cli/tmp/jobs/1.log · nothing after the command line, "
            "unchanged since your wait"
        )
        assert lines[3] == (
            "If it is making progress, call `wait` again; if nothing reports, you "
            "are woken again in 10m. If it is not, check its output files, or stop "
            f"it (`kill -- -{job.pid}`) and change the approach."
        )
        assert _armed(reg) is None  # 발화했으니 걸린 것은 없다

        (mail,) = _mail(reg)
        assert mail["kind"] == "checkin" and mail["ledger"] == "none"
        assert mail["expects_reply"] is False and mail["author"] == "main"
        assert mail["item_id"].startswith("n-")
        rec = build_reply_record(mail)
        assert rec["tool"] == "wait" and rec["content"] == text and rec["success"]
        assert mail_item(mail) is None  # 장부 밖 — 열린 항목이 되지 않는다

    def test_log_growth_since_the_wait(self, reg, jobs, tmp_path):
        job = jobs.start("sleep 30", owner="main")
        reg.arm_checkin("main", checkin_ids("main"))
        with (tmp_path / job.log).open("a") as f:
            f.write("frame 1 done\nframe 2 done\nframe 3 done\n")
        text = reg.fire_checkin("main")
        assert (
            "  log: .agent-cli/tmp/jobs/1.log · +3 lines since your wait (last 2 lines)"
            in text
        )
        assert "  frame 2 done\n  frame 3 done\n" in text
        assert "frame 1" not in text

    def test_run_started_cancels_the_timer_and_nothing_is_sent(self, reg, jobs):
        jobs.start("sleep 30", owner="main")
        reg.arm_checkin("main", checkin_ids("main"))
        timer = FakeTimer.made[-1]
        reg.run_started("main")
        assert timer.cancelled and _armed(reg) is None
        assert reg.fire_checkin("main") == "" and _mail(reg) == []

    def test_nothing_running_at_fire_time_sends_nothing(self, reg, jobs):
        job = jobs.start("sleep 30", owner="main")
        reg.arm_checkin("main", checkin_ids("main"))
        jobs.kill(job)  # 통지 없이 사라졌다(소유자 사망·세션 종료 경로)
        assert reg.fire_checkin("main") == "" and _mail(reg) == []

    def test_schedule_only_wait_arms_nothing(self, reg, monkeypatch):
        from agent_cli.session_slots import set_schedule_registry

        set_schedule_registry(SimpleNamespace(has_active_work=lambda: True))
        assert Mailbox(get_agent_registry(), "main").wake_sources() == ["a schedule"]
        assert checkin_ids("main") == frozenset()
        assert reg.next_checkin_s("main", checkin_ids("main")) is None
        assert (
            reg.arm_checkin("main", checkin_ids("main")) is None and _armed(reg) is None
        )

    def test_a_schedule_beside_a_job_arms_but_is_not_in_the_notice(
        self, reg, jobs, monkeypatch
    ):
        from agent_cli.session_slots import set_schedule_registry

        set_schedule_registry(SimpleNamespace(has_active_work=lambda: True))
        jobs.start("sleep 30", owner="main")
        assert checkin_ids("main") == {"job:1"}
        reg.arm_checkin("main", checkin_ids("main"))
        text = reg.fire_checkin("main")
        assert "job #1" in text and "schedule" not in text


class TestDoubling:
    def test_same_targets_double_to_an_hour_and_a_new_target_resets(self, reg, jobs):
        jobs.start("sleep 30", owner="main")
        seen = []
        for _ in range(6):
            assert reg.next_checkin_s("main", checkin_ids("main")) == reg.arm_checkin(
                "main", checkin_ids("main")
            )
            seen.append(_armed(reg).interval_s)
            assert reg.fire_checkin("main")
        assert seen == [300, 600, 1200, 2400, 3600, 3600]
        assert tuple(seen[:5]) == CHECKIN_STEPS_S
        # "woken again in" 은 다음 단계 — 마지막은 60분에 머문다
        jobs.start("sleep 30", owner="main")  # 대상 집합이 바뀌었다
        assert reg.next_checkin_s("main", checkin_ids("main")) == 300
        reg.arm_checkin("main", checkin_ids("main"))
        assert _armed(reg).step == 0

    def test_next_interval_in_the_notice_follows_the_step(self, reg, jobs):
        jobs.start("sleep 30", owner="main")
        reg.arm_checkin("main", checkin_ids("main"))
        assert "woken again in 10m" in reg.fire_checkin("main")
        reg.arm_checkin("main", checkin_ids("main"))
        assert "Check-in after 10m" in (t := reg.fire_checkin("main"))
        assert "woken again in 20m" in t
        for _ in range(3):
            reg.arm_checkin("main", checkin_ids("main"))
            t = reg.fire_checkin("main")
        assert "Check-in after 1h" in t and "woken again in 1h" in t

    def test_a_cancelled_timer_does_not_advance_the_step(self, reg, jobs):
        """진짜 보고로 깨면(타이머가 풀리면) 단계는 직전에 **발화한** 깨우기 기준이다."""
        jobs.start("sleep 30", owner="main")
        reg.arm_checkin("main", checkin_ids("main"))
        reg.run_started("main")  # 발화 전에 런이 시작됐다
        assert reg.next_checkin_s("main", checkin_ids("main")) == 300


# ── 알림 내용 ────────────────────────────────────────────────


class TestNoticeLines:
    def test_two_jobs_list_both_kills(self, reg, jobs):
        a = jobs.start("sleep 30", owner="main")
        b = jobs.start("sleep 31", owner="main")
        reg.arm_checkin("main", checkin_ids("main"))
        text = reg.fire_checkin("main")
        assert "job #1 ·" in text and "job #2 ·" in text
        assert f"`kill -- -{a.pid}` for job #1; `kill -- -{b.pid}` for job #2" in text

    def test_monitor_line_and_delete_hint(self, reg, mons, tmp_path):
        log = tmp_path / "ocr.log"
        log.write_text("")
        mon = mons.add(
            build({"file": str(log), "pattern": "done"}), owner="main", deadline_s=6540
        )
        assert checkin_ids("main") == {f"mon:{mon.id}"}
        reg.arm_checkin("main", checkin_ids("main"))
        text = reg.fire_checkin("main")
        assert f"monitor {mon.id} · `file` + `pattern` on {log} · 1h49m left" in text
        assert (
            "If it is not, check the files it should have written, or delete the "
            'monitor (`monitor` with `mode` "delete") and change the approach.'
        ) in text
        assert "kill" not in text

    def test_job_and_monitor_together(self, reg, jobs, mons, tmp_path):
        job = jobs.start("sleep 30", owner="main")
        log = tmp_path / "ocr.log"
        log.write_text("")
        mons.add(build({"file": str(log), "idle": "5m"}), owner="main", deadline_s=7200)
        reg.arm_checkin("main", checkin_ids("main"))
        text = reg.fire_checkin("main")
        assert f"`file` + `idle` on {log} for 5m · 2h left" in text
        assert (
            f'stop it (`kill -- -{job.pid}`; `monitor` with `mode` "delete" for a '
            "monitor)"
        ) in text

    def test_another_owners_work_is_not_in_my_notice(self, reg, jobs):
        jobs.start("sleep 30", owner="main")
        jobs.start("sleep 40", owner="agent:x")
        reg.arm_checkin("main", checkin_ids("main"))
        text = reg.fire_checkin("main")
        assert "job #1" in text and "job #2" not in text


def _fake_agent(
    reg,
    key,
    *,
    state="idle",
    current_author="main",
    profile="explorer",
    ordinal=0,
    last_at=0.0,
    inbox=(),
):
    """워커 없는 진짜 `AgentInstance` — 레지스트리가 읽는 필드만 세운다."""
    tm = AgentInstance(
        key,
        profile_name=profile,
        role_prompt="",
        allowed_tools=None,
        model="m",
        hooks_config=None,
        context_mode="fresh",
        home_dir=reg.session_dir / key,
    )
    reg._agents[key] = tm
    if state == "busy":
        # 일하는 중 = 그 요청자의 요청을 런이 집어 들고 있다 — 워커 없는 에이전트라 런 도중 흡수(공개 길)로 집는다.
        # 집은 요청은 빚이 된다(런 시작·흡수가 같은 규칙)
        assert reg.request(key, "job", author=current_author) == ""
        assert len(reg.absorb_pending(tm)) == 1
    # 대기열의 요청 = 보낸 쪽이 회신을 기다리는 것 — 받은편지함에 넣는 공개 길(`request`)
    for it in inbox:
        assert reg.request(key, "job", author=it["author"]) == ""
    tm.state = state
    tm.current_author = current_author
    tm.ctx = SimpleNamespace(last_ordinal=ordinal, last_added_at=last_at)
    return tm


class TestAgentLines:
    """새 필드 없이 레지스트리가 아는 것만으로 — 상태 넷과 사람 질문 줄."""

    def test_working_agent_counts_steps_since_the_wait(self, reg):
        tm = _fake_agent(reg, "k", state="busy", ordinal=3, last_at=time.time() - 40)
        assert checkin_ids("main") == {"agent:k"}
        reg.arm_checkin("main", checkin_ids("main"))
        assert _armed(reg).step_marks == {"agent:k": 3}
        tm.ctx.last_ordinal = 15
        text = reg.fire_checkin("main")
        assert (
            "agent:k (explorer) · working on your request · 12 steps since your "
            "wait, last one 40s ago"
        ) in text
        assert text.splitlines()[-1] == (
            "If they are making progress, call `wait` again; if nothing reports, you "
            "are woken again in 10m. An agent whose steps have stopped may be stuck: "
            '`agent` with `mode` "kill" ends it.'
        )
        assert "kill --" not in text and "output files" not in text

    def test_a_stalled_agent_shows_no_step(self, reg):
        _fake_agent(reg, "k", state="busy", ordinal=7, last_at=time.time() - 540)
        reg.arm_checkin("main", checkin_ids("main"))
        text = reg.fire_checkin("main")
        assert (
            "working on your request · no step since your wait, last one 9m ago" in text
        )

    def test_queued_request_shows_its_place_in_line(self, reg):
        _fake_agent(
            reg,
            "j",
            state="busy",
            current_author="agent:z",
            profile="writer",
            last_at=time.time() - 60,
            inbox=[{"author": "agent:z"}, {"author": "agent:y"}, {"author": "main"}],
        )
        reg.arm_checkin("main", checkin_ids("main"))
        text = reg.fire_checkin("main")
        assert (
            "agent:j (writer) · has your request queued behind 2 others · busy on "
            "another request, last step 1m ago"
        ) in text

    def test_parked_debtor_shows_what_it_waits_on(self, reg, jobs):
        _fake_agent(reg, "m", profile="coder")  # idle, 받은편지함 빔
        reg.mark_waiting("agent:m")  # 받아들여진 `wait` — 스스로 잠듦(기다림 표시)
        reg.note_owes("agent:m", "main", "t", id="r-1")
        jobs.start("pytest -x tests/", owner="agent:m")
        assert checkin_ids("main") == {"agent:m"}
        reg.arm_checkin("main", checkin_ids("main"))
        text = reg.fire_checkin("main")
        # 이름만(v10.166.0) — 종전엔 `wait` 수락 문장의 줄 그대로 "job #1 (`pytest -x tests/`)" 였다
        assert text.splitlines()[1] == (
            "agent:m (coder) · owes you a reply · itself waiting on job #1"
        )
        assert "answered by the user" not in text

    def test_parked_debtor_blocked_on_a_human_question_adds_the_line(
        self, reg, jobs, monkeypatch
    ):
        _fake_agent(reg, "n", profile="reviewer")
        reg.mark_waiting("agent:n")  # 받아들여진 `wait` — 스스로 잠듦(기다림 표시)
        reg.note_owes("agent:n", "main", "t", id="r-2")
        jobs.start("sleep 30", owner="agent:n")
        qid, err = reg.register_question("n", "?")  # 사람에게 건 질문 — 공개 길
        assert not err
        reg.arm_checkin("main", checkin_ids("main"))
        asked = time.time()
        with monkeypatch.context() as m:
            m.setattr(time, "time", lambda: asked + 540)  # 9분 뒤의 알림
            text = reg.fire_checkin("main")
        assert (
            "agent:n (reviewer) · owes you a reply · itself waiting on job #1 "
            f'· blocked on question "{qid}" to the user, unanswered for 9m'
        ) in text
        assert text.endswith(
            "A question to the user is answered by the user, not by you."
        )

    def test_busy_debtor_line(self, reg):
        _fake_agent(
            reg, "p", state="busy", current_author="agent:q", last_at=time.time() - 5
        )
        reg.note_owes("agent:p", "main", "t", id="r-3")
        reg.arm_checkin("main", checkin_ids("main"))
        text = reg.fire_checkin("main")
        assert (
            "agent:p (explorer) · owes you a reply · busy on another request, last step 5s ago"
            in text
        )

    def test_idle_debtor_line_says_idle(self, reg):
        """빚진 채 idle 이고 받은편지함엔 런을 열지 않는 항목뿐 — "busy on another request" 가 아니다."""
        reg.note_owes("agent:p", "main", "t", id="r-903")
        _fake_agent(reg, "p", state="idle")
        _fake_agent(reg, "q", state="idle")
        reg._deliver_peer_reply(
            "p", "q", "stopped", 0, re="r-1", mark="stopped by user", opens_run=False
        )
        assert [v.wakes for v in reg.queued_for("agent:p")] == [False]
        assert reg.checkin_agent_lines("main", {}) == (
            ["agent:p (explorer) · owes you a reply · idle"],
            True,
            False,
        )

    def test_a_parked_debtor_waiting_on_agents_is_named_by_key(self, reg):
        """기다림 줄은 이름만(v10.166.0) — 에이전트는 키, main 은 "main". 종전엔 수락 문장의 줄("agent:b working on
        your request")을 그대로 실어, main 이 읽는 글에 그 채무자 시점의 "your" 가 섰다."""
        _fake_agent(reg, "a", profile="coder")
        reg.mark_waiting("agent:a")
        reg.note_owes("agent:a", "main", "t", id="r-900")
        _fake_agent(reg, "b", state="busy", current_author="agent:a")
        (line,) = reg.checkin_agent_lines("main", {})[0]
        assert line == "agent:a (coder) · owes you a reply · itself waiting on b"

    def test_a_waiting_agent_reads_as_waiting_in_the_three_places(self, reg):
        """받아들여진 `wait` 로 기다리는 에이전트(우편의 기다림 표시)를 모델이 읽는 세 자리 — 꼬리의 `## Live Agents`,
        폴백 회신의 꼬리, 체크인의 줄 선 요청 줄 — 가 같은 낱말 "waiting" 으로 말한다(v10.166.0; 종전엔 작업 상태
        "busy"). 표시가 내려가면 셋 다 다시 작업 상태로 말한다."""
        _fake_agent(
            reg,
            "w",
            state="busy",
            current_author="agent:z",
            last_at=time.time() - 60,
            inbox=[{"author": "main"}],
        )
        reg.mark_waiting("agent:w")
        assert "- `w` (explorer) [waiting · 1 queued]" in build_live_agents_section(
            reg, include_state=True
        )
        fallback = {"kind": "reply", "key": "w", "output": "summary", "success": True}
        assert build_reply_record(fallback, registry=reg)["content"].endswith(
            "(agent status: waiting · 1 queued — remaining replies arrive automatically; "
            "no need to poll.)"
        )
        (line,) = reg.checkin_agent_lines("main", {})[0]
        assert line == (
            "agent:w (explorer) · has your request queued, next in line · waiting, "
            "last step 1m ago"
        )
        reg.clear_waiting("agent:w")
        assert "- `w` (explorer) [busy · 1 queued]" in build_live_agents_section(
            reg, include_state=True
        )
        assert (
            "(agent status: busy · 1 queued — "
            in (build_reply_record(fallback, registry=reg)["content"])
        )
        (line,) = reg.checkin_agent_lines("main", {})[0]
        assert line.endswith("· busy on another request, last step 1m ago")
        assert reg.get("w").state == "busy"  # 작업 상태는 내내 그대로다

    def test_pending_for_text_is_unchanged(self, reg):
        """에이전트 몫의 줄(`wake.relation_items`) — 종전 문장 그대로."""
        _fake_agent(reg, "k", state="busy")
        _fake_agent(reg, "j", inbox=[{"author": "main"}])
        # 손으로 고른 id 는 할당기가 이 테스트에서 내지 않는 값 — 같은 id 의 봉투가 뒤에 오지 않게
        reg.note_owes("agent:p", "main", "t", id="r-903")
        _fake_agent(reg, "p", state="busy", current_author="agent:q")
        # 일하는 중인 k 는 집어 든 main 의 요청을 빚지기도 하지만 한 에이전트는 한 줄 — "working" 하나
        assert agent_wake_lines(reg, "main") == [
            "agent:p owes you a reply",
            "agent:k working on your request",
            "agent:j has your request queued",
        ]


# ── 상주 에이전트 ──────────────────────────────────────────────


class TestResident:
    def test_resident_checkin_is_an_envelope_in_its_inbox(
        self, tmp_path, jobs, monkeypatch
    ):
        import threading

        from agent_cli.session_slots import set_schedule_registry

        set_schedule_registry(None)
        gate = threading.Event()
        starts = []  # 런마다 엔진이 받은 시작(글·장부 id·사람 요청)
        gated = make_runner(block=gate)

        def runner(kind, start, ctx):
            starts.append(start)
            return gated(kind, start, ctx)

        r = make_registry(tmp_path, runner=runner)
        r.checkin_timer_factory = FakeTimer
        set_agent_registry(r)
        try:
            key, _ = r.spawn()
            assert wait_until(lambda: r.get(key).state == "idle")
            assert r.request(key, "do X") == ""  # 워커를 묶어 둔다 — 항목이 쌓인다
            assert wait_until(lambda: r.get(key).state == "busy")
            assert r.request(key, "do Y") == ""  # 뒤에 줄 선 보통 요청
            me = f"agent:{key}"
            jobs.start("sleep 30", owner=me)
            # 에이전트 자신이 제자리 `wait` 에 들었다(워커 바깥에서 같은 호출을 흉내 — `Mailbox.arm_checkin`)
            r.arm_checkin(me, checkin_ids(me))
            assert _armed(r, me) is not None
            text = r.fire_checkin(me)
            assert text.startswith("⏰ Check-in after 5m — still running:\njob #1")
            (item,) = [m for m in r.queued_for(me) if m.text.startswith("⏰ Check-in")]
            # 회신을 빚지우지 않는다(장부 밖 알림)
            assert item.expects_reply is False
            # 상주가 main 을 기다릴 때 — main 의 알림에 "working" 줄로 보인다
            r.arm_checkin("main", checkin_ids("main"))
            assert f"agent:{key}" in r.fire_checkin("main")
            # 하니스 통지는 봉투 밖 (v10.84.0, 사용자 결정 C) — main 이 받는 모양과 같다: 그 알림이 연 런의 시작 글
            gate.set()
            assert wait_until(
                lambda: any(
                    st.query.startswith("⏰ Check-in after 5m") for st in starts
                )
            )
            # 장부 항목이 아니다 — 그 알림이 연 런은 장부 id(핀·`answers`)도 사람 요청 줄(Open Items)도 없다
            (checkin_run,) = [
                st for st in starts if st.query.startswith("⏰ Check-in after 5m")
            ]
            assert checkin_run.request_id == "" and checkin_run.user_requests == []
        finally:
            gate.set()
            set_agent_registry(None)
            r.shutdown_all()

    def test_kill_and_shutdown_cancel_timers(self, reg, jobs):
        _fake_agent(reg, "k", state="busy")
        jobs.start("sleep 30", owner="agent:k")
        reg.arm_checkin("main", checkin_ids("main"))  # main 은 agent:k 를 기다린다
        reg.arm_checkin(
            "agent:k", checkin_ids("agent:k")
        )  # agent:k 는 자기 작업을 기다린다
        assert {"main", "agent:k"} <= set(reg._checkin_armed)
        reg._cancel_checkins("agent:k")
        assert "agent:k" not in reg._checkin_armed and "main" in reg._checkin_armed
        reg.shutdown_all()
        assert reg._checkin_armed == {}
        assert all(t.cancelled for t in FakeTimer.made if t.started)


# ── 루프와 디스패치 ────────────────────────────────────────────


def _loop(provider, *, query="Q", rid="u-1"):
    # 포트는 런마다 만든다 — 메일함은 포트를 만들 때의 슬롯 레지스트리를 쥔다(v10.133.28,
    # 프로덕션 빌더와 같다). 모듈 수준 상수면 import 때(슬롯 None)의 것이 된다.
    return make_loop(
        ports=make_ports(inbox=queue_inbox(lambda: None)),
        query=query,
        query_request_id=rid,
        provider=provider,
        capabilities=ModelCapabilities(
            context_window=32768, max_output_tokens=4096, supports_thinking=False
        ),
        model="m",
        dialect=get_dialect("json_fc"),
    )


def _provider(*replies: dict):
    p = MagicMock()
    p.call.side_effect = [LLMResponse(content=json.dumps(r)) for r in replies]
    return p


class TestLoopWiring:
    def test_wait_result_names_the_interval(self, reg, jobs):
        job = jobs.start("sleep 30", owner="main")
        # 제자리 대기 — 기다리기 시작하면 작업을 바깥에서 죽여(깨울 것이 사라짐) 같은 런이 닫게 한다
        threading.Thread(
            target=lambda: (
                wait_until(lambda: reg._post.is_waiting("main")),
                jobs.kill(job),
            ),
            daemon=True,
        ).start()
        loop = _loop(
            _provider({"action": "wait"}, {"action": "complete", "answers": ["u-1"]})
        )
        res = loop.run()
        assert res.success
        obs = [
            str(m["content"])
            for m in loop.messages
            if m["role"] == "user" and "waiting —" in str(m["content"])
        ]
        assert obs[0] == (
            "Observation: waiting — will wake on: job #1 (`sleep 30`), or in 5m if "
            "nothing has reported."
        )  # 5c-1: "Open items stay open for the next run: …" 이 빠졌다(다음 런이 없다)

    def test_wait_result_without_a_timer_is_unchanged(self, reg, monkeypatch):
        from agent_cli.session_slots import set_schedule_registry

        active = {"on": True}
        set_schedule_registry(SimpleNamespace(has_active_work=lambda: active["on"]))
        set_job_registry(None)
        # 제자리 대기 — 기다리기 시작하면 예약을 꺼(깨울 것이 사라짐) 같은 런이 닫게 한다
        threading.Thread(
            target=lambda: (
                wait_until(lambda: reg._post.is_waiting("main")),
                active.update(on=False),
            ),
            daemon=True,
        ).start()
        loop = _loop(
            _provider({"action": "wait"}, {"action": "complete", "answers": ["u-1"]})
        )
        assert loop.run().success
        obs = [
            str(m["content"]) for m in loop.messages if "waiting —" in str(m["content"])
        ]
        assert obs[0] == "Observation: waiting — will wake on: a schedule."

    def test_a_delivered_checkin_wakes_a_run_that_can_wait_again(self, reg, jobs):
        """알림이 배달되고, 모델이 다시 wait 하면 이번엔 10분이다. 그 기다림은 작업이 바깥에서 죽어 끝난다(깨울 것이
        사라짐) — 같은 런이 닫는다."""
        job = jobs.start("sleep 30", owner="main")
        reg.arm_checkin("main", checkin_ids("main"))
        reg.fire_checkin("main")

        def kill_once_waiting():
            assert wait_until(lambda: "main" in reg._checkin_armed)
            jobs.kill(job)

        threading.Thread(target=kill_once_waiting, daemon=True).start()
        loop = make_loop(
            ports=make_ports(inbox=queue_inbox(lambda: None), agent_registry=reg),
            query="",
            provider=_provider(
                {"action": "wait"}, {"action": "complete", "answers": []}
            ),
            capabilities=ModelCapabilities(
                context_window=32768, max_output_tokens=4096, supports_thinking=False
            ),
            model="m",
            dialect=get_dialect("json_fc"),
        )
        res = loop.run()
        assert res.success
        users = [str(m["content"]) for m in loop.messages if m["role"] == "user"]
        assert users[0].startswith("⏰ Check-in after 5m")
        assert any("or in 10m if nothing has reported" in u for u in users)
        assert loop._state.ledger.request_ids() == []  # 알림은 열린 항목이 아니다
        assert (
            _armed(reg) is None
        )  # 제자리 대기가 깨면 그 자리에서 푼다 — 런 끝·런 시작에 풀 것이 없다


# ── 문구 — 네 자리의 kill, wait 설명, 인벤토리 ─────────────────


class TestWording:
    def test_kill_hint_is_the_process_group_everywhere(self, jobs):
        from agent_cli.prompts.system_prompt import TASK_GUIDELINES
        from agent_cli.tools.base import RunContext
        from agent_cli.tools.shell import ShellTool

        job = jobs.start("sleep 30", owner="main")
        assert f"stop it: `kill -- -{job.pid}`" in jobs.render_tail("main")
        r = ShellTool()._run(
            {"command": "sleep 30", "background": True}, ctx=RunContext(owner="main")
        )
        assert r.output.endswith(
            f"To stop it: `kill -- -{jobs.running('main')[-1].pid}`."
        )
        assert "`kill -- -<pid>` shown in the background-jobs list" in TASK_GUIDELINES
        assert "`kill <pid>`" not in TASK_GUIDELINES

    def test_wait_description_mentions_the_checkin_without_numbers(self):
        from agent_cli.tools.registry import TOOLS

        d = TOOLS["wait"].description
        assert (
            "If nothing reports for a while, you are woken with what is still running."
            in d
        )
        assert (
            "5m" not in d
            and "WITHOUT" not in d
            and "without closing any open item" in d
        )

    def test_checkin_text_is_in_the_prompt_inventory(self):
        from agent_cli.prompts.inventory import build_inventory

        inv = build_inventory()
        ids = {r["id"] for r in inv["dialects"]["json_fc"]["runtime"]}
        assert "wait_checkin" in ids

    def test_format_duration(self):
        assert [
            format_duration(s) for s in (0, 45, 60, 300, 1799.9, 3600, 6540, 7200)
        ] == ["0s", "45s", "1m", "5m", "30m", "1h", "1h49m", "2h"]
