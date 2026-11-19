"""구현 대 독립 참조 모델 — 시드 고정 무작위 연산열(0..499). 매 걸음 불변식 + 관찰값 일치.

연산열은 미리 만든다(인자는 그 시점까지 만들어진 id 에서 고른다 — 틀린 연산도 섞인다: 둘 다 거절해야 한다).
갚기·런 끝·✕ 의 절반은 "겨냥" 연산이다: 적용하는 순간 **참조 모델**의 열린 줄에서 대상을 골라(구현이 아니라)
맞는 회신을 짓는다 — 무작위 id 로는 받아들여지는 칸에 거의 닿지 않는다.
어긋나면 시드와 줄인 연산열을 메시지에 싣는다. hypothesis 는 쓰지 않는다(이 저장소의 간헐 실패 이력).
"""

from __future__ import annotations

import random

import pytest

from agent_cli.post import envelope as E
from tests.post.twin import Twin

ADDRS = ("main", "agent:a", "agent:b")
STEPS = 40


def make_ops(seed: int) -> list[tuple[str, dict]]:
    rnd = random.Random(seed)
    n = 0
    ids: list[str] = []
    qids: list[str] = []
    runs = ["run-1", "run-2"]

    def nid(prefix):
        nonlocal n
        n += 1
        ids.append(f"{prefix}-{n}")
        return ids[-1]

    def any_id():
        return rnd.choice(ids) if ids else "r-0"

    def envelope():
        src, dst = rnd.sample(ADDRS, 2)
        pick = rnd.randrange(6)
        if pick == 0:
            return E.request(nid("r"), src, dst, "ask", hop=rnd.randrange(3))
        if pick == 1:
            return E.human(nid("u"), "user:dj", rnd.choice(ADDRS[1:]), "hi")
        if pick == 2:
            return E.notice(nid("n"), src, dst, "fyi")
        if pick == 3:
            return E.reply(
                nid("n"), src, dst, "ans", re=any_id(), wakes=rnd.random() < 0.7
            )
        return E.system(
            nid("n"),
            "job",
            dst,
            "done",
            opens_row=rnd.random() < 0.6,
            persists=rnd.random() < 0.8,
        )

    ops: list[tuple[str, dict]] = []
    for _ in range(STEPS):
        k = rnd.randrange(15)
        addr = rnd.choice(ADDRS)
        run = rnd.choice(runs)
        if k <= 3:
            ops.append(("deliver", {"e": envelope()}))
        elif k == 4:
            ops.append(("next_run", {"addr": addr}))
        elif k == 5:
            ops.append(("absorb", {"addr": addr, "human_first": rnd.random() < 0.5}))
        elif k == 6 and rnd.random() < 0.5:
            ops.append(
                ("pay", {"aim": rnd.random(), "fresh": [nid("n")], "run_id": run})
            )
        elif k == 9 and rnd.random() < 0.5:
            ops.append(
                (
                    "end_run",
                    {
                        "aim": rnd.random(),
                        "fresh": [nid("n") for _ in range(4)],
                        "addr": addr,
                        "run_id": run,
                        "outcome": rnd.choice(["done", "stopped"]),
                    },
                )
            )
        elif k == 10 and rnd.random() < 0.5:
            ops.append(
                (
                    "remove_queued",
                    {
                        "aim": rnd.random(),
                        "fresh": [nid("n") for _ in range(4)],
                        "addr": addr,
                    },
                )
            )
        elif k == 6:
            rid = any_id()
            ops.append(
                (
                    "pay",
                    {
                        "debtor": addr,
                        "rid": rid,
                        "reply": E.reply(
                            nid("n"), addr, rnd.choice(ADDRS), "r", re=rid
                        ),
                        "run_id": run,
                    },
                )
            )
        elif k == 7:
            # `message` 는 빚을 갚지 않는다(v10.145.1) — 통지는 평범한 배달
            ops.append(
                ("deliver", {"e": E.notice(nid("n"), addr, rnd.choice(ADDRS), "m")})
            )
        elif k == 8:
            ops.append(
                (
                    "complete",
                    {
                        "addr": addr,
                        "ids": rnd.sample(ids, min(2, len(ids))),
                        "run_id": run,
                    },
                )
            )
        elif k == 9:
            outcome = rnd.choice(["done", "stopped", "wait", "session_end"])
            replies = {}
            for rid in rnd.sample(ids, min(2, len(ids))):
                replies[rid] = E.reply(nid("n"), addr, rnd.choice(ADDRS), "fb", re=rid)
            ops.append(
                (
                    "end_run",
                    {
                        "addr": addr,
                        "run_id": run,
                        "outcome": outcome,
                        "replies": replies,
                    },
                )
            )
        elif k == 10:
            replies = {
                rid: E.reply(nid("n"), addr, rnd.choice(ADDRS), "rm", re=rid)
                for rid in rnd.sample(ids, min(1, len(ids)))
            }
            ops.append(("remove_queued", {"addr": addr, "replies": replies}))
        elif k == 11:
            ops.append(("take_reply", {"addr": addr, "rid": any_id()}))
        elif k == 13:
            debtor, creditor = rnd.sample(ADDRS, 2)
            rid = nid("r") if rnd.random() < 0.8 else any_id()
            ops.append(("owe", {"rid": rid, "debtor": debtor, "creditor": creditor}))
        elif k == 14:
            ops.append(("take", {"addr": rnd.choice(ADDRS)}))
        else:
            if qids and rnd.random() < 0.6:
                qid = rnd.choice(qids)
                if rnd.random() < 0.7:
                    ops.append(
                        (
                            "answer",
                            {
                                "qid": qid,
                                "env": E.human(
                                    nid("u"), "user:dj", rnd.choice(ADDRS[1:]), "v2"
                                ),
                            },
                        )
                    )
                else:
                    ops.append(
                        ("forget_questions_of", {"asker": rnd.choice(ADDRS[1:])})
                    )
            else:
                n += 1
                qids.append(f"q-{n}")
                ops.append(
                    (
                        "ask",
                        {
                            "qid": qids[-1],
                            "asker": rnd.choice(ADDRS[1:]),
                            "target": "user:dj",
                        },
                    )
                )
    return ops


