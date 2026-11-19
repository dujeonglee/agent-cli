"""에이전트 정지 — 사람이 멈춘 에이전트가 빚진 상대 모두에게 알리는 규칙 (docs/agent-engine/DESIGN.md §2.3).

정지는 둘이다. **■ 런 정지**(`AgentRegistry.stop_run`)는 지금 런만 끝내고 에이전트는 남는다. **✕ 제거**
(`AgentRegistry.kill` — 모델의 `agent` `kill` 도 같다)는 에이전트를 없앤다. run 에이전트(``ends_after_task``)는
맡은 일 하나가 수명이라 ■ 도 ✕ 와 같다 — 내려온다. 어느 쪽이든 그 에이전트가 들고 있던 빚은 조용히 버리지
않는다: 요청자마다 "stopped by user" 회신이 간다. 무엇을 누구에게 알리는지는 아래 표(`STOP_RULES`) 한 장이다.

| 들고 있던 것 | ■ (상주) | ✕ · run 에이전트 |
|---|---|---|
| 지금 하던 일의 요청자 · 그 밖의 빚 | stopped 회신 | 같음 |
| 아직 시작 안 한 대기 요청 | 남김(다음 차례) | 원인의 회신(✕ removed · run 의 ■ stopped) |
| 자기가 맡긴 하위 일 | 그대로 | 하위 run 은 함께 멈춤 · 하위 상주는 남음 |

세션 종료(`shutdown_all`)는 사람의 정지가 아니다 — 원인이 비어 있고, 빚은 지금처럼 버려 재시작이 되넣는다.
스스로 내려간 에이전트(과제를 끝낸 run·죽은 워커)도 사람의 정지가 아니지만 더는 상자를 읽지 않으니, 대기 요청에는
✕ 와 같은 길로 회신한다(`ENDED_REPLY`·`DIED_REPLY`, 표지 ``ended``·``died`` — 표 밖).

회신은 회신이다(P2 규칙): 요청자의 기다리는 회신 항목을 닫고 새 장부 항목을 만들지 않는다. 글은 여기서
짓는다 — 모델이 읽는 문장이라 모듈 상수다(`prompts.inventory` 표본). 마지막 줄은 다음 걸음을 요청자에게
맡긴다(사용자 결정 C, 2026-11-15): 사람은 일꾼만 멈춘 것이니 요청자가 다른 방법으로 마저 해도 된다. 종전 금지 문장
("Do not redo …")은 run 하나를 멈춘 실측에서 35B 가 3/3 지키지 않았고 GLM 의 "다르게 해 달라" 해석만 막았다.

레지스트리를 import 하지 않는 잎이다 — 레지스트리(`agents_live`)와 에이전트 루프(`resident_loop`)가 부른다.
"""

from __future__ import annotations

import threading
from typing import NamedTuple

#: 정지 원인 — 사람의 ■(런 정지)과 ✕(제거). 빈 문자열은 사람의 정지가 아니다(세션 종료).
STOPPED = "stopped"
REMOVED = "removed"

#: 봉투 머리줄 끝의 표지 — ``── reply [n-7] from agt-3 re [r-21] · stopped by user ──``. ``by`` 는 멈춘
#: 쪽 — 사람이면 ``user``, 모델의 `agent` `kill` 이면 ``main``·에이전트 키.
STOPPED_MARK = "stopped by {by}"
REMOVED_MARK = "removed by {by}"
_MARKS = {STOPPED: STOPPED_MARK, REMOVED: REMOVED_MARK}
#: 사람이 한 일이 아닌 내려감의 표지 — ``by`` 가 없다. 정상 종료한 run 에이전트와 죽은 워커.
ENDED_MARK = "ended"
DIED_MARK = "died"

