"""메일함·요청 장부 — 단계 1 에서 먼저 재현하는 추정 버그 다섯 (시안 v4.1 §8 R5).

재현되는 것은 ``xfail(strict=True)`` 로 둔다 — 새 구조(우편함·장부, 단계 2~4)에서 통과해야 한다. 통과하는 순간
strict 가 깨져 표지를 걷게 된다. 재현되지 않는 것은 그 사실을 일반 테스트로 고정한다. 이 단계는 고치지 않는다.

시간에 기대지 않는다: 틈은 레지스트리의 공개 훅(`on_reply` — 메일이 main 메일함에 든 직후, CLI 대기 줄이 쓰는
자리)이나 대본 provider 의 단계 함수가 연다. 기다림은 공개 상태를 보는 ``wait_until`` 뿐이다.
"""

from __future__ import annotations

import threading

import pytest

from agent_cli.session_slots import set_agent_registry
from agent_cli.subagent.agents_live import AgentRegistry
from agent_cli.wake import wake_sources
from tests.binding_support import make_binding
from tests.test_agents_live import make_registry
from tests.test_engine_p2 import CAPS, Provider, call, owed_id, wait_until


def _engine_registry(tmp_path, provider) -> AgentRegistry:
    """진짜 엔진(`run_loop`)으로 도는 레지스트리 — 에이전트의 대본은 ``provider`` 가 든다."""
    binding = make_binding(provider=provider, capabilities=CAPS, model="m")
    reg = AgentRegistry(
        tmp_path / "s",
        runtime={"binding": binding, "max_turns": 20, "depth": 0, "max_depth": 2},
    )
    set_agent_registry(reg)
    return reg


def _state(reg, key) -> str:
    (row,) = [r for r in reg.roster_snapshot() if r["key"] == key]
    return row["state"]


@pytest.fixture
def cleanup():
    regs: list = []
    yield regs
    for r in regs:
        r.shutdown_all()
    set_agent_registry(None)


# ── 1. 깨우지 않는 회신을 든 한가한 상주 ─────────────────────────────────────


class TestHeldResidentAndTheRequestersWait:
    """Q 가 H 에게 요청했고, H 는 X 에게 요청한 뒤 `wait` 로 잠들었다. "Stop everything" 이 X 를 멈추면 X 의
    정지 회신은 H 의 런을 열지 않는 항목으로 H 에게 간다(H 는 그것을 든 채 한가하다). 이제 H 를 깨울 것은
    아무것도 없다 — 그러니 Q 의 `wait` 는 받아들여지면 안 된다(아무도 Q 를 깨우지 않는다)."""

    def _setup(self, tmp_path, cleanup):
        x_stopped = threading.Event()
        p = Provider(
            {
                "TASK-Q1": [
                    call("message", to="hh", text="TASK-H1 needs xx"),
                    call("wait"),
                ],
                "TASK-H1": [
                    call("message", to="xx", text="TASK-X1 slow"),
                    call("wait"),
                ],
                "TASK-X1": [
                    lambda m: (
                        x_stopped.wait(10),
                        call("shell", command="true"),
                    )[1]
                ],
            }
        )
        reg = _engine_registry(tmp_path, p)
        cleanup.append(reg)
        keys = {}
        for name in ("hh", "xx", "qq"):
            keys[name], err = reg.spawn(name=name)
            assert not err
        assert wait_until(lambda: all(_state(reg, k) == "idle" for k in keys.values()))
        assert reg.request(keys["qq"], "TASK-Q1 ask hh") == ""
        assert wait_until(lambda: _state(reg, keys["xx"]) == "busy")
        assert wait_until(
            lambda: (
                _state(reg, keys["hh"]) == "idle" and _state(reg, keys["qq"]) == "idle"
            )
        )
        assert reg.stop_all_runs(wakes_requesters=False) == [keys["xx"]]
        x_stopped.set()
        hh = f"agent:{keys['hh']}"
        # ■ — 상주 X 는 남는다. 정지 회신은 H 의 런을 열지 않는 항목으로 줄에 선다
        assert wait_until(lambda: _state(reg, keys["xx"]) == "idle")
        assert wait_until(lambda: [m.wakes for m in reg.queued_for(hh)] == [False])
        assert _state(reg, keys["hh"]) == "idle"
        return reg, keys

    def test_the_held_reply_does_not_wake_the_resident(self, tmp_path, cleanup):
        """사전 조건 — 받은 회신은 줄에 남고 H 는 한가하다(빚은 열린 채)."""
        reg, keys = self._setup(tmp_path, cleanup)
        hh = f"agent:{keys['hh']}"
        (held,) = reg.queued_for(hh)
        assert held.mark == "stopped by user" and held.wakes is False
        assert [d.to for d in reg.reply_debts(hh)] == [f"agent:{keys['qq']}"]

    @pytest.mark.xfail(
        strict=True,
        reason="받은편지함에 깨우지 않는 항목만 든 한가한 상주를 '잠들지 않았다' 로 보아 요청자의 wait 를 "
        "받아들인다(_debtor_is_parked 가 inbox_items 를 본다) — 새 구조에서 통과해야 함",
    )
    def test_the_requesters_wait_has_nothing_to_wake_it(self, tmp_path, cleanup):
        reg, keys = self._setup(tmp_path, cleanup)
        assert wake_sources(f"agent:{keys['qq']}", agents=reg) == []


