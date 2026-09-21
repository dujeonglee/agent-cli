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
        "dequeue_user_message": None,
    }
    base.update(overrides)
    return LoopPorts(**base)


#: 포트를 안 쓰는 생성 지점용 공유 인스턴스 — frozen 이라 공유가 안전하다.
TEST_PORTS = make_ports()

_PORT_NAMES = frozenset(make_ports().handler_resources()) | {"owner"}


def split_ports(kw: dict) -> dict:
    """`**kw` 를 받는 테스트 헬퍼용 — 포트 kwarg 를 뽑아 `ports=` 로 접는다.

    루프 생성을 감싸는 헬퍼들은 호출자의 `**kw` 를 그대로 흘려보낸다.
    포트가 그 안에 섞여 오므로 헬퍼가 갈라 줘야 한다.
    """
    taken = {k: kw.pop(k) for k in list(kw) if k in _PORT_NAMES}
    kw["ports"] = make_ports(**taken)
    return kw
