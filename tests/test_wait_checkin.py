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
import time
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import MagicMock

import pytest

from agent_cli.constants import CHECKIN_STEPS_S, format_duration
from agent_cli.dialects import get as get_dialect
from agent_cli.jobs import JobRegistry, set_job_registry
from agent_cli.loop import AgentLoop
from agent_cli.loop.core import mail_item
from agent_cli.monitor import MonitorRegistry, build
from agent_cli.monitor.runtime import set_monitor_registry
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.runtime import wake_source_ids, wake_sources
from agent_cli.subagent.agents_live import (
    AgentInstance,
    Question,
    ReplyDebt,
    build_reply_record,
    set_main_registry,
)
from tests.loop_ports import make_ports
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
    import agent_cli.schedule.runtime as sched

    monkeypatch.setattr(sched, "get_schedule_registry", lambda: None)
    FakeTimer.made.clear()
    r = make_registry(tmp_path)
    r.checkin_timer_factory = FakeTimer
    set_main_registry(r)
    yield r
    set_main_registry(None)
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
    return [m for m in reg.drain_replies() if m.get("kind") == "checkin"]


# ── 걸고 풀기 ────────────────────────────────────────────────


class TestArmAndFire:
    def test_wait_arms_five_minutes_and_fires_a_fact_statement(self, reg, jobs):
        job = jobs.start("sleep 30", owner="main")
        assert reg.next_checkin_s("main") == 300
        assert reg.run_ended("main", "⏸ waiting", waiting=True) == 0
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
        reg.run_ended("main", "", waiting=True)
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
        reg.run_ended("main", "", waiting=True)
        timer = FakeTimer.made[-1]
        reg.run_started("main")
        assert timer.cancelled and _armed(reg) is None
        assert reg.fire_checkin("main") == "" and _mail(reg) == []

    def test_a_normal_run_end_clears_a_stale_timer(self, reg, jobs):
        jobs.start("sleep 30", owner="main")
        reg.run_ended("main", "", waiting=True)
        reg.run_ended("main", "done")  # 보통의 끝 — 기다림이 아니다
        assert _armed(reg) is None

    def test_nothing_running_at_fire_time_sends_nothing(self, reg, jobs):
        job = jobs.start("sleep 30", owner="main")
        reg.run_ended("main", "", waiting=True)
        jobs.kill(job)  # 통지 없이 사라졌다(소유자 사망·세션 종료 경로)
        assert reg.fire_checkin("main") == "" and _mail(reg) == []

    def test_schedule_only_wait_arms_nothing(self, reg, monkeypatch):
        import agent_cli.schedule.runtime as sched

        monkeypatch.setattr(
            sched,
            "get_schedule_registry",
            lambda: SimpleNamespace(has_active_work=lambda: True),
        )
        assert wake_sources("main") == ["a schedule"]
        assert wake_source_ids("main") == frozenset()
        assert reg.next_checkin_s("main") is None
        assert reg.arm_checkin("main") is None and _armed(reg) is None

    def test_a_schedule_beside_a_job_arms_but_is_not_in_the_notice(
        self, reg, jobs, monkeypatch
    ):
        import agent_cli.schedule.runtime as sched

        monkeypatch.setattr(
            sched,
            "get_schedule_registry",
            lambda: SimpleNamespace(has_active_work=lambda: True),
        )
        jobs.start("sleep 30", owner="main")
        assert wake_source_ids("main") == {"job:1"}
        reg.run_ended("main", "", waiting=True)
        text = reg.fire_checkin("main")
        assert "job #1" in text and "schedule" not in text


