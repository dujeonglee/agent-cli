"""테스트용 `LoopPorts` 팩토리.

`LoopPorts` 는 **기본값이 없다** — 조립 지점이 포트를 빠뜨리면 그 자리에서
`TypeError` 가 나는 것이 설계의 전부다(docs/wiring/DESIGN.md §4.2). 그
강제는 **프로덕션 빌더**(`agent_cli/runtime.py` 의 다섯)에 걸려야 의미가
있고, 거기엔 뒷문이 없다.

테스트는 다르다. 190곳의 루프 생성 지점 중 포트를 실제로 쓰는 것은 17곳
뿐이라, 나머지에까지 아홉 줄을 적게 하면 **테스트가 읽히지 않는다**. 그래서
여기 팩토리를 둔다 — 뒷문이 아니라, 강제가 걸리는 자리(프로덕션)와 안
걸려도 되는 자리(테스트)를 나누는 선이다.
"""

from __future__ import annotations

from typing import Any

from agent_cli.loop.ports import LoopPorts
from agent_cli.session_slots import get_agent_registry
from agent_cli.wake import Mailbox


def make_ports(**overrides: Any) -> LoopPorts:
    """전부 미연결인 포트 묶음 + 덮어쓰기. `owner` 기본값은 `"main"`."""
    base: dict[str, Any] = {
        "owner": "main",
        "questions": None,
        "message_handler": None,
        "agent_registry": None,
        "mcp_manager": None,
        "hook_runner": None,
        "route_message": None,
        "enqueue_user_message": None,
        "peer_roster": None,
    }
    inbox = overrides.pop("inbox", None)
    base.update(overrides)
    reg = base["agent_registry"]
    if base["owner"] == "main" and callable(getattr(type(reg), "absorb_main", None)):
        # 프로덕션 main 처럼: 턴 경계가 main 상자를 흡수한다(`absorb_main` — 도착 순서). 테스트가 따로
        # 주는 사람의 말(``inbox``)은 그 앞에 선다.
        words = inbox
        inbox = lambda: (words() if words else []) + reg.absorb_main()
    if "mailbox" not in base:
        # 프로덕션 빌더처럼: 포트를 만들 때의 슬롯 레지스트리. ``inbox`` 는 받은편지함(`take`)
        # — 주면 상주·private 처럼 깨울 수 있는 루프가 된다.
        base["mailbox"] = Mailbox(get_agent_registry(), base["owner"], take=inbox)
    return LoopPorts(**base)


def queue_inbox(pop):
    """``pop()`` 이 사람의 말 항목을 하나씩(없으면 None) 내는 대역 → 턴 경계 흡수(``inbox`` — main 상자의 `take`
    모양 ``{"message": 항목}``)."""

    def take() -> list:
        items = []
        while True:
            item = pop()
            if not item:
                return items
            items.append({"message": item})

    return take


#: 포트를 안 쓰는 생성 지점용 공유 인스턴스 — frozen 이라 공유가 안전하다.
TEST_PORTS = make_ports()

_PORT_NAMES = frozenset(make_ports().handler_resources()) | {"owner", "inbox"}


def split_ports(kw: dict) -> dict:
    """`**kw` 를 받는 테스트 헬퍼용 — 포트 kwarg 를 뽑아 `ports=` 로 접는다.

    루프 생성을 감싸는 헬퍼들은 호출자의 `**kw` 를 그대로 흘려보낸다.
    포트가 그 안에 섞여 오므로 헬퍼가 갈라 줘야 한다.
    """
    taken = {k: kw.pop(k) for k in list(kw) if k in _PORT_NAMES}
    kw["ports"] = make_ports(**taken)
    return kw
