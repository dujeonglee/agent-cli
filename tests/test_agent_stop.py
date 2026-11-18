"""에이전트 정지 — ■ 런 정지 · ✕ 제거, 빚진 상대 모두에게 회신 (docs/agent-engine/DESIGN.md §2.3, v10.142.0).

`agent_stop.STOP_RULES` 의 칸마다 하나 이상: ■/✕ × (지금 일·그 밖의 빚·대기 요청·하위 run·하위 상주) ×
(run·상주). 실제 레지스트리·실제 워커·실제 `ResidentLoop` 에 가짜 엔진(``run_loop``)을 꽂는다 — 엔진은
테스트가 쥔 Event 로만 움직인다(시각에 기대는 단언 없음): 시작을 알리고(``started``), 시킨 만큼 받은편지함을
흡수하고(``absorb``), 정지가 설 때까지 막는다. 기다림은 전부 상태(Event·상태값)를 본다.
"""

from __future__ import annotations

import json
import re
import threading
import time

import pytest

from agent_cli.loop.core import mail_item
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.session_slots import get_agent_registry, set_agent_registry
from agent_cli.subagent import agent_stop
from agent_cli.subagent.agent_mail import build_reply_record
from agent_cli.subagent.agent_modes import tool_agent
from agent_cli.subagent.agents_live import AgentInstance, AgentRegistry
from agent_cli.tools.result import ToolResult
from tests.binding_support import make_binding
from tests.loop_kinds import make_caller
from tests.mail_support import agent_wake_lines

CAPS = ModelCapabilities(
    context_window=32768, max_output_tokens=4096, supports_thinking=False
)


def until(pred, limit=10.0) -> bool:
    """상태가 참이 될 때까지 — 상한은 고장 난 테스트가 멈추지 않게 하는 안전망일 뿐 단언이 아니다."""
    end = time.monotonic() + limit
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


def gone(tm) -> bool:
    """워커가 완전히 내려갔는가 — ``state == "dead"`` 는 내려가는 정리(회신 보내기·줄 닫기)보다 **먼저** 찍힌다.
    그 정리의 결과를 단언하려면 워커 스레드가 끝난 것까지 기다린다(상태만 보면 느린 러너에서 회신이 아직 없다)."""
    return tm.state == "dead" and not tm.worker.is_alive()


class WorkerDies(BaseException):
    """런 안의 `except Exception` 을 빠져나가 워커를 죽인다 — 워커가 예외로 죽는 길."""


class Stage:
    """가짜 엔진. 런의 시작 글에 든 표지로 행동을 고른다.

    - ``HOLD``: 시작을 알리고(``started[tag]``), ``absorb[tag]`` 가 서면 받은편지함을 이 런으로 흡수하고
      (``absorbed[tag]``), 정지가 설 때까지 막는다. 멈추면 interrupt 결과, 아니면 성공.
    - ``PARK``: 바로 `wait` 로 끝난다(빚은 열린 채 — 스스로 잠든 상주).
    - ``GATE``·``DIE``: 시작을 알리고 ``absorb[tag]`` 를 기다렸다가 성공으로 끝나거나(GATE) 워커를 죽인다(DIE).
    - 그 밖: 바로 성공(``ran`` 에 표지를 적는다).
    런 기록(호출 둘·생각 하나)을 ctx 에 남겨 회신의 "Last activity"·"Partial notes" 재료로 쓴다."""

    def __init__(self):
        self.started: dict[str, threading.Event] = {}
        self.absorb: dict[str, threading.Event] = {}
        self.absorbed: dict[str, threading.Event] = {}
        self.ran: list[str] = []
        self.absorbed_by: dict[str, list] = {}
        self.stopped: list[str] = []

    def _ev(self, d, tag):
        return d.setdefault(tag, threading.Event())

    def __call__(self, kind, start, ctx):
        m = re.search(r"\b(HOLD\d+|PARK\d+|GATE\d+|DIE\d+|NEXT)\b", start.query)
        tag = m.group(1) if m else ""
        if tag.startswith(("GATE", "DIE")):
            # 시작을 알리고 ``absorb[tag]`` 가 서면 끝난다 — GATE 는 성공, DIE 는 워커를 죽인다
            self._ev(self.started, tag).set()
            self._ev(self.absorb, tag).wait(10)
            if tag.startswith("DIE"):
                raise WorkerDies(tag)
            return ToolResult(True, output=f"{tag} done")
        if tag.startswith("PARK"):
            return ToolResult(True, output="⏸ waiting", waiting=True)
        if not tag.startswith("HOLD"):
            self.ran.append(tag)
            # 그 런의 첫 흡수가 읽은 것 — 들고 있던 항목(``opens_run`` 거짓)이 여기서 보인다
            self.absorbed_by.setdefault(tag, []).extend(kind.intake())
            return ToolResult(True, output=f"{tag} done")
        ctx.add(
            {
                "role": "assistant",
                "thought": "found callers so far: lexer.py:42, cli.py:118",
                "ops": [
                    {
                        "action": "shell",
                        "action_input": {"command": "rg -n parse_tokens"},
                    },
                    {"action": "read_file", "action_input": {"path": "src/lexer.py"}},
                ],
            }
        )
        ctx.add(
            {
                "role": "user",
                "tool": "shell+read_file",
                "success": True,
                "content": "Observation: ...",
                "parts": [
                    {"tool": "shell", "success": True, "content": "a"},
                    {"tool": "read_file", "success": True, "content": "b"},
                ],
            }
        )
        self._ev(self.started, tag).set()
        absorb = self._ev(self.absorb, tag)
        while not start.stop_event.is_set():
            if absorb.is_set() and not self._ev(self.absorbed, tag).is_set():
                kind.intake()
                self._ev(self.absorbed, tag).set()
            time.sleep(0.002)
        self.stopped.append(tag)
        return ToolResult(False, error="Interrupted by user")