class TestDoubling:
    def test_same_targets_double_to_an_hour_and_a_new_target_resets(self, reg, jobs):
        jobs.start("sleep 30", owner="main")
        seen = []
        for _ in range(6):
            assert reg.next_checkin_s("main") == reg.arm_checkin("main")
            seen.append(_armed(reg).interval_s)
            assert reg.fire_checkin("main")
        assert seen == [300, 600, 1200, 2400, 3600, 3600]
        assert tuple(seen[:5]) == CHECKIN_STEPS_S
        # "woken again in" 은 다음 단계 — 마지막은 60분에 머문다
        jobs.start("sleep 30", owner="main")  # 대상 집합이 바뀌었다
        assert reg.next_checkin_s("main") == 300
        reg.arm_checkin("main")
        assert _armed(reg).step == 0

    def test_next_interval_in_the_notice_follows_the_step(self, reg, jobs):
        jobs.start("sleep 30", owner="main")
        reg.arm_checkin("main")
        assert "woken again in 10m" in reg.fire_checkin("main")
        reg.arm_checkin("main")
        assert "Check-in after 10m" in (t := reg.fire_checkin("main"))
        assert "woken again in 20m" in t
        for _ in range(3):
            reg.arm_checkin("main")
            t = reg.fire_checkin("main")
        assert "Check-in after 1h" in t and "woken again in 1h" in t

    def test_a_cancelled_timer_does_not_advance_the_step(self, reg, jobs):
        """진짜 보고로 깨면(타이머가 풀리면) 단계는 직전에 **발화한** 깨우기 기준이다."""
        jobs.start("sleep 30", owner="main")
        reg.arm_checkin("main")
        reg.run_started("main")  # 발화 전에 런이 시작됐다
        assert reg.next_checkin_s("main") == 300


# ── 알림 내용 ────────────────────────────────────────────────


class TestNoticeLines:
    def test_two_jobs_list_both_kills(self, reg, jobs):
        a = jobs.start("sleep 30", owner="main")
        b = jobs.start("sleep 31", owner="main")
        reg.arm_checkin("main")
        text = reg.fire_checkin("main")
        assert "job #1 ·" in text and "job #2 ·" in text
        assert f"`kill -- -{a.pid}` for job #1; `kill -- -{b.pid}` for job #2" in text

    def test_monitor_line_and_delete_hint(self, reg, mons, tmp_path):
        log = tmp_path / "ocr.log"
        log.write_text("")
        mon = mons.add(
            build({"file": str(log), "pattern": "done"}), owner="main", deadline_s=6540
        )
        assert wake_source_ids("main") == {f"mon:{mon.id}"}
        reg.arm_checkin("main")
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
        reg.arm_checkin("main")
        text = reg.fire_checkin("main")
        assert f"`file` + `idle` on {log} for 5m · 2h left" in text
        assert (
            f'stop it (`kill -- -{job.pid}`; `monitor` with `mode` "delete" for a '
            "monitor)"
        ) in text

    def test_another_owners_work_is_not_in_my_notice(self, reg, jobs):
        jobs.start("sleep 30", owner="main")
        jobs.start("sleep 40", owner="agent:x")
        reg.arm_checkin("main")
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
    tm.state = state
    tm.current_author = current_author
    tm.inbox_items = list(inbox)
    tm.ctx = SimpleNamespace(last_ordinal=ordinal, last_added_at=last_at)
    reg._agents[key] = tm
    return tm


