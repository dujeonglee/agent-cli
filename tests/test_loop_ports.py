"""`LoopPorts` 와 조립 지점 다섯 (docs/wiring/DESIGN.md §4, C3).

배선 누락은 이 저장소에서 세 번 났고 셋 다 유닛 테스트가 초록이었다 —
테스트가 협력자를 **직접 만들어** 검사하니 조립 지점이 그걸 넘기는지는
아무도 안 봤다. 여기 있는 것은 그 빈자리를 메우는 두 장치다:

1. **무기본값** — 포트를 빠뜨리면 생성 시 `TypeError`. 언어가 잡는다.
2. **패리티** — 빌더가 `None` 으로 둔 포트는 `unwired` 에 **사유가 있어야**
   한다. 의도적 미연결과 빠뜨린 것이 둘 다 `None` 이던 것이 이 설계가
   생긴 이유의 절반이다.

사유 **문자열의 진위**는 여기서 못 잡는다(설계 2판이 거짓 사유를 적고
통과했다). 강제되는 것은 "사유가 있다" 뿐이고, 그건 문서에도 적혀 있다.
"""

from __future__ import annotations

import dataclasses
from dataclasses import fields

import pytest

from agent_cli.loop.ports import LoopPorts
from agent_cli.runtime import (
    ports_for_oneshot,
    ports_for_resident,
    ports_for_run,
    ports_for_skill,
    ports_for_web,
)

#: 조립 지점 다섯 — 각 입력은 **None 이 아닌 센티넬**이다. 빌더가 자기
#: 입력을 흘리는지를 봐야지, 호출자가 None 을 준 결과를 보면 안 된다.
_S = object()
BUILDERS = {
    "run": lambda: ports_for_run(
        agent_registry=_Reg(), monitor_registry=_S, mcp_manager=_S
    ),
    "web": lambda: ports_for_web(
        agent_registry=_Reg(),
        monitor_registry=_S,
        mcp_manager=_S,
        dequeue_user_message=_S,
        route_message=_S,
    ),
    "skill": lambda: ports_for_skill(agent_registry=_Reg()),
    "oneshot": ports_for_oneshot,
    "resident": lambda: ports_for_resident(key="k1", message_handler=_S, questions=_S),
}


class _Reg:
    """`question_port` 만 있는 최소 레지스트리 대역."""

    def question_port(self, key):
        return _S


class TestNoDefaults:
    """포트를 빠뜨리면 **언어가** 잡는다 — 테스트가 아니라."""

    def test_missing_port_is_a_construction_error(self):
        with pytest.raises(TypeError) as e:
            LoopPorts(owner="main")  # 나머지 여덟 없음
        assert "questions" in str(e.value)

    def test_every_port_field_is_required(self):
        """새 포트를 기본값과 함께 더하면 이 테스트가 잡는다 — 기본값이
        있는 포트는 조립 지점이 조용히 빠뜨릴 수 있다."""
        optional = [
            f.name
            for f in fields(LoopPorts)
            if f.name != "unwired" and (f.default is not f.default_factory)
        ]
        assert optional == [], f"기본값이 붙은 포트: {optional}"

    def test_frozen(self):
        p = BUILDERS["oneshot"]()
        with pytest.raises(dataclasses.FrozenInstanceError):
            p.owner = "agent:x"  # type: ignore[misc]


class TestParity:
    """§2.2 의 표가 코드가 된 자리."""

    @pytest.mark.parametrize("host", sorted(BUILDERS))
    def test_no_port_is_silently_none(self, host):
        ports = BUILDERS[host]()
        silent = [
            name
            for name, value in ports.handler_resources().items()
            if value is None and name not in ports.unwired
        ]
        assert silent == [], f"{host}: 사유 없이 미연결 — {silent}"

    @pytest.mark.parametrize("host", sorted(BUILDERS))
    def test_reasons_are_not_empty(self, host):
        ports = BUILDERS[host]()
        blank = [k for k, v in ports.unwired.items() if not (v or "").strip()]
        assert blank == [], f"{host}: 빈 사유 — {blank}"

    @pytest.mark.parametrize("host", sorted(BUILDERS))
    def test_reasons_name_real_ports(self, host):
        """없는 포트에 사유를 달아두면(오타·삭제된 포트) 그건 거짓 문서다."""
        ports = BUILDERS[host]()
        unknown = set(ports.unwired) - set(ports.handler_resources())
        assert unknown == set(), f"{host}: 없는 포트의 사유 — {unknown}"

    @pytest.mark.parametrize("host", sorted(BUILDERS))
    def test_wired_ports_have_no_reason(self, host):
        """값이 있는데 '미연결 사유' 가 남아 있으면 문서가 코드와 어긋난다."""
        ports = BUILDERS[host]()
        stale = [
            k
            for k, v in ports.handler_resources().items()
            if v is not None and k in ports.unwired
        ]
        assert stale == [], f"{host}: 연결됐는데 사유가 남음 — {stale}"


class TestOwner:
    """C2 가 소비할 값 — 지금은 아무도 안 읽는다(§6)."""

    def test_resident_carries_its_key(self):
        assert BUILDERS["resident"]().owner == "agent:k1"

    @pytest.mark.parametrize("host", ["run", "web"])
    def test_main_hosts_are_main(self, host):
        assert BUILDERS[host]().owner == "main"

    @pytest.mark.parametrize("host", ["skill", "oneshot"])
    def test_nested_hosts_are_placeholders_until_c2(self, host):
        """부모 owner 를 나르는 seam 이 C2 의 몫이라 지금은 알 수 없다.
        **알면서 틀린 값**이고, 영구 기본값과는 다르다 — 값이 생길 때까지의
        한시적 자리다. C2 가 이 테스트를 바꾼다."""
        assert BUILDERS[host]().owner == "main"


class TestHandlerResources:
    """`Tool.requires_handler` 가 보는 뷰 — 손-유지 dict 를 대체한다."""

    def test_excludes_non_resources(self):
        keys = BUILDERS["oneshot"]().handler_resources()
        assert "owner" not in keys and "unwired" not in keys

    def test_covers_every_port(self):
        p = BUILDERS["oneshot"]()
        expected = {f.name for f in fields(LoopPorts)} - {"owner", "unwired"}
        assert set(p.handler_resources()) == expected

    def test_declared_requirements_are_resolvable(self):
        """도구가 요구하는 이름은 포트 뷰 또는 `ctx` 로 해결돼야 한다 —
        아니면 그 도구는 **어느 루프에서도** 안 붙는다(조용한 실종)."""
        from agent_cli.tools import TOOLS

        available = set(BUILDERS["oneshot"]().handler_resources()) | {"ctx"}
        required = {t.requires_handler for t in TOOLS.values() if t.requires_handler}
        assert required <= available, f"해결 불가한 요구: {required - available}"
