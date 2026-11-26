"""도구 테스트의 확인 대역 (6d-1) — 도구가 `c-N` 을 열면 그 자리에서 정해 둔 답을 돌려준다.

실제 기다림(레지스트리의 `ask_confirm`)은 `tests/test_confirms.py` 가 본다. 도구 쪽 테스트는 "무엇을 물었나" 와
"답에 따라 무엇을 돌려주나" 만 본다 — 그래서 `agent_cli.confirm` 의 두 입구(`answerable`·`ask_person`)를 갈아 끼운다.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_cli import confirm


@dataclass
class Asked:
    title: str
    detail: str
    choices: tuple
    owner: str
    command: str | None
    danger_spans: list


def answer_confirms(monkeypatch, *answers):
    """확인마다 ``answers`` 를 차례로 답한다 — 하나는 ``"approve"``·``"allow"``·``"deny"``·``"stopped"`` 이거나
    ``(답, 남긴 말)``. 답이 떨어진 뒤의 확인은 실패다(묻지 않아야 할 자리를 잡는다). 물은 것들의 목록을 돌려준다."""
    queue = list(answers)
    asked: list[Asked] = []

    def ask_person(
        title, detail, choices, *, owner, stopped, command=None, danger_spans=None
    ):
        asked.append(
            Asked(title, detail, choices, owner, command, list(danger_spans or []))
        )
        assert queue, f"an unexpected confirmation: {title}"
        answer = queue.pop(0)
        action, note = answer if isinstance(answer, tuple) else (answer, "")
        return f"c-{len(asked)}", confirm.Decision(action, note, "user")

    monkeypatch.setattr(confirm, "answerable", lambda: True)
    monkeypatch.setattr(confirm, "ask_person", ask_person)
    return asked
