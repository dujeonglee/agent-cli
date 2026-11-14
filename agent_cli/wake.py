"""한 주소를 무엇이 깨우는가와 그 주소가 무엇을 받는가 — 세션 슬롯만 본다, 조립은 모른다.

`wait` 의 판정(`wake_sources`·`wake_source_ids`·`arrived_mail`)과 중간 깨우기 알림
(`wait_checkin`), 하니스 작업 판정(`harness_work_running`), 메일 한 통 → 관찰 레코드
(`harness_record`·`mail_record`·`_private_inbox`), 한 주소가 가진 것 거두기(`release_owner`).
읽는 것은 세션 슬롯(`session_slots` — 잡·모니터·예약·에이전트 레지스트리)과 `constants`·`jobs`
의 작은 함수뿐이다 — 세션 조립(`runtime`)은 이것을 쓰고, 이것은 조립을 모른다.
"""

from __future__ import annotations

import time
from typing import NamedTuple

from agent_cli.constants import format_duration, harness_mail_tool, is_private_owner
from agent_cli.jobs import _short
from agent_cli.session_slots import (
    get_agent_registry,
    get_job_registry,
    get_monitor_registry,
    get_schedule_registry,
)

#: 예약의 깨울 것 id — 중간 깨우기의 대상 집합에서는 뺀다 (`wake_source_ids`).
_SCHEDULE_ID = "schedule"


def _wake_items(
    owner: str, _seen: frozenset = frozenset(), *, agents
) -> list[tuple[str, str]]:
    """`wake_sources` 의 재료 — ``(id, 사람이 읽을 줄)`` 쌍 (v10.79.0).

    id 는 ``"job:1"``·``"mon:mon-504d33"``·``"agent:k"``·``"schedule"`` — 중간
    깨우기가 "같은 대상을 다시 기다리는가" 를 이 집합으로 비교한다. 사람이 읽는
    줄로 비교하면 같은 에이전트라도 "queued↔working" 처럼 글이 바뀌어 헛되이
    5분으로 돌아간다."""
    out: list[tuple[str, str]] = []
    jobs = get_job_registry()
    if jobs is not None:
        for j in jobs.running(owner):
            out.append((f"job:{j.id}", f"job #{j.id} (`{j.command[:60]}`)"))
    mons = get_monitor_registry()
    if mons is not None:
        for m in mons.list_all():
            if m.owner == owner:
                out.append((f"mon:{m.id}", f"monitor {m.id}"))
    if agents is not None:
        out.extend(agents.pending_agents_for(owner, exclude=_seen))
        seen = _seen | {owner}
        for debtor in agents.parked_debtors_for(owner):
            if debtor in seen:
                continue  # 고리 — 나를 기다리는 쪽은 나를 깨우지 못한다
            if _wake_items(debtor, seen, agents=agents):
                out.append(
                    (debtor, f"{debtor} owes you a reply (it is waiting on others)")
                )
    # 예약은 세션 것 — main 에게 발화한다. private 주소(one-shot·하니스 작업)의
    # 깨울 것으로 세면 잡 없이도 `wait` 가 받아들여져 영원히 막힌다 (v10.84.0).
    sched = get_schedule_registry()
    if sched is not None and sched.has_active_work() and not is_private_owner(owner):
        out.append((_SCHEDULE_ID, "a schedule"))
    return out


def harness_work_running(owner: str) -> bool:
    """이 소유자의 하니스 작업이 도는 중인가 — 돌고 있는 `shell background` 작업이나 살아 있는
    모니터 (v10.120.0). StaleGuard 가 그 턴을 제자리로 세지 않는다: 작업을 기다리며 같은 로그를
    다시 보는 것은 고리가 아니다(오프라인 대입의 폴링 오탐 — apt-get 을 tail 로 반복해 봄)."""
    jobs = get_job_registry()
    if jobs is not None and jobs.running(owner):
        return True
    mons = get_monitor_registry()
    return mons is not None and any(m.owner == owner for m in mons.list_all())


def wake_sources(owner: str, _seen: frozenset = frozenset(), *, agents) -> list[str]:
    """이 소유자를 **깨울 수 있는 것** — `wait` 도구의 판정 (v10.65.0, docs/wait/DESIGN.md).

    돌고 있는 백그라운드 작업·살아 있는 모니터는 소유자 것만, 에이전트 활동은
    **요청자별**(`AgentRegistry.pending_for` — 세션 전체 `has_active_work` 로 보면
    상주 에이전트 자신이 busy 라 늘 참이 된다), 예약은 세션 것. 사람이 읽을 한 줄씩.

    v10.77.0 — 순환 대기: 나에게 회신을 빚진 채 **스스로 잠든**(`wait`) 에이전트는
    그를 깨울 것이 있을 때만 센다. 거슬러 올라가며(``_seen`` 으로 순환을 자른다)
    A→B→A 처럼 서로를 기다리는 고리면 두 번째로 잠들려는 쪽의 `wait` 가 거부되고,
    A→B→(일하는 C) 사슬이면 그대로 받아들여진다.

    ``agents`` 는 에이전트 레지스트리(없으면 None) — 부르는 쪽이 넘긴다(v10.133.26). 슬롯을
    읽지 않으므로 서브루프는 포트로 받은 것을, 레지스트리는 자기를 넘긴다."""
    return [text for _, text in _wake_items(owner, _seen, agents=agents)]


