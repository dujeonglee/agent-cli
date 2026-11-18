"""3단계 기준 보강 — `reply` 거절 문장의 전문과 판정이 갈리는 지금 동작 (v10.146.0 준비, 테스트만).

3단계(판정 하나)는 main 갈래와 에이전트 갈래의 `reply` 판정을 한 함수로 합치고, "나를 깨울 것" 조회의 재료를
장부·명단의 사본 하나로 받는다. 동작은 같아야 한다 — 그래서 지금 **접두로만** 고정된 문장을 전문으로 고정하고,
두 갈래가 우연히 다르게 말하는 입력(R-1·D-1·W-1)을 지금 동작 그대로 적는다.

표지 ``[3단계 기준 — 바뀌면 사용자 결정]`` 은 지금 글이 덜 알려 주거나 틀린 상대를 가리키는 칸이다 — 고치는 것은
3단계 뒤의 별도 수정(R-1·D-1)이나 5단계(W-1)의 "바뀌는 기대값" 이다. 여기서 바뀌면 판정을 합치다 동작이 움직인 것이다.
"""

from __future__ import annotations

import json
import threading

from tests.test_agents_live import _FakeLoopResult, make_registry
from tests.test_engine_p2 import (
    Provider,
    call,
    contents,
    owed_id,
    session,  # noqa: F401 — 픽스처
    wait_until,
)
from tests.test_wait_checkin import FakeTimer

MAIN_NOTHING_OWED = (
    "nothing to reply to — no agent is waiting on a reply from you. "
    "To give an agent new work, use `message`."
)
AGENT_NOTHING_OWED = (
    "nothing to reply to — this run was not started by a request "
    "that expects one (it was a delivered reply, a notice, or an "
    "answer). Do not acknowledge those. If you need something from "
    "someone, use `message` (`to` the recipient, `text` your message)."
)
HUMAN_REQUESTER = (
    "nothing to send — your requester is a person watching this "
    "window, and they see your `complete` here. Just `complete`."
)


def _needs_id(listing: str) -> str:
    return (
        "reply needs `id` — which request is this the reply to? You owe: "
        f"{listing}. Send `reply` with that `id`."
    )


def _no_unpaid(rid: str, listing: str) -> str:
    return f'no unpaid request "{rid}" — you owe: {listing}.'


def _already_replied_to(who: str) -> str:
    key = who.split(":", 1)[-1]
    return (
        f"already replied to {who} in this run — a second reply is not "
        "delivered. If you must correct it, send the correction with "
        f'`message` (`to` "{key}", `text` your correction).'
    )


def _already_replied_id(rid: str, to: str) -> str:
    key = to.split(":", 1)[-1]
    return (
        f"already replied to [{rid}] ({to}) in this run — a second "
        "reply is not delivered. If you must correct it, send the "
        f'correction with `message` (`to` "{key}", '
        "`text` your correction)."
    )


def _gone(key: str, rid: str) -> str:
    return (
        f'not delivered — agent "{key}", which sent request [{rid}], was already gone. '
        f"[{rid}] is closed; there is nothing more to do for it."
    )


def _agents(tmp_path, n=2):
    """런이 문에서 막히는 상주 에이전트들 — 빚은 테스트가 직접 적는다(`note_owes`)."""
    door = threading.Event()

    def runner(kind, start, ctx):
        door.wait()
        return _FakeLoopResult(output="ok"), 0.01

    reg = make_registry(tmp_path, runner=runner)
    keys = [reg.spawn(name=f"n{i}")[0] for i in range(n)]
    assert wait_until(lambda: all(reg.get(k).state == "idle" for k in keys))
    return reg, keys, door


# ── main 의 `reply` ──────────────────────────────────────────