# ── 2. 재시작 뒤 에이전트→main 미배달 요청의 보낸 쪽 기록 ────────────────────


class TestRestoredAgentToMainRequest:
    def _sent_then_restored(self, tmp_path):
        reg = make_registry(tmp_path)
        key, _ = reg.spawn(name="asker")
        assert wait_until(lambda: _state(reg, key) == "idle")
        rid = reg.message_to_main(key, "which branch?", expects_reply=True)
        before = reg.awaited_replies(f"agent:{key}")
        reg.shutdown_all()
        fresh = make_registry(tmp_path)
        fresh.restore()
        return fresh, key, rid, before

    def test_the_undelivered_request_itself_survives(self, tmp_path):
        """사전 조건 — 미배달 메일은 main 메일함에 그대로 돌아온다."""
        fresh, _key, rid, before = self._sent_then_restored(tmp_path)
        try:
            assert [r["id"] for r in before] == [rid]
            assert [(m.id, m.expects_reply) for m in fresh.queued_for("main")] == [
                (rid, True)
            ]
        finally:
            fresh.shutdown_all()

    def test_the_sender_still_awaits_the_reply(self, tmp_path):
        """`[v10.145.0 에서 고쳐짐 — 2단계 예외 D1]` 재시작이 main 메일함에 되넣은 요청의 보낸 쪽은 그 회신을
        기다리는 것으로 선다 — 우편에서는 상자에 든 요청마다 줄이 있다. 옛 구현은 받은편지함 항목만 보낸 쪽 기록을
        다시 적어 그 기다림이 사라졌다."""
        fresh, key, rid, _before = self._sent_then_restored(tmp_path)
        try:
            assert [r["id"] for r in fresh.awaited_replies(f"agent:{key}")] == [rid]
        finally:
            fresh.shutdown_all()


# ── 3. 세션 종료가 런을 제때 멈출 때의 요청 ──────────────────────────────────


class TestSessionEndStopsTheRunInTime:
    """세션 종료(`shutdown_all`)가 상주의 런을 도구 실행 전에 멈추면, 그 런이 집어 든 요청은 재시작이 새 id 로
    다시 넣어야 한다(`run_ended` 의 문서: "세션 종료의 빚은 재시작이 끊긴 요청을 새 id 로 다시 넣으며 다시
    적는다")."""

    def _run_stopped_by_session_end(self, tmp_path, cleanup):
        box: dict = {}
        p = Provider(
            {
                "TASK-S1": [
                    lambda m: (
                        wait_until(lambda: _state(box["reg"], box["key"]) == "dead"),
                        call("shell", command="true"),
                    )[1]
                ]
            }
        )
        reg = box["reg"] = _engine_registry(tmp_path, p)
        key, _ = reg.spawn(name="worker")
        box["key"] = key
        assert wait_until(lambda: _state(reg, key) == "idle")
        assert reg.request(key, "TASK-S1 long job") == ""
        assert wait_until(lambda: reg.taken_by(f"agent:{key}") != ())
        before = reg.awaited_replies("main")
        reg.shutdown_all()
        fresh = AgentRegistry(tmp_path / "s", runtime=dict(reg.runtime))
        cleanup.append(fresh)
        fresh.restore()
        return fresh, key, before

    def test_the_request_was_open_before_the_end(self, tmp_path, cleanup):
        _fresh, _key, before = self._run_stopped_by_session_end(tmp_path, cleanup)
        assert [r["text"] for r in before] == ["TASK-S1 long job"]

    @pytest.mark.xfail(
        strict=True,
        reason="멈춘 런의 정리(ResidentLoop.finish → _close_run)가 처리 중 목록을 비운 뒤 마지막 저장이 돌아 "
        "재시작에 넘길 항목이 없다 — 요청이 유실된다 — 새 구조에서 통과해야 함",
    )
    def test_the_restart_puts_the_request_back(self, tmp_path, cleanup):
        fresh, key, _before = self._run_stopped_by_session_end(tmp_path, cleanup)
        assert wait_until(lambda: fresh.awaited_replies("main") != [], timeout=3)
        (row,) = fresh.awaited_replies("main")
        assert row["from"] == f"agent:{key}" and "TASK-S1 long job" in row["text"]


# ── 4. message_to_main 이 메일을 넣은 뒤 보낸 쪽 기록을 적는 틈 ──────────────


