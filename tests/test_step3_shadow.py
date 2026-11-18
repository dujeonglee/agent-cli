"""3단계 그림자 — 옛 판정(레지스트리의 private 메서드·`QuestionPort` 의 두 갈래)과 새 판정(`wake` 의 순수 함수·
`question_port.judge_reply`)이 같은 상태에서 같은 답을 내는가. **임시** — 3-다에서 옛 판정과 함께 지운다.

상태는 공개 API(`note_owes`·`request`·`kill`·`question_port().reply`)와 상태 칸(`state`·`current_author`)으로 만든다.
행동열은 씨앗을 고정한 난수로 뽑는다(결정적). 워커 스레드가 움직이는 순간은 옛 판정을 앞뒤로 두 번 읽어 같을 때만
비교한다 — 시간으로 기다리지 않는다.
"""

from __future__ import annotations

import random
import threading

import pytest

from agent_cli import wake
from agent_cli.subagent.question_port import judge_reply, reply_refusal
from tests.test_agents_live import _FakeLoopResult, make_registry
from tests.test_engine_p2 import wait_until

SEEDS = range(40)
STEPS = 25
EXCLUDES = [frozenset(), frozenset({"main"})]


@pytest.fixture
def world(tmp_path):
    door = threading.Event()

    def runner(kind, start, ctx):
        door.wait()
        return _FakeLoopResult(output="ok"), 0.01

    reg = make_registry(tmp_path, runner=runner)
    keys = [reg.spawn(name=f"s{i}")[0] for i in range(3)]
    assert wait_until(lambda: all(reg.get(k).state == "idle" for k in keys))
    yield reg, keys
    door.set()
    reg.shutdown_all()


def _addrs(keys):
    return ["main", *(f"agent:{k}" for k in keys), "user", "harness:x"]


def _old_wake(reg, addr, exclude):
    return (
        reg._agent_relations(addr, exclude),
        reg.pending_agents_for(addr, exclude),
        reg.parked_debtors_for(addr),
    )


def _new_wake(facts, addr, exclude):
    return (
        wake.agent_relations(facts, addr, exclude),
        wake.relation_items(facts, addr, exclude),
        wake.parked_debtors(facts, addr),
    )


def _compare_wake(reg, keys, extra_excludes):
    for _ in range(50):
        before = {
            (a, ex): _old_wake(reg, a, ex)
            for a in _addrs(keys)
            for ex in extra_excludes
        }
        facts = reg.wake_facts()
        after = {
            (a, ex): _old_wake(reg, a, ex)
            for a in _addrs(keys)
            for ex in extra_excludes
        }
        if before == after:
            for (a, ex), old in before.items():
                assert _new_wake(facts, a, ex) == old, (a, ex)
            return True
    return False  # 계속 움직였다 — 이 걸음은 비교하지 않는다


def _ids(reg):
    return [r.rid for r in reg._post.rows()]


def _compare_reply(reg, keys):
    """거절이면 옛 포트가 같은 글을 내는가 — 거절은 아무것도 바꾸지 않으므로 옛 포트를 불러도 된다."""
    seen = 0
    for who in [None, *keys, "nobody"]:
        port = reg.question_port(who)
        me = port.me
        for text in ["ok", "", " !! "]:
            for rid in ["", "r-999", "u-0", *_ids(reg)]:
                facts = reg.reply_facts(me, rid.strip())
                verdict, d = judge_reply(text.strip(), rid.strip(), facts)
                if verdict == "pay":
                    assert d is not None and d.id == rid.strip()
                    continue
                assert reply_refusal(verdict, text.strip(), rid.strip(), facts) == (
                    port.reply(text, id=rid)
                ), (who, text, rid, verdict)
                seen += 1
    return seen


def _act(rng, reg, keys):
    me = rng.choice(["main", *(f"agent:{k}" for k in keys)])
    other = rng.choice(["main", "user", *(f"agent:{k}" for k in keys)])
    k = rng.choice(keys)
    roll = rng.random()
    if roll < 0.25 and other != me:
        reg.note_owes(me, other, f"t{rng.randrange(99)}")
    elif roll < 0.45:
        reg.request(
            k,
            f"q{rng.randrange(99)}",
            author=rng.choice(["main", *(f"agent:{x}" for x in keys if x != k)]),
            expects_reply=rng.random() < 0.7,
        )
    elif roll < 0.65:
        owed = reg.reply_debts(me)
        if owed:
            d = rng.choice(owed)
            port = reg.question_port(None if me == "main" else me.split(":", 1)[1])
            facts = reg.reply_facts(me, d.id)
            assert judge_reply("paid", d.id, facts)[0] == "pay"
            port.reply("paid", id=d.id)
    elif roll < 0.8:
        tm = reg.get(k)
        if tm.state != "dead":
            tm.state = rng.choice(["idle", "busy"])
    elif roll < 0.92:
        reg.get(k).current_author = rng.choice(
            ["main", "user", "", *(f"agent:{x}" for x in keys)]
        )
    elif roll < 0.95 and sum(reg.get(x).state != "dead" for x in keys) > 1:
        # 일하던 것은 문이 열릴 때까지 dead 로 안 찍힌다 — 멈추는 중도 한 상태다
        reg.kill(k)


@pytest.mark.parametrize("seed", SEEDS)
def test_old_and_new_judgements_agree(world, seed):
    reg, keys = world
    rng = random.Random(seed)
    compared = replies = 0
    excludes = [*EXCLUDES, frozenset({f"agent:{keys[0]}"})]
    for _ in range(STEPS):
        _act(rng, reg, keys)
        compared += _compare_wake(reg, keys, excludes)
        replies += _compare_reply(reg, keys)
    assert compared > STEPS // 2
    assert replies > 0


def test_human_opener_agrees(world):
    """난수 행동열이 드물게 닿는 칸 — 사람이 연 런(u 줄만, 갚을 r 없음)."""
    reg, keys = world
    me = f"agent:{keys[1]}"
    reg.note_owes(me, "user", "do it")
    reg.get(keys[1]).current_author = "user"
    facts = reg.reply_facts(me, "")
    assert judge_reply("ok", "", facts)[0] == "none_owed_human"
    assert _compare_reply(reg, keys) > 0