@pytest.fixture
def stage(monkeypatch):
    s = Stage()
    monkeypatch.setattr("agent_cli.loop.run.run_loop", s)
    return s


@pytest.fixture
def reg(tmp_path):
    prev = get_agent_registry()
    binding = make_binding(provider=object(), capabilities=CAPS)
    registry = AgentRegistry(
        tmp_path / "sess",
        runtime={"binding": binding, "max_turns": 5, "depth": 0, "max_depth": 3},
    )
    set_agent_registry(registry)
    yield registry
    registry.shutdown_all()
    set_agent_registry(prev)


def spawn(reg, *, parent="main", run=False, task=""):
    key, err = reg.spawn(parent=parent, ends_after_task=run, task_summary=task)
    assert not err, err
    tm = reg.get(key)
    assert until(lambda: tm.state == "idle")
    return tm


def peer(reg, key="agt-peer0001"):
    """명단에만 오른 상주(스레드 없음) — 요청을 보내고 회신을 받는 쪽. 받은 회신은 받은편지함 미러에 남는다."""
    tm = AgentInstance(
        key,
        profile_name="reviewer",
        role_prompt="",
        allowed_tools=None,
        model="m",
        hooks_config=None,
        context_mode="none",
        home_dir=reg.session_dir / "agents" / key,
    )
    tm.state = "idle"
    reg._agents[key] = tm
    return tm


def main_mail(reg, rid):
    """main 메일함에 줄 선 ``rid`` 의 회신 — 공개 조회(`queued_for`)를 정지 회신의 메일 모양으로 되짠다
    (`build_reply_record`·`mail_item` 이 읽는 키). 정지 회신은 에이전트가 main 에게 보낸 회신이다."""
    return [
        {
            "kind": "peer_message",
            "key": v.src.removeprefix("agent:"),
            "item_id": v.id,
            "re": v.re,
            "mark": v.mark,
            "output": v.text,
            "expects_reply": v.expects_reply,
            "wakes_main": v.wakes,
        }
        for v in reg.queued_for("main")
        if v.re == rid
    ]


def _inbox_rows(tm):
    """``tm`` 의 받은편지함에 줄 선 항목 — 공개 조회를 항목 모양(dict)으로."""
    return [
        {
            "id": v.id,
            "re": v.re,
            "mark": v.mark,
            "text": v.text,
            "expects_reply": v.expects_reply,
            "opens_run": v.wakes,
        }
        for v in get_agent_registry().queued_for(f"agent:{tm.key}")
    ]


def peer_replies(tm):
    """``tm`` 의 받은편지함에 든 회신(무엇에 대한 것인지 ``re`` 를 단, 빚 아닌 항목)."""
    return [i for i in _inbox_rows(tm) if i["re"] and not i["expects_reply"]]


