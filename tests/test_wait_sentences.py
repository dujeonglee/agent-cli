"""`wait` 의 문장 — 판정이 준 사실을 글로만 바꾼다 (`loop/wait_sentences.py`, 5b)."""

from __future__ import annotations

from agent_cli.loop.wait_sentences import (
    arrived_refusal,
    mutual_wait_refusal,
    wait_interrupted,
)

A, B = "agent:a", "agent:b"
HEAD = (
    "{who} owes you a reply but is itself waiting, and nothing that would wake {it} is "
    "running, so if you wait too, nobody wakes."
)


def test_mutual_wait_when_you_owe_it_names_the_reply():
    assert mutual_wait_refusal([A], ["r-7"]) == (
        HEAD.format(who=A, it="it") + " You owe it a reply [r-7]: send that first."
    )


def test_mutual_wait_when_you_owe_nothing_says_keep_working():
    assert mutual_wait_refusal(["main"], []) == (
        HEAD.format(who="main", it="it")
        + " Keep working, or call `complete` if the task is done."
    )


def test_mutual_wait_with_two_waiting_debtors():
    assert mutual_wait_refusal([A, B], ["r-1", "r-2"]) == (
        HEAD.format(who=f"{A}, {B}", it="them")
        + " You owe them a reply [r-1, r-2]: send that first."
    )


def test_the_module_leans_only_on_constants():
    """비교 트리가 이 파일 하나만 바꿀 수 있게 — `constants` 의 낱말 말고는 import 하지 않는다."""
    import ast
    from pathlib import Path

    import agent_cli.loop.wait_sentences as mod

    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    imported = [
        n.module if isinstance(n, ast.ImportFrom) else a.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.Import, ast.ImportFrom))
        for a in (n.names if isinstance(n, ast.Import) else [None])
    ]
    assert set(imported) <= {"__future__", "agent_cli.constants"}, imported


# ── 무엇이 왔나 (거절 2번·깸) ─────────────────────────────────────────

TAIL = ", then `wait` again if something is still running."
NIGHTLY = "schedule:nightly"

REFUSALS = [
    (
        ("user",),
        "the user wrote to you while you were thinking; their message is right below. Answer it first",
    ),
    (
        ("user", "user"),
        "the user wrote to you while you were thinking; their message is right below. Answer it first",
    ),
    (
        ("notice",),
        "1 notice arrived while you were thinking; it is right below. Deal with it first",
    ),
    (
        ("notice", "notice"),
        "2 notices arrived while you were thinking; they are right below. Deal with them first",
    ),
    (
        ("notice", "user"),
        "the user wrote to you and 1 notice arrived while you were thinking; they are right below. Answer the user first",
    ),
    (
        (NIGHTLY,),
        'the schedule "nightly" fired while you were thinking; its prompt is right below. Deal with it first',
    ),
    (
        ("user", NIGHTLY),
        (
            'the user wrote to you and the schedule "nightly" fired while you were thinking; they are right below. '
            "Answer the user first"
        ),
    ),
    (
        (NIGHTLY, "notice"),
        (
            'the schedule "nightly" fired and 1 notice arrived while you were thinking; they are right below. '
            "Deal with them first"
        ),
    ),
    (
        ("user", NIGHTLY, "notice", "notice"),
        (
            'the user wrote to you, the schedule "nightly" fired and 2 notices arrived while you were thinking; '
            "they are right below. Answer the user first"
        ),
    ),
]


def test_arrived_refusal_table():
    for kinds, head in REFUSALS:
        assert arrived_refusal(kinds) == head + TAIL, kinds


def test_wait_interrupted_keeps_the_user_sentence_and_names_a_schedule():
    still = " — r-1 still awaited, so `wait` again when you are done"
    assert wait_interrupted(("user",), still) == (
        "the wait was interrupted — the user wrote to you; their message is right below"
        + still
        + "."
    )
    assert wait_interrupted((NIGHTLY,), "") == (
        'the wait was interrupted — the schedule "nightly" fired; its prompt is right below.'
    )
    assert wait_interrupted(("user", NIGHTLY), "") == (
        'the wait was interrupted — the user wrote to you and the schedule "nightly" fired; they are right below.'
    )
