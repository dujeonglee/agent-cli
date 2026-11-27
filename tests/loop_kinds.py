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
        "turn": 0,
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


def runtime_of(caller: Caller) -> dict:
    """호출자 → 그 실행 배선(`AgentRuntime` 사전) — tool_bridge 가 루프에서 만드는 것과 같은 키."""
    return {
        "binding": caller.binding,
        "max_turns": caller.max_turns,
        "depth": caller.depth,
        "max_depth": caller.max_depth,
        "session": caller.session,
        "hooks_config": caller.hooks_config,
        "compaction_enabled": caller.compaction_enabled,
    }


def _spec_args(spec: dict) -> dict:
    """옛 과제 사전(``agent`` 키) → `agent` 도구 인자(``profile`` 키)."""
    args = {k: v for k, v in spec.items() if k != "agent"}
    if spec.get("agent"):
        args["profile"] = spec["agent"]
    return args


def run_tasks(args: dict, caller: Caller) -> list:
    """`agent` ``mode`` "run" 여럿을 띄우고 각자의 **회신**을 기다린다 — 과제마다 `ToolResult`
    (거부면 그 거부문, 아니면 회신 본문 — `reply` 없이 끝났으면 하니스의 "답하지 않았다" 문장). 호출자는 main 이어야 한다(회신이
    main 메일함으로 온다). 세션 슬롯에 레지스트리가 없으면 이 호출 동안만 임시 세션 디렉터리의
    레지스트리를 둔다."""
    import shutil
    import tempfile
    from pathlib import Path

    from agent_cli.session_slots import get_agent_registry, set_agent_registry
    from agent_cli.subagent.agents_live import AgentRegistry
    from agent_cli.subagent.run_agent import launch_run
    from agent_cli.tools.result import ToolResult

    registry = get_agent_registry()
    tmp = None
    if registry is None:
        tmp = tempfile.mkdtemp(prefix="run-task-")
        registry = AgentRegistry(Path(tmp))
        set_agent_registry(registry)
    try:
        launched = [
            launch_run(
                registry, _spec_args(spec), caller=caller, runtime=runtime_of(caller)
            )
            for spec in args["tasks"]
        ]
        out = []
        for one in launched:
            if one.error:
                out.append(ToolResult(False, error=one.error))
                continue
            mail = registry.take_reply(one.rid, stop_event=caller.stop_event)
            tm = registry.get(one.key)
            if tm is not None and tm.worker is not None:
                tm.worker.join(timeout=10)
            if mail is None:
                out.append(ToolResult(False, error="ended without a reply"))
                continue
            body = str(mail.get("output") or "")
            out.append(ToolResult(bool(mail.get("success", True)), output=body))
        return out
    finally:
        if tmp is not None:
            registry.shutdown_all()
            set_agent_registry(None)
            shutil.rmtree(tmp, ignore_errors=True)


def run_task(spec: dict, caller: Caller):
    """과제 하나 — 그 회신(`run_tasks` 의 한 줄)."""
    return run_tasks({"tasks": [spec]}, caller)[0]
