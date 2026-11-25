"""메일 테스트 대역 — main 메일함에 메일을 넣는 공개 길.

작업·모니터·감사가 main 에게 보내는 것과 같은 길(`AgentRegistry.deliver`)이다. 테스트가 레지스트리의
안쪽 저장 함수를 부르지 않게 한다.
"""

from __future__ import annotations

from agent_cli.session_slots import get_agent_registry
from agent_cli.wake import WaitingOn, schedule_wakes, wake_items


def to_main(reg, mail: dict) -> str:
    """``mail`` 을 main 메일함으로 배달한다 — 에러 문자열 또는 ""."""
    return reg.deliver("main", mail=mail, text="", author="")


def absorbed_mail(reg) -> list[dict]:
    """main 의 턴 경계가 지금 흡수할 메일 — 꺼내 돌려준다(`AgentRegistry.absorb_main` 의 메일 항목, 도착 순)."""
    return [it["mail"] for it in reg.absorb_main() if "mail" in it]


def agent_wake_lines(reg, addr: str) -> list[str]:
    """``addr`` 를 깨울 에이전트 활동의 줄 — `wait` 수락 문장의 에이전트 조각(`wake.relation_items`, 레지스트리 사본)."""
    from agent_cli.wake import relation_items

    return [text for _, text in relation_items(reg.wake_facts(), addr)]


def checkin_ids(owner: str, agents=None) -> frozenset[str]:
    """``owner`` 가 지금 `wait` 하면 걸릴 체크인 대상 — 받아들여진 `wait` 의 값(`WaitingOn.checkin_ids`)과 같은 계산을
    표시 없이(`Mailbox.accept_wait` 는 기다림 표시를 세운다). ``agents`` 가 없으면 슬롯의 레지스트리."""
    reg = agents if agents is not None else get_agent_registry()
    items = wake_items(owner, agents=reg, with_schedule=schedule_wakes(owner))
    return WaitingOn(tuple(items), ()).checkin_ids
