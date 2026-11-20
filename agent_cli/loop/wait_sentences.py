"""`wait` 가 모델에게 말하는 문장 — 한 곳 (5b).

거절(이미 도착·서로 기다림)과 기다림이 깨진 까닭을 짓는 함수들이다. 판정은 하지 않는다 — 부르는 쪽
(`dispatch._op_wait`)이 판정한 사실을 받아 글로만 바꾼다. 의존이 없는 잎이라 문장만 다른 비교 트리를 만들 때
이 파일 하나를 바꾸면 된다.
"""

from __future__ import annotations


def mutual_wait_refusal(parked: list[str], owed_ids: list[str]) -> str:
    """서로 기다림 거절 — ``parked`` 는 이 주소에게 회신을 빚진 채 기다리는 쪽(그를 깨울 것이 이 주소 말고는 없다),
    ``owed_ids`` 는 이 주소가 그쪽에 갚지 않은 회신의 id. 코드가 확인한 것만 말한다: 그가 기다리고 있고 그를 깨울
    것이 돌고 있지 않다. 이 주소가 그에게 빚졌으면 그 id 를, 아니면 일을 잇는 길을 말한다."""
    who = ", ".join(parked)
    it = "it" if len(parked) == 1 else "them"
    head = (
        f"{who} owes you a reply but is itself waiting, and nothing that would wake "
        f"{it} is running, so if you wait too, nobody wakes."
    )
    if owed_ids:
        return f"{head} You owe {it} a reply [{', '.join(owed_ids)}]: send that first."
    return f"{head} Keep working, or call `complete` if the task is done."