def held_items(tm):
    """``tm`` 이 런을 열지 않고 든 항목("Stop everything" 의 정지 회신) — 받은편지함의 깨우지 않는 항목."""
    return [i for i in _inbox_rows(tm) if not i["opens_run"]]


def busy_with(reg, stage, tm, tag, rid, *, author="main"):
    assert not reg.request(
        tm.key, f"{tag} review the lexer changes", author=author, item_id=rid
    )
    assert stage._ev(stage.started, tag).wait(10)


class TestResidentStopRun:
    """■ 상주 — 지금 일·그 밖의 빚은 stopped 회신, 대기 요청은 남아 다음 차례, 에이전트는 남는다."""

    def test_debts_answered_queue_kept_agent_stays(self, reg, stage):
        p6 = peer(reg)
        tm = spawn(reg)
        busy_with(reg, stage, tm, "HOLD1", "r-30")
        # 그 밖의 빚 — agt-6 의 요청을 이 런이 흡수했다
        assert not reg.request(
            tm.key, "check the test names", author=f"agent:{p6.key}", item_id="r-31"
        )
        stage.absorb["HOLD1"].set()
        assert stage.absorbed.setdefault("HOLD1", threading.Event()).wait(10)
        # 아직 시작 안 한 대기 요청
        assert not reg.request(
            tm.key, "NEXT then the parser", author="main", item_id="r-32"
        )
        assert [r["id"] for r in reg.awaited_replies("main")] == ["r-30", "r-32"]

        assert reg.stop_run(tm.key) == ""
        assert until(lambda: "NEXT" in stage.ran)  # 남은 대기 요청이 다음 런으로
        assert stage.stopped == ["HOLD1"]
        assert tm.state != "dead" and not tm.stop_event.is_set()  # 에이전트는 남는다

        (m30,) = main_mail(reg, "r-30")
        text = build_reply_record(m30)["content"]
        assert text.startswith(
            f"── reply [{m30['item_id']}] from {tm.key} re [r-30] · stopped by user ──\n"
        )
        assert (
            f"The user stopped {tm.key} while it was working on your request [r-30] "
            '("HOLD1 review the lexer changes").'
        ) in text
        assert (
            'Last activity: `shell` "rg -n parse_tokens" ✓ · `read_file` "src/lexer.py" ✓'
            in text
        )
        assert "Partial notes: found callers so far: lexer.py:42, cli.py:118" in text
        assert (
            "\nDecide whether to continue another way or report back to the user."
            in text
        )
        (r31,) = peer_replies(p6)
        assert r31["re"] == "r-31" and r31["mark"] == "stopped by user"
        assert "your request [r-31]" in r31["text"]
        # r-32 는 회신(폴백)으로 끝났고 기다림이 모두 닫혔다
        assert until(lambda: reg.awaited_replies("main") == [])
        assert reg.awaited_replies(f"agent:{p6.key}") == []
        assert tm.stop_cause == "" and tm.stop_notified == []

    def test_stop_run_on_an_idle_or_unknown_agent_is_refused(self, reg, stage):
        tm = spawn(reg)
        assert reg.stop_run(tm.key) == f'agent "{tm.key}" is not working on a request'
        assert reg.stop_run("agt-nope") == 'unknown agent "agt-nope"'
        assert tm.stop_cause == ""

    def test_sub_run_keeps_going_when_its_parent_run_is_stopped(self, reg, stage):
        parent = spawn(reg)
        sub = spawn(reg, parent=f"agent:{parent.key}", run=True)
        busy_with(reg, stage, parent, "HOLD2", "r-40")
        busy_with(reg, stage, sub, "HOLD3", "r-41", author=f"agent:{parent.key}")
        assert reg.stop_run(parent.key) == ""
        assert until(lambda: "HOLD2" in stage.stopped)
        assert sub.state == "busy" and not sub.stop_event.is_set()  # 하위 run 은 계속

    def test_stop_that_arrives_before_the_run_starts_stops_that_run(self, reg, stage):
        tm = spawn(reg)
        tm.state = "busy"  # 꺼냈지만 시작 전(`_worker` 가 busy 로 바꾼 직후)
        assert reg.stop_run(tm.key) == ""
        tm.state = "idle"
        assert not reg.request(tm.key, "HOLD4 x", author="main", item_id="r-45")
        assert until(lambda: "HOLD4" in stage.stopped)
        assert until(lambda: main_mail(reg, "r-45"))  # 그 런이 바로 멈추고 회신했다