def wake_source_ids(owner: str, *, agents) -> frozenset[str]:
    """중간 깨우기의 **대상 집합** (v10.79.0, docs/wait/DESIGN.md §6) — `wake_sources`
    와 같은 것들의 id, 예약만 뺀다. 예약만 기다리는 `wait` 는 빈 집합이라 타이머를
    걸지 않고, 예약과 작업을 함께 기다리면 걸되 알림에는 작업·monitor·에이전트만
    적힌다."""
    return frozenset(
        i for i, _ in _wake_items(owner, agents=agents) if i != _SCHEDULE_ID
    )


# ── `wait` 중간 깨우기 알림 (v10.79.0, docs/wait/DESIGN.md §6) ─────────────
#
# 알림은 **사실 진술**이다 — "아직 돌고 있는 것" 을 적는다. 2판의 "nothing has
# reported yet" 은 메일함 상태에 달린 부재 진술이라 진짜 보고와 같은 순간에 오면
# 거짓이 됐고 경합 가드가 필요했다. 사실 진술은 겹쳐도 군더더기 한 줄일 뿐이다.
# 모델에게 "다음 확인" 의 주체는 하니스다("you are woken again") — "the next check
# is in 10m" 은 약한 모델이 "내가 확인해야 한다" 로 읽는다.

CHECKIN_HEAD = "⏰ Check-in after {span} — still running:"
CHECKIN_AGAIN = (
    "If {subj} making progress, call `wait` again; if nothing reports, you are "
    "woken again in {span}."
)
CHECKIN_STOP = (
    "If it is not, check its output files, or stop it ({stops}) and change the "
    "approach."
)
CHECKIN_STOP_MONITOR = (
    "If it is not, check the files it should have written, or delete the monitor "
    '(`monitor` with `mode` "delete") and change the approach.'
)
CHECKIN_AGENT_STUCK = (
    'An agent whose steps have stopped may be stuck: `agent` with `mode` "kill" '
    "ends it."
)
CHECKIN_QUESTION = "A question to the user is answered by the user, not by you."
MONITOR_DELETE_HINT = '`monitor` with `mode` "delete" for a monitor'


def wait_checkin(
    owner: str,
    *,
    interval_s: int,
    next_s: int,
    log_marks: dict[int, int],
    step_marks: dict[str, int],
    registry,
) -> str:
    """중간 깨우기 알림 본문 — 비어 있으면(아무것도 안 돌고 있음) 보내지 않는다.

    작업 줄은 경과 시간·명령·로그 변화(P(a): `JobRegistry.log_growth`), monitor 줄은
    조건과 남은 시간, 에이전트 줄은 레지스트리가 이미 아는 것(`checkin_agent_lines`).
    과제 시간은 하니스도 모른다 — 사실만 주고 판단은 모델이 한다."""
    lines: list[str] = []
    stops: list[str] = []
    jobs = get_job_registry()
    running = jobs.running(owner) if jobs is not None else []
    for j in running:
        lines.append(f"job #{j.id} · {j.elapsed()} · `{_short(j.command)}`")
        growth = jobs.log_growth(j, log_marks.get(j.id, jobs.log_size(j)))
        lines.extend(f"  {ln}" for ln in growth.splitlines())
    if len(running) == 1:
        stops.append(running[0].stop_hint)
    else:
        stops.extend(f"{j.stop_hint} for job #{j.id}" for j in running)
    mons = get_monitor_registry()
    now = time.time()
    had_monitor = False
    if mons is not None:
        for m in mons.list_all():
            if m.owner != owner:
                continue
            had_monitor = True
            left = format_duration(m.deadline_at - now)
            lines.append(f"monitor {m.id} · {m.cond.describe()} · {left} left")
    if had_monitor and running:
        stops.append(MONITOR_DELETE_HINT)
    agent_lines, question = (
        registry.checkin_agent_lines(owner, step_marks)
        if registry is not None
        else ([], False)
    )
    lines.extend(agent_lines)
    if not lines:
        return ""
    agents_only = not running and not had_monitor
    closing = [
        CHECKIN_AGAIN.format(
            subj="they are" if agents_only else "it is",
            span=format_duration(next_s),
        )
    ]
    if running:
        closing.append(CHECKIN_STOP.format(stops="; ".join(stops)))
    elif had_monitor:
        closing.append(CHECKIN_STOP_MONITOR)
    if agent_lines:
        closing.append(CHECKIN_AGENT_STUCK)
    if question:
        closing.append(CHECKIN_QUESTION)
    head = CHECKIN_HEAD.format(span=format_duration(interval_s))
    return "\n".join([head, *lines, " ".join(closing)])


