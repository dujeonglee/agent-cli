"""묻고 기다리는 에이전트 (6a) — 사람에게 `ask` 한 뒤 `wait` 하면 무엇이 일어나나, 그리고 질문이 열린 턴에 묻는 쪽이 읽는 글.

장면은 오라클 드라이버의 대본 공급자와 세션으로 돌린다(모델 호출 없음). 동기점은 제품이 읽는 값이다 — 창구의 기다림
표시(`is_waiting`), 에이전트 상태, 공급자의 호출 수. 시간 판정이 없다."""

from __future__ import annotations

import pytest

import agent_cli.render as render_mod
from agent_cli.prompts.system_prompt import _ASK_INLINE_RESIDENT
from agent_cli.tools.virtual import AskTool
from tests.oracle.driver import Provider, Session, call, complete_all, owed_id, until

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
    root, *, owes: bool, job: bool = False, mode: str = "spawn"
) -> dict:
    """에이전트 aa 가 일을 받아 (작업을 띄우고) `ask` 한 뒤 `wait` 한다. 판정이 난 뒤 사람이 트레이 길로 답한다.

    ``owes`` — main 이 회신을 기다리는 요청으로 맡겼다(aa 가 r- 를 빚진다). ``mode`` "run" 은 일회용 에이전트이고
    main 은 그 결과를 `wait` 로 기다린다. 돌려주는 것: aa 가 `wait` 뒤 읽은 관찰, `ask` 바로 다음 호출의 마지막
    메시지(관찰 + 꼬리), 작업 구간 수, 답을 읽었는가, main 이 읽은 것."""
    got: dict = {"aa_after_wait": None, "read_answer": False, "main_reads": []}

    def waits(m):
        got["after_ask"] = _content(m[-1])
        return WAIT_CALL

    def rest(m):
        if got["aa_after_wait"] is None:
            got["aa_after_wait"] = m
        if any("── answer [" in _content(x) for x in m):
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
    i = next(
        n
        for n, x in enumerate(m)
        if x.get("role") == "assistant" and '"action": "wait"' in _content(x)
    )
    got["wait_obs"] = _content(m[i + 1]).removeprefix("[wait]\n").split("\n\n", 1)[0]
    return got


# ── 묻고 `wait` 한 뒤 ─────────────────────────────────────────


def test_a_resident_holding_a_request_asks_then_waits(where):
    """빚(r-)을 든 상주가 묻고 `wait` — 답할 것을 받기 전에 회신부터 하라고 거절된다. 답은 회신 뒤에 와서 새 구간을 연다."""
    g = _asks_then_waits(where, owes=True)
    assert g["wait_obs"].startswith(
        "Observation: wait refused — nothing would wake you, and you still owe a `reply` [r-"
    )
    assert not g["waiting_in_place"]
    assert g["runs"] == 2


def test_a_resident_with_nothing_owed_asks_then_waits(where):
    """빚 없이 묻고 `wait` — 기다릴 것이 없다며 런이 끝나고, 답이 새 구간을 연다."""
    g = _asks_then_waits(where, owes=False)
    assert g["wait_obs"] == (
        "Observation: nothing to wait for: no job or monitor of yours is running, no agent owes you "
        "a reply, and you owe none. This run has ended."
    )
    assert g["runs"] == 2


def test_a_job_and_a_question_the_acceptance_names_only_the_job(where):
    """작업도 함께 기다리면 받아들여진다 — 수락 문장은 작업만 말한다. 답은 같은 구간에서 읽힌다."""
    g = _asks_then_waits(where, owes=False, job=True)
    assert g["wait_obs"] == (
        "Observation: waiting — will wake on: job #1 (`sleep 600`), or in 5m if nothing has reported."
    )
    assert g["runs"] == 1


def test_a_run_agent_asks_then_waits_while_main_waits_for_it(where):
    """일회용(`mode` "run") 에이전트가 묻고 `wait` — 결과(회신)를 빚졌으므로 상주와 같은 거절이다. 답 없이 회신하고
    내려오면 질문은 닫힌다 — main 은 답 없는 결과를 읽는다."""
    g = _asks_then_waits(where, owes=True, mode="run")
    assert g["wait_obs"].startswith(
        "Observation: wait refused — nothing would wake you, and you still owe a `reply` [r-"
    )
    assert g["qid"] is None and not g["read_answer"]
    assert any("done without the answer" in r for r in g["main_reads"])


# ── 질문이 열린 턴에 묻는 쪽이 읽는 글 ─────────────────────────


def test_the_turn_after_ask_says_where_to_go_in_the_observation(where):
    """`ask` 바로 다음 호출 — 관찰이 "할 게 없으면 `complete`" 로 보내고, 꼬리에는 질문이 없다."""
    g = _asks_then_waits(where, owes=False)
    after = g["after_ask"]
    assert (
        "If nothing else can proceed, `complete` — you will be resumed when it arrives."
        in after
    )
    assert "## Your open questions" not in after


def test_the_resident_description_and_guide_send_a_stuck_asker_to_complete():
    assert "If nothing else can proceed, `complete`." in AskTool.RESIDENT_DESCRIPTION
    assert (
        "When nothing else can proceed, `complete` and report what you did."
        in _ASK_INLINE_RESIDENT
    )