class TestAgentLines:
    """새 필드 없이 레지스트리가 아는 것만으로 — 상태 넷과 사람 질문 줄."""

    def test_working_agent_counts_steps_since_the_wait(self, reg):
        tm = _fake_agent(reg, "k", state="busy", ordinal=3, last_at=time.time() - 40)
        assert wake_source_ids("main") == {"agent:k"}
        reg.arm_checkin("main")
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
        reg.arm_checkin("main")
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
        reg.arm_checkin("main")
        text = reg.fire_checkin("main")
        assert (
            "agent:j (writer) · has your request queued behind 2 others · busy on "
            "another request, last step 1m ago"
        ) in text

    def test_parked_debtor_shows_what_it_waits_on(self, reg, jobs):
        _fake_agent(reg, "m", profile="coder")  # idle, 받은편지함 빔 — 스스로 잠듦
        reg._reply_debts["agent:m"] = [ReplyDebt(to="main", text="t", id="r-1")]
        jobs.start("pytest -x tests/", owner="agent:m")
        assert wake_source_ids("main") == {"agent:m"}
        reg.arm_checkin("main")
        text = reg.fire_checkin("main")
        assert (
            "agent:m (coder) · owes you a reply · itself waiting on job #1 "
            "(`pytest -x tests/`)"
        ) in text
        assert "answered by the user" not in text

    def test_parked_debtor_blocked_on_a_human_question_adds_the_line(self, reg, jobs):
        _fake_agent(reg, "n", profile="reviewer")
        reg._reply_debts["agent:n"] = [ReplyDebt(to="main", text="t", id="r-2")]
        jobs.start("sleep 30", owner="agent:n")
        reg._questions["q-3"] = Question(
            id="q-3", asker="n", target="user", text="?", asked_at=time.time() - 540
        )
        reg.arm_checkin("main")
        text = reg.fire_checkin("main")
        assert (
            "agent:n (reviewer) · owes you a reply · itself waiting on job #1 "
            "(`sleep 30`) · blocked on question q-3 to the user, unanswered for 9m"
        ) in text
        assert text.endswith(
            "A question to the user is answered by the user, not by you."
        )

    def test_busy_debtor_line(self, reg):
        _fake_agent(
            reg, "p", state="busy", current_author="agent:q", last_at=time.time() - 5
        )
        reg._reply_debts["agent:p"] = [ReplyDebt(to="main", text="t", id="r-3")]
        reg.arm_checkin("main")
        text = reg.fire_checkin("main")
        assert (
            "agent:p (explorer) · owes you a reply · busy on another request, last step 5s ago"
            in text
        )

    def test_pending_for_text_is_unchanged(self, reg):
        """`pending_for` 는 `pending_agents_for` 의 글만 — 종전 문장 그대로."""
        _fake_agent(reg, "k", state="busy")
        _fake_agent(reg, "j", inbox=[{"author": "main"}])
        reg._reply_debts["agent:p"] = [ReplyDebt(to="main", text="t", id="r-3")]
        _fake_agent(reg, "p", state="busy", current_author="agent:q")
        assert reg.pending_for("main") == [
            "agent:p owes you a reply",
            "agent:k working on your request",
            "agent:j has your request queued",
        ]


# ── 상주 에이전트 ──────────────────────────────────────────────


class TestResident:
    def test_resident_checkin_is_an_envelope_in_its_inbox_and_not_persisted(
        self, tmp_path, jobs, monkeypatch
    ):
        import threading

        import agent_cli.schedule.runtime as sched

        monkeypatch.setattr(sched, "get_schedule_registry", lambda: None)
        gate = threading.Event()
        r = make_registry(tmp_path, runner=make_runner(block=gate))
        r.checkin_timer_factory = FakeTimer
        set_main_registry(r)
        try:
            key, _ = r.spawn()
            assert wait_until(lambda: r.get(key).state == "idle")
            assert r.request(key, "do X") == ""  # 워커를 묶어 둔다 — 항목이 쌓인다
            assert wait_until(lambda: r.get(key).state == "busy")
            assert r.request(key, "do Y") == ""  # 뒤에 줄 선 보통 요청 — 미러에 남는다
            me = f"agent:{key}"
            jobs.start("sleep 30", owner=me)
            # 에이전트 자신이 wait 로 끝났다(워커 바깥에서 같은 호출을 흉내)
            r.run_ended(me, "⏸ waiting", waiting=True)
            assert _armed(r, me) is not None
            text = r.fire_checkin(me)
            assert text.startswith("⏰ Check-in after 5m — still running:\njob #1")
            tm = r.get(key)
            (item,) = [i for i in tm.inbox_items if i.get("kind") == "checkin"]
            assert item["ledger"] == "none" and item["expects_reply"] is False
            assert r._model_text(item).startswith(
                f"── checkin [{item['id']}] from main ──\n⏰"
            )
            saved = json.loads((tmp_path / "agents.json").read_text())
            (entry,) = [e for e in saved["agents"] if e["key"] == key]
            assert [i["text"] for i in entry["inbox"]] == ["do Y"]  # 알림은 빠졌다
            # 상주가 main 을 기다릴 때 — main 의 알림에 "working" 줄로 보인다
            r.arm_checkin("main")
            assert f"agent:{key}" in r.fire_checkin("main")
        finally:
            gate.set()
            set_main_registry(None)
            r.shutdown_all()

    def test_kill_and_shutdown_cancel_timers(self, reg, jobs):
        _fake_agent(reg, "k", state="busy")
        jobs.start("sleep 30", owner="agent:k")
        reg.arm_checkin("main")  # main 은 agent:k 를 기다린다
        reg.arm_checkin("agent:k")  # agent:k 는 자기 작업을 기다린다
        assert {"main", "agent:k"} <= set(reg._checkin_armed)
        reg._cancel_checkins("agent:k")
        assert "agent:k" not in reg._checkin_armed and "main" in reg._checkin_armed
        reg.shutdown_all()
        assert reg._checkin_armed == {}
        assert all(t.cancelled for t in FakeTimer.made if t.started)