class TestRemove:
    """✕ — 지금 일·그 밖의 빚 stopped, 대기 요청 removed, 하위 run 은 함께 멈춤, 하위 상주는 남음."""

    def test_busy_resident_removed(self, reg, stage):
        p6 = peer(reg)
        tm = spawn(reg)
        sub_run = spawn(reg, parent=f"agent:{tm.key}", run=True)
        sub_res = spawn(reg, parent=f"agent:{tm.key}")
        busy_with(reg, stage, sub_run, "HOLD5", "r-50", author=f"agent:{tm.key}")
        busy_with(reg, stage, tm, "HOLD6", "r-30")
        assert not reg.request(
            tm.key, "check names", author=f"agent:{p6.key}", item_id="r-31"
        )
        stage.absorb["HOLD6"].set()
        assert stage.absorbed.setdefault("HOLD6", threading.Event()).wait(10)
        assert not reg.request(tm.key, "NEXT parser", author="main", item_id="r-32")

        killer = threading.Thread(target=reg.kill, args=(tm.key,))
        killer.start()
        killer.join()
        assert until(lambda: gone(tm))
        assert until(lambda: gone(sub_run))  # 하위 run — 함께 멈춤
        assert (
            sub_res.state == "idle" and not sub_res.stop_event.is_set()
        )  # 하위 상주는 남음
        assert "NEXT" not in stage.ran

        (m30,) = main_mail(reg, "r-30")
        assert m30["mark"] == "stopped by user"
        (m32,) = main_mail(reg, "r-32")
        text = build_reply_record(m32)["content"]
        assert "re [r-32] · removed by user ──" in text
        assert (
            f'The user removed {tm.key} before it started your request [r-32] ("NEXT parser"). '
            "Nothing was done for it.\nDecide whether to continue another way or report back to the user."
        ) in text
        (r31,) = peer_replies(p6)
        assert r31["re"] == "r-31" and r31["mark"] == "stopped by user"
        assert reg.awaited_replies("main") == []
        assert reg.awaited_replies(f"agent:{p6.key}") == []
        assert tm.revivable is False

    def test_idle_resident_removed_answers_parked_debt_and_queue(self, reg, stage):
        p6 = peer(reg)
        tm = spawn(reg)
        # 스스로 잠든 빚(지난 런이 `wait` 로 끝남) — 장부에만 있다
        assert not reg.request(
            tm.key, "PARK1 old ask", author=f"agent:{p6.key}", item_id="r-60"
        )
        assert until(lambda: tm.handled == 1 and tm.state == "idle")
        assert reg.kill(tm.key) == ""
        assert until(lambda: gone(tm))
        (r60,) = peer_replies(p6)
        assert (
            r60["re"] == "r-60"
            and "while it was working on your request [r-60]" in r60["text"]
        )
        assert "Last activity" not in r60["text"]  # 런 없이 남은 빚 — 실을 활동이 없다
        assert reg.awaited_replies(f"agent:{p6.key}") == []

    def test_model_kill_is_the_same_device_and_names_the_actor(self, reg, stage):
        tm = spawn(reg)
        busy_with(reg, stage, tm, "HOLD7", "r-70")
        caller = make_caller(owner="main", agent_registry=reg)
        out = tool_agent({"mode": "kill", "key": tm.key}, registry=reg, caller=caller)
        assert out.success
        assert until(lambda: gone(tm))
        (m70,) = main_mail(reg, "r-70")
        assert m70["mark"] == "stopped by main"
        assert m70["output"].startswith(f"main stopped {tm.key} while it was working")

    def test_a_dead_agents_leftover_shutdown_mark_is_not_work(self, reg, stage):
        """런 도중 ✕ 로 끝난 워커는 끝내라는 표식을 꺼내지 않고 나간다 — 그 표식을 일로 세면 `run` 이 끝나지 않았다."""
        tm = spawn(reg)
        busy_with(reg, stage, tm, "HOLD17", "r-17")
        threading.Thread(target=reg.kill, args=(tm.key,)).start()
        assert until(lambda: gone(tm))
        # 끝난 워커는 아무것도 들고 있지 않다(끝내라는 표식은 꺼내지 않은 채 나갔다 — 그 표식은 구현의 것)
        assert reg.taken_by(f"agent:{tm.key}") == ()
        reg.drain_replies()
        assert not reg.has_active_work() and not reg.any_activity()

    def test_kill_of_a_dead_agent_and_shutdown_send_nothing(self, reg, stage):
        tm = spawn(reg)
        busy_with(reg, stage, tm, "HOLD8", "r-80")
        reg.shutdown_all()  # 세션 종료 — 사람의 정지가 아니다
        assert until(lambda: gone(tm))
        assert main_mail(reg, "r-80") == [] and tm.stop_cause == ""
        reg.kill(tm.key)  # 이미 죽었다 — 원인을 적지 않는다
        assert tm.stop_cause == "" and main_mail(reg, "r-80") == []


