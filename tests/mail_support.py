"""메일 테스트 대역 — main 메일함에 메일을 넣는 공개 길.

작업·모니터·감사가 main 에게 보내는 것과 같은 길(`AgentRegistry.deliver`)이다. 테스트가 레지스트리의
안쪽 저장 함수를 부르지 않게 한다.
"""

from __future__ import annotations


def to_main(reg, mail: dict) -> str:
    """``mail`` 을 main 메일함으로 배달한다 — 에러 문자열 또는 ""."""
    return reg.deliver("main", mail=mail, text="", author="", expects_reply=False)


def agent_wake_lines(reg, addr: str) -> list[str]:
    """``addr`` 를 깨울 에이전트 활동의 줄 — `wait` 수락 문장의 에이전트 조각(`wake.relation_items`, 레지스트리 사본)."""
    from agent_cli.wake import relation_items

    return [text for _, text in relation_items(reg.wake_facts(), addr)]