class TestMainReplySentences:
    def test_nothing_owed(self, tmp_path):
        reg, _keys, door = _agents(tmp_path)
        try:
            assert reg.question_port(None).reply("x", id="r-1") == MAIN_NOTHING_OWED
            assert reg.question_port(None).reply("x") == MAIN_NOTHING_OWED
        finally:
            door.set()
            reg.shutdown_all()

    def test_needs_id_and_unknown_id(self, tmp_path):
        reg, (a, _b), door = _agents(tmp_path)
        try:
            d = reg.note_owes("main", f"agent:{a}", "pick one")
            port = reg.question_port(None)
            listing = f'[{d.id}] {a}: "pick one"'
            assert port.reply("x") == _needs_id(listing)
            assert port.reply("x", id="r-999") == _no_unpaid("r-999", listing)
        finally:
            door.set()
            reg.shutdown_all()

    def test_replying_again_by_id_says_already_replied(self, tmp_path):
        """R-1 (1) — 에이전트와 같은 "already replied to [id]" (사용자 결정 2026-11-18)."""
        reg, (a, _b), door = _agents(tmp_path)
        try:
            d1 = reg.note_owes("main", f"agent:{a}", "first")
            d2 = reg.note_owes("main", f"agent:{a}", "second")
            port = reg.question_port(None)
            assert port.reply("answer", id=d1.id) == ""
            assert port.reply("again", id=d1.id) == _already_replied_id(
                d1.id, f"agent:{a}"
            )
            assert port.reply("done", id=d2.id) == ""
            assert port.reply("again", id=d2.id) == _already_replied_id(
                d2.id, f"agent:{a}"
            )
        finally:
            door.set()
            reg.shutdown_all()

    def test_replying_again_to_a_gone_requester_while_owing_another(self, tmp_path):
        """R-1 (2) — 에이전트와 같이 "not delivered" 를 다시 읽는다 (사용자 결정 2026-11-18)."""
        reg, (a, b), door = _agents(tmp_path)
        try:
            d = reg.note_owes("main", f"agent:{a}", "late one")
            other = reg.note_owes("main", f"agent:{b}", "keep open")
            assert reg.kill(a) == ""
            assert wait_until(lambda: reg.get(a).state == "dead")
            port = reg.question_port(None)
            assert port.reply("late", id=d.id) == _gone(a, d.id)
            assert port.reply("late again", id=d.id) == _gone(a, d.id)
            assert port.reply("x", id="r-999") == _no_unpaid(
                "r-999", f'[{other.id}] {b}: "keep open"'
            )
        finally:
            door.set()
            reg.shutdown_all()

    def test_replying_again_to_a_gone_requester_with_nothing_left(self, tmp_path):
        """R-1 (2) — 빚이 하나도 안 남아도 그 id 의 줄로 "not delivered" (사용자 결정 2026-11-18). 다른 id 는
        "nothing to reply to"."""
        reg, (a, _b), door = _agents(tmp_path)
        try:
            d = reg.note_owes("main", f"agent:{a}", "late one")
            assert reg.kill(a) == ""
            assert wait_until(lambda: reg.get(a).state == "dead")
            port = reg.question_port(None)
            assert port.reply("late", id=d.id) == _gone(a, d.id)
            assert port.reply("late again", id=d.id) == _gone(a, d.id)
            assert port.reply("x", id="r-999") == MAIN_NOTHING_OWED
        finally:
            door.set()
            reg.shutdown_all()


# ── 에이전트의 `reply` ───────────────────────────────────────


class TestAgentReplySentences:
    def test_unknown_self(self, tmp_path):
        reg, _keys, door = _agents(tmp_path)
        try:
            assert reg.question_port("nobody").reply("x", id="r-1") == "unknown agent"
        finally:
            door.set()
            reg.shutdown_all()

    def test_nothing_owed_by_a_run_not_started_by_a_request(self, tmp_path):
        reg, (_a, b), door = _agents(tmp_path)
        try:
            assert reg.question_port(b).reply("x", id="r-1") == AGENT_NOTHING_OWED
        finally:
            door.set()
            reg.shutdown_all()

    def test_human_requester(self, tmp_path):
        reg, (_a, b), door = _agents(tmp_path)
        try:
            me = f"agent:{b}"
            reg.note_owes(me, "user", "do it")
            reg.get(b).current_author = "user"
            assert reg.question_port(b).reply("x") == HUMAN_REQUESTER
        finally:
            door.set()
            reg.shutdown_all()

    def test_already_replied_to_the_opener(self, tmp_path):
        reg, (_a, b), door = _agents(tmp_path)
        try:
            me = f"agent:{b}"
            d = reg.note_owes(me, "main", "the task")
            reg.get(b).current_author = "main"
            port = reg.question_port(b)
            assert port.reply("done", id=d.id) == ""
            assert port.reply("done again") == _already_replied_to("main")
        finally:
            door.set()
            reg.shutdown_all()

    def test_already_replied_by_id_and_needs_id(self, tmp_path):
        reg, (a, b), door = _agents(tmp_path)
        try:
            me, them = f"agent:{b}", f"agent:{a}"
            d1 = reg.note_owes(me, them, "first")
            d2 = reg.note_owes(me, them, "second")
            port = reg.question_port(b)
            listing = f'[{d1.id}] {a}: "first"; [{d2.id}] {a}: "second"'
            assert port.reply("x") == _needs_id(listing)
            assert port.reply("one", id=d1.id) == ""
            assert port.reply("again", id=d1.id) == _already_replied_id(d1.id, them)
        finally:
            door.set()
            reg.shutdown_all()

    def test_replying_again_names_the_id_not_the_opener(self, tmp_path):
        """D-1 — 빚이 하나도 안 남아도 `id` 가 이 런에서 닫힌 줄이면 그 줄로 말한다(사용자 결정 2026-11-18): a 의 r 에
        다시 답하면 a 에게 정정하라고 안내한다. id 없이 다시 답하면 런을 연 주소(main)로."""
        reg, (a, b), door = _agents(tmp_path)
        try:
            me = f"agent:{b}"
            opened = reg.note_owes(me, "main", "the task")
            absorbed = reg.note_owes(me, f"agent:{a}", "a side ask")
            reg.get(b).current_author = "main"
            port = reg.question_port(b)
            assert port.reply("task done", id=opened.id) == ""
            assert port.reply("side answer", id=absorbed.id) == ""
            assert port.reply("side again", id=absorbed.id) == _already_replied_id(
                absorbed.id, f"agent:{a}"
            )
            assert port.reply("task again", id=opened.id) == _already_replied_id(
                opened.id, "main"
            )
            assert port.reply("again") == _already_replied_to("main")
        finally:
            door.set()
            reg.shutdown_all()

    def test_replying_again_in_a_human_opened_run_names_the_id(self, tmp_path):
        """D-1 — 사람이 연 런에서 흡수한 요청에 다시 답하면 그 줄로 "already replied to [id]" (사용자 결정 2026-11-18).
        id 없이 다시 답하면 "requester is a person"."""
        reg, (a, b), door = _agents(tmp_path)
        try:
            me = f"agent:{b}"
            reg.note_owes(me, "user", "do it")
            absorbed = reg.note_owes(me, f"agent:{a}", "a side ask")
            reg.get(b).current_author = "user"
            port = reg.question_port(b)
            assert port.reply("side answer", id=absorbed.id) == ""
            assert port.reply("side again", id=absorbed.id) == _already_replied_id(
                absorbed.id, f"agent:{a}"
            )
            assert port.reply("again") == HUMAN_REQUESTER
        finally:
            door.set()
            reg.shutdown_all()


