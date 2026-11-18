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
from agent_cli.constants import has_words
from agent_cli.session_slots import (
    set_job_registry,
    set_monitor_registry,
    set_schedule_registry,
)
from agent_cli.subagent.question_port import (
    _not_delivered,
    judge_reply,
    reply_refusal,
)
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

    set_job_registry(None)
    set_monitor_registry(None)
    set_schedule_registry(None)
    reg = make_registry(tmp_path, runner=runner)
    keys = [reg.spawn(name=f"s{i}")[0] for i in range(3)]
    assert wait_until(lambda: all(reg.get(k).state == "idle" for k in keys))
    yield reg, keys
    door.set()
    reg.shutdown_all()


def _addrs(keys):
    return ["main", *(f"agent:{k}" for k in keys), "user", "harness:x"]


def _old_walk(reg, owner, _seen=frozenset()):
    """옛 `wake._wake_items` 의 에이전트 몫(v10.145.2) 그대로 — 작업·모니터·예약 슬롯은 이 테스트에서 비어 있다."""
    out = list(reg.pending_agents_for(owner, exclude=_seen))
    seen = _seen | {owner}
    for debtor in reg.parked_debtors_for(owner):
        if debtor in seen:
            continue
        if _old_walk(reg, debtor, seen):
            out.append((debtor, f"{debtor} owes you a reply (it is waiting on others)"))
    return out


def _old_wake(reg, addr, exclude):
    return (
        reg._agent_relations(addr, exclude),
        reg.pending_agents_for(addr, exclude),
        reg.parked_debtors_for(addr),
        _old_walk(reg, addr, exclude),
    )


def _new_wake(facts, addr, exclude, reg):
    return (
        wake.agent_relations(facts, addr, exclude),
        wake.relation_items(facts, addr, exclude),
        wake.parked_debtors(facts, addr),
        wake._wake_items(addr, exclude, agents=reg, facts=facts),
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
                assert _new_wake(facts, a, ex, reg) == old, (a, ex)
            return True
    return False  # 계속 움직였다 — 이 걸음은 비교하지 않는다


def _ids(reg):
    return [r.rid for r in reg._post.rows()]


# ── 옛 `QuestionPort.reply` (v10.145.2) 그대로 — 3-나 전환 뒤에도 대조를 잇는다. 거절만 낸다(갚지 않는다). ──


def _old_listing(owed) -> str:
    return "; ".join(
        f'[{d.id}] {d.to.split(":", 1)[-1]}: "{d.text[:60]}"' for d in owed
    )


def _old_main_reply(reg, me, text, id):
    owed = reg.reply_debts(me)
    if not owed:
        return (
            "nothing to reply to — no agent is waiting on a reply from you. "
            "To give an agent new work, use `message`."
        )
    listing = _old_listing(owed)
    id = str(id or "").strip()
    if not id:
        return (
            "reply needs `id` — which request is this the reply to? You owe: "
            f"{listing}. Send `reply` with that `id`."
        )
    d = next((x for x in owed if x.id == id), None)
    if d is None:
        return f'no unpaid request "{id}" — you owe: {listing}.'
    return PAY


PAY = object()


def _old_reply(reg, key, text, id=""):
    """옛 판정 — 거절 글, 또는 갚을 차례면 `PAY`."""
    me = "main" if key is None else f"agent:{key}"
    text = (text or "").strip()
    if not text:
        return "empty reply — nothing sent"
    if not has_words(text):
        return f"reply has no words ({text[:20]!r}) — nothing sent; say it in words"
    if key is None:
        return _old_main_reply(reg, me, text, id)
    tm = reg.get(key)
    if tm is None:
        return "unknown agent"
    owed = reg.reply_debts(me)
    if not owed:
        a = tm.current_author
        d = reg.reply_debt(me, a)
        if d is not None and d.human:
            return (
                "nothing to send — your requester is a person watching this "
                "window, and they see your `complete` here. Just `complete`."
            )
        if d is not None and d.settled and d.closed_by == "unsent":
            return _not_delivered(d)
        if d is not None and d.settled:
            return (
                f"already replied to {a} in this run — a second reply is not "
                "delivered. If you must correct it, send the correction with "
                f'`message` (`to` "{a.split(":", 1)[-1]}", `text` your correction).'
            )
        return (
            "nothing to reply to — this run was not started by a request "
            "that expects one (it was a delivered reply, a notice, or an "
            "answer). Do not acknowledge those. If you need something from "
            "someone, use `message` (`to` the recipient, `text` your message)."
        )
    listing = _old_listing(owed)
    id = str(id or "").strip()
    if not id:
        return (
            "reply needs `id` — which request is this the reply to? You owe: "
            f"{listing}. Send `reply` with that `id`."
        )
    d = next((x for x in owed if x.id == id), None)
    if d is None:
        prior = reg.debt_by_id(me, id)
        if prior is not None and prior.settled and prior.closed_by == "unsent":
            return _not_delivered(prior)
        if prior is not None and prior.settled:
            return (
                f"already replied to [{id}] ({prior.to}) in this run — a second "
                "reply is not delivered. If you must correct it, send the "
                f'correction with `message` (`to` "{prior.to.split(":", 1)[-1]}", '
                "`text` your correction)."
            )
        return f'no unpaid request "{id}" — you owe: {listing}.'
    return PAY


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
                old = _old_reply(reg, who, text, rid)
                if verdict == "pay":
                    assert old is PAY and d is not None and d.id == rid.strip()
                    continue
                new = reply_refusal(verdict, text.strip(), rid.strip(), facts)
                assert new == old == port.reply(text, id=rid), (who, text, rid, verdict)
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
