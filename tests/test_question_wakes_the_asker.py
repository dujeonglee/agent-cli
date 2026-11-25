"""묻고 기다리는 에이전트 (6a) — 사람에게 `ask` 한 뒤 `wait` 하면 무엇이 일어나나, 그리고 질문이 열린 턴에 묻는 쪽이 읽는 글.

장면은 오라클 드라이버의 대본 공급자와 세션으로 돌린다(모델 호출 없음). 동기점은 제품이 읽는 값이다 — 창구의 기다림
표시(`is_waiting`), 에이전트 상태, 공급자의 호출 수. 시간 판정이 없다."""

from __future__ import annotations

import pytest

import agent_cli.render as render_mod
from agent_cli.loop.ledger import OPEN_QUESTIONS_HEAD
from agent_cli.prompts.system_prompt import _ASK_INLINE
from agent_cli.tools.virtual import AskTool
from agent_cli.wake import WaitingOn, wake_items
from tests.oracle.driver import (
    Provider,
    Session,
    call,
    complete_all,
    owed_id,
    runs_ended,
    until,
)
from tests.test_agents_live import make_registry
from tests.test_wait_checkin import _fake_agent

Q = "which directory should I use?"
ANSWER = "use src/util"
WAIT_CALL = call("wait")


@pytest.fixture(autouse=True)
def _keep_renderer():
    """오라클 세션은 전역 렌더러를 자기 기록 렌더러로 갈아 끼운다 — 다음 테스트로 새지 않게 되돌린다."""
    before = render_mod.get_renderer()
    yield
    render_mod.set_renderer(before)