class TestReplyInTheMessageToMainGap:
    """에이전트가 main 에게 요청을 보내는 `message_to_main` 은 메일을 메일함에 넣고(`on_reply` 가 불린다)
    그 **뒤**에 보낸 쪽 기록을 적는다. 그 틈에 main 이 메일을 집어 회신하면 회신은 배달되고 기록은 그 뒤에
    적혀, 보낸 에이전트는 이미 받은 회신을 영영 기다린다."""

    def _registry_with_agent(self, tmp_path):
        reg = make_registry(tmp_path)
        key, _ = reg.spawn(name="asker")
        assert wait_until(lambda: _state(reg, key) == "idle")
        return reg, key

    def test_the_hook_fires_while_message_to_main_is_in_flight(self, tmp_path):
        """사전 조건 — 틈을 여는 자리가 실제로 그 사이에 있다(메일은 들었고 함수는 아직 돌아오지 않았다)."""
        reg, key = self._registry_with_agent(tmp_path)
        seen: list = []
        reg.on_reply = lambda mail: seen.append([m.id for m in reg.queued_for("main")])
        try:
            rid = reg.message_to_main(key, "which branch?", expects_reply=True)
            assert seen == [[rid]]
        finally:
            reg.shutdown_all()

    def test_a_reply_in_the_gap_closes_the_wait(self, tmp_path):
        """`[v10.145.0 에서 고쳐짐 — 2단계: 배달과 줄 열기가 한 락 구간]` 옛 구현은 메일을 넣은 뒤에 보낸 쪽
        기록을 적어 그 틈에 온 회신을 놓쳤다.

        회신을 받은 런은 문에서 멈춘다 — 안 그러면 워커가 그 런을 끝내 상자가 비는 것과 단언이 경주한다(부하 아래
        200회에 2회, 2026-11-17)."""
        from tests.test_agents_live import _FakeLoopResult

        door = threading.Event()

        def runner(kind, start, ctx):
            door.wait()
            return _FakeLoopResult(output="ok"), 0.01

        reg = make_registry(tmp_path, runner=runner)
        key, _ = reg.spawn(name="asker")
        assert wait_until(lambda: _state(reg, key) == "idle")

        def main_answers_at_once(mail):
            # main 의 턴 경계가 하는 일 — 꺼내고(빚이 적힌다), 바로 그 id 로 갚는다
            (got,) = reg.drain_replies()
            debt = reg.note_owes("main", f"agent:{key}", "q", id=got["item_id"])
            assert reg.pay_debt("main", debt, "use main") == ""

        reg.on_reply = main_answers_at_once
        try:
            reg.message_to_main(key, "which branch?", expects_reply=True)
            assert reg.queued_for(f"agent:{key}") != () or reg.taken_by(
                f"agent:{key}"
            ), "회신이 배달되지 않았다"
            assert reg.awaited_replies(f"agent:{key}") == []
        finally:
            reg.on_reply = None
            door.set()
            reg.shutdown_all()


# ── 5. answer_debts_as_stopped 와 pay_debt 의 순서 ───────────────────────────


class TestStoppedReplyOrder:
    """`pay_debt` 는 갚음으로 적고 → 배달하고 → 기다림을 닫는다. `answer_debts_as_stopped` 는 배달하고 → 닫고
    → 갚음으로 적는다. 배달 순간(main 메일함에 든 직후의 `on_reply`)의 판정 재료가 다른가."""

    def _run(self, tmp_path, cleanup, *, stop: bool):
        box: dict = {}
        go = threading.Event()

        def work(m):
            go.wait(10)
            if stop:
                return call("shell", command="true")
            return call("reply", id=owed_id(m), text="done")

        p = Provider({"TASK-T1": [work, call("complete", result="ok")]})
        reg = box["reg"] = _engine_registry(tmp_path, p)
        cleanup.append(reg)
        key, _ = reg.spawn(name="worker")
        assert wait_until(lambda: _state(reg, key) == "idle")
        seen: list = []

        def at_delivery(mail):
            if mail.get("re"):
                seen.append(reg.pending_for("main"))

        reg.on_reply = at_delivery
        assert reg.request(key, "TASK-T1 job") == ""
        assert wait_until(lambda: reg.taken_by(f"agent:{key}") != ())
        if stop:
            assert reg.stop_run(key) == ""
        go.set()
        assert wait_until(lambda: len(seen) == 1)
        return seen[0], key

    def test_an_explicit_reply_is_already_paid_when_it_lands(self, tmp_path, cleanup):
        view, key = self._run(tmp_path, cleanup, stop=False)
        assert f"agent:{key} owes you a reply" not in view

    def test_the_requesters_wait_is_unaffected_either_way(self, tmp_path, cleanup):
        """`wait` 는 도착한 메일을 먼저 본다(`dispatch` 의 거절 순서) — 배달 순간의 빚 표시가 달라도 메일이
        이미 있으니 결과는 "도착" 이다. 판정 재료만 다르다."""
        _view, _key = self._run(tmp_path, cleanup, stop=True)
        reg = cleanup[-1]
        assert reg.arrived_for("main") == 1

    def test_a_stopped_reply_is_already_paid_when_it_lands(self, tmp_path, cleanup):
        """`[v10.145.0 에서 고쳐짐 — 2단계: 정지 회신의 배달과 갚음이 한 락 구간(`pay`)]` 옛 구현은 배달한 뒤에
        갚음으로 적어 배달 순간의 판정 재료에 "owes you a reply" 가 남았다."""
        view, key = self._run(tmp_path, cleanup, stop=True)
        assert f"agent:{key} owes you a reply" not in view
