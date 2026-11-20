"""`wait` 의 문장 — 판정이 준 사실을 글로만 바꾼다 (`loop/wait_sentences.py`, 5b)."""

from __future__ import annotations

from agent_cli.loop.wait_sentences import mutual_wait_refusal

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


def test_the_module_is_a_leaf():
    """비교 트리가 이 파일 하나만 바꿀 수 있게 — agent_cli 의 무엇도 import 하지 않는다."""
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
    assert all(m == "__future__" for m in imported), imported