# ── W-1: main 이 제자리에서 기다리는 고리 ─────────────────────


def _resident_history(reg, key) -> list[str]:
    hist = reg.session_dir / "agents" / key / "history.jsonl"
    return [
        str(json.loads(ln).get("content") or "")
        for ln in hist.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]


class TestMainWaitsFirstInACycle:
    """W-1 [3단계 기준 — 바뀌면 사용자 결정, 5단계] — main 과 상주가 서로 빚진 채 **main 이 먼저** 제자리에서
    기다린다. 상주의 `wait` 는 "main owes you a reply" 로 받아들여지고(main 은 잠든 채무자로 세지 않는다),
    main 은 고리 문장이 아니라 "nothing you were waiting on is running any more." 로 깬다. 반대 순서(상주가 먼저)는
    `tests/test_baseline_wait_sentences.py::TestMainRefuses` 가 고리 문장으로 고정한다."""

    def test_main_first(self, session):  # noqa: F811
        holder: dict = {}

        def main_timers() -> int:
            reg = holder["reg"]
            return sum(
                1
                for t in FakeTimer.made
                if t.started
                and t.args == ("main",)
                and getattr(t.fn, "__self__", None) is reg
            )

        def asks(msgs):
            assert wait_until(lambda: main_timers() >= 1)  # main 의 첫 제자리 `wait`
            return call("message", to="main", text="which file?")

        def waits_after_main(msgs):
            assert wait_until(
                lambda: main_timers() >= 2
            )  # main 의 둘째 `wait` 가 받아들여져 막혔다
            return call("wait")

        p = Provider(
            {
                "MAIN": [
                    call("agent", mode="spawn", name="helper", task="TASK-W1 do it"),
                    call("wait"),
                    call("wait"),
                    lambda m: call("reply", id=owed_id(m), text="use a.py"),
                    call("complete", result="done", answers=[]),
                ],
                "TASK-W1": [
                    asks,
                    waits_after_main,
                    call("complete", result="ok"),
                ],
            }
        )
        s = session(p)
        holder["reg"] = s["reg"]
        s["reg"].checkin_timer_factory = FakeTimer
        s["run"]()
        main_text = contents(s["ctx"])
        (key,) = [r["key"] for r in s["reg"].roster_snapshot()]
        main_lines = [
            ln
            for ln in main_text.splitlines()
            if ln.startswith(
                (
                    "Observation: waiting —",
                    "Observation: wait refused —",
                    "Observation: nothing you were waiting on",
                )
            )
        ]
        accepted = (
            f"Observation: waiting — will wake on: agent:{key} working on your request, "
            "or in 5m if nothing has reported."
        )
        assert main_lines == [
            accepted,  # 첫 `wait` — 상주의 물음에 깬다
            accepted,  # 둘째 `wait` — 상주가 아직 일한다(그 뒤 잠든다)
            "Observation: nothing you were waiting on is running any more.",
        ]
        # 상주가 런을 다 끝낸 뒤에 그 기록을 읽는다 — 마지막 줄을 쓰는 도중에 읽으면 반쪽 JSON 이다(컨테이너 1회)
        assert wait_until(lambda: s["reg"].get(key).state == "idle")
        helper = "\n".join(_resident_history(s["reg"], key))
        assert (
            "Observation: waiting — will wake on: main owes you a reply, or in 5m if "
            "nothing has reported." in helper
        )