class TestRunAgent:
    """run 에이전트(``ends_after_task``) — ■ 도 ✕ 와 같다: 내려오고, 대기 요청에도 알리고, 하위 run 도 멈춘다."""

    def test_stop_run_brings_a_run_agent_down_with_its_channel_kept(
        self, reg, stage, monkeypatch
    ):
        cleared = []
        from agent_cli.render import get_renderer

        monkeypatch.setattr(
            type(get_renderer()),
            "clear_agent_conversation",
            lambda self, k: cleared.append(k),
        )
        p6 = peer(reg)
        tm = spawn(reg, run=True)
        sub = spawn(reg, parent=f"agent:{tm.key}", run=True)
        busy_with(reg, stage, sub, "HOLD9", "r-91", author=f"agent:{tm.key}")
        busy_with(reg, stage, tm, "HOLD10", "r-21")
        assert not reg.request(
            tm.key, "also this", author=f"agent:{p6.key}", item_id="r-22"
        )
        assert reg.stop_run(tm.key) == ""
        assert until(lambda: gone(tm)) and tm.revivable is False
        assert until(lambda: gone(sub))
        (m21,) = main_mail(reg, "r-21")
        assert m21["mark"] == "stopped by user"
        (r22,) = peer_replies(p6)
        assert r22["mark"] == "removed by user"  # 시작 안 한 대기 요청 — 알림
        assert cleared == []  # ■ 는 창을 비우지 않는다 — 회색 칩, 읽기 가능
        assert reg.stop_run(tm.key) == f'agent "{tm.key}" has already ended'

    def test_remove_run_agent_is_the_same(self, reg, stage):
        tm = spawn(reg, run=True)
        busy_with(reg, stage, tm, "HOLD11", "r-23")
        threading.Thread(target=reg.kill, args=(tm.key,)).start()
        assert until(lambda: gone(tm))
        (m23,) = main_mail(reg, "r-23")
        assert m23["mark"] == "stopped by user"

    def test_room_stop_does_not_stop_a_run_agent(self, reg, stage, tmp_path):
        from agent_cli.subagent import run_agent

        room = threading.Event()
        caller = make_caller(stop_event=room, binding=reg.runtime["binding"])
        launched = run_agent.launch_run(
            reg, {"task": "HOLD12 go"}, caller=caller, runtime=reg.runtime
        )
        assert not launched.error
        tm = reg.get(launched.key)
        assert stage._ev(stage.started, "HOLD12").wait(10)
        assert tm.parent == "main"
        room.set()  # 방 ■ — main 만
        assert not tm.run_stop.is_set() and not tm.stop_event.is_set()
        assert reg.stop_run(tm.key) == ""
        assert until(lambda: gone(tm))