def aim(model, op: str, args: dict) -> dict:
    """겨냥 연산의 인자를 참조 모델의 상태에서 짓는다 — 고를 것이 없으면 빈 회신(거절되거나 아무 일 없음)."""
    if "aim" not in args:
        return args
    rows = model.rows
    fresh = list(args["fresh"])
    if op == "pay":
        owed = [
            (rid, r)
            for rid, r in rows.items()
            if r["kind"] == "r" and r["state"] == "picked"
        ]
        if not owed:
            return {
                "debtor": "main",
                "rid": "r-0",
                "reply": E.reply(fresh[0], "main", "agent:a", "x", re="r-0"),
                "run_id": args["run_id"],
            }
        rid, r = owed[int(args["aim"] * len(owed))]
        return {
            "debtor": r["dst"],
            "rid": rid,
            "reply": E.reply(fresh[0], r["dst"], r["src"], "ok", re=rid),
            "run_id": args["run_id"],
        }
    addr = args["addr"]
    want = "picked" if op == "end_run" else "queued"
    targets = [
        (rid, r)
        for rid, r in rows.items()
        if r["kind"] == "r" and r["state"] == want and r["dst"] == addr
    ]
    keep = targets[
        : max(0, int(args["aim"] * (len(targets) + 1)))
    ]  # 몇은 회신 없이 — dropped 칸
    replies = {
        rid: E.reply(fresh.pop(0), addr, r["src"], "fb", re=rid)
        for rid, r in keep[: len(fresh)]
    }
    out = {k: v for k, v in args.items() if k not in ("aim", "fresh")}
    out["replies"] = replies
    return out


def run_ops(ops) -> str:
    twin = Twin(ADDRS)
    for i, (op, args) in enumerate(ops):
        why = twin.step(op, aim(twin.model, op, args))
        if why:
            return f"step {i}: {why}"
    return ""


def shrink(ops) -> list:
    """어긋남이 남는 한 연산을 하나씩 뺀다 — 결정적."""
    cur = list(ops)
    changed = True
    while changed:
        changed = False
        for i in range(len(cur)):
            trial = cur[:i] + cur[i + 1 :]
            if run_ops(trial):
                cur, changed = trial, True
                break
    return cur


@pytest.mark.parametrize("seed", range(500))
def test_implementation_matches_the_reference(seed):
    ops = make_ops(seed)
    why = run_ops(ops)
    if why:
        small = shrink(ops)
        lines = "\n".join(f"  {op} {args}" for op, args in small)
        pytest.fail(
            f"seed {seed}: {why}\nshrunk ({len(small)} ops):\n{lines}\n{run_ops(small)}"
        )


def test_the_generator_reaches_every_operation_and_both_verdicts():
    """연산열이 표의 칸을 실제로 밟는다 — 모든 연산이 나오고, 받아들여진 것과 거절된 것이 둘 다 많다."""
    seen: set[str] = set()
    accepted = refused = 0
    for seed in range(500):
        twin = Twin(ADDRS)
        for op, args in make_ops(seed):
            seen.add(op)
            impl_refused, _ = twin.apply(op, aim(twin.model, op, args))
            refused += impl_refused
            accepted += not impl_refused
    assert seen == {
        "deliver", "next_run", "absorb", "pay", "complete", "end_run",
        "remove_queued", "take_reply", "ask", "answer", "forget_questions_of",
        "owe", "take",
    }  # fmt: skip
    assert accepted > 5000 and refused > 500, (accepted, refused)
