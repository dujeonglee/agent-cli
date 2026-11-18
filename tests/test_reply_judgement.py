"""`reply` 판정 하나 — `judge_reply` 와 거절 글 `reply_refusal` 을 손으로 만든 사본(`ReplyFacts`)으로 (v10.146.0).

main 과 에이전트가 갈리는 것은 ``opener``(레지스트리가 이 런을 연 주소를 아는가) 하나다. 글은 전문으로 본다.
``id`` 가 이 런에서 이미 닫힌 내 줄이면 누구든 그 줄로 말한다(빚이 남았든, 런을 누가 열었든).
"""

from __future__ import annotations

import pytest

from agent_cli.subagent.question_port import (
    ReplyDebt,
    ReplyFacts,
    judge_reply,
    reply_refusal,
)

R1 = ReplyDebt(to="agent:a", text="pick one", id="r-1")
R2 = ReplyDebt(to="main", text="the task", id="r-2")
PAID = ReplyDebt(to="agent:a", text="old", id="r-3", settled=True, closed_by="reply")
UNSENT = ReplyDebt(to="agent:a", text="old", id="r-4", settled=True, closed_by="unsent")
HUMAN = ReplyDebt(to="user", text="do it", id="u-5")
PAID_MAIN = ReplyDebt(
    to="main", text="the task", id="r-6", settled=True, closed_by="reply"
)


def facts(*, owed=(), opener=None, opener_row=None, by_id=None, known=True):
    return ReplyFacts(known, tuple(owed), opener, opener_row, by_id)


GONE_R4 = (
    'not delivered — agent "a", which sent request [r-4], was already gone. '
    "[r-4] is closed; there is nothing more to do for it."
)

ALREADY_R3 = (
    "already replied to [r-3] (agent:a) in this run — a second reply is not "
    "delivered. If you must correct it, send the correction with `message` (`to` "
    '"a", `text` your correction).'
)