class TestGoneAgentQueue:
    """스스로 내려간 에이전트 — 정상 종료한 run·죽은 워커. 더는 상자를 읽지 않으니 줄 선 요청을 닫고 요청자마다
    사실 한 통(✕ 의 removed 와 같은 길, 표지 ``ended``·``died``). 요청자를 깨운다."""

    def _queue_behind(self, reg, stage, tm, tag):
        p6 = peer(reg)
        busy_with(reg, stage, tm, tag, "r-40")
        assert not reg.request(
            tm.key, "check names", author=f"agent:{p6.key}", item_id="r-41"
        )
        assert not reg.request(tm.key, "NEXT parser", author="main", item_id="r-42")
        me = f"agent:{p6.key}"
        assert agent_wake_lines(reg, me) == [f"agent:{tm.key} has your request queued"]
        (line,), _ = reg.checkin_agent_lines(me, {})
        assert "has your request queued, next in line" in line
        return p6

    def _answered(self, reg, tm, p6, *, mark, body):
        assert until(lambda: gone(tm))
        me = f"agent:{p6.key}"
        (r41,) = peer_replies(p6)
        assert r41["re"] == "r-41" and r41["mark"] == mark
        assert r41["opens_run"] is True  # 요청자를 깨운다
        assert r41["text"] == body.format(agent=tm.key, rid="r-41", quote="check names")
        (m42,) = main_mail(reg, "r-42")
        assert m42["wakes_main"] is True
        text = build_reply_record(m42)["content"]
        assert f"re [r-42] · {mark} ──" in text
        assert body.format(agent=tm.key, rid="r-42", quote="NEXT parser") in text
        for rid in ("r-41", "r-42"):
            assert reg._post.row(rid) is None  # 줄이 닫혀 장부에서 빠졌다
        assert reg.queued_for(f"agent:{tm.key}") == ()
        assert agent_wake_lines(reg, me) == []
        # main 의 줄 선 요청(r-42)의 근거는 사라진다 — 집어 든 r-40 은 이 규칙 밖(죽음이면 빚으로 남는다)
        assert not [ln for ln in agent_wake_lines(reg, "main") if "queued" in ln]
        assert reg.checkin_agent_lines(me, {}) == ([], False)
        assert reg.awaited_replies(me) == [] and reg.awaited_replies("main") == []

    ENDED = (
        '{agent} finished its task and is gone before it started your request [{rid}] ("{quote}"). '
        "Nothing was done for it.\nDecide whether to continue another way or report back to the user."
    )
    DIED = (
        '{agent} died before it started your request [{rid}] ("{quote}"). '
        "Nothing was done for it.\nDecide whether to continue another way or report back to the user."
    )

    def test_run_agent_that_finishes_answers_its_queue(self, reg, stage):
        tm = spawn(reg, run=True)
        p6 = self._queue_behind(reg, stage, tm, "GATE1")
        stage.absorb["GATE1"].set()
        self._answered(reg, tm, p6, mark="ended", body=self.ENDED)
        assert "NEXT" not in stage.ran

    def test_worker_that_dies_answers_its_queue(self, reg, stage):
        tm = spawn(reg)
        p6 = self._queue_behind(reg, stage, tm, "DIE1")
        stage.absorb["DIE1"].set()
        self._answered(reg, tm, p6, mark="died", body=self.DIED)
        assert tm.error.startswith("WorkerDies")

    def test_a_late_kill_sends_no_second_notice(self, reg, stage):
        tm = spawn(reg, run=True)
        p6 = self._queue_behind(reg, stage, tm, "GATE2")
        stage.absorb["GATE2"].set()
        assert until(lambda: gone(tm))
        assert reg.kill(tm.key, by="main") == ""
        assert len(peer_replies(p6)) == 1 and len(main_mail(reg, "r-42")) == 1

    def test_a_dead_requester_gets_nothing_and_its_request_closes(self, reg, stage):
        tm = spawn(reg, run=True)
        p6 = self._queue_behind(reg, stage, tm, "GATE3")
        p6.state = "dead"
        stage.absorb["GATE3"].set()
        assert until(lambda: gone(tm))
        assert peer_replies(p6) == []
        assert reg._post.row("r-41") is None

    def test_a_crash_after_stop_everything_still_wakes(self, reg, stage):
        """ "Stop everything" ■ 는 그 정지 회신만 조용하다 — 다음 런은 원인 없이 시작하고, 그 뒤 죽음의 통지는 깨운다."""
        tm = spawn(reg)
        busy_with(reg, stage, tm, "HOLD30", "r-130")
        assert reg.stop_all_runs(wakes_requesters=False) == [tm.key]
        assert until(lambda: tm.state == "idle" and tm.stop_cause == "")
        p6 = self._queue_behind(reg, stage, tm, "DIE2")
        stage.absorb["DIE2"].set()
        assert until(lambda: gone(tm))
        (r41,) = peer_replies(p6)
        assert r41["mark"] == "died" and r41["opens_run"] is True