@pytest.fixture
def where(tmp_path, monkeypatch):
    """세션 조립이 작업 폴더에 무엇을 만들든 저장소 밖에서 — 오라클 드라이버와 같다."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_CLI_SNAPSHOT", "0")
    return tmp_path


def _content(m) -> str:
    return str(m.get("content") or "")


def _asks_then_waits(
    root,
    *,
    owes: bool,
    job: bool = False,
    mode: str = "spawn",
    then: str = "wait",
    interrupt: str | None = None,
) -> dict:
    """에이전트 aa 가 일을 받아 (작업을 띄우고) `ask` 한 뒤 `wait` 한다. 판정이 난 뒤 사람이 트레이 길로 답한다.

    ``owes`` — main 이 회신을 기다리는 요청으로 맡겼다(aa 가 r- 를 빚진다). ``mode`` "run" 은 일회용 에이전트이고
    main 은 그 결과를 `wait` 로 기다린다. 돌려주는 것: aa 가 `wait` 뒤 읽은 관찰, `ask` 바로 다음 호출의 마지막
    메시지(관찰 + 꼬리), 작업 구간 수, 답을 읽었는가, main 이 읽은 것.

    ``then`` "complete" — `wait` 대신 `complete`(빚을 든 채면 되묻기를 받는다). ``interrupt`` — 판정 뒤 답 대신
    ■("stop": 런 정지, 그 뒤 답) 또는 ✕("kill": 에이전트를 끝냄, 답하지 않는다)."""
    got: dict = {"aa_after_wait": None, "read_answer": False, "main_reads": []}

    def waits(m):
        got["after_ask"] = _content(m[-1])
        return (
            WAIT_CALL
            if then == "wait"
            else call("complete", result="asked", answers=[])
        )

    def rest(m):
        if got["aa_after_wait"] is None:
            got["aa_after_wait"] = m
        if any("── answer [" in _content(x) for x in m):
            got.setdefault("after_answer", _content(m[-1]))
            got["read_answer"] = True
        try:
            said = (
                f"done: {ANSWER}" if got["read_answer"] else "done without the answer"
            )
            return call("reply", id=owed_id(m), text=said)
        except AssertionError:
            return complete_all("aa done")(m)

    def main_reads(m):
        got["main_reads"].append(_content(m[-1]))
        return complete_all("main done")(m)

    aa = (
        ([call("shell", command="sleep 600", background=True)] if job else [])
        + [
            call("ask", question=Q),
            waits,
        ]
        + [rest] * 8
    )

    def main_waits(m):
        until(lambda: judged(), "aa judged (main side)")
        return WAIT_CALL

    if mode == "run":
        main = [call("agent", mode="run", task="TASK-A do the job"), main_waits] + [
            main_reads
        ] * 6
    else:
        main = [
            call("agent", mode="spawn", name="aa"),
            lambda m: call(
                "message", to="aa", text="TASK-A do the job", expects_reply=owes
            ),
            complete_all("handed off"),
        ] + [main_reads] * 6
    p = Provider({"MAIN": main, "TASK-A": aa})
    s = Session(root, p, path="web")

    def key() -> str:
        keys = s.agent_keys()
        return keys[0] if keys else ""

    def judged() -> bool:
        k = key()
        # 판정 = 제자리 대기가 섰거나 런이 끝났다(거절 뒤 회신하고 끝냄 · 끝 글). 답은 그 뒤에만 넣는다 — 도는 런에
        # 넣으면 같은 런에 흡수돼 구간 수가 경합에 달린다
        return bool(k) and (
            s.reg._post.is_waiting(f"agent:{k}") or s.agent_state(k) in ("idle", "dead")
        )

    s.say("MAIN go", nickname="dj")
    t = s.pump_in_thread()
    try:
        until(lambda: "after_ask" in got and judged(), "aa called wait and was judged")
        k = key()
        got["waiting_in_place"] = s.reg._post.is_waiting(f"agent:{k}")
        open_qs = [q.id for q in s.reg.open_human_questions() if q.asker == k]
        got["qid"] = (
            open_qs[0] if open_qs else None
        )  # 내려온 일회용 에이전트의 질문은 닫힌다
        if mode == "run":
            until(
                lambda: s.reg._post.is_waiting("main") or got["main_reads"],
                "main judged",
            )
            got["main_waiting_in_place"] = s.reg._post.is_waiting("main")
        if interrupt == "stop":
            assert not s.reg.stop_run(k)
            until(lambda: s.agent_state(k) == "idle", "aa idle after the stop")
        elif interrupt == "kill":
            assert not s.reg.kill(k)
            got["qid"] = None
        got["open_after_interrupt"] = [q.id for q in s.reg.open_human_questions()]
        got["state_after_interrupt"] = s.agent_state(k)
        if got["qid"]:
            assert not s.reg.answer_question(got["qid"], ANSWER, by="user:dj")
            until(lambda: got["read_answer"], "aa read the answer")
            until(lambda: s.agent_state(k) in ("idle", "dead"), "aa settled")
        if mode == "run":
            until(
                lambda: any("done" in r for r in got["main_reads"]),
                "main read the result",
            )
        got["runs"] = sum(
            1
            for e in s.recorder.lanes.get(f"agent-{k}", [])
            if e[0] == "begin_agent_work"
        )
    finally:
        s.close()
        t.join(20)
    m = got["aa_after_wait"]
    if m is None:  # ✕ — 그 뒤 호출이 없다
        return got
    i = next(
        n
        for n, x in enumerate(m)
        if x.get("role") == "assistant" and f'"action": "{then}"' in _content(x)
    )
    obs = _content(m[i + 1])
    got["wait_obs"] = obs.removeprefix(f"[{then}]\n").split("\n\n", 1)[0]
    got["then_obs"] = obs.split("── end of message", 1)[0]
    return got


# ── 묻고 `wait` 한 뒤 ─────────────────────────────────────────


def _accepted_on(g) -> str:
    return (
        f'Observation: waiting — will wake on: your question "{g["qid"]}" to the user.'
    )


def test_a_resident_holding_a_request_asks_then_waits(where):
    """빚(r-)을 든 상주가 묻고 `wait` — 내 질문이 나를 깨우므로 받아들여진다. 답을 읽은 **뒤** 회신하고, 구간은 하나다."""
    g = _asks_then_waits(where, owes=True)
    assert g["wait_obs"] == _accepted_on(g)
    assert g["waiting_in_place"]
    assert g["runs"] == 1


def test_a_resident_with_nothing_owed_asks_then_waits(where):
    """빚 없이 묻고 `wait` — 받아들여지고 답이 같은 구간을 잇는다."""
    g = _asks_then_waits(where, owes=False)
    assert g["wait_obs"] == _accepted_on(g)
    assert g["runs"] == 1


def test_a_job_and_a_question_the_acceptance_names_both(where):
    """작업과 질문을 함께 기다린다 — 수락 문장이 둘 다 말하고, 타이머는 작업 때문에 건다."""
    g = _asks_then_waits(where, owes=False, job=True)
    assert g["wait_obs"] == (
        "Observation: waiting — will wake on: job #1 (`sleep 600`); "
        f'your question "{g["qid"]}" to the user, or in 5m if nothing has reported.'
    )
    assert g["runs"] == 1


def test_a_run_agent_asks_then_waits_while_main_waits_for_it(where):
    """일회용(`mode` "run") 에이전트가 묻고 `wait` — 상주와 같은 규칙: 내려오지 않고 답을 기다린다. 그를 기다리는
    main 도 받아들여지고(잠든 채무자를 질문이 깨운다), 답을 실은 결과가 main 을 깨운다."""
    g = _asks_then_waits(where, owes=True, mode="run")
    assert g["wait_obs"] == _accepted_on(g)
    assert g["main_waiting_in_place"]
    assert g["read_answer"]
    assert any(f"done: {ANSWER}" in r for r in g["main_reads"])


# ── 질문이 열린 턴에 묻는 쪽이 읽는 글 ─────────────────────────


def test_the_turn_after_ask_reads_the_question_in_the_tail(where):
    """`ask` 바로 다음 호출 — 관찰은 일어난 일만, 어디로 가나는 꼬리의 ``## Your open questions`` 가 말한다."""
    g = _asks_then_waits(where, owes=False)
    obs, tail = g["after_ask"].split("── end of message", 1)
    # 관찰 뒤의 넛지(`_OBS_COMPLETE_NUDGE`)는 모든 관찰에 붙는 것이라 그대로다
    assert obs.strip().split("\n\n")[0] == (
        f'[ask]\nObservation: question "{g["qid"]}" sent — "{Q}"'
    )
    block = f'{OPEN_QUESTIONS_HEAD}\n- "{g["qid"]}" · "{Q}"\n'
    assert block in tail
    assert OPEN_QUESTIONS_HEAD == (
        "## Your open questions (do not wait for the answer: do what you can without it, "
        "and call `wait` only when you cannot go further)"
    )