#: ``who`` 는 문장 주어 — 사람이면 "The user", 모델이면 그 주소(``main``·키).
STOPPED_REPLY = (
    '{who} stopped {agent} while it was working on your request [{rid}] ("{quote}").'
)
#: 시작 전에 멈춘 요청의 회신 — ``cause`` 는 정지 원인 그대로의 동사(``stopped``·``removed``): run 에이전트의 ■ 는
#: 정지이고 ✕(모델의 `kill` 포함)는 제거다.
UNSTARTED_REPLY = (
    "{who} {cause} {agent} before it started your request [{rid}] "
    '("{quote}"). Nothing was done for it.'
)
#: 내려간 에이전트(정상 종료한 run·죽은 워커)의 받은편지함에 줄 서 있던 요청의 회신 — 사용자 승인 문장.
ENDED_REPLY = (
    "{agent} finished its task and is gone before it started your request [{rid}] "
    '("{quote}"). Nothing was done for it.'
)
DIED_REPLY = (
    "{agent} died before it started your request [{rid}] "
    '("{quote}"). Nothing was done for it.'
)
#: 죽은 워커가 집어 든(지금 일·흡수한) 요청의 회신 — 죽은 런의 활동·생각은 싣지 않는다(믿을 수 없다).
DIED_WORKING_REPLY = (
    '{agent} died while it was working on your request [{rid}] ("{quote}").'
)
#: 요청한 쪽이 사라졌다 — 빚진 쪽이 받는 통지(그 빚은 이미 닫혔다, 깨우지 않는다). ``agent`` 는 사라진 쪽의 이름표.
REQUESTER_GONE = (
    'request [{rid}] ("{quote}") is closed — {agent} is gone; nothing is owed back.'
)
LAST_ACTIVITY = "Last activity: {calls}"
PARTIAL_NOTES = "Partial notes: {notes}"
NEXT_STEP = "Decide whether to continue another way or report back to the user."

#: "Last activity" 에 싣는 호출 수(뒤에서부터)와 인용·메모 길이.
ACTIVITY_CALLS = 3
QUOTE_CHARS = 100
NOTES_CHARS = 300


class StopRule(NamedTuple):
    """한 정지가 그 에이전트의 것들에 하는 일 — `STOP_RULES` 의 한 칸."""

    ends_agent: bool  # 에이전트가 없어진다(내려옴·제거)
    answers_queued: (
        bool  # 시작 안 한 대기 요청에도 회신(원인의 동사 — `unstarted_reply`)
    )
    stops_sub_runs: bool  # 그 에이전트가 띄운 run 에이전트도 멈춘다


#: (원인, run 에이전트인가) → 규칙. 모든 원인에서 지금 일·그 밖의 빚은 stopped 회신이다.
STOP_RULES: dict[tuple[str, bool], StopRule] = {
    (STOPPED, False): StopRule(
        ends_agent=False, answers_queued=False, stops_sub_runs=False
    ),
    (STOPPED, True): StopRule(
        ends_agent=True, answers_queued=True, stops_sub_runs=True
    ),
    (REMOVED, False): StopRule(
        ends_agent=True, answers_queued=True, stops_sub_runs=True
    ),
    (REMOVED, True): StopRule(
        ends_agent=True, answers_queued=True, stops_sub_runs=True
    ),
}


def stop_rule(cause: str, ends_after_task: bool) -> StopRule:
    """정지 원인과 에이전트 종류의 규칙. 원인이 비면(세션 종료) KeyError — 사람의 정지만 묻는다."""
    return STOP_RULES[(cause, bool(ends_after_task))]


class LinkedStop(threading.Event):
    """에이전트 런 하나의 정지 — 그 런만의 정지(■)와 에이전트의 평생 정지(✕·세션 종료) 중 어느 쪽이 서도
    ``is_set()`` 이 참이다. ``set()`` 은 자기 것만 세운다(에이전트를 죽이지 않는다).

    ``wait()`` 은 부모를 보지 않는다 — 정지를 보는 쪽(엔진의 턴 경계·도구 실행 직전)은 전부 ``is_set()`` 으로
    본다."""

    def __init__(self, parent: threading.Event | None) -> None:
        super().__init__()
        self._parent = parent

    def is_set(self) -> bool:
        return super().is_set() or (self._parent is not None and self._parent.is_set())


