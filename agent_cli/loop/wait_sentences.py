"""`wait` 가 모델에게 말하는 문장 — 한 곳 (5b).

거절(이미 도착·서로 기다림)과 기다림이 깨진 까닭을 짓는 함수들이다. 판정은 하지 않는다 — 부르는 쪽
(`dispatch._op_wait`)이 판정한 사실을 받아 글로만 바꾼다. `constants` 의 낱말 말고는 기대는 것이 없어, 문장만
다른 비교 트리를 만들 때 이 파일 하나를 바꾸면 된다. 깨울 것이 없을 때의 거절과 그 끝맺음(`WAIT_NOTHING_PENDING`·
`WAIT_NEXT_*`)은 아직 `loop/kind.py` 에 있다 — 5d 에서 끝맺음을 하나로 줄일 때 이리 옮긴다.
"""

from __future__ import annotations

from agent_cli.constants import ARRIVAL_NOTICE, ARRIVAL_USER, SCHEDULE_ADDR_PREFIX


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


# ── 무엇이 왔나 — 거절 2번("이미 도착")과 기다림이 깨진 까닭이 같이 쓴다 ─────────────────────
#
# 도착한 깨우는 봉투마다 종류 하나(`wake.Mailbox.arrivals`): ``"user"`` 사람의 말, ``"schedule:<이름>"`` 예약 발화,
# ``"notice"`` 그 밖(회신·요청·통지·하니스 알림) — 낱말은 `constants.ARRIVAL_*`.

USER = ARRIVAL_USER
NOTICE = ARRIVAL_NOTICE
SCHEDULE = SCHEDULE_ADDR_PREFIX


def arrival_clause(kinds: tuple[str, ...]) -> tuple[str, str]:
    """무엇이 왔나의 구절 ``(무엇이 왔나, 어디 있나)`` — 예: ``("the user wrote to you and 1 notice arrived", "they are
    right below")``. 글만 짓는다 — 거절인지 깸인지 모르고, 다음 걸음은 부르는 쪽이 붙인다. 종류가 섞이면 늘
    "they are right below" 하나다."""
    user = USER in kinds
    schedules = [k[len(SCHEDULE) :] for k in kinds if k.startswith(SCHEDULE)]
    notices = sum(1 for k in kinds if k == NOTICE)
    parts = []
    if user:
        parts.append("the user wrote to you")
    if schedules:
        names = ", ".join(f'"{n}"' for n in dict.fromkeys(schedules))
        parts.append(
            f"the schedule {names} fired"
            if len(set(schedules)) == 1
            else f"the schedules {names} fired"
        )
    if notices:
        parts.append(f"{notices} notice{'s' if notices > 1 else ''} arrived")
    what = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    if len(parts) > 1:
        where = "they are right below"
    elif user:
        where = "their message is right below"
    elif schedules:
        where = (
            "its prompt is right below"
            if len(schedules) == 1
            else "their prompts are right below"
        )
    else:
        where = "it is right below" if notices == 1 else "they are right below"
    return what, where


def arrived_refusal(kinds: tuple[str, ...]) -> str:
    """거절 2번 — 생각하는 동안 깨우는 봉투가 와 있다. 사람의 말이 있으면 지금 `complete` 로 답하라고(답은 바로 전달되고
    런은 열린 것이 있으면 이어진다 — `dispatch._op_complete`, main·에이전트 같다), 아니면 먼저 처리하라고 한다.
    사람의 말 쪽 글은 측정으로 골랐다(GLM: "Answer it first, then `wait` again" 은 바로 답함 0/10, 이 글 9/10)."""
    what, where = arrival_clause(kinds)
    if USER in kinds:
        who = "now" if set(kinds) == {USER} else "the user now"
        return (
            f"{what} while you were thinking; {where}. Answer {who} with `complete`: "
            "your answer is delivered right away, and what is still running still "
            "reports to you."
        )
    first = "Deal with it first" if len(kinds) == 1 else "Deal with them first"
    return (
        f"{what} while you were thinking; {where}. {first}, then `wait` again if "
        "something is still running."
    )


def wait_interrupted(kinds: tuple[str, ...], still: str) -> str:
    """제자리 대기가 사람 입력으로 깼다 — 무엇이 왔나(사람이 썼나, 예약이 발화했나). ``still`` 은 아직 기다리는 회신의
    꼬리(있으면 ``" — r-1 still awaited, …"``)."""
    what, where = arrival_clause(kinds)
    return f"the wait was interrupted — {what}; {where}{still}."