def test_the_description_and_guide_no_longer_say_where_to_go():
    """경로 선택은 꼬리 한 곳에서만 — 머리의 설명·안내는 못 나아갈 때 `complete` 로 보내지 않는다(6a). 설명의
    `complete` 는 질문이 아닌 인사말의 끝맺음만 말한다(6b — 설명 하나)."""
    assert "If nothing else can proceed" not in AskTool.description
    assert "`complete`" not in _ASK_INLINE
    assert (
        "  - The answer may never come: a person is free not to answer, and nobody\n"
        "    chases them.\n" in _ASK_INLINE
    )


def test_a_closed_question_leaves_the_tail_on_the_next_turn(where):
    """답이 오면 질문이 닫힌다 — 답을 읽는 그 호출의 꼬리에는 블록이 없다."""
    g = _asks_then_waits(where, owes=True)
    assert "── end of message" in g["after_answer"]  # 꼬리가 붙은 메시지다
    assert (
        "## Your open questions"
        not in g["after_answer"].split("── end of message", 1)[-1]
    )


def test_stop_while_waiting_on_a_question_keeps_it_open(where):
    """■ — 제자리 대기가 정지로 깨고 런이 끝난다. 질문은 열린 채 남고, 답은 지금처럼 새 구간을 연다."""
    g = _asks_then_waits(where, owes=False, interrupt="stop")
    assert g["open_after_interrupt"] == [g["qid"]]
    assert g["read_answer"] and g["runs"] == 2


def test_kill_while_waiting_on_a_question_closes_it(where):
    """✕ — 에이전트를 끝내면 그가 건 질문도 닫힌다."""
    g = _asks_then_waits(where, owes=False, interrupt="kill")
    assert g["open_after_interrupt"] == [] and g["state_after_interrupt"] == "dead"


def test_complete_while_owing_now_hears_of_wait(where):
    """빚을 든 채 묻고 `complete` — 되묻기에 `wait` 안내가 붙는다: 열린 질문이 깨울 것이기 때문이다(글은 그대로)."""
    g = _asks_then_waits(where, owes=True, then="complete")
    assert (
        "If you are only waiting for something you started to report back, call `wait` "
        "instead — it keeps these items open and the report wakes you."
    ) in g["then_obs"]


