"""배달의 대화 줄은 ``conversation.jsonl`` 에 그 배달이 상자에 드는 락 구간 안에서 적힌다 (N11).

종전엔 상자에 넣은 **뒤에** 적었다 — 넣자마자 받는 쪽이 꺼내 런을 돌고 답을 적으면, 답이 그 요청보다 위에 적혔다
(resume 재생이 뒤집힌다; 오라클 s06_human·s14 가 부하에서 가끔 잡았다). 순서는 조건변수의 ``__exit__`` 훅
(`HookedCondition`)으로 강제한다: 봉투가 상자에 든 구간을 나온 **직후**, 부른 스레드가 다음 줄로 가기 **전**에 받는 쪽의
동작을 끼운다. 시간을 재지 않는다.

창 이벤트(`agent_message`)는 구간 **뒤**에 나간다(사용자 결정 전의 (나′) — 락을 쥔 채 출력하지 않는다). 그래서 같은
장면의 라이브 순서는 지금도 뒤집힐 수 있다 — `test_live_order` 가 그 사실을 strict xfail 로 적는다.
"""

from __future__ import annotations

import json
import threading

import pytest

import agent_cli.render as render_mod
from agent_cli.post.office import PostOffice
from agent_cli.subagent.agents_live import AgentRegistry
from tests.test_agents_live import RecordingRenderer, _FakeLoopResult
from tests.test_engine_p2 import wait_until
from tests.test_mail_notice_at_delivery import HookedCondition


class LandingOffice(PostOffice):
    """상자에 든 봉투(``dst``)와 열린 질문을 적어 둔다 — 훅이 "방금 들었다" 를 안다."""

    def __init__(self, cv):
        super().__init__(cv=cv)
        self.landed: list[str] = []

    def _deliver(self, env):
        super()._deliver(env)
        self.landed.append(env.dst)

    def ask(self, qid, asker, target, text, *, meta=None):
        row = super().ask(qid, asker, target, text, meta=meta)
        self.landed.append(f"question:{asker}")
        return row


class World:
    def __init__(self, reg, cv, office, door, renderer):
        self.reg, self.cv, self.office, self.door, self.renderer = (
            reg,
            cv,
            office,
            door,
            renderer,
        )

    def spawn(self, name):
        key, _ = self.reg.spawn(name=name)
        assert wait_until(lambda: self.reg.get(key).state == "idle")
        return key

    def once_landed(self, dst, action):
        """이 스레드가 지금부터 ``dst`` 에 봉투를 넣은(또는 질문을 연) 구간을 나온 직후 한 번 ``action``."""
        me, since = threading.get_ident(), len(self.office.landed)
        self.fired: list[bool] = []

        def hook():
            if self.fired or threading.get_ident() != me:
                return
            if dst not in self.office.landed[since:]:
                return
            self.fired.append(True)
            action()

        self.cv.after_exit = hook

    def fired_once(self) -> bool:
        return getattr(self, "fired", []) == [True]

    def lines(self, key) -> list[dict]:
        path = self.reg.get(key).home_dir / "conversation.jsonl"
        if not path.exists():
            return []
        return [json.loads(s) for s in path.read_text(encoding="utf-8").splitlines()]

    def directions(self, key) -> list[str]:
        return [r["direction"] for r in self.lines(key)]

    def live(self, key) -> list[tuple[str, str]]:
        """그 창의 라이브 이벤트 ``(direction, text)`` — 파일과 같은 순서여야 한다."""
        with self.renderer.lock:
            calls = list(self.renderer.calls)
        return [
            (kw["direction"], kw["text"])
            for name, kw in calls
            if name == "agent_message" and kw["key"] == key
        ]


@pytest.fixture
def world(tmp_path, monkeypatch):
    renderer = RecordingRenderer()
    monkeypatch.setattr(render_mod, "get_renderer", lambda: renderer)
    cv = HookedCondition()
    office = LandingOffice(cv)
    door = threading.Event()
    door.set()  # 열린 문 — 런이 곧바로 끝난다. 닫으면 런이 그 자리에서 기다린다

    def runner(kind, start, ctx):
        door.wait(10)
        return _FakeLoopResult(output="hi dj"), 0.01

    reg = AgentRegistry(
        tmp_path, runtime={"model": "m", "timeout": 5}, runner=runner, post=office
    )
    try:
        yield World(reg, cv, office, door, renderer)
    finally:
        cv.after_exit = None
        door.set()
        reg.shutdown_all()


def _request(world):
    """사람의 창 입력 — 워커가 넣자마자 꺼내 런을 끝내고 ``out`` 을 낸다(s06_human 의 뒤집힘)."""
    key = world.spawn("helper")
    world.once_landed(
        f"agent:{key}",
        lambda: wait_until(lambda: "out" in [d for d, _ in world.live(key)]),
    )
    assert world.reg.request(key, "hi there", author="user:dj") == ""
    return key, ["in", "out"]


def _absorbed(world):
    """런 도중 흡수 — 넣자마자 턴 경계가 꺼내도 ``absorbed`` 는 그 항목의 ``in`` 아래(웹 머리 두 번 붙음의 원인)."""
    reg = world.reg
    key = world.spawn("helper")
    world.door.clear()
    assert reg.request(key, "long job", author="main") == ""
    assert wait_until(lambda: reg.get(key).state == "busy")
    tm = reg.get(key)
    world.once_landed(f"agent:{key}", lambda: reg.absorb_pending(tm))
    assert reg.request(key, "fyi", author="main", expects_reply=False) == ""
    return key, ["in", "in", "absorbed"]


