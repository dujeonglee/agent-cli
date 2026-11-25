"""사람에게 건 확인 — 항목 하나(`c-N`)와 그 어휘, 답하는 명령 하나 (6d-1).

확인은 **도구 호출 하나가 그 자리에서** 사람의 결정을 기다리는 일이다(위험 셸·작업 공간 밖이나 민감한 경로·제자리
고리 멈춤). 질문(`q-N`)과 같은 장부의 줄(``"c"``)이고 같은 번호 공간을 쓴다(`item_ids`). 기다림·답·목록은
`subagent.confirms.ConfirmsMixin`(에이전트 레지스트리)이 한다 — 여기는 그 항목의 모양과 부르는 문이다.

규칙 하나: 답은 번호를 적은 명령(``/approve c-N [note]``·``/allow c-N [note]``·``/deny c-N [reason]``)이나 웹 트레이의
버튼으로만 온다. 친 글자를 답으로 해석하지 않는다 — 그 밖의 입력은 언제나 main 의 요청이다. 명령은 들어오는 문에서 바로
처리한다(`answer_command` — 터미널의 `main.read_terminal_line`, 웹 입력창의 `/api/input`, 그리고 큐를 지난 `run` 의
argv 질의가 같은 함수를 부른다). 기다리는 호출을 깨우는 것은 셋 — 답, 그 런의 정지, 세션 종료(정지가 선다).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from agent_cli import item_ids, session_slots
from agent_cli import render as render_surface

APPROVE = "approve"
ALLOW = "allow"
DENY = "deny"
#: 답이 아니라 기다리던 호출이 떠났다 — 그 런이 멈췄거나 세션이 끝났다
STOPPED = "stopped"

#: 기다리는 동안 정지를 보는 간격(초) — 전경 `shell` 의 ``STOP_POLL_S`` 와 같은 값
STOP_POLL_S = 0.2

#: 기다리던 중 런이 멈췄다 — 그 도구 호출의 결과(모델이 읽는다, 일곱 도구 공통)
STOPPED_WHILE_WAITING = "This run was stopped while this call was waiting for approval [{cid}] — it did not run."

#: 명령 이름 → 답
ACTION_OF = {"/approve": APPROVE, "/allow": ALLOW, "/deny": DENY}
USAGE = {
    "/approve": "usage: /approve <c-N> [note]",
    "/allow": "usage: /allow <c-N> [note]",
    "/deny": "usage: /deny <c-N> [reason]",
}
DONE = {APPROVE: "approved {cid}", ALLOW: "allowed {cid}", DENY: "denied {cid}"}
UNKNOWN = 'unknown or already-answered confirmation "{cid}"'
NOT_A_PERSON = 'confirmation "{cid}" is addressed to the person, not "{by}"'
#: 선택지에 always 가 없는 확인(고리 멈춤)에 ``/allow`` — approve·deny 는 모든 확인에 있다
NO_ALWAYS = 'confirmation "{cid}" has no always option'
#: 터미널 확인 줄의 답하는 법 — 선택지에 있는 것만, 이 순서로
HINT = {
    APPROVE: "approve with: /approve {cid}",
    ALLOW: "always: /allow {cid}",
    DENY: "deny with: /deny {cid} <reason>",
}


@dataclass(frozen=True)
class Choice:
    """선택지 하나 — 답(``APPROVE``·``ALLOW``·``DENY``)과 웹 버튼의 글."""

    action: str
    label: str


@dataclass(frozen=True)
class Decision:
    """확인이 끝난 값 — 답이면 ``by`` 는 답한 사람의 주소(``user``·``user:<별명>``), 정지면 ``STOPPED`` 와 빈 칸."""

    action: str
    note: str = ""
    by: str = ""


@dataclass
class Confirm:
    """열린 확인 하나 — 장부 c 줄의 ``meta``. ``decision`` 은 답이 줄을 닫으며 채운다(창구의 조건변수 아래).
    ``provenance`` 는 묻는 스레드의 출처(`Renderer.prompt_meta` — 웹 트레이의 메타 블록)."""

    id: str
    asker: str  # "main" · 에이전트 키 · 그 밖의 소유자(harness:…)
    title: str
    detail: str
    choices: tuple[Choice, ...]
    command: str | None = None
    danger_spans: list[tuple[int, int]] = field(default_factory=list)
    provenance: dict = field(default_factory=dict)
    asked_at: float = field(default_factory=time.time)
    decision: Decision | None = None

    def answer_hint(self) -> str:
        """터미널 확인 줄의 마지막 줄 — 이 확인에 답하는 명령들."""
        have = {ch.action for ch in self.choices}
        return " · ".join(HINT[a].format(cid=self.id) for a in HINT if a in have)


def safety_choices(once: str, always: str) -> tuple[Choice, ...]:
    """안전 확인(위험 셸·경로)의 선택지 — 웹 버튼 순서 once · deny · always."""
    return (Choice(APPROVE, once), Choice(DENY, "deny"), Choice(ALLOW, always))


def answerable() -> bool:
    """답이 올 수 있는가 — 표면이 답을 받을 수 있고(`Renderer.can_prompt`) 확인을 걸 장부(에이전트 레지스트리)가
    있다. 거짓이면 호출자는 묻지 않고 거절한다."""
    return (
        render_surface.get_renderer().can_prompt()
        and session_slots.get_agent_registry() is not None
    )


def ask_person(
    title: str,
    detail: str,
    choices: tuple[Choice, ...],
    *,
    owner: str,
    stopped,
    command: str | None = None,
    danger_spans: list[tuple[int, int]] | None = None,
) -> tuple[str, Decision]:
    """``c-N`` 을 열고 그 자리에서 답·정지를 기다린다 — ``(c-N, Decision)``. `answerable` 이 참일 때만 부른다.
    ``owner`` 는 묻는 루프의 소유자(``main``·``agent:<키>``·그 밖), ``stopped`` 는 그 런의 정지를 읽는 콜러블
    (`RunContext.stopped`, 없으면 None)."""
    c = Confirm(
        id=item_ids.next_id(item_ids.CONFIRM),
        asker=owner.removeprefix("agent:"),
        title=title,
        detail=detail,
        choices=choices,
        command=command,
        danger_spans=list(danger_spans or []),
    )
    decision = session_slots.get_agent_registry().ask_confirm(
        c, stopped=stopped, wait_input=session_slots.get_confirm_wait_input()
    )
    return c.id, decision


def stopped_while_waiting(cid: str) -> str:
    return STOPPED_WHILE_WAITING.format(cid=cid)


def answer_command(message: str, registry, author: str) -> tuple[str, bool]:
    """``/approve|/allow|/deny <c-N> [말]`` 하나 — ``(사람에게 보일 한 줄, 성공)``. 번호가 없으면 사용법만 말한다:
    확인이 하나뿐이어도 짐작해 답하지 않는다(답은 번호를 적은 명령이나 트레이로만 — 사용자 결정)."""
    parts = message.split(maxsplit=2)
    name = parts[0]
    if registry is None:
        return "(no agent registry in this mode)", False
    if len(parts) < 2:
        return USAGE[name], False
    cid = parts[1]
    action = ACTION_OF[name]
    error = registry.answer_confirm(
        cid, action, parts[2] if len(parts) > 2 else "", by=author
    )
    if error:
        return f"{name}: {error}", False
    return DONE[action].format(cid=cid), True
