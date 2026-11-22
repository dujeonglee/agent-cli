"""재시작이 무엇을 올리나 — 장면마다 한 사실 (5c-0).

"강제 종료" 는 앞 레지스트리를 정리하지 않은 채 같은 세션 폴더로 새 레지스트리를 복원하는 것이다(프로세스가 죽은
것과 같다). "새 프로세스" 는 번호 할당기를 0 으로 되돌린다. LLM 은 부르지 않는다 — 가짜 러너가 쿼리를 기록한다.

규칙(5c-0): 꺼짐 = 전부 멈춤. 재시작은 main·상주의 컨텍스트, 명단(상주 idle, 과제 도중이던 run 은 dead), 예약, 화면
재생만 올린다. 메일 상자·하던 일·줄 선 요청·빚·기다림 표시·열린 질문은 버린다. 스스로 이어 하는 것은 없다.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from agent_cli import item_ids
from agent_cli.subagent import conversation_log
from agent_cli.subagent.agents_live import AgentRegistry
from agent_cli.subagent.resident_loop import ResidentLoop
from tests.test_agents_live import (
    _FakeLoopResult,
    _RecordingRunner,
    make_registry,
    renderer,  # noqa: F401
    wait_until,
)

OLD_SAMPLE = Path(__file__).parent / "fixtures" / "agents_state_sample_v10_158.json"


def _blocking_runner():
    """첫 쿼리에서 막히는 러너 — 런 도중 강제 종료를 만든다."""
    gate = threading.Event()
    started = threading.Event()

    def runner(kind, start, ctx):
        started.set()
        gate.wait(5)
        return _FakeLoopResult(output=f"done:{start.query}"), 0.01

    return runner, gate, started


def _write_old_sample(tmp_path):
    """표본은 시각을 ``"<t>"`` 로 지웠다 — 읽을 수 있게 수로 되돌려 세션 폴더에 둔다."""
    text = OLD_SAMPLE.read_text(encoding="utf-8").replace('"<t>"', "0.0")
    (tmp_path / "agents.json").write_text(text, encoding="utf-8")


def _new_process():
    item_ids._last = 0  # 할당기는 저장되지 않는다


@pytest.fixture
def regs():
    made: list = []
    gates: list = []
    yield made, gates
    for g in gates:
        g.set()
    for r in made:
        r.shutdown_all()


def _mid_run(tmp_path, regs, *, ends_after_task=False):
    """reg1: 에이전트가 main 의 요청 "task-1" 을 처리하는 도중(러너가 막힘)."""
    made, gates = regs
    runner, gate, started = _blocking_runner()
    gates.append(gate)
    reg1 = make_registry(tmp_path, runner=runner)
    made.append(reg1)
    key, _ = reg1.spawn(name="worker")
    assert wait_until(lambda: reg1.get(key).state == "idle")
    if ends_after_task:
        tm = reg1.get(key)
        tm.ends_after_task = True
    assert reg1.request(key, "task-1") == ""
    assert started.wait(3)
    if ends_after_task:
        reg1.get(key).task_id = reg1.taken_by(f"agent:{key}")[0].id
        reg1._save_state()
    return reg1, key


def _restart(tmp_path, regs, runner=None):
    made, _ = regs
    _new_process()
    rec = runner or _RecordingRunner()
    reg2 = make_registry(tmp_path, runner=rec)
    made.append(reg2)
    reg2.restore()
    return reg2, rec


# ── (a) 에이전트가 일하는 도중 강제 종료 ─────────────────────────────────


def test_a_mid_run_agent_comes_back_idle_with_nothing_to_do(tmp_path, renderer, regs):  # noqa: F811
    """끊긴 런은 다시 들어가지 않는다 — 상주는 idle 로 서고 받은편지함은 비었다(종전: 안내를 붙여 다시 돌았다)."""
    _reg1, key = _mid_run(tmp_path, regs)
    reg2, rec = _restart(tmp_path, regs)
    assert wait_until(lambda: reg2.get(key).state == "idle")
    assert reg2.queued_for(f"agent:{key}") == () and rec.queries() == []


# ── (b) main 이 회신을 기다리던 중 강제 종료 ───────────────────────────────


def test_b_main_awaits_nothing_after_restart(tmp_path, renderer, regs):  # noqa: F811
    """main 이 기다리던 회신은 없다 — 빚도 기다리는 회신도 버린다(종전: 다시 넣은 요청의 새 id 를 기다렸다)."""
    reg1, key = _mid_run(tmp_path, regs)
    assert reg1.awaited_replies("main") != []
    reg2, _rec = _restart(tmp_path, regs)
    assert wait_until(lambda: reg2.get(key).state == "idle")
    assert reg2.awaited_replies("main") == []
    assert reg2.reply_debts(f"agent:{key}") == []


# ── (c) 끝난 회신을 main 이 읽기 전 종료 ──────────────────────────────────


def test_c_an_unread_reply_is_dropped(tmp_path, renderer, regs):  # noqa: F811
    """main 이 읽지 않은 회신은 버린다 — 다음 세션의 main 상자는 비었다(종전: 다시 섰다). 전문은 회신 파일에 남는다."""
    made, _ = regs
    reg1 = make_registry(tmp_path, runner=_RecordingRunner())
    made.append(reg1)
    key, _ = reg1.spawn(name="worker")
    assert wait_until(lambda: reg1.get(key).state == "idle")
    assert reg1.request(key, "task-1") == ""
    assert wait_until(lambda: len(reg1.queued_for("main")) == 1)
    reg2, _rec = _restart(tmp_path, regs)
    assert reg2.queued_for("main") == ()


# ── (d) 열린 사람 질문 ─────────────────────────────────────────────────────


def test_d_an_open_question_is_dropped(tmp_path, renderer, regs):  # noqa: F811
    """열린 사람 질문은 버린다 — 트레이가 빈다(종전: 다시 떴다). 질문 글은 묻던 쪽의 history·🤝 창에 남는다."""
    made, _ = regs
    reg1 = make_registry(tmp_path, runner=_RecordingRunner())
    made.append(reg1)
    key, _ = reg1.spawn(name="asker")
    assert wait_until(lambda: reg1.get(key).state == "idle")
    qid, err = reg1.register_question(key, "v1 or v2?")
    assert qid and not err
    reg2, _rec = _restart(tmp_path, regs)
    assert reg2.open_human_questions() == []


# ── (e) 일회용(run) 에이전트가 과제 도중 종료 ─────────────────────────────


def test_e_a_run_agent_mid_task_stands_dead(tmp_path, renderer, regs):  # noqa: F811
    """과제 도중이던 run 에이전트는 dead 로 선다 — 되살아나지 않는다(종전: 되살아나 과제를 다시 했다)."""
    _reg1, key = _mid_run(tmp_path, regs, ends_after_task=True)
    reg2, rec = _restart(tmp_path, regs)
    assert reg2.get(key).state == "dead" and reg2.get(key).worker is None
    assert rec.queries() == []


# ── 옛 모양의 agents.json ────────────────────────────────────────────────


def test_an_old_shaped_file_is_read(tmp_path, renderer, regs):  # noqa: F811
    """옛 구현이 쓴 표본(줄 선 요청·처리 중 요청·main 앞 메일 둘·열린 질문) — 깨지지 않고 명단만 세운다. 옛 칸은
    읽지 않는다(종전: 메일·질문·줄을 되살리고 끊긴 런을 다시 돌렸다)."""
    made, gates = regs
    _write_old_sample(tmp_path)
    _new_process()
    seen: list = []
    hold = threading.Event()
    gates.append(hold)

    def runner(kind, start, ctx):
        seen.append(start.query)
        hold.wait(5)  # 회신이 main 상자에 서지 않게 — 아래 수는 되살린 메일만
        return _FakeLoopResult(), 0.01

    reg = make_registry(tmp_path, runner=runner)
    made.append(reg)
    assert reg.restore() == 2
    assert sorted(t.key for t in reg.instances()) == ["agt-A", "agt-B"]
    assert wait_until(lambda: all(t.state == "idle" for t in reg.instances()))
    assert reg.queued_for("main") == () and reg.open_human_questions() == []
    assert all(reg.queued_for(f"agent:{k}") == () for k in ("agt-A", "agt-B"))
    assert seen == []


# ── 번호 — 저장에 없는 id 와 겹치지 않는다 ─────────────────────────────────


def _roster_only_session(tmp_path) -> str:
    """명단만 든 agents.json(재시작이 올리는 모양)과, 기록 글에만 남은 id 두 개 — 에이전트 history 의 질문 ``q-30``,
    🤝 창의 요청 ``r-40``. 에이전트 키를 돌려준다."""
    key = "agt-K"
    home = tmp_path / "agents" / key
    home.mkdir(parents=True)
    (home / "history.jsonl").write_text(
        json.dumps({"role": "user", "content": 'asked the user "v1?" as q-30'}) + "\n",
        encoding="utf-8",
    )
    (home / "conversation.jsonl").write_text(
        json.dumps({"direction": "in", "item": "r-40", "text": "TASK queued"}) + "\n",
        encoding="utf-8",
    )
    doc = json.loads(OLD_SAMPLE.read_text(encoding="utf-8"))
    entry = {
        k: v
        for k, v in doc["agents"][1].items()
        if k not in ("inbox", "in_flight", "created_at")
    }
    entry["key"] = key
    (tmp_path / "agents.json").write_text(
        json.dumps({"version": 2, "agents": [entry]}), encoding="utf-8"
    )
    return key


def test_new_ids_do_not_reuse_ids_written_in_the_records(tmp_path, renderer):  # noqa: F811
    from agent_cli.context.manager import ContextManager

    _roster_only_session(tmp_path)
    _new_process()
    ContextManager(
        tmp_path, max_context_tokens=10000, resume=True
    )  # main ctx — 세션 폴더 전체
    assert item_ids.number_of(item_ids.next_id(item_ids.USER)) > 40


def test_ids_are_read_only_where_they_stand_as_ids(tmp_path):
    """``agt-5`` 의 ``t-5``·``user-9`` 의 ``r-9`` 는 id 가 아니다 — 접두 앞이 낱말 글자이면 세지 않는다."""
    (tmp_path / "history.jsonl").write_text(
        '{"content": "agt-55 and user-90 but [n-7]"}\n', encoding="utf-8"
    )
    _new_process()
    item_ids.observe_records(tmp_path)
    assert item_ids.next_id(item_ids.MAIL) == "n-8"


# ── 재시작 기록 — main·되살린 상주의 컨텍스트 끝에 한 줄 ──────────────────────


def _last(ctx) -> dict | None:
    tail = ctx.records_since(ctx.last_ordinal) if ctx.last_ordinal is not None else []
    return tail[-1] if tail else None


def _is_restart_record(rec) -> bool:
    from agent_cli.constants import RESTART_NOTICE

    return bool(rec) and (rec.get("tool"), rec.get("content")) == (
        "interrupt",
        RESTART_NOTICE,
    )


def test_record_restart_writes_once_on_a_context_with_history(tmp_path):
    from agent_cli.context.manager import ContextManager
    from agent_cli.session.records import interrupt_record, record_restart

    fresh = ContextManager(tmp_path / "new", max_context_tokens=10000)
    assert record_restart(fresh) is False and fresh.last_ordinal is None  # 새 세션
    ctx = ContextManager(tmp_path / "old", max_context_tokens=10000)
    ctx.add({"role": "user", "content": "before the cut"})
    again = ContextManager(tmp_path / "old", max_context_tokens=10000, resume=True)
    assert record_restart(again) is True and _is_restart_record(_last(again))
    twice = ContextManager(tmp_path / "old", max_context_tokens=10000, resume=True)
    assert record_restart(twice) is False  # 사이에 아무 런도 없던 거듭 재시작 — 한 줄
    twice.add({"role": "user", "content": "a run in between"})
    third = ContextManager(tmp_path / "old", max_context_tokens=10000, resume=True)
    assert (
        record_restart(third) is True
    )  # 기록 뒤에 런이 돌고 다시 재시작 — 두 번의 재시작은 두 사실이다
    restarts = [r for r in third.records_since(0) if _is_restart_record(r)]
    assert len(restarts) == 2 and _is_restart_record(_last(third))
    assert interrupt_record("x") == {
        "role": "user",
        "tool": "interrupt",
        "success": False,
        "content": "x",
    }  # Stop 의 기록과 같은 모양


def test_a_revived_resident_reads_the_restart_record(tmp_path, renderer, regs):  # noqa: F811
    reg1, key = _mid_run(tmp_path, regs)
    reg1.get(key).ctx.add(
        {"role": "user", "content": "task-1"}
    )  # 가짜 러너는 ctx 에 쓰지 않는다 — 이력을 세운다
    reg2, _rec = _restart(tmp_path, regs)
    assert wait_until(lambda: reg2.get(key).state == "idle")
    assert _is_restart_record(_last(reg2.get(key).ctx))


def _restart_lines(reg, key) -> list[dict]:
    return [
        ln
        for ln in conversation_log.read(reg.get(key).home_dir)
        if ln["direction"] == "restarted"
    ]


def test_a_revived_resident_gets_one_restart_line_in_its_window(
    tmp_path,
    renderer,  # noqa: F811
    regs,
):
    """재시작 기록을 적은 때 🤝 창에도 한 줄(v10.165.0) — `conversation.jsonl` 에 남고(resume 재생이 다시 그린다)
    화면에 나간다. 기록을 적지 않는 거듭 재시작(사이에 런이 없다)에는 줄도 없다."""
    reg1, key = _mid_run(tmp_path, regs)
    reg1.get(key).ctx.add({"role": "user", "content": "task-1"})
    reg2, _rec = _restart(tmp_path, regs)
    assert wait_until(lambda: reg2.get(key).state == "idle")
    (line,) = _restart_lines(reg2, key)
    assert (line["key"], line["to"], line["text"]) == (key, key, "")
    shown = [c[1] for c in renderer.named("agent_message")]
    assert [c["key"] for c in shown if c["direction"] == "restarted"] == [key]
    reg3, _rec = _restart(tmp_path, regs)
    assert wait_until(lambda: reg3.get(key).state == "idle")
    assert _is_restart_record(_last(reg3.get(key).ctx))
    assert len(_restart_lines(reg3, key)) == 1


def _directions(reg, key) -> list[str]:
    return [ln["direction"] for ln in conversation_log.read(reg.get(key).home_dir)]


def _ran_once(tmp_path, regs):
    """reg1: 상주가 요청 하나를 끝냈다(창에 in·out 두 줄, history 한 줄)."""
    made, _ = regs
    reg1 = make_registry(tmp_path, runner=_RecordingRunner())
    made.append(reg1)
    key, _ = reg1.spawn(name="worker")
    assert wait_until(lambda: reg1.get(key).state == "idle")
    assert reg1.request(key, "task-1", author="user:dj") == ""
    assert wait_until(lambda: reg1.get(key).handled >= 1)
    reg1.get(key).ctx.add({"role": "user", "content": "task-1"})
    assert _directions(reg1, key) == ["in", "out"]
    return reg1, key


def test_a_delivery_right_after_the_restart_stands_below_the_restart_line(
    tmp_path,
    renderer,  # noqa: F811
    regs,
    monkeypatch,
):
    """[A1(11) 에서 바뀜] 되살린 워커가 컨텍스트를 열기 전에 새 요청이 오면 그 요청이 재시작 줄보다 먼저 적힌다 —
    워커의 `ResidentLoop.open` 을 그 배달이 기록될 때까지 잡아 그 순서를 세운다."""
    _reg1, key = _ran_once(tmp_path, regs)
    delivered = threading.Event()
    real_open = ResidentLoop.open

    def held_open(self, parent_ctx):
        if self.tm.restarted:
            assert delivered.wait(10)
        return real_open(self, parent_ctx)

    monkeypatch.setattr(ResidentLoop, "open", held_open)
    reg2, _rec = _restart(tmp_path, regs)
    assert reg2.request(key, "task-2", author="user:dj") == ""
    delivered.set()
    assert wait_until(lambda: reg2.get(key).handled >= 2)
    assert _directions(reg2, key) == ["in", "out", "in", "restarted", "out"]


def test_the_restart_line_goes_out_twice_when_the_worker_writes_it_before_the_replay(
    tmp_path,
    renderer,  # noqa: F811
    regs,
    monkeypatch,
):
    """[A1(11) 에서 바뀜] `restore` 의 창 재생과 워커가 적는 재시작 줄의 경주 — 워커가 먼저 적으면 재생이 그 줄을
    읽어 한 번, 워커가 한 번 낸다. 재생을 그 줄이 선 뒤로 잡아 그 순서를 세운다."""
    _reg1, key = _ran_once(tmp_path, regs)
    real_replay = AgentRegistry._replay_conversation

    def replay_after_the_line(self, tm):
        if tm.restarted:
            assert wait_until(lambda: "restarted" in _directions(self, tm.key))
        real_replay(self, tm)

    monkeypatch.setattr(AgentRegistry, "_replay_conversation", replay_after_the_line)
    reg2, _rec = _restart(tmp_path, regs)
    assert wait_until(lambda: reg2.get(key).state == "idle")
    assert wait_until(lambda: _is_restart_record(_last(reg2.get(key).ctx)))
    shown = [c[1]["direction"] for c in renderer.named("agent_message")]
    assert shown.count("restarted") == 2
    assert _directions(reg2, key) == ["in", "out", "restarted"]


def test_a_resident_with_history_but_an_empty_window_gets_the_line_too(
    tmp_path,
    renderer,  # noqa: F811
    regs,
):
    """[A1(11) 에서 바뀜] 지금은 history 가 기준이다 — 창에 줄이 없어도 재시작 기록을 적으면 줄이 선다."""
    reg1, key = _mid_run(tmp_path, regs)
    reg1.get(key).ctx.add({"role": "user", "content": "task-1"})
    conversation_log.path_of(reg1.get(key).home_dir).unlink()
    reg2, _rec = _restart(tmp_path, regs)
    assert wait_until(lambda: _is_restart_record(_last(reg2.get(key).ctx)))
    assert wait_until(lambda: _directions(reg2, key) == ["restarted"])


def test_no_restart_record_for_a_run_agent_or_a_resident_revived_after_a_kill(
    tmp_path,
    renderer,  # noqa: F811
    regs,
):
    made, _ = regs
    _reg1, run_key = _mid_run(tmp_path, regs, ends_after_task=True)
    reg2, _rec = _restart(tmp_path, regs)
    assert reg2.get(run_key).ctx is None  # dead — 컨텍스트를 열지 않는다
    reg3 = make_registry(tmp_path / "other", runner=_RecordingRunner())
    made.append(reg3)
    key, _ = reg3.spawn(name="w")
    assert wait_until(lambda: reg3.get(key).state == "idle")
    assert reg3.request(key, "task") == ""
    assert wait_until(lambda: reg3.get(key).handled >= 1)
    reg3.get(key).ctx.add(
        {"role": "user", "content": "task"}
    )  # 가짜 러너는 ctx 에 쓰지 않는다 — 이력을 세운다
    assert reg3.kill(key) == ""
    assert reg3.resume_teammate(key) == ""  # ✕ 뒤 사람이 되살림 — 재시작이 아니다
    assert wait_until(lambda: reg3.get(key).state == "idle")
    assert not _is_restart_record(_last(reg3.get(key).ctx))
    assert _restart_lines(reg3, key) == []  # 창의 재시작 줄도 없다