def _peer_message(world):
    """에이전트 → 에이전트 `message` — 받는 쪽의 회신(폴백)이 보낸 쪽 창에 ``in`` 으로 먼저 오면 안 된다."""
    sender, receiver = world.spawn("alpha"), world.spawn("beta")
    world.once_landed(
        f"agent:{receiver}",
        lambda: wait_until(lambda: "in" in [d for d, _ in world.live(sender)]),
    )
    receipt, err = world.reg.message_handler(f"agent:{sender}")(
        receiver, "please check"
    )
    assert err == "" and receipt
    return sender, ["out", "in"]


def _reply_to_peer(world):
    """`_pay_debt` 의 에이전트 요청자 길 — 요청자가 회신을 받자마자 다시 `message` 해도 빚진 쪽 창에서 ``out`` 이 위."""
    reg = world.reg
    asker, debtor = world.spawn("alpha"), world.spawn("beta")
    world.door.clear()  # 빚진 쪽의 런이 그 자리에서 기다린다 — 그 런 안에서 갚는다
    _, err = reg.message_handler(f"agent:{asker}")(debtor, "what is 2+2?")
    assert err == ""
    assert wait_until(lambda: reg.get(debtor).state == "busy")
    (d,) = reg.reply_debts(f"agent:{debtor}")
    world.once_landed(
        f"agent:{asker}",
        lambda: reg.message_handler(f"agent:{asker}")(debtor, "thanks, one more"),
    )
    assert reg.pay_debt(f"agent:{debtor}", d, "4") == ""
    return debtor, ["in", "out", "in"]


def _message_to_main(world):
    """에이전트 → main — main 의 답(``in``)이 보낸 쪽의 ``out`` 보다 위에 오면 안 된다."""
    key = world.spawn("helper")
    world.once_landed(
        "main",
        lambda: world.reg.request(
            key, "noted", author="main", expects_reply=False, kind="reply"
        ),
    )
    world.reg.message_to_main(key, "status: half done", expects_reply=True)
    return key, ["out", "in"]


def _stop_reply_to_main(world):
    """`_send_stop_reply` 의 main 길 — main 이 정지 회신을 읽자마자 새 요청을 보내도 멈춘 쪽 창에서 ``out`` 이 위."""
    reg = world.reg
    key = world.spawn("helper")
    world.door.clear()
    assert reg.request(key, "do the job", author="main") == ""
    assert wait_until(lambda: reg.get(key).state == "busy")
    world.once_landed("main", lambda: reg.request(key, "try again", author="main"))
    reg.answer_debts_as_stopped(reg.get(key), activity=[], notes="")
    return key, ["in", "out", "in"]


def _question(world):
    """사람에게 묻기 — 트레이에 뜨자마자 온 답(``in``)이 질문 줄보다 위에 오면 안 된다."""
    reg = world.reg
    key = world.spawn("helper")

    def answer():
        (q,) = reg.open_human_questions()
        assert reg.answer_question(q.id, "yes", by="user") == ""

    world.once_landed(f"question:agent:{key}", answer)
    qid, err = reg.register_question(key, "may I delete the cache?")
    assert err == "" and qid
    return key, ["question", "in"]


SCENES = {
    f.__name__.lstrip("_"): f
    for f in (
        _request,
        _absorbed,
        _peer_message,
        _reply_to_peer,
        _message_to_main,
        _stop_reply_to_main,
        _question,
    )
}


@pytest.mark.parametrize("scene", list(SCENES))
def test_file_order(world, scene):
    """재생 소스 — 원인 줄(요청·질문·보낸 쪽 out)이 그것이 부른 줄보다 언제나 위."""
    key, expected = SCENES[scene](world)
    assert world.fired_once()
    assert wait_until(lambda: len(world.directions(key)) >= len(expected))
    assert world.directions(key)[: len(expected)] == expected


@pytest.mark.xfail(
    strict=True,
    reason="N11 라이브 몫 — 창 이벤트는 배달 구간 뒤에 나가 경주가 남는다(고치지 않았다, (나′))",
)
@pytest.mark.parametrize("scene", list(SCENES))
def test_live_order(world, scene):
    """창 이벤트 — 같은 장면에서 라이브 순서는 지금도 뒤집힌다(파일과 다르다)."""
    key, expected = SCENES[scene](world)
    assert world.fired_once()
    live = [d for d, _ in world.live(key)]
    assert live[: len(expected)] == expected


def test_a_refused_delivery_leaves_no_line(world):
    """두 뷰어가 같은 질문에 동시에 답한다 — 둘째의 줄 확인과 넣기 사이에 첫째가 답을 마친다. 둘째의 넣기는 거절되고
    (`RowError` → "already-answered"), 창에도 파일에도 둘째의 줄이 없다."""
    reg = world.reg
    key = world.spawn("helper")
    qid, _ = reg.register_question(key, "which branch?")
    me = threading.get_ident()
    first: list[str] = []

    def hook():  # 둘째가 줄을 읽고 구간을 나온 직후 — 첫째가 끝까지 답한다
        if not first and threading.get_ident() == me:
            first.append(reg.answer_question(qid, "main", by="user:a"))

    world.cv.after_exit = hook
    second = reg.answer_question(qid, "dev", by="user:b")
    world.cv.after_exit = None
    assert first == [""]
    assert "already-answered" in second
    answers = [t for d, t in world.live(key) if d == "in"]
    assert answers == ["Your question: which branch?\nAnswer: main"]
    assert [r["text"] for r in world.lines(key) if r["direction"] == "in"] == answers