class TestStopAll:
    def test_stops_every_working_agent_and_no_idle_one(self, reg, stage):
        a = spawn(reg)
        b = spawn(reg, run=True)
        idle = spawn(reg)
        busy_with(reg, stage, a, "HOLD13", "r-1")
        busy_with(reg, stage, b, "HOLD14", "r-2")
        stopped = reg.stop_all_runs()
        assert sorted(stopped) == sorted([a.key, b.key])
        assert until(lambda: b.state == "dead" and "HOLD13" in stage.stopped)
        assert idle.state == "idle" and a.state != "dead"
        assert main_mail(reg, "r-1") and main_mail(reg, "r-2")


class TestRepliesAreNotItems:
    def test_main_mail_is_outside_the_ledger_and_peer_item_is_a_reply(self, reg, stage):
        p6 = peer(reg)
        tm = spawn(reg)
        busy_with(reg, stage, tm, "HOLD15", "r-5")
        assert not reg.request(tm.key, "x", author=f"agent:{p6.key}", item_id="r-6")
        stage.absorb["HOLD15"].set()
        assert stage.absorbed.setdefault("HOLD15", threading.Event()).wait(10)
        reg.stop_run(tm.key)
        assert until(lambda: main_mail(reg, "r-5") and peer_replies(p6))
        (m,) = main_mail(reg, "r-5")
        assert mail_item(m) is None  # 장부 항목이 아니다 — 기다리던 r-5 를 닫을 뿐
        (it,) = peer_replies(p6)
        assert it["expects_reply"] is False  # 빚이 아니다
        assert reg.reply_debts(f"agent:{p6.key}") == []


class TestPersistence:
    def test_parent_is_saved_and_old_files_without_it_read(self, reg, stage, tmp_path):
        tm = spawn(reg, parent="agent:agt-x")
        reg._save_state()
        data = json.loads((reg.session_dir / "agents.json").read_text())
        (e,) = [x for x in data["agents"] if x["key"] == tm.key]
        assert e["parent"] == "agent:agt-x"
        main_child = spawn(reg)
        reg._save_state()
        data = json.loads((reg.session_dir / "agents.json").read_text())
        (e2,) = [x for x in data["agents"] if x["key"] == main_child.key]
        assert e2["parent"] == "main"
        # 옛 파일(키 없음)
        for x in data["agents"]:
            x.pop("parent", None)
            x["state"] = "dead"
        sdir = tmp_path / "old"
        sdir.mkdir()
        (sdir / "agents.json").write_text(json.dumps(data))
        old = AgentRegistry(sdir, runtime=dict(reg.runtime))
        old.restore()
        assert all(t.parent == "" for t in old._agents.values())


class TestRules:
    def test_the_table(self):
        r = agent_stop.stop_rule
        assert r("stopped", False) == (False, False, False)
        assert r("stopped", True) == (True, True, True)
        assert r("removed", False) == (True, True, True)
        assert r("removed", True) == (True, True, True)
        with pytest.raises(KeyError):
            r("", False)  # 세션 종료는 규칙이 아니다

    def test_activity_skips_unrun_ops_and_reads_only(self):
        records = [
            {
                "role": "assistant",
                "thought": "t1",
                "ops": [{"action": "shell", "action_input": {"command": "ls"}}],
            },
            {"role": "user", "tool": "shell", "success": False, "content": "x"},
            {
                "role": "assistant",
                "thought": "",
                "ops": [{"action": "read_file", "action_input": {"path": "a.py"}}],
            },
            {"role": "user", "tool": "interrupt", "success": False, "content": "stop"},
        ]
        before = json.dumps(records)
        assert agent_stop.run_activity(records) == ['`shell` "ls" ✗']
        assert agent_stop.partial_notes(records) == "t1"
        assert json.dumps(records) == before

    def test_linked_stop(self):
        life = threading.Event()
        run = agent_stop.LinkedStop(life)
        run.set()
        assert run.is_set() and not life.is_set()  # ■ 는 평생 정지를 세우지 않는다
        other = agent_stop.LinkedStop(life)
        life.set()
        assert other.is_set()  # ✕·세션 종료는 런도 멈춘다


