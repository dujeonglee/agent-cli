"""1단계 기준 보강 ④ — `wait` 의 수락·거절 문장, 근거별·주인별 (v10.144.1, 테스트만).

진짜 `MainLoop`·진짜 상주 워커(`AgentRegistry`)에 대본 provider 를 꽂아 모델이 읽는 관찰 문장을 **원문 그대로**
비교한다. 오라클(s01·s13·s21·s23·s25)이 이미 고정한 칸은 여기 없다:

- main: 이미 도착 거절, 사람 말·회신에 깸 — s13 / 에이전트 owes·working — s01·s23 /
  queued(내려온 run) — s25
- 상주: main 의 회신을 기다림("main owes you a reply") — s21

표지 ``[단계 N 에서 바뀜 — …]`` 는 그 단계의 "바뀌는 기대값 목록" 이다(one-actor-plan §2).
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from agent_cli.jobs import JobRegistry
from agent_cli.monitor import MonitorRegistry, build
from agent_cli.schedule.registry import ScheduleRegistry
from agent_cli.session_slots import (
    set_agent_registry,
    set_job_registry,
    set_monitor_registry,
    set_schedule_registry,
)
from agent_cli.subagent.agents_live import AgentRegistry
from tests.binding_support import make_binding
from tests.mail_support import absorbed_mail
from tests.monitor_delivery import RecordingDelivery
from tests.test_engine_p2 import (
    CAPS,
    Provider,
    call,
    complete_all,
    contents,
    owed_id,
    session,  # noqa: F401 — 픽스처
    wait_until,
)
from tests.test_wait_checkin import FakeTimer

#: 예약 등록부 대역이 가진 이름 — 실물(`ScheduleRegistry`)에도 있어야 한다(대역이 어긋나 조용히 초록이 되지 않게)
_SCHEDULE_SURFACE = ("has_active_work",)


def _schedule_stub():
    assert all(hasattr(ScheduleRegistry, n) for n in _SCHEDULE_SURFACE)
    return SimpleNamespace(has_active_work=lambda: True)


@pytest.fixture
def jobs(tmp_path):
    r = JobRegistry(tmp_path)
    r.deliver = lambda *a, **k: ""
    set_job_registry(r)
    yield r
    r.stop_all()
    set_job_registry(None)


@pytest.fixture
def mons():
    r = MonitorRegistry()
    r.deliver = RecordingDelivery()
    r.stop()  # 감시 스레드 없이 — 등록만(발화하지 않는다)
    set_monitor_registry(r)
    yield r
    set_monitor_registry(None)
    r.stop()


@pytest.fixture(autouse=True)
def _no_schedule():
    set_schedule_registry(None)
    yield
    set_schedule_registry(None)


def _monitor(mons, tmp_path, owner: str):
    log = tmp_path / f"{owner.replace(':', '_')}.log"
    log.write_text("")
    return mons.add(
        build({"file": str(log), "pattern": "done"}), owner=owner, deadline_s=6540
    )


def _wait_lines(text: str) -> list[str]:
    return [
        ln
        for ln in text.splitlines()
        if ln.startswith(("Observation: waiting —", "Observation: wait refused —"))
    ]


# ── main ─────────────────────────────────────────────────────


def _main_wait(session_factory, scripts, release=None) -> tuple[list[str], object]:
    """main 의 런 하나 — ``release`` 가 있으면 main 이 제자리 대기에 든 뒤(기다림 표시) 불러 기다릴 것을 끝낸다(5c-1:
    `wait` 는 런을 끝내지 않는다 — 깨울 것이 사라지면 깨어 같은 런이 잇는다)."""
    s = session_factory(Provider(scripts))
    s["reg"].checkin_timer_factory = FakeTimer
    if release is not None:
        reg = s["reg"]

        def _release():
            assert wait_until(lambda: reg._post.is_waiting("main"))
            release()

        threading.Thread(target=_release, daemon=True).start()
    result = s["run"]()
    return _wait_lines(contents(s["ctx"])), result


#: `[5d 에서 바뀜]` 깨울 것이 없는데 과제의 회신(r-1)을 빚졌다 — 거절이 그 번호를 말한다. 종류마다 갈리던 끝맺음이 없다
_OWED_R1 = (
    "Observation: wait refused — nothing would wake you, and you still owe a "
    "`reply` [r-1]. Reply first, or keep working."
)

#: 깬 뒤 같은 런이 닫는 대본(5c-1)
_WAIT_THEN_DONE = {"MAIN": [call("wait"), complete_all("done")]}


class TestMainAccepts:
    """main 이 기다리는 회신 없이 `wait` — 받아들인다. `[5c-1 에서 바뀜]` 런을 끝내지 않고 제자리에서 기다린다 — 수락
    문장 끝의 "Open items stay open for the next run: …" 이 빠졌다(다음 런이 없다). 대본은 기다릴 것을 끝내 깨우고, 같은
    런이 닫는다."""

    def test_job_only(self, session, jobs):  # noqa: F811
        job = jobs.start("sleep 30", owner="main")
        lines, result = _main_wait(session, _WAIT_THEN_DONE, lambda: jobs.kill(job))
        assert result.success
        assert lines[0] == (
            "Observation: waiting — will wake on: job #1 (`sleep 30`), or in 5m if "
            "nothing has reported."
        )

    def test_monitor_only(self, session, mons, tmp_path):  # noqa: F811
        mon = _monitor(mons, tmp_path, "main")
        lines, result = _main_wait(
            session, _WAIT_THEN_DONE, lambda: mons.delete(mon.id)
        )
        assert result.success
        assert lines[0] == (
            f"Observation: waiting — will wake on: monitor {mon.id}, or in 5m if "
            "nothing has reported."
        )

    def test_schedule_only_has_no_timer(self, session):  # noqa: F811
        stub = _schedule_stub()
        set_schedule_registry(stub)
        lines, result = _main_wait(
            session,
            _WAIT_THEN_DONE,
            lambda: setattr(stub, "has_active_work", lambda: False),
        )
        assert result.success
        assert lines[0] == "Observation: waiting — will wake on: a schedule."

    def test_several_sources_in_registry_order(self, session, jobs, mons, tmp_path):  # noqa: F811
        job = jobs.start("sleep 30", owner="main")
        mon = _monitor(mons, tmp_path, "main")
        stub = _schedule_stub()
        set_schedule_registry(stub)

        def end_all():
            jobs.kill(job)
            mons.delete(mon.id)
            stub.has_active_work = lambda: False

        lines, result = _main_wait(session, _WAIT_THEN_DONE, end_all)
        assert result.success
        assert lines[0] == (
            f"Observation: waiting — will wake on: job #1 (`sleep 30`); monitor {mon.id}; "
            "a schedule, or in 5m if nothing has reported."
        )


class TestMainRefuses:
    def test_an_agent_waiting_on_mains_answer_is_not_a_source(self, session):  # noqa: F811
        """상주가 main 에게 회신 기대 `message` 로 묻고 잠들었다 — main 이 `wait` 하면 거절된다: 상주를 깨울 것은
        main 의 회신뿐이다. `[v10.145.1 에서 바뀜 — 그 `message` 는 상주의 과제 빚을 갚지 않는다: 상주는 main 에게
        빚진 채 main 을 기다리므로 거절 문장이 "근거 없음" 에서 고리("owes you a reply but is itself waiting …")로
        바뀐다]`"""
        holder: dict = {"waiting": threading.Event()}

        def asks(msgs):
            assert holder["waiting"].wait(10)
            return call("message", to="main", text="which file?")

        def first_wait(msgs):
            holder["waiting"].set()  # 대본 순서 — 상주는 main 의 첫 `wait` 뒤에 묻는다
            return call("wait")

        def main_waits_after_parked(msgs):
            reg = holder["reg"]
            # 상주가 제자리에서 기다린다(기다림 표시 — 5c-1 부터 런 안, busy)
            assert wait_until(
                lambda: (
                    [
                        reg._post.is_waiting(f"agent:{r['key']}")
                        for r in reg.roster_snapshot()
                    ]
                    == [True]
                )
            )
            holder["owed"] = [d.id for d in reg.reply_debts("main") if not d.settled]
            return call("wait")

        p = Provider(
            {
                "MAIN": [
                    call("agent", mode="spawn", name="helper", task="TASK-P1 do it"),
                    first_wait,
                    main_waits_after_parked,
                    lambda m: call("reply", id=owed_id(m), text="use a.py"),
                    call("complete", result="done", answers=[]),
                ],
                # main 이 그 물음을 빚으로 적은 뒤(셋째 호출이 시작된 뒤) 잠든다 — 먼저 잠들면 기다릴 근거가 없어
                # 거절되고, 과제 빚(갚지 않은)이 되묻기·폴백으로 닫혀 main 의 거절 문장이 갈린다(v10.145.1)
                "TASK-P1": [
                    asks,
                    lambda m: (
                        wait_until(lambda: len(holder["p"].calls["MAIN"]) >= 3),
                        call("wait"),
                    )[1],
                    call("complete", result="ok"),
                ],
            }
        )
        holder["p"] = p
        s = session(p)
        holder["reg"] = s["reg"]
        s["reg"].checkin_timer_factory = FakeTimer
        s["run"]()
        lines = _wait_lines(contents(s["ctx"]))
        (key,) = [r["key"] for r in s["reg"].roster_snapshot()]
        # [5b 항목 6] 확인한 사실만 — "on you" 대신 "그를 깨울 것이 돌고 있지 않다", main 이 진 회신의 id
        (owed,) = holder["owed"]
        assert lines[1] == (
            f"Observation: wait refused — agent:{key} owes you a reply but is itself "
            "waiting, and nothing that would wake it is running, so if you wait too, "
            f"nobody wakes. You owe it a reply [{owed}]: send that first."
        )


# ── 상주 ─────────────────────────────────────────────────────


@pytest.fixture
def renderer(monkeypatch):
    import agent_cli.render as render_mod
    from tests.test_agents_live import RecordingRenderer

    r = RecordingRenderer()
    monkeypatch.setattr(render_mod, "get_renderer", lambda: r)
    return r


@pytest.fixture
def resident(tmp_path, renderer):
    """상주 하나 — 진짜 워커. ``run(scripts, before=None)`` 이 main 의 요청 하나로 런 하나를 돌리고 그 상주의
    history 글을 돌려준다(런이 끝나 한가해질 때까지)."""
    made: dict = {}

    def run(scripts: dict, *, before=None, release=None, ends_after_task=False) -> str:
        """``release`` — 그 상주가 제자리 대기에 든 뒤(기다림 표시) 불러 기다릴 것을 끝낸다(5c-1)."""
        p = Provider(scripts)
        binding = make_binding(provider=p, capabilities=CAPS, model="m")
        reg = AgentRegistry(
            tmp_path / "s",
            runtime={"binding": binding, "max_turns": 20, "depth": 0, "max_depth": 2},
        )
        reg.checkin_timer_factory = FakeTimer
        set_agent_registry(reg)
        made["reg"] = reg
        key, _ = reg.spawn(name="aa", ends_after_task=ends_after_task)
        made["key"] = key
        assert wait_until(lambda: reg.get(key).state == "idle")
        if before is not None:
            before(f"agent:{key}")
        assert reg.request(key, "TASK-R1 the job") == ""
        if release is not None:
            assert wait_until(lambda: reg._post.is_waiting(f"agent:{key}"))
            release(f"agent:{key}")
        assert wait_until(lambda: _ended(renderer, key) >= 1)
        if ends_after_task:  # run 에이전트는 런 하나 뒤 내려간다
            assert wait_until(
                lambda: (
                    reg.get(key).state == "dead" and not reg.get(key).worker.is_alive()
                )
            )
        else:
            assert wait_until(lambda: reg.get(key).state == "idle")
        hist = tmp_path / "s" / "agents" / key / "history.jsonl"
        import json

        return "\n".join(
            str(json.loads(ln).get("content") or "")
            for ln in hist.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        )

    made["run"] = run
    yield made
    reg = made.get("reg")
    if reg is not None:
        reg.shutdown_all()
    set_agent_registry(None)


def _ended(renderer, key) -> int:
    return sum(
        1
        for name, kw in renderer.calls
        if name == "end_agent_work" and kw.get("key") == key
    )


class TestResidentAccepts:
    """상주의 `wait` 는 받아들인다. `[5c-1 에서 바뀜]` 런을 끝내지 않고 제자리에서 기다린다 — 대본은 기다릴 것을 끝내
    깨우고, 같은 런이 회신하고 닫는다."""

    def test_job_only_and_what_main_sees_at_the_run_boundary(
        self, resident, jobs, renderer
    ):
        made: dict = {}
        text = resident["run"](
            {
                "TASK-R1": [
                    call("wait"),
                    lambda m: call("reply", id=owed_id(m), text="done"),
                    call("complete", result="ok"),
                ]
            },
            before=lambda owner: made.setdefault(
                "job", jobs.start("sleep 30", owner=owner)
            ),
            release=lambda owner: jobs.kill(made["job"]),
        )
        assert _wait_lines(text) == [
            (
                "Observation: waiting — will wake on: job #1 (`sleep 30`), or in 5m if "
                "nothing has reported."
            )
        ]
        # main 쪽에 보이는 것 — 런 하나가 열리고 닫힌다(카드 한 쌍, 기다림을 끼고도), 성공·정지 아님, 상태는 한가함.
        # `[5c-1 에서 바뀜]` 깬 뒤 같은 런이 회신했다 — 빚은 닫혔다(종전: 런이 wait 로 끝나 빚이 열린 채, 회신 없음)
        key = resident["key"]
        work = [
            (name, {k: kw.get(k) for k in ("success", "stopped", "error")})
            for name, kw in renderer.calls
            if name in ("begin_agent_work", "end_agent_work") and kw.get("key") == key
        ]
        assert work == [
            ("begin_agent_work", {"success": None, "stopped": None, "error": None}),
            ("end_agent_work", {"success": True, "stopped": False, "error": ""}),
        ]
        reg = resident["reg"]
        assert [r["state"] for r in reg.roster_snapshot()] == ["idle"]
        assert reg.awaited_replies("main") == []  # 같은 런이 갚았다

    def test_monitor_only(self, resident, mons, tmp_path):
        made: dict = {}
        text = resident["run"](
            {
                "TASK-R1": [
                    call("wait"),
                    lambda m: call("reply", id=owed_id(m), text="done"),
                    call("complete", result="ok"),
                ]
            },
            before=lambda owner: made.setdefault(
                "mon", _monitor(mons, tmp_path, owner)
            ),
            release=lambda owner: mons.delete(made["mon"].id),
        )
        assert _wait_lines(text) == [
            (
                f"Observation: waiting — will wake on: monitor {made['mon'].id}, or in 5m if "
                "nothing has reported."
            )
        ]

    def test_schedule_does_not_wake_a_resident(self, resident):
        """예약은 main 에게 발화한다 — 상주의 깨울 것이 아니다(5b 변화 8). 예약만 켜져 있으면 깨울 것이 없다 — 과제의
        회신을 빚졌으니 거절이다."""
        set_schedule_registry(_schedule_stub())
        text = resident["run"](
            {
                "TASK-R1": [
                    call("wait"),
                    lambda m: call("reply", id=owed_id(m), text="done"),
                    call("complete", result="ok"),
                ]
            }
        )
        assert _wait_lines(text) == [_OWED_R1]


class TestResidentRefuses:
    def test_nothing_to_wake_while_owing(self, resident):
        text = resident["run"](
            {
                "TASK-R1": [
                    call("wait"),
                    lambda m: call("reply", id=owed_id(m), text="done"),
                    call("complete", result="ok"),
                ]
            }
        )
        assert _wait_lines(text) == [_OWED_R1]

    def test_paid_then_wait_ends_and_the_next_message_opens_a_new_run_with_memory(
        self, resident, renderer
    ):
        """`[5d 에서 바뀜]` 갚은 뒤의 `wait` 는 빚도 깨울 것도 없어 받아들여지고 작업 구간이 끝난다(종전: 거절 → 그 안내대로
        `complete`). 아무것도 더 보내지 않고 한가해지고, 다음 메시지가 새 작업 구간을 연다. 새 구간의 모델은 앞 구간을
        기억한다(같은 ctx) — 끝을 알린 관찰도."""
        seen: list = []

        def second_run(msgs):
            seen.append(list(msgs))
            return call("reply", id=owed_id(msgs), text="second done")

        text = resident["run"](
            {
                "TASK-R1": [
                    lambda m: call("reply", id=owed_id(m), text="first done"),
                    call("wait"),
                    second_run,
                    call("complete", result="ok"),
                ]
            }
        )
        assert text.endswith(
            "Observation: nothing to wait for: no job or monitor of yours is running, no "
            "agent owes you a reply, no question of yours is open, and you owe none. This run has ended."
        )
        reg, key = resident["reg"], resident["key"]
        replies = [m.get("output") for m in absorbed_mail(reg)]
        assert replies == [
            "first done"
        ]  # `complete` 의 result 는 아무에게도 가지 않는다
        assert reg.request(key, "SECOND ask") == ""
        assert wait_until(lambda: _ended(renderer, key) >= 2)
        assert wait_until(lambda: reg.get(key).state == "idle")
        assert [m.get("output") for m in absorbed_mail(reg)] == ["second done"]
        (msgs,) = seen
        body = "\n".join(str(m.get("content") or "") for m in msgs)
        # 앞 구간(과제·회신·끝)이 그대로 있고 그 뒤에 새 메시지
        assert body.index("TASK-R1 the job") < body.index("first done")
        assert body.index("nothing to wait for") < body.index("SECOND ask")

    def test_nothing_to_wake_while_owing_run_agent(self, resident):
        """run 에이전트도 같은 문장이다(5d — 종전엔 상주와 끝맺음이 갈렸다)."""
        text = resident["run"](
            {
                "TASK-R1": [
                    call("wait"),
                    lambda m: call("reply", id=owed_id(m), text="done"),
                    call("complete", result="ok"),
                ]
            },
            ends_after_task=True,
        )
        assert _wait_lines(text) == [_OWED_R1]

    def test_mail_arrived_while_thinking(self, resident):
        def notice_then_wait(msgs):
            reg, key = resident["reg"], resident["key"]
            assert reg.request(key, "fyi: nothing to do", expects_reply=False) == ""
            return call("wait")

        text = resident["run"](
            {
                "TASK-R1": [
                    notice_then_wait,
                    lambda m: call("reply", id=owed_id(m), text="done"),
                    call("complete", result="ok"),
                ]
            }
        )
        assert _wait_lines(text) == [
            (
                # [5b] 무엇이 왔나와 먼저 할 일 — 사람의 말이면 "Answer now with `complete`"
                "Observation: wait refused — 1 notice arrived while you were thinking; "
                "it is right below. Deal with it first, then `wait` again if something is "
                "still running."
            )
        ]