CASES = [
    # (이름, text, id, 사본, 판정, 글)
    ("빈 글", "", "r-1", facts(owed=[R1]), "empty", "empty reply — nothing sent"),
    (
        "낱말 없음",
        "!!",
        "r-1",
        facts(owed=[R1]),
        "no_words",
        "reply has no words ('!!') — nothing sent; say it in words",
    ),
    (
        "주체 없음",
        "ok",
        "",
        facts(known=False, opener=None),
        "unknown_self",
        "unknown agent",
    ),
    (
        "main — 빚 없음",
        "ok",
        "r-1",
        facts(),
        "none_owed",
        (
            "nothing to reply to — no agent is waiting on a reply from you. "
            "To give an agent new work, use `message`."
        ),
    ),
    (
        "에이전트 — 빚 없음, 런을 연 요청이 없음",
        "ok",
        "",
        facts(opener="main"),
        "none_owed",
        (
            "nothing to reply to — this run was not started by a request that expects "
            "one (it was a delivered reply, a notice, or an answer). Do not acknowledge "
            "those. If you need something from someone, use `message` (`to` the "
            "recipient, `text` your message)."
        ),
    ),
    (
        "에이전트 — 사람이 연 런",
        "ok",
        "",
        facts(opener="user", opener_row=HUMAN),
        "none_owed_human",
        (
            "nothing to send — your requester is a person watching this window, and "
            "they see your `complete` here. Just `complete`."
        ),
    ),
    (
        "에이전트 — 런을 연 요청에 이미 답함",
        "ok",
        "",
        facts(opener="main", opener_row=PAID_MAIN),
        "none_owed_replied",
        (
            "already replied to main in this run — a second reply is not delivered. If "
            'you must correct it, send the correction with `message` (`to` "main", '
            "`text` your correction)."
        ),
    ),
    (
        "에이전트 — 런을 연 요청자에게 가지 못함",
        "ok",
        "",
        facts(opener="agent:a", opener_row=UNSENT),
        "none_owed_unsent",
        GONE_R4,
    ),
    (
        "id 없음 — main·에이전트 같은 글",
        "ok",
        "",
        facts(owed=[R1, R2]),
        "needs_id",
        (
            "reply needs `id` — which request is this the reply to? You owe: [r-1] a: "
            '"pick one"; [r-2] main: "the task". Send `reply` with that `id`.'
        ),
    ),
    (
        "빚에 없는 id",
        "ok",
        "r-9",
        facts(owed=[R1], opener="main"),
        "id_not_owed",
        'no unpaid request "r-9" — you owe: [r-1] a: "pick one".',
    ),
    (
        "에이전트 — 이미 답한 id",
        "ok",
        "r-3",
        facts(owed=[R1], opener="main", by_id=PAID),
        "id_replied",
        ALREADY_R3,
    ),
    (
        "에이전트 — 가지 못한 id",
        "ok",
        "r-4",
        facts(owed=[R1], opener="main", by_id=UNSENT),
        "id_unsent",
        GONE_R4,
    ),
    (
        "main — 이미 답한 id",
        "ok",
        "r-3",
        facts(owed=[R1], by_id=PAID),
        "id_replied",
        ALREADY_R3,
    ),
    (
        "main — 가지 못한 id",
        "ok",
        "r-4",
        facts(owed=[R1], by_id=UNSENT),
        "id_unsent",
        GONE_R4,
    ),
    (
        "main — 빚이 다 닫힌 뒤 이미 답한 id",
        "ok",
        "r-3",
        facts(by_id=PAID),
        "id_replied",
        ALREADY_R3,
    ),
    (
        "main — 요청자가 main 인 줄에 이미 답함",
        "ok",
        "r-6",
        facts(by_id=PAID_MAIN),
        "id_replied",
        (
            "already replied to [r-6] (main) in this run — a second reply is not "
            "delivered. If you must correct it, send the correction with `message` (`to` "
            '"main", `text` your correction).'
        ),
    ),
    (
        "에이전트 — 빚이 다 닫힌 뒤 이미 답한 id 는 런을 연 주소가 아니라 그 줄로",
        "ok",
        "r-3",
        facts(opener="main", opener_row=PAID_MAIN, by_id=PAID),
        "id_replied",
        ALREADY_R3,
    ),
    (
        "에이전트 — 사람이 연 런에서 이미 답한 id 는 그 줄로",
        "ok",
        "r-3",
        facts(opener="user", opener_row=HUMAN, by_id=PAID),
        "id_replied",
        ALREADY_R3,
    ),
    (
        "에이전트 — 통지가 연 런에서 가지 못한 id 는 그 줄로",
        "ok",
        "r-4",
        facts(opener="", by_id=UNSENT),
        "id_unsent",
        GONE_R4,
    ),
    (
        "에이전트 — 사람 줄을 id 로 가리켜도 사람이 연 런의 글",
        "ok",
        "u-5",
        facts(opener="user", opener_row=HUMAN, by_id=HUMAN),
        "none_owed_human",
        (
            "nothing to send — your requester is a person watching this window, and "
            "they see your `complete` here. Just `complete`."
        ),
    ),
]


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_refusals(case):
    _name, text, rid, f, verdict, sentence = case
    got, debt = judge_reply(text, rid, f)
    assert (got, debt) == (verdict, None)
    assert reply_refusal(got, text, rid, f) == sentence


def test_every_refusal_verdict_has_a_row():
    from typing import get_args

    from agent_cli.subagent.question_port import Verdict

    assert {c[4] for c in CASES} == set(get_args(Verdict)) - {"pay"}


def test_pay_returns_the_owed_debt():
    assert judge_reply("ok", "r-2", facts(owed=[R1, R2])) == ("pay", R2)


def test_an_open_id_row_is_paid():
    """id 의 줄이 열려 있으면(빚) 갚는다 — 닫힌 줄만 사유가 된다."""
    assert judge_reply("ok", "r-1", facts(owed=[R1], by_id=R1)) == ("pay", R1)


def test_a_human_row_by_id_is_not_settled_so_owed_rules_apply():
    """사람 줄(u)은 런 안에서 닫혀 남지 않는다 — 빚이 남았으면 그 id 는 "그런 빚 없음"."""
    assert judge_reply("ok", "u-5", facts(owed=[R1], by_id=HUMAN)) == (
        "id_not_owed",
        None,
    )


def test_listing_cuts_long_texts():
    long = ReplyDebt(to="agent:a", text="x" * 80, id="r-1")
    msg = reply_refusal("needs_id", "ok", "", facts(owed=[long]))
    assert f'[r-1] a: "{"x" * 60}"' in msg and "x" * 61 not in msg


def test_pay_is_not_a_refusal():
    with pytest.raises(ValueError):
        reply_refusal("pay", "ok", "r-1", facts(owed=[R1]))