class TestNoHiddenSideEffects:
    def test_stop_run_writes_only_the_cause_and_the_run_stop(self, reg, monkeypatch):
        tm = peer(
            reg, "agt-busy0001"
        )  # 스레드 없는 개체 — 정지 뒤의 런 정리가 끼어들지 않는다
        tm.state = "busy"
        tm.run_stop = agent_stop.LinkedStop(tm.stop_event)
        calls = []
        monkeypatch.setattr(reg, "_save_state", lambda: calls.append("save"))
        monkeypatch.setattr(reg, "_notify_roster", lambda: calls.append("roster"))
        assert reg.stop_run(tm.key) == ""
        assert (
            calls == []
            and list(reg.queued_for("main")) == []
            and list(reg.queued_for(f"agent:{tm.key}")) == []
        )
        assert (tm.stop_cause, tm.stop_by) == ("stopped", "user")
        assert tm.run_stop.is_set() and not tm.stop_event.is_set()

    def test_reply_builders_read_only(self):
        acts = ["shell ls ✓"]
        out = agent_stop.stopped_reply("agt-1", "r-1", "do it", acts, "", by="user")
        assert acts == ["shell ls ✓"]
        assert out == (
            'The user stopped agt-1 while it was working on your request [r-1] ("do it").\n'
            "Last activity: shell ls ✓\n"
            "Decide whether to continue another way or report back to the user."
        )


class TestStopEverythingAgentRequesters:
    """사용자 결정 B 의 짝: "Stop everything" 의 정지 회신은 살아 남은 상주의 런도 열지 않는다(``opens_run``
    거짓) — 그 상주가 다음에 일을 받는 런이 함께 읽는다. Stop all agents·채널 ■/✕ 는 지금처럼 깨운다."""

    def _resident_with_sub_run(self, reg, stage):
        res = spawn(reg)
        sub = spawn(reg, parent=f"agent:{res.key}", run=True)
        busy_with(reg, stage, sub, "HOLD20", "r-120", author=f"agent:{res.key}")
        return res, sub

    def test_everything_leaves_the_resident_asleep_until_its_next_request(
        self, reg, stage
    ):
        res, sub = self._resident_with_sub_run(reg, stage)
        assert reg.stop_all_runs(wakes_requesters=False) == [sub.key]
        assert until(lambda: gone(sub))
        assert until(lambda: len(held_items(res)) == 1)
        (held,) = held_items(res)
        assert held["opens_run"] is False and held["re"] == "r-120"
        assert held["mark"] == "stopped by user"
        assert (
            stage.ran == [] and res.handled == 0 and res.state == "idle"
        )  # 상주 런 0회
        assert [i["id"] for i in _inbox_rows(res)] == [held["id"]]  # 미러에 남는다
        assert not reg.has_active_work()  # 들고 있는 항목은 일이 아니다
        assert reg.awaited_replies(f"agent:{res.key}") == []
        # 다음 요청의 런이 함께 읽는다
        assert not reg.request(res.key, "NEXT go on", item_id="r-121")
        assert until(lambda: "NEXT" in stage.ran and res.state == "idle")
        (rec,) = stage.absorbed_by["NEXT"]
        assert "· stopped by user ──" in rec["record"]["content"]
        assert "your request [r-120]" in rec["record"]["content"]
        assert held_items(res) == [] and _inbox_rows(res) == []

    def test_stop_all_agents_wakes_the_resident(self, reg, stage):
        res, sub = self._resident_with_sub_run(reg, stage)
        assert reg.stop_all_runs() == [sub.key]
        # 회신이 런을 열었다(인용된 과제 글의 표지로 가짜 엔진이 그 런을 붙잡는다)
        assert until(lambda: res.state == "busy" and held_items(res) == [])

    def test_held_items_survive_a_restart(self, reg, stage, tmp_path):
        res, _sub = self._resident_with_sub_run(reg, stage)
        reg.stop_all_runs(wakes_requesters=False)
        assert until(lambda: len(held_items(res)) == 1)
        reg._save_state()
        data = json.loads((reg.session_dir / "agents.json").read_text())
        (e,) = [x for x in data["agents"] if x["key"] == res.key]
        assert [i.get("opens_run") for i in e["inbox"]] == [False]
