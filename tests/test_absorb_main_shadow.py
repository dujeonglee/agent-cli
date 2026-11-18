"""main 의 턴 경계 흡수 한 번(`AgentRegistry.absorb_main`)이 지금의 두 단계(사람의 말을 `take_next` 로 전부, 그다음
`drain_replies`)와 같은 항목 열을 내는가 — 넣는 연산 열(사람의 말·깨우는 메일·깨우지 않는 회신)을 전부 돌려 본다."""

from __future__ import annotations

import itertools

import pytest

from agent_cli.main_input import MainInput
from agent_cli.subagent.agents_live import AgentRegistry


def _world(tmp_path):
    q = MainInput()
    reg = AgentRegistry(tmp_path, runtime={"model": "m", "timeout": 5}, post=q.post)
    return q, reg


def _put(q, reg, op, i):
    if op == "words":
        q.enqueue("c1", f"w{i}", nickname="dj")
    elif op == "job":
        reg.deliver(
            "main",
            mail={"kind": "job", "success": True, "output": f"job {i}"},
            text="",
            author="",
            expects_reply=False,
        )
    else:  # 깨우지 않는 메일
        reg.deliver(
            "main",
            mail={
                "kind": "job",
                "success": True,
                "output": f"quiet {i}",
                "wakes_main": False,
            },
            text="",
            author="",
            expects_reply=False,
        )


def _two_step(q, reg):
    items = []
    while (it := q.take_next()) is not None:
        items.append({"message": it})
    items.extend({"mail": m} for m in reg.drain_replies())
    return items


def _shape(items):
    """id 를 빼고 비교한다 — 두 세계가 할당기를 번갈아 쓴다."""
    out = []
    for it in items:
        if "message" in it:
            out.append(("message", it["message"]["text"], it["message"]["nickname"]))
        else:
            out.append(
                ("mail", it["mail"].get("output"), it["mail"].get("wakes_main", True))
            )
    return out


@pytest.mark.parametrize("n", [1, 2, 3, 4])
def test_one_absorb_gives_the_same_items_as_the_two_steps(tmp_path, n):
    for k, ops in enumerate(itertools.product(("words", "job", "quiet"), repeat=n)):
        a_dir, b_dir = tmp_path / f"a{n}-{k}", tmp_path / f"b{n}-{k}"
        a_dir.mkdir(), b_dir.mkdir()
        qa, ra = _world(a_dir)
        qb, rb = _world(b_dir)
        for i, op in enumerate(ops):
            _put(qa, ra, op, i)
            _put(qb, rb, op, i)
        assert _shape(rb.absorb_main()) == _shape(_two_step(qa, ra)), ops
        assert qb.pending_count() == qa.pending_count() == 0
        assert rb.pending_mail() == ra.pending_mail() == []