def one_line(text: str, cap: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= cap else flat[: cap - 1].rstrip() + "…"


#: 호출 한 줄 요약에 쓸 인자 — 사람이 고른 값 하나(명령·경로·URL·질의·패턴·상대·키).
_SUMMARY_ARGS = ("command", "path", "url", "query", "pattern", "to", "key")


def _call_summary(action: str, args) -> str:
    """호출 하나 → 도구 이름은 백틱, 고른 인자 값은 큰따옴표 — 표기 규칙(v10.77.0)."""
    if isinstance(args, dict):
        for k in _SUMMARY_ARGS:
            v = args.get(k)
            if isinstance(v, str) and v.strip():
                return f'`{action}` "{one_line(v, 60)}"'
    return f"`{action}`"


def run_activity(records: list[dict]) -> list[str]:
    """그 런이 기록한 레코드(`ContextManager.records_since`) → 실행된 호출 줄(도구 백틱·값 큰따옴표·✓/✗), 뒤에서
    `ACTIVITY_CALLS` 개. 호출은 assistant 레코드의 op(``complete`` 제외), 결과는 그 뒤 관찰 레코드(묶음이면 op 별
    조각 ``parts``)의 성공 여부 — 결과가 없는 op(멈춘 뒤라 실행되지 않음)는 뺀다. 읽기만 한다."""
    calls: list[str] = []
    pending: list[str] = []
    for r in records:
        if r.get("role") == "assistant":
            pending = [
                _call_summary(str(op.get("action")), op.get("action_input"))
                for op in r.get("ops") or []
                if isinstance(op, dict)
                and op.get("action")
                and op.get("action") != "complete"
            ]
            continue
        if r.get("role") != "user" or not r.get("tool") or r.get("recovery"):
            continue
        if r.get("tool") == "interrupt":
            pending = []  # 멈춘 자리 — 남은 op 는 실행되지 않았다
            continue
        parts = r.get("parts") or [r]
        for p in parts:
            if not pending:
                break
            calls.append(f"{pending.pop(0)} {'✓' if p.get('success') else '✗'}")
    return calls[-ACTIVITY_CALLS:]


def partial_notes(records: list[dict]) -> str:
    """그 런의 마지막 생각(assistant 레코드의 ``thought``) 앞부분 한 줄 — 멈추기 직전까지 알아낸 것. 없으면 ""."""
    for r in reversed(records):
        if r.get("role") == "assistant" and str(r.get("thought") or "").strip():
            return one_line(r["thought"], NOTES_CHARS)
    return ""


def stop_actor(by: str) -> str:
    """멈춘 쪽(``"user"``·``"main"``·키) → 회신 문장의 주어."""
    return "The user" if not by or by == "user" else by


def stopped_reply(
    agent: str, rid: str, request: str, activity: list[str], notes: str, *, by: str
) -> str:
    """일하던(또는 빚진) 요청에 대한 stopped 회신의 본문 — 봉투 안쪽."""
    lines = [
        STOPPED_REPLY.format(
            who=stop_actor(by),
            agent=agent,
            rid=rid,
            quote=one_line(request, QUOTE_CHARS),
        )
    ]
    if activity:
        lines.append(LAST_ACTIVITY.format(calls=" · ".join(activity)))
    if notes:
        lines.append(PARTIAL_NOTES.format(notes=notes))
    lines.append(NEXT_STEP)
    return "\n".join(lines)


def stop_mark(cause: str, by: str) -> str:
    """정지 원인(``STOPPED``·``REMOVED``)과 멈춘 쪽 → 봉투 머리 표지·화면 한 줄의 ``stopped by user``·``removed by main``."""
    return _MARKS[cause].format(by=by)


def unstarted_reply(agent: str, rid: str, request: str, *, by: str, cause: str) -> str:
    """시작 전에 멈춘(``cause``) 요청에 대한 회신의 본문."""
    return "\n".join(
        [
            UNSTARTED_REPLY.format(
                who=stop_actor(by),
                cause=cause,
                agent=agent,
                rid=rid,
                quote=one_line(request, QUOTE_CHARS),
            ),
            NEXT_STEP,
        ]
    )


def gone_reply(agent: str, rid: str, request: str, *, died: bool) -> str:
    """스스로 내려간(``died`` 면 죽은) 에이전트에게 줄 서 있던 요청에 대한 회신의 본문."""
    return "\n".join(
        [
            (DIED_REPLY if died else ENDED_REPLY).format(
                agent=agent, rid=rid, quote=one_line(request, QUOTE_CHARS)
            ),
            NEXT_STEP,
        ]
    )


def died_working_reply(agent: str, rid: str, request: str) -> str:
    """죽은 워커가 집어 든 요청(갚지 않은 빚)에 대한 회신의 본문."""
    return "\n".join(
        [
            DIED_WORKING_REPLY.format(
                agent=agent, rid=rid, quote=one_line(request, QUOTE_CHARS)
            ),
            NEXT_STEP,
        ]
    )


def answer_main_debts(registry, records: list[dict], *, by: str = "user") -> int:
    """멈춘 main 의 갚지 않은 빚 모두에 stopped 회신을 보내고 그 배달로 갚는다 — 에이전트의 ■ 와 같은 문장·표지
    (`stopped_reply`, ``stopped by user``). ``records`` 는 그 런이 기록한 레코드(마지막 호출·생각, `run_activity`·
    `partial_notes`). "Stop everything" 이면 받는 쪽의 런을 열지 않는다 — 레지스트리의 ``main_stop_wakes``: 서버가 main 을
    멈추기 **앞에** 내리고 main 의 런이 열릴 때 다시 세운다(`pump.MainActor.open`). 보낸 수를 돌려준다."""
    activity, notes = run_activity(records), partial_notes(records)
    mark = STOPPED_MARK.format(by=by)
    sent = 0
    for d in registry.reply_debts("main"):
        if not d.to.startswith("agent:"):
            continue
        err = registry._deliver_peer_reply(
            d.to.split(":", 1)[1],
            "main",
            stopped_reply("main", d.id, d.text, activity, notes, by=by),
            d.hop,
            re=d.id,
            mark=mark,
            opens_run=registry.main_stop_wakes,
            settles=d.id,
        )
        sent += 0 if err else 1
    return sent


def requester_gone(agent: str, rid: str, request: str) -> str:
    """요청한 쪽이 사라져 닫힌 빚의 통지 — 빚진 쪽이 다음 턴 경계에 읽는다."""
    return REQUESTER_GONE.format(
        rid=rid, quote=one_line(request, QUOTE_CHARS), agent=agent
    )


def close_debts_owed_to(registry, tm, label: str) -> None:
    """사라진 에이전트(✕·사망·과제를 마친 run — 되살리지 않는 끝)가 기다리던 회신을 닫는다: 빚으로 적힌 줄을 회신이 갈
    곳 없는 갚음으로(빚진 쪽의 지금 런 끝까지 남아 그 뒤의 `reply` 는 "가지 못했다" 로 듣는다 —
    `PostOffice.forget_requests_of`), 빚진 쪽마다 깨우지 않는 통지 한 통(`requester_gone`) — 그 빚은 다음 꼬리부터
    Open Items 에 없다. ``label`` 은 사라진 쪽의 이름표. 세션 종료(되살릴 에이전트)는 여기 오지 않는다."""
    gone = registry._post.forget_requests_of(f"agent:{tm.key}", registry._run_of)
    for row in gone:
        text = requester_gone(label, row.rid, row.text)
        if row.dst == "main":
            registry._push_reply(
                {
                    "kind": "peer_message",
                    "key": tm.key,
                    "profile": tm.profile_name,
                    "name": tm.instance_name,
                    "success": True,
                    "output": text,
                    "expects_reply": False,
                    "notice": True,
                    "ledger": "none",
                    "wakes_main": False,
                }
            )
            continue
        peer = registry.get(row.dst.removeprefix("agent:"))
        if peer is not None and peer.state != "dead":
            registry.request(
                peer.key,
                text,
                author=f"agent:{tm.key}",
                expects_reply=False,
                kind="notice",
                ledger="none",
                opens_run=False,
            )
    if gone:
        registry._save_state()
        registry._notify_roster()
