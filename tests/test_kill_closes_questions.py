"""✕ 가 그 에이전트의 질문을 바로 닫고, 받을 수 없는 묻는 쪽에 간 답은 거절된다 (v10.147.5).

종전: ✕(kill) 는 정지를 세우고 명단을 알렸지만 질문 정리는 워커의 정리(다음 턴 경계)에서야 했다 — 일하는
중이면 그 사이 질문이 트레이에 남았고, 거기 답하면 배달이 거절됐는데도(`request` 의 dead) 그 결과를
버리고 질문을 잊은 채 "" 를 돌려줘 웹이 200 을 받았다. 답은 어디에도 가지 않았다.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

from agent_cli.subagent.agents_live import AgentRegistry
from tests.test_agents_live import renderer, wait_until  # noqa: F401


def _held_runner():
    gate, inside = threading.Event(), threading.Event()

    def runner(kind, start, ctx):
        inside.set()
        gate.wait(10)
        return SimpleNamespace(success=True, output="ok"), 0.01

    return runner, gate, inside


def _registry(path, runner):
    return AgentRegistry(path, runtime={"model": "m", "timeout": 5}, runner=runner)


def _open_qs(reg, key):
    (row,) = [r for r in reg.roster_snapshot() if r["key"] == key]
    return [q["id"] for q in row["open_questions"]]


def _busy_agent_with_a_question(tmp_path):
    runner, gate, inside = _held_runner()
    reg = _registry(tmp_path / "s", runner)
    key, _ = reg.spawn(name="alpha")
    assert wait_until(lambda: reg.get(key).state == "idle")
    assert reg.request(key, "work", author="user:dj") == ""
    assert inside.wait(5)
    qid, err = reg.register_question(key, "which file?")
    assert not err and _open_qs(reg, key) == [qid]
    return reg, key, qid, gate


def test_kill_while_working_closes_its_questions_at_once(tmp_path, renderer):  # noqa: F811
    reg, key, qid, gate = _busy_agent_with_a_question(tmp_path)
    try:
        with renderer.lock:
            renderer.calls.clear()
        assert reg.kill(key) == ""
        assert reg.get(key).state == "busy"  # 워커는 아직 런 안 — 그래도
        assert _open_qs(reg, key) == []  # 질문은 이미 닫혔다
        assert reg.open_human_questions() == []
        last = renderer.named("agent_roster")[-1][1]["roster"]
        assert [r["open_questions"] for r in last if r["key"] == key] == [[]]
        assert "already-answered" in reg.answer_question(qid, "x", by="user:dj")
    finally:
        gate.set()
        reg.shutdown_all()


def test_answer_racing_the_kill_is_refused_and_closes_the_question(
    tmp_path,
    renderer,  # noqa: F811
):
    """답이 질문을 찾은 뒤·배달 전에 ✕ 가 끼어든다 — 배달이 거절되면 그 거절을 돌려주고 질문을 닫는다."""
    reg, key, qid, gate = _busy_agent_with_a_question(tmp_path)
    found, go = threading.Event(), threading.Event()
    deliver = reg._deliver_answer

    def deliver_after_the_kill(q, text):  # 질문을 찾은 뒤에 멈춘다 — ✕ 가 먼저 닿게
        found.set()
        assert go.wait(5)
        return deliver(q, text)

    reg._deliver_answer = deliver_after_the_kill
    out: dict = {}
    t = threading.Thread(
        target=lambda: out.setdefault(
            "r", reg.answer_question(qid, "src/main.py", by="user:dj")
        )
    )
    try:
        t.start()
        assert found.wait(5)
        assert reg.kill(key) == ""
        with renderer.lock:
            renderer.calls.clear()
        go.set()
        t.join(5)
        assert (
            out["r"]
            == f'the agent that asked "{qid}" has stopped — the question is closed'
        )
        assert reg.open_human_questions() == []
        assert reg.queued_for(f"agent:{key}") == ()  # 답은 어디에도 들어가지 않았다
    finally:
        gate.set()
        reg.shutdown_all()


def test_answer_to_a_stopping_asker_is_refused_even_without_the_kill(
    tmp_path,
    renderer,  # noqa: F811
):
    """정지가 선 묻는 쪽(✕ 를 거치지 않아 질문이 남은 경우 — 세션 종료 중)에 간 답 — 거절, 질문 닫힘, 명단 알림.
    종전엔 "" (웹 200)."""
    reg, key, qid, gate = _busy_agent_with_a_question(tmp_path)
    try:
        reg.get(key).stop_event.set()
        with renderer.lock:
            renderer.calls.clear()
        r = reg.answer_question(qid, "src/main.py", by="user:dj")
        assert r == f'the agent that asked "{qid}" has stopped — the question is closed'
        assert reg.open_human_questions() == []
        last = renderer.named("agent_roster")[-1][1]["roster"]
        assert [r["open_questions"] for r in last if r["key"] == key] == [[]]
    finally:
        gate.set()
        reg.shutdown_all()


def test_shutdown_keeps_questions_for_resume_and_kill_does_not(tmp_path, renderer):  # noqa: F811
    """세션 종료는 ✕ 의 길을 지나지 않는다 — 산 에이전트의 질문은 resume 이 되살리고, ✕ 된 것의 질문은 없다."""

    def runner(kind, start, ctx):
        return SimpleNamespace(success=True, output="ok"), 0.01

    reg = _registry(tmp_path / "s", runner)
    alive, _ = reg.spawn(name="alive")
    killed, _ = reg.spawn(name="killed")
    assert wait_until(
        lambda: reg.get(alive).state == "idle" and reg.get(killed).state == "idle"
    )
    qa, _ = reg.register_question(alive, "qa?")
    reg.register_question(killed, "qb?")
    assert reg.kill(killed) == ""
    reg.shutdown_all()
    again = _registry(tmp_path / "s", runner)
    again.restore()
    try:
        assert [q.id for q in again.open_human_questions()] == [qa]
        assert _open_qs(again, alive) == [qa] and _open_qs(again, killed) == []
        assert again.stale_questions == 0
    finally:
        again.shutdown_all()