# ── 누구의 질문이 누구를 깨우나 (레지스트리) ─────────────────


def test_only_my_own_question_wakes_me_and_it_sets_no_timer(tmp_path):
    """남의 질문은 나를 깨우지 않고 내 꼬리에도 없다. 질문만 기다리는 `wait` 는 체크인 타이머를 걸지 않는다."""
    reg = make_registry(tmp_path)
    try:
        _fake_agent(reg, "m")
        _fake_agent(reg, "n")
        qid, err = reg.register_question("n", "which directory?")
        assert not err
        mine = wake_items("agent:n", agents=reg, with_schedule=False)
        assert mine == [(f"q:{qid}", f'your question "{qid}" to the user')]
        assert wake_items("agent:m", agents=reg, with_schedule=False) == []
        assert reg.question_port("n").open_questions() == [(qid, "which directory?")]
        assert reg.question_port("m").open_questions() == []
        assert WaitingOn(tuple(mine), ()).checkin_ids == frozenset()
        assert reg.next_checkin_s("agent:n", frozenset()) is None
    finally:
        reg.shutdown_all()


def test_an_answer_read_mid_run_is_an_envelope_not_the_users_words(where):
    """제자리 대기에서 깬 런이 답을 턴 경계에 흡수한다 — 답은 줄 없는 ``n-`` 봉투다. 사람의 말이 아니라 ``[user]: ``
    머리가 없고, 장부 항목이 되지 않는다(6b)."""
    g = _asks_then_waits(where, owes=False)
    assert g["after_answer"].startswith("[agent]\n── answer [n-")
    tail = g["after_answer"].split("── end of message", 1)[-1]
    assert "## Open Items" not in tail


# ── main 이 묻는다 (6b) ───────────────────────────────────────


def _main_asks_then_waits(root, *, close: bool) -> dict:
    got: dict = {}

    def waits(m):
        got["after_ask"] = _content(m[-1])
        return WAIT_CALL

    def reads(m):
        got["read"] = _content(m[-1])
        return complete_all("main done")(m)

    p = Provider({"MAIN": [call("ask", question=Q), waits, reads, reads]})
    s = Session(root, p, path="web")
    s.say("MAIN go", nickname="dj")
    t = s.pump_in_thread()
    try:
        until(lambda: s.reg._post.is_waiting("main"), "main waits in place")
        (q,) = s.reg.open_human_questions()
        got["qid"], got["asker"] = q.id, q.asker
        err = (
            s.reg.close_question(q.id, by="user:dj")
            if close
            else s.reg.answer_question(q.id, ANSWER, by="user:dj")
        )
        assert not err
        until(lambda: "read" in got, "main read it")
        until(s.main_idle, "main idle")
        got["open_after"] = [x.id for x in s.reg.open_human_questions()]
        got["runs"] = runs_ended(s)
    finally:
        s.close()
        t.join(20)
    return got


def test_main_asks_waits_and_the_answer_wakes_it(where):
    g = _main_asks_then_waits(where, close=False)
    assert g["asker"] == "main"
    obs, tail = g["after_ask"].split("── end of message", 1)
    assert (
        obs.strip().split("\n\n")[0]
        == f'[ask]\nObservation: question "{g["qid"]}" sent — "{Q}"'
    )
    assert f'{OPEN_QUESTIONS_HEAD}\n- "{g["qid"]}" · "{Q}"\n' in tail
    assert g["read"].startswith("[ask]\n── answer [n-")
    assert f"re [{g['qid']}] ──\nYour question: {Q}\nAnswer: {ANSWER}" in g["read"]
    assert g["open_after"] == [] and g["runs"] == 1


def test_main_asks_waits_and_the_close_wakes_it(where):
    g = _main_asks_then_waits(where, close=True)
    assert g["read"].startswith("[ask]\n── closed [n-")
    assert (
        "(The user closed it without an answer. Go on with what you have, and say what you assumed.)"
        in g["read"]
    )
    assert g["open_after"] == [] and g["runs"] == 1