# ── 루프와 디스패치 ────────────────────────────────────────────

WAKEABLE = make_ports(dequeue_user_message=lambda: None)


def _loop(provider, *, query="Q", rid="u-1"):
    return AgentLoop(
        ports=WAKEABLE,
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
        jobs.start("sleep 30", owner="main")
        loop = _loop(_provider({"action": "wait"}))
        res = loop.run()
        assert res.waiting and res.output == "⏸ waiting on job #1 (`sleep 30`)"
        obs = [
            str(m["content"])
            for m in loop.messages
            if m["role"] == "user" and "waiting —" in str(m["content"])
        ]
        assert obs[0] == (
            "Observation: waiting — will wake on: job #1 (`sleep 30`), or in 5m if "
            "nothing has reported. Open items stay open for the next run: u-1."
        )

    def test_wait_result_without_a_timer_is_unchanged(self, reg, monkeypatch):
        import agent_cli.schedule.runtime as sched

        monkeypatch.setattr(
            sched,
            "get_schedule_registry",
            lambda: SimpleNamespace(has_active_work=lambda: True),
        )
        set_job_registry(None)
        loop = _loop(_provider({"action": "wait"}))
        assert loop.run().waiting
        obs = [
            str(m["content"]) for m in loop.messages if "waiting —" in str(m["content"])
        ]
        assert obs[0] == (
            "Observation: waiting — will wake on: a schedule. Open items stay open "
            "for the next run: u-1."
        )

    def test_a_run_with_input_cancels_but_an_empty_wake_run_does_not(self, reg, jobs):
        jobs.start("sleep 30", owner="main")
        reg.arm_checkin("main")
        # 깨우기 런인데 배달된 것이 없다 — 다른 런이 가져갔다. 타이머는 그대로.
        res = _loop(MagicMock(), query="", rid="").run()
        assert res.output == "" and _armed(reg) is not None
        # 입력이 있는 런은 푼다
        _loop(
            _provider({"action": "complete", "result": "ok", "answers": ["u-1"]})
        ).run()
        assert _armed(reg) is None

    def test_a_delivered_checkin_wakes_a_run_that_can_wait_again(self, reg, jobs):
        """알림이 배달되고, 모델이 다시 wait 하면 이번엔 10분이다."""
        jobs.start("sleep 30", owner="main")
        reg.arm_checkin("main")
        reg.fire_checkin("main")
        loop = AgentLoop(
            ports=make_ports(dequeue_user_message=lambda: None, agent_registry=reg),
            query="",
            provider=_provider({"action": "wait"}),
            capabilities=ModelCapabilities(
                context_window=32768, max_output_tokens=4096, supports_thinking=False
            ),
            model="m",
            dialect=get_dialect("json_fc"),
        )
        res = loop.run()
        assert res.waiting
        users = [str(m["content"]) for m in loop.messages if m["role"] == "user"]
        assert users[0].startswith("⏰ Check-in after 5m")
        assert any("or in 10m if nothing has reported" in u for u in users)
        assert loop._state.ledger.request_ids() == []  # 알림은 열린 항목이 아니다


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
