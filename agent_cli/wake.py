"""한 주소를 무엇이 깨우는가와 그 주소가 무엇을 받는가 — 세션 슬롯만 본다, 조립은 모른다.

`wait` 의 깨울 것 판정(`wake_items` — 도착분은 창구의 `PostOffice.arrived`)과 중간 깨우기 알림
(`wait_checkin`), 하니스 작업 판정(`harness_work_running`), 메일 한 통 → 관찰 레코드
(`harness_record`·`mail_record`·`_private_inbox`), 한 주소가 가진 것 거두기(`release_owner`).
읽는 것은 세션 슬롯(`session_slots` — 잡·모니터·예약·에이전트 레지스트리)과 `constants`·`jobs`
의 작은 함수뿐이다 — 세션 조립(`runtime`)은 이것을 쓰고, 이것은 조립을 모른다.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, NamedTuple

from agent_cli.constants import (
    ARRIVAL_NOTICE,
    ARRIVAL_USER,
    INPUT_IS_COMMAND,
    SCHEDULE_ADDR_PREFIX,
    format_duration,
    harness_mail_tool,
)
from agent_cli.item_ids import number_of
from agent_cli.jobs import _short
from agent_cli.post.envelope import is_human_words
from agent_cli.session_slots import (
    get_agent_registry,
    get_job_registry,
    get_monitor_registry,
    get_schedule_registry,
)

if TYPE_CHECKING:
    from agent_cli.subagent.agents_live import AgentRegistry

#: 예약의 깨울 것 id
_SCHEDULE_ID = "schedule"


# ── 에이전트 관계 — 장부·명단의 사본 하나에서 (v10.146.0, 단일 구조 3단계) ─────────────
#
# "누가 이 주소를 깨우는가" 의 에이전트 몫은 레지스트리의 사본(`AgentRegistry.wake_facts` — 락 한 번)만 읽는
# 순수 함수다. main·상주·run·하니스 주소가 같은 함수를 지난다 — 주소 종류로 갈리는 것은 "잠든 채무자는 에이전트
# 뿐" 하나다(`parked_debtors`).


class AgentFact(NamedTuple):
    """명단의 에이전트 하나 — 사본. ``queued``·``taken`` 은 항목의 ``(author, id)`` 이다(``queued`` 는 깨우지 않는
    봉투도 든다 — 관계 "queued" 는 내가 보낸 갚지 않은 요청만 본다)."""

    addr: str
    active: bool
    dead: bool
    queued: tuple[tuple[str, str], ...]
    taken: tuple[tuple[str, str], ...]


class WakeFacts(NamedTuple):
    """장부와 명단의 사본 — ``agents`` 는 명단 순서, ``owing`` 은 집힌 r 줄의 ``(빚진 쪽, 요청자)`` 장부 순서,
    ``awaited`` 는 닫히지 않은 r 줄의 ``(요청자, id)``, ``waiting`` 은 창구의 기다림 표시가 선 주소들
    (`PostOffice.waiting_addresses` — 받아들여진 `wait` 로 기다리는 쪽). ``replies`` 는 그중 받는 쪽이 살아 있는
    줄의 ``(요청자, {"id", "from", "text"})`` — 기다리는 회신(`awaited_of`)의 원본이다."""

    agents: tuple[AgentFact, ...]
    owing: tuple[tuple[str, str], ...]
    awaited: frozenset[tuple[str, str]]
    waiting: frozenset[str] = frozenset()
    replies: tuple[tuple[str, dict], ...] = ()


def awaited_of(facts: WakeFacts, addr: str) -> list[dict]:
    """``addr`` 가 보내 놓고 아직 받지 못한 회신 — ``[{"id", "from", "text"}]``, 번호 순(`AgentRegistry.awaited_replies`
    와 받아들여진 `wait` 의 대기 줄이 같은 사본에서 읽는다)."""
    return sorted(
        (dict(r) for src, r in facts.replies if src == addr),
        key=lambda r: number_of(r["id"]),
    )


def _debtors_owing(facts: WakeFacts, addr: str) -> list[str]:
    """``addr`` 에게 갚지 않은 회신을 빚진 쪽들(``addr`` 자신·죽은 에이전트 빼고) — 빚진 쪽의 첫 열린 줄 순서. 죽은
    채무자는 회신을 보낼 수 없어 깨울 것이 아니다(5b 항목 8 — 그 빚은 세션 종료의 정리 단계에서만 남는다, 재현 r2)."""
    dead = {a.addr for a in facts.agents if a.dead}
    order: list[str] = []
    for debtor, _ in facts.owing:
        if debtor not in order:
            order.append(debtor)
    owes = set(facts.owing)
    return [d for d in order if d != addr and d not in dead and (d, addr) in owes]


def _is_parked(facts: WakeFacts, debtor: str) -> bool:
    """빚진 쪽이 **스스로 잠들어 있다** — 창구의 기다림 표시가 섰고(받아들여진 `wait`), main 이거나 명단에 있는 죽지
    않은 에이전트(5b). 그런 채무자는 그 자체로는 아무도 깨우지 못한다: 그를 깨울 것이 있어야 회신이 온다. 받은편지함에
    깨우지 않는 봉투가 있어도 잠든 쪽이다 — 그 봉투는 그를 깨우지 않는다."""
    if debtor not in facts.waiting:
        return False
    if debtor == "main":
        return True
    fact = next((a for a in facts.agents if a.addr == debtor), None)
    return fact is not None and not fact.dead


def parked_debtors(facts: WakeFacts, addr: str) -> list[str]:
    """``addr`` 에게 회신을 빚진 채 잠든 에이전트 주소들 (v10.77.0) — 그들을 깨울 것이 있는지는 `parked_view` 가
    거슬러 본다. 서로 `message` 하고 둘 다 `wait` 하면 아무도 깨지 않는 교착을 여기서 끊는다."""
    return [d for d in _debtors_owing(facts, addr) if _is_parked(facts, d)]


def _holds_unpaid_request_from(
    facts: WakeFacts, items: tuple[tuple[str, str], ...], addr: str
) -> bool:
    """``items`` 가운데 ``addr`` 가 보낸 요청으로 아직 회신이 가지 않은 것이 있는가 (v10.143.0, 사용자 결정) — 이미
    회신한 요청·통지로는 그 에이전트가 무엇을 해도 ``addr`` 에게 돌아올 것이 없다."""
    return any(author == addr and (addr, rid) in facts.awaited for author, rid in items)


def agent_relations(
    facts: WakeFacts, addr: str, exclude: frozenset = frozenset()
) -> list[tuple[str, str]]:
    """``addr`` 를 깨울 에이전트들과 그 관계 — ``("owes"|"working"|"queued", 주소)`` (v10.65.0). 빚진 채 **스스로
    잠든** 쪽은 여기 없다 — `parked_debtors` 가 따로 들고 `parked_view` 가 거슬러 본다. ``exclude`` (v10.77.0):
    순환 대기 판정에서 거슬러 온 주소들. **이미 도착한** 메일은 여기 안 든다(v10.66.2) — 그건 `Mailbox.arrivals`.

    한 에이전트는 한 관계로만 나온다 — working > queued > owes: 내 요청을 들고 일하는(줄 세운) 쪽은 그 요청을 빚진
    쪽이기도 하지만 더 많이 말하는 쪽 하나로 센다.

    기다림 표시가 선 쪽은 working 이 아니다 — 잠든 채무자 길(`_is_parked`·`parked_view`)로 간다(5c-1: 제자리에서
    기다리는 에이전트도 런 안이라 ``active`` 다)."""
    held: dict[str, str] = {}
    for a in facts.agents:
        if a.addr == addr or a.addr in exclude:
            continue
        if (
            a.active
            and a.addr not in facts.waiting
            and _holds_unpaid_request_from(facts, a.taken, addr)
        ):
            held[a.addr] = "working"
        elif _holds_unpaid_request_from(facts, a.queued, addr):
            held[a.addr] = "queued"  # 받은편지함에 줄 선 것으로 본다
    owes = [
        ("owes", debtor)
        for debtor in _debtors_owing(facts, addr)
        if debtor not in exclude
        and debtor not in held
        and not _is_parked(facts, debtor)
    ]
    return [*owes, *((kind, me) for me, kind in held.items())]


#: 관계 → `wait` 수락 문장의 한 조각
_RELATION_TEXT = {
    "owes": "{me} owes you a reply",
    "working": "{me} working on your request",
    "queued": "{me} has your request queued",
}


def relation_items(
    facts: WakeFacts, addr: str, exclude: frozenset = frozenset()
) -> list[tuple[str, str]]:
    """`agent_relations` 를 ``(에이전트 주소, 사람이 읽을 줄)`` 로 (v10.79.0) — 주소는 중간 깨우기의 대상 id 가 된다."""
    return [
        (me, _RELATION_TEXT[kind].format(me=me))
        for kind, me in agent_relations(facts, addr, exclude)
    ]


def wake_items(
    owner: str,
    _seen: frozenset = frozenset(),
    *,
    agents,
    with_schedule: bool,
    facts: WakeFacts | None = None,
) -> list[tuple[str, str]]:
    """이 소유자를 **깨울 수 있는 것** — ``(id, 사람이 읽을 줄)`` 쌍 (v10.65.0·v10.79.0, docs/wait/DESIGN.md).
    `wait` 의 수락·제자리 대기의 "아직 기다릴 것"(`Mailbox.wake_sources`)과 받아들여진 `wait` 의 값(`Mailbox.accept_wait` → `WaitingOn`)이
    이 하나를 묻는다.

    돌고 있는 백그라운드 작업·살아 있는 모니터는 소유자 것만, 에이전트 활동은 **요청자별**(`agent_relations` —
    세션 전체 `has_agent_work` 로 보면 상주 에이전트 자신이 busy 라 늘 참이 된다), 예약은 ``with_schedule`` 일
    때(세션 것 — 근거인지는 `schedule_wakes`). 순환 대기(v10.77.0): 나에게 회신을 빚진 채 **스스로 잠든**
    에이전트는 그를 깨울 것이 있을 때만 센다 — 거슬러 올라가며(``_seen`` 으로 순환을 자른다) A→B→A 고리면 두
    번째로 잠들려는 쪽의 `wait` 가 거부되고, A→B→(일하는 C) 사슬이면 받아들여진다.

    id 는 ``"job:1"``·``"mon:mon-504d33"``·``"agent:k"``·``"schedule"`` — 중간
    깨우기가 "같은 대상을 다시 기다리는가" 를 이 집합으로 비교한다. 사람이 읽는
    줄로 비교하면 같은 에이전트라도 "queued↔working" 처럼 글이 바뀌어 헛되이
    5분으로 돌아간다.

    에이전트 몫은 레지스트리의 사본(``facts`` — 없으면 맨 위에서 한 번 뜬다)을 재귀 내내 함께 쓴다. ``agents`` 는
    에이전트 레지스트리(없으면 None) — 부르는 쪽이 넘긴다(v10.133.26)."""
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
        facts = facts if facts is not None else agents.wake_facts()
        out.extend(relation_items(facts, owner, _seen))
        for debtor, waits_on in parked_view(
            owner, _seen | {owner}, agents=agents, facts=facts
        ):
            if waits_on:
                out.append(
                    (debtor, f"{debtor} owes you a reply (it is waiting on others)")
                )
    sched = get_schedule_registry()
    if with_schedule and sched is not None and sched.has_active_work():
        out.append((_SCHEDULE_ID, "a schedule"))
    return out


def schedule_wakes(owner: str) -> bool:
    """켜진 예약이 이 주소의 `wait` 를 받아들일 근거인가 — main 만. 예약은 세션 것이라 main 에게 발화한다(사람의
    말로 main 앞에 선다). 에이전트·private 주소(one-shot·하니스 작업)의 근거로 세면 예약이 그를 깨우지 않는데도
    `wait` 가 받아들여져 막힌다 (v10.84.0 private, 5b 변화 8 에이전트)."""
    return owner == "main"


def parked_view(
    owner: str, seen: frozenset, *, agents, facts: WakeFacts
) -> list[tuple[str, list[str]]]:
    """고리 찾기의 걸음 하나 — ``owner`` 에게 빚진 채 잠든 채무자마다 ``(주소, 그를 깨울 것의 줄)`` (v10.77.0). ``seen``
    (거슬러 온 주소들, ``owner`` 포함)에 든 채무자는 뺀다 — 고리다, 나를 기다리는 쪽은 나를 깨우지 못한다. 그를 깨울
    것은 ``seen`` 을 뺀 채 다시 묻는다. `wait` 의 판정(`wake_items`)과 중간 깨우기의 에이전트 줄
    (`AgentRegistry.checkin_agent_lines`)이 같은 이 걸음을 쓴다."""
    return [
        (
            debtor,
            [
                text
                for _, text in wake_items(
                    debtor,
                    seen,
                    agents=agents,
                    with_schedule=schedule_wakes(debtor),
                    facts=facts,
                )
            ],
        )
        for debtor in parked_debtors(facts, owner)
        if debtor not in seen
    ]


def harness_work_running(owner: str) -> bool:
    """이 소유자의 하니스 작업이 도는 중인가 — 돌고 있는 `shell background` 작업이나 살아 있는
    모니터 (v10.120.0). StaleGuard 가 그 턴을 제자리로 세지 않는다: 작업을 기다리며 같은 로그를
    다시 보는 것은 고리가 아니다(오프라인 대입의 폴링 오탐 — apt-get 을 tail 로 반복해 봄)."""
    jobs = get_job_registry()
    if jobs is not None and jobs.running(owner):
        return True
    mons = get_monitor_registry()
    return mons is not None and any(m.owner == owner for m in mons.list_all())


def _short_wake_name(wake_id: str) -> str:
    """`wake_items` 의 id 하나를 대기 줄의 짧은 이름으로 — ``job:1`` → "job #1", ``mon:m-2`` → "monitor m-2",
    ``schedule`` → "a schedule". 에이전트 몫(``agent:…``)은 회신이 말하므로 빈 문자열."""
    kind, _, rest = wake_id.partition(":")
    if kind == "job":
        return f"job #{rest}"
    if kind == "mon":
        return f"monitor {rest}"
    if wake_id == _SCHEDULE_ID:
        return "a schedule"
    return ""


class WaitingOn(NamedTuple):
    """받아들여진 `wait` 가 **무엇을 기다리기로 했나** — 받아들일 때 한 번 읽은 값(5c-1 4b). 수락 문장·대기 줄·체크인
    대상이 모두 이 하나에서 나온다(규칙: 무엇을 기다리기로 했나는 받아들일 때 한 번, 아직 살아 있나는 기다리는 동안
    매번 — `Mailbox.wake_sources`). ``items`` 는 `wake_items` 의 ``(id, 사람이 읽을 줄)``, ``awaited`` 는 같은 사본의
    기다리는 회신(`awaited_of`)."""

    items: tuple[tuple[str, str], ...]
    awaited: tuple[dict, ...]

    @property
    def sources(self) -> list[str]:
        """수락 문장의 "will wake on: …" 조각들."""
        return [text for _, text in self.items]

    @property
    def others(self) -> list[str]:
        """대기 줄의 회신 아닌 것의 짧은 이름(결정 C) — 회신은 ``awaited`` 가 말한다."""
        return [short for wid, _ in self.items if (short := _short_wake_name(wid))]

    @property
    def checkin_ids(self) -> frozenset[str]:
        """중간 깨우기의 **대상 집합** (v10.79.0, docs/wait/DESIGN.md §6) — 예약 없이. 예약만 기다리는 `wait` 는 빈
        집합이라 타이머를 걸지 않고, 예약과 작업을 함께 기다리면 걸되 알림에는 작업·monitor·에이전트만 적힌다."""
        return frozenset(wid for wid, _ in self.items if wid != _SCHEDULE_ID)


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
    agent_lines, agents, question = (
        registry.checkin_agent_lines(owner, step_marks)
        if registry is not None
        else ([], False, False)
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
    if agents:
        closing.append(CHECKIN_AGENT_STUCK)
    if question:
        closing.append(CHECKIN_QUESTION)
    head = CHECKIN_HEAD.format(span=format_duration(interval_s))
    return "\n".join([head, *lines, " ".join(closing)])


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
    """private 주소의 메일함을 턴 경계에 흡수하는 함수 — 메일함 포트의 ``take`` (v10.84.0)."""

    def absorb() -> list[dict]:
        reg = get_agent_registry()
        if reg is None:
            return []
        return [mail_record(m) for m in reg.take_mail(owner)]

    return absorb


def _is_queued_command(env) -> bool:
    """들어올 때 명령으로 표시된 사람의 말인가(`MainInput.enqueue` 의 표시) — 모델의 우편이 아니다."""
    return is_human_words(env) and bool(env.payload.get(INPUT_IS_COMMAND))


def _arrival_kind(env) -> str:
    """도착한 봉투 하나의 종류 — `Mailbox.arrivals`."""
    if not is_human_words(env):
        return ARRIVAL_NOTICE
    if env.src.startswith(SCHEDULE_ADDR_PREFIX):
        sid = env.src[len(SCHEDULE_ADDR_PREFIX) :]
        sched = get_schedule_registry()
        s = sched.get(sid) if sched is not None else None
        return SCHEDULE_ADDR_PREFIX + ((s.label or s.id) if s is not None else sid)
    return ARRIVAL_USER


class Mailbox:
    """주소 하나에 묶인 에이전트 레지스트리의 좁은 창 — 루프 포트 ``mailbox`` (v10.133.27).

    엔진이 우편 일(깨울 것 판정·도착 메일·체크인 타이머·제자리 기다림·메일함 버리기·
    받은편지함 흡수)을 할 때 쓰는 것은 이것뿐이다. 레지스트리 전체(`agent_registry` 포트)는
    **권한**이다 — 팀원을 만들 수 있는 루프(main)에만 값이 있다. 우편은 모든 루프에 필요하다.

    ``registry`` 는 None 일 수 있다(레지스트리 없는 세션) — 그때 각 메서드는 레지스트리가 없을
    때 엔진이 하던 값을 돌려준다(0·[]·None·"nothing"·아무것도 안 함). 그래서 포트 값은 언제나
    Mailbox 다: 미연결이 아니라 "연결됐고 저쪽에 아무도 없음". 종류가 포트를 만들 때 한 번
    정해진다. ``take`` 는 턴 경계에 흡수할 받은편지함 — 있는 루프(에이전트·private)만 준다.
    ``wait_input`` 은 ``(timeout) -> bool`` — 사람 입력이 도착했는가(꺼내지 않는다). main 만
    준다: 기다리는 회신을 둔 제자리 대기(`wait`)를 사람의 말이 깨운다 (v10.141.0) — 기다리는 **도중** 온 사람의
    말은 그것이 맡는다(줄 선 명령은 그 자리에서 라우팅하고 깨우지 않는다, run 의 터미널 줄은 거기서만 읽힌다).
    ``closed`` 는 ``() -> bool`` — 세션이 끝나는 중인가(main 상자가 닫혔다, `MainInput.closed`). main 만 준다: 닫히면
    제자리 대기가 곧바로 끝나고 `wait` 가 ``"closed"`` 를 돌려준다(5c-pre)."""

    def __init__(
        self,
        registry: AgentRegistry | None,
        owner: str,
        *,
        take=None,
        wait_input=None,
        closed=None,
    ) -> None:
        self._registry = registry
        self.owner = owner
        self._take = take
        self._wait_input = wait_input
        self._closed = closed

    @property
    def has_inbox(self) -> bool:
        """턴 경계에 흡수할 받은편지함이 있는가."""
        return self._take is not None

    def take(self) -> list:
        """받은편지함을 비워 흡수 항목으로 — ``has_inbox`` 일 때만 부른다."""
        return self._take()

    def arrivals(self) -> tuple[str, ...]:
        """이 주소 앞에 **이미 도착해 다음 턴 경계에 배달될** 깨우는 봉투가 무엇인지 — 도착 순서로 봉투마다
        ``"user"``(사람의 말)·``"schedule:<이름>"``(예약 발화, 이름은 label 이나 id)·``"notice"``(그 밖 — 회신·요청·
        통지·하니스 알림). `wait` 의 둘째 판정(v10.66.2): 깨울 것이 "아직 돌고 있는 것" 이라면 이건 "끝나서 와 있는
        것"(모니터가 `wait` 보다 먼저 발화한 벤치 사례) — 비지 않았으면 `wait` 는 거부되고 런이 이어져 그것을 바로
        받는다. 깨우는 봉투만 본다(5b) — 깨우지 않는 봉투("Stop everything" 의 정지 회신)는 다음에 깰 때 함께 읽힌다.
        사람의 말도 깨우는 봉투다 — main·에이전트가 같다(5b A-4). 들어올 때 명령으로 표시된 말은 세지 않는다(5b-3 —
        하니스가 실행하고 모델에게 가지 않는다). 글은 `loop.wait_sentences` 가 짓는다."""
        if self._registry is None:
            return ()
        envs = self._registry.arrivals_for(self.owner, waking_only=True, words=True)
        return tuple(_arrival_kind(e) for e in envs if not _is_queued_command(e))

    def _mail_arrived_while_waiting(self) -> int:
        """제자리 대기를 "mail" 로 깨우는 도착분 — `arrived` 와 같되, 사람 입력을 기다리는 루프(``wait_input``,
        main)면 사람의 말은 빼고 센다: 기다리는 도중 온 사람의 말은 ``wait_input`` 이 맡는다(명령은 그 자리에서
        라우팅하고 깨우지 않는다 — 세면 줄 선 명령이 기다림을 깨운다)."""
        if self._registry is None:
            return 0
        return self._registry.arrived_for(
            self.owner, waking_only=True, words=self._wait_input is None
        )

    def wake_sources(self) -> list[str]:
        """이 주소를 깨울 것 — 사람이 읽을 한 줄씩(`wake_items`, 예약은 `schedule_wakes`)."""
        return [
            text
            for _, text in wake_items(
                self.owner,
                agents=self._registry,
                with_schedule=schedule_wakes(self.owner),
            )
        ]

    def run_started(self) -> None:
        if self._registry is not None:
            self._registry.run_started(self.owner)

    def arm_checkin(self, ids: frozenset[str]) -> None:
        """체크인 타이머를 건다 — 대상은 받아들일 때 읽은 것(`WaitingOn.checkin_ids`)."""
        if self._registry is not None:
            self._registry.arm_checkin(self.owner, ids)

    def wait(self, *, stop_event, still_waiting) -> str:
        """메일함에 통지가 오거나 사람 입력이 도착할 때까지 막는다 — 깬 이유를 돌려준다
        (`AgentRegistry.wait_mail`). 레지스트리가 없으면 기다릴 메일함이 없다. ``closed`` 가 있으면 세션이 끝나는 것도
        기다림을 끝낸다 — 그때 이유는 ``"closed"``."""
        if self._registry is None:
            return "nothing"
        closed = self._closed
        why = self._registry.wait_mail(
            arrived=self._mail_arrived_while_waiting,
            stop_event=stop_event,
            still_waiting=(
                still_waiting
                if closed is None
                else (lambda: still_waiting() and not closed())
            ),
            wait_input=self._wait_input,
        )
        return "closed" if closed is not None and why == "nothing" and closed() else why

    def awaited(self) -> list[dict]:
        """이 주소가 보내 놓고 아직 받지 못한 회신 (v10.141.0) — `AgentRegistry.awaited_replies`."""
        if self._registry is None:
            return []
        return self._registry.awaited_replies(self.owner)

    def next_checkin_s(self, ids: frozenset[str]) -> int | None:
        if self._registry is None:
            return None
        return self._registry.next_checkin_s(self.owner, ids)

    def parked_debtors(self) -> list[str]:
        """이 주소에게 빚진 채 잠든 에이전트 — `wait` 거절 문장의 주어(`parked_debtors`)."""
        if self._registry is None:
            return []
        return parked_debtors(self._registry.wake_facts(), self.owner)

    def debts_to(self, addrs: list[str]) -> list[str]:
        """이 주소가 ``addrs`` 에게 갚지 않은 회신의 id — 장부 순서(`AgentRegistry.reply_debts`). 읽기만 한다."""
        if self._registry is None:
            return []
        wanted = set(addrs)
        return [
            d.id
            for d in self._registry.reply_debts(self.owner)
            if d.to in wanted and not d.settled
        ]

    def accept_wait(self) -> WaitingOn | None:
        """`wait` 의 받아들임 판정 — 창구에 이 주소의 기다림 표시를 **먼저** 세우고 깨울 것을 묻는다. 있으면 무엇을
        기다리기로 했나(`WaitingOn` — 깨울 것과 기다리는 회신을 레지스트리의 사본 하나에서, 5c-1 4b), 없으면 표시를
        내리고 None(거절). 표시를 판정 앞에 세우므로, 서로 빚진 둘이 같은 때 기다리려 해도 둘 다 받아들여지는 일이
        없다 — 늦게 묻는 쪽은 먼저 표시한 쪽을 잠든 채무자로 본다(5b, 시안 §3.6)."""
        reg = self._registry
        if reg is not None:
            reg.mark_waiting(self.owner)
        facts = reg.wake_facts() if reg is not None else None
        items = wake_items(
            self.owner,
            agents=reg,
            with_schedule=schedule_wakes(self.owner),
            facts=facts,
        )
        if not items:
            if reg is not None:
                reg.clear_waiting(self.owner)
            return None
        awaited = awaited_of(facts, self.owner) if facts is not None else []
        return WaitingOn(tuple(items), tuple(awaited))

    def clear_waiting(self) -> None:
        """기다림이 끝났다 — 제자리 대기가 돌아왔다(`TurnDispatcher._wait_in_place` 의 ``finally``, 5c-1 부터 내리는 곳은
        그 하나)."""
        if self._registry is not None:
            self._registry.clear_waiting(self.owner)

    def drop(self) -> None:
        """메일함과 걸린 체크인을 버린다 — 런이 끝나 읽을 사람이 없다."""
        if self._registry is not None:
            self._registry.drop_mailbox(self.owner)


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
