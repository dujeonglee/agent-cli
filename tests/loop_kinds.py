"""테스트용 `Caller` 팩토리 (v10.87.0 LoopKind) — `tests/loop_ports.py` 의 짝.

`Caller` 는 **기본값이 없다** — 중첩 루프를 여는 자리가 호출자 정보를 빠뜨리면 생성 시
`TypeError` 가 나는 것이 설계의 전부다(주소 상속이 조용히 ``"main"`` 으로 새던 사고).
그 강제는 프로덕션 조립 지점(`Caller.from_loop`·`Caller.main_session`)에 걸려야 의미가
있고, 테스트는 필요한 몇 개만 덮어쓰면 된다 — 여기 팩토리가 그 선이다.
"""

from __future__ import annotations

from typing import Any

from agent_cli.loop.kind import Caller, LoopKind
from tests.loop_ports import make_ports


def make_caller(**overrides: Any) -> Caller:
    """전부 비어 있는 main 호출자 + 덮어쓰기. ``owner`` 기본값은 ``"main"``, 깊이 0."""
    base: dict[str, Any] = {
        "owner": "main",
        "depth": 0,
        "max_depth": 2,
        "max_turns": 0,
        "binding": None,
        "tools": None,
        "hooks_config": None,
        "hook_runner": None,
        "mcp_manager": None,
        "agent_registry": None,
        "session": None,
        "compaction_enabled": True,
        "graceful_interrupt": False,
        "verbose": False,
        "agent_role": "",
        "skill_stack": [],
        "agent_stack": [],
        "ctx": None,
        "stop_event": None,
    }
    base.update(overrides)
    return Caller(**base)


def make_kind(*, ports=None, **attrs) -> LoopKind:
    """맨 종류 — 기본 구현이 포트·깊이에서 답을 유도한다 (`AgentLoop` 가 종류 없는
    `run_loop` 호출에 만드는 것과 같은 모양). 테스트가 포트·깊이만 주고 엔진을 돌릴 때."""
    k = LoopKind()
    k.ports = ports if ports is not None else make_ports()
    k.owner = k.ports.owner
    for name, value in attrs.items():
        setattr(k, name, value)
    return k


def run_tasks(args: dict, caller: Caller):
    """`agent` ``mode`` "run" 을 호스트(`run_agents`)로 돌린다 — ``args`` 는 ``{"tasks": [...]}``.
    run 에이전트는 레지스트리의 것이라 세션 슬롯이 비어 있으면 이 호출 동안만 세션 디렉터리
    없는 레지스트리를 둔다(실제 세션은 늘 슬롯에 레지스트리가 있다)."""
    from agent_cli.session_slots import get_agent_registry, set_agent_registry
    from agent_cli.subagent.agents_live import AgentRegistry
    from agent_cli.subagent.run_agent import run_agents

    if get_agent_registry() is not None:
        return run_agents(args, caller)
    set_agent_registry(AgentRegistry(None))
    try:
        return run_agents(args, caller)
    finally:
        set_agent_registry(None)


def run_task(spec: dict, caller: Caller):
    """과제 하나 — 종전 테스트가 `OneShotLoop(spec, caller).run()` 을 부르던 자리."""
    return run_tasks({"tasks": [spec]}, caller)