def arrived_mail(owner: str, *, agents) -> int:
    """이 소유자 앞에 **이미 도착해 다음 턴 경계에 배달될** 메일 수 (v10.66.2) —
    `wait` 의 둘째 판정. 깨울 것(`wake_sources`)이 "아직 돌고 있는 것" 이라면 이건
    "끝나서 통지가 와 있는 것": 모니터가 wait 보다 먼저 발화한 벤치 사례가 그렇다.
    0 이 아니면 wait 는 거부되고 런이 이어져 그 통지를 바로 받는다."""
    return agents.arrived_for(owner) if agents is not None else 0


def harness_record(mail: dict) -> dict | None:
    """하니스 통지(잡·모니터·체크인) 한 통 → 관찰 레코드 — **한 계약** (v10.84.0 사용자 결정 C,
    v10.89.0 한 함수): 봉투 없이 보고 그대로, ``tool`` 은 `constants.HARNESS_MAIL_TOOL`
    (job·monitor·checkin→wait), ``source`` "harness_mail". main 의 `build_reply_record`·상주의
    `absorb_pending`·private 의 `mail_record` 가 모두 이것을 부른다 — 종전엔 같은 모양을
    세 곳이 각자 적었다. 하니스 통지가 아니면 None."""
    tool = harness_mail_tool(str(mail.get("kind") or ""))
    if tool is None:
        return None
    return {
        "role": "user",
        "tool": tool,
        "success": bool(mail.get("success", True)),
        "content": str(mail.get("output") or mail.get("text") or ""),
        "source": "harness_mail",
    }


def mail_record(mail: dict) -> dict:
    """private 메일함의 메일 한 통 → 루프가 그대로 넣는 흡수 항목(``{"record": …}``,
    `core._inject_item` 의 모양). 하니스 통지는 `harness_record`; 그 밖(지금은 없다)은 kind 를
    tool 로 — 빈 tool 은 형식 개입 표식이라 쓰지 않는다."""
    rec = harness_record(mail)
    if rec is None:
        kind = str(mail.get("kind") or "")
        rec = {
            "role": "user",
            "tool": kind or "mail",
            "success": bool(mail.get("success", True)),
            "content": str(mail.get("output") or mail.get("text") or ""),
            "source": "harness_mail",
        }
    return {"record": rec}


def _private_inbox(owner: str):
    """private 주소의 메일함을 턴 경계에 흡수하는 ``absorb_inbox`` 포트 (v10.84.0)."""

    def absorb() -> list[dict]:
        reg = get_agent_registry()
        if reg is None:
            return []
        return [mail_record(m) for m in reg.take_mail(owner)]

    return absorb


class Released(NamedTuple):
    """`release_owner` 의 결과 — 거둔 자원 수와 결과에 붙일 한 줄(없으면 "")."""

    count: int
    note: str


def release_owner(owner: str, *, jobs=None, monitors=None, registry=None) -> Released:
    """한 주소가 소유한 것을 전부 거둔다 — **한 함수** (v10.89.0; 종전 private 의
    `release_private_owner` 와 상주의 `_drop_monitors`+`_cancel_checkins` 두 구현).
    돌고 있는 잡은 죽이고(결과에 알린다), 모니터는 폐기하고(재부모화하지 않는다),
    ``registry`` 가 있으면 메일함·체크인을 버린다(읽을 사람이 없다).
    ``jobs``·``monitors`` 는 레지스트리가 주입받은 것을 넘길 수 있고(테스트 DI) 없으면 전역."""
    count = 0
    parts: list[str] = []
    jobs = jobs if jobs is not None else get_job_registry()
    if jobs is not None:
        running = jobs.running(owner)
        try:
            n = jobs.drop_owner(owner)  # 돌던 것은 죽이고, 끝난 기록도 거둔다
        except Exception:
            n = 0
        count += n
        if running:
            cmds = "; ".join(f"`{j.command[:60]}`" for j in running)
            k = len(running)
            parts.append(
                f"{k} background job{'s' if k != 1 else ''} still running "
                f"{'were' if k != 1 else 'was'} stopped when this agent finished: {cmds}"
            )
    monitors = monitors if monitors is not None else get_monitor_registry()
    if monitors is not None:
        try:
            n = monitors.drop_owner(owner)
        except Exception:
            n = 0
        count += n
        if n:
            parts.append(
                f"{n} monitor{'s' if n != 1 else ''} it set {'were' if n != 1 else 'was'} dropped"
            )
    if registry is not None:
        registry.drop_mailbox(owner)  # 체크인 취소 포함
    return Released(count, f"[{'; '.join(parts)}]" if parts else "")
