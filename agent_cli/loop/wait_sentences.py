"""`wait` 가 모델에게 말하는 문장 — 한 곳 (5b).

거절(이미 도착·서로 기다림·닫지 않은 것)과 기다릴 것 없는 끝, 기다림이 깨진 까닭을 짓는 함수들이다. 판정은 하지
않는다 — 부르는 쪽(`dispatch._op_wait`)이 판정한 사실을 받아 글로만 바꾼다. `constants` 의 낱말 말고는 기대는 것이
없어, 문장만 다른 비교 트리를 만들 때 이 파일 하나를 바꾸면 된다.
"""

from __future__ import annotations

from agent_cli.constants import ARRIVAL_NOTICE, ARRIVAL_USER, SCHEDULE_ADDR_PREFIX

#: 깨울 것도 닫지 않은 것(갚지 않은 회신·답하지 않은 사람의 요청)도 없는 `wait` — 받아들이고 작업 구간을 끝낸다(5d). 기록에 남아 다음 작업 구간의 모델이 읽는다.
#: 누구에게나 한 문장이다
NOTHING_TO_WAIT_FOR = (
    "nothing to wait for: no job or monitor of yours is running, no agent owes you "
    "a reply, no question of yours is open, and you owe none. This run has ended."
)


#: 그 끝의 화면 한 줄(사람이 본다 — 모델 입력이 아니다)
RUN_ENDED_NOTHING_TO_WAIT_FOR = "Run ended — nothing to wait for."


#: 사람의 요청이 남은 거절의 끝 — 기다리는 방법(에이전트에게서 들으려면 요청을 먼저 보낸다). 누가 읽든 같은 글
HEAR_FROM_AN_AGENT = " To hear from an agent, send it a request with `message` first."


def open_items_refusal(reply_ids: list[str], request_ids: list[str]) -> str:
    """깨울 것이 없는데 닫지 않은 것이 있다 — 갚지 않은 회신(``reply_ids``, `r-`)과 답하지 않은 사람의 요청(``request_ids``,
    `u-`)의 번호를 말한다(되묻기와 같은 번호). 번호가 여럿이면 하나씩 대괄호로 쉼표로 잇는다 — ``[r-2], [r-5]``."""
    replies = ", ".join(f"[{i}]" for i in reply_ids)
    requests = ", ".join(f"[{i}]" for i in request_ids)
    if replies and requests:
        return (
            f"nothing would wake you, and you still owe a `reply` {replies} and an "
            f"answer to {requests}. Settle those first, or keep working."
            + HEAR_FROM_AN_AGENT
        )
    if requests:
        return (
            f"nothing would wake you, and {requests} is still unanswered. Answer it "
            "with `complete`, or keep working." + HEAR_FROM_AN_AGENT
        )
    return (
        f"nothing would wake you, and you still owe a `reply` {replies}. Reply "
        "first, or keep working."
    )


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
