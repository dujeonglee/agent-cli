"""`LoopPorts` 와 종류별 `make_ports` (docs/wiring/DESIGN.md §4, C3; v10.87.0 LoopKind).

배선 누락은 이 저장소에서 세 번 났고 셋 다 유닛 테스트가 초록이었다 —
테스트가 협력자를 **직접 만들어** 검사하니 조립 지점이 그걸 넘기는지는
아무도 안 봤다. 여기 있는 것은 그 빈자리를 메우는 두 장치다:

**무기본값** — 포트를 빠뜨리면 생성 시 `TypeError`. 언어가 잡는다. 빌더가 `None` 으로 둔 포트는 그 줄 옆 주석이
왜인지 말한다(v10.177.4 — 종전의 `unwired` 사유 맵은 "사유 문자열이 있다" 만 강제했고 진위는 못 잡았다).
"""

from __future__ import annotations

import dataclasses
from dataclasses import MISSING, fields

import pytest

from agent_cli.loop.kind import PrivateLoop
from agent_cli.loop.ports import LoopPorts
from agent_cli.runtime import MainLoop
from agent_cli.skills.executor import SkillLoop
from agent_cli.subagent.resident_loop import ResidentLoop

#: 조립 지점 — 종류마다 `make_ports` (v10.87.0; 종전 `runtime.ports_for_*`). 각 입력은
#: **None 이 아닌 센티넬**이다. 빌더가 자기 입력을 흘리는지를 봐야지, 호출자가 None 을
#: 준 결과를 보면 안 된다. 하니스 작업은 `PrivateLoop.make_ports`(private 주소).
_S = object()
BUILDERS = {
    "main": lambda: MainLoop.make_ports(
        agent_registry=_Reg(),
        mcp_manager=_S,
        enqueue_user_message=_S,
        route_message=_S,
    ),
    "skill": lambda: SkillLoop.make_ports(
        agent_registry=_Reg(), owner="agent:parent", address="skill:s-1"
    ),
    "private": lambda: PrivateLoop.make_ports("harness:auditor:1-2"),
    "resident": lambda: ResidentLoop.make_ports(
        key="k1",
        message_handler=_S,
        questions=_S,
        inbox=_S,
        peer_roster=_S,
        agent_registry=_Reg(),
    ),
}


class _Reg:
    """`question_port` 만 있는 최소 레지스트리 대역."""

    def question_port(self, key=None, *, address="", name=""):
        return _S

    def absorb_main(self):  # main 의 턴 경계 흡수
        return []

    def message_handler(self, sender):  # main 의 `message` (v10.141.0)
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
            if not (f.default is MISSING and f.default_factory is MISSING)
        ]
        assert optional == [], f"기본값이 붙은 포트: {optional}"

    def test_frozen(self):
        p = BUILDERS["private"]()
        with pytest.raises(dataclasses.FrozenInstanceError):
            p.owner = "agent:x"  # type: ignore[misc]


class TestOwner:
    """C2 가 소비할 값 — 지금은 아무도 안 읽는다(§6)."""

    def test_resident_carries_its_key(self):
        assert BUILDERS["resident"]().owner == "agent:k1"

    def test_main_host_is_main(self):
        assert BUILDERS["main"]().owner == "main"

    def test_skill_inherits_the_parent(self):
        """상주 에이전트 안에서 돈 스킬이 건 모니터는 **그 에이전트**에게 보고해야 한다.
        기본값을 두면 조용히 main 으로 간다."""
        assert BUILDERS["skill"]().owner == "agent:parent"

    def test_private_has_its_own_address(self):
        """하니스 작업은 부모 주소를 빌리지 않는다 (v10.84.0) — `harness:<job>:<label>`.
        run 에이전트는 v10.141.0 부터 ``agent:<key>`` 다(상주 빌더)."""
        assert BUILDERS["private"]().owner == "harness:auditor:1-2"

    @pytest.mark.parametrize("builder", ["skill", "private"])
    def test_nested_builders_require_an_owner(self, builder):
        """기본값이 없어야 상속 누락이 TypeError 가 된다."""
        with pytest.raises(TypeError):
            (
                SkillLoop.make_ports(agent_registry=None)
                if builder == "skill"
                else PrivateLoop.make_ports()
            )


class TestHandlerResources:
    """`Tool.requires_handler` 가 보는 뷰 — 손-유지 dict 를 대체한다."""

    def test_excludes_non_resources(self):
        keys = BUILDERS["private"]().handler_resources()
        assert "owner" not in keys

    def test_covers_every_port(self):
        p = BUILDERS["private"]()
        expected = {f.name for f in fields(LoopPorts)} - {"owner"}
        assert set(p.handler_resources()) == expected

    def test_declared_requirements_are_resolvable(self):
        """도구가 요구하는 이름은 포트 뷰 또는 `ctx` 로 해결돼야 한다 —
        아니면 그 도구는 **어느 루프에서도** 안 붙는다(조용한 실종)."""
        from agent_cli.tools import TOOLS

        # ``ctx``·``snapshot``·``browser``·``vision``·``skills`` 는 포트가
        # 아닌 세션 자원 — core 가 얹는다(``vision`` 은 바인딩의 능력, ``skills`` 는
        # 모델이 부를 수 있는 스킬 유무). ``deferred``(v10.109.0)는 게이트가 끝난
        # 도구 목록에서 core 가 판정한다(지연 도구가 남았나).
        available = set(BUILDERS["private"]().handler_resources()) | {
            "ctx",
            "snapshot",
            "browser",
            "wake",
            "vision",
            "skills",
            "deferred",
        }
        required = {t.requires_handler for t in TOOLS.values() if t.requires_handler}
        assert required <= available, f"해결 불가한 요구: {required - available}"
