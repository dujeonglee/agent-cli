"""run — `agent` ``mode`` "run": 과제 하나를 맡은 에이전트를 띄운다 (docs/agent-engine/DESIGN.md §2·§3).

run 은 spawn 과 같은 에이전트다 — 레지스트리의 `spawn(ends_after_task=True)` 로 올라 같은 루프
(`ResidentLoop`)·명단·상한·agents.json·채널을 쓰고, 과제를 부른 쪽 주소(``caller.owner`` —
main 이든 에이전트든)의 요청으로 받아(`request`), 런이 끝나면 내려온다(사용자 결정 — 받아들여진 `wait` 는
런을 끝내지 않고(5c-1), 런 중간의 `reply` 는 끝이 아니다. 닫지 않은 것도 깨울 것도 없는 `wait` 는 런을 끝낸다 — 5d). 회신은 장부가 보장한다: 갚을
빚이 남으면 `complete` 가 되돌아오고, 그래도 남으면 런 끝 폴백이 런 요약으로 회신한다. **결과는 그 회신이다**: 여기는 기다리지 않고 바로 돌아오고, 부른 쪽의 장부에
"기다리는 회신" 이 선다(레지스트리가 안다 — `AgentRegistry.awaited_replies`).

이 모듈이 정하는 run 만의 것 둘:

- 앞 거부 판정 — `delegation_checks.run_refusal`(빈 과제·provider 없음·순환·깊이).
- 훅 — ``OnAgentStart`` 는 띄울 때, ``OnAgentEnd`` 는 과제를 마치고 내려올 때(그 마지막 런의 결과).

정지는 다른 에이전트와 같다(v10.142.0, `agent_stop`) — 부른 런의 정지에 묶이지 않는다: 방 ■·Ctrl+C 한 번은
main 만 멈추고, run 은 자기 채널의 ■ 나 "모두 정지" 로 멈춘다(멈추면 요청자에게 stopped 회신). 띄운 쪽
주소(``parent``)를 남겨 그 쪽이 ✕ 로 지워지면 함께 멈춘다.

레지스트리를 import 하지 않는 잎이다 — 레지스트리의 모드 표가 `start_run` 을 핸들러로 부른다.
"""

from __future__ import annotations

from typing import NamedTuple

from agent_cli import item_ids
from agent_cli.subagent.delegation_checks import run_refusal
from agent_cli.tools.result import ToolResult

#: run 의 결과 — 기다리지 않는다는 사실과 회신 표지. 기다리는 법은 꼬리(`## Open Items`)가 말한다.
RUN_STARTED = (
    'started agent "{key}"{as_} on request "{rid}" — its `reply` comes back marked '
    "[{rid}] and wakes you (if it ends without one, you get a note saying so); "
    "the agent ends when it completes."
)


class RunLaunch(NamedTuple):
    """run 하나를 띄운 결과 — 거부면 ``error`` 만, 아니면 에이전트 키와 과제 요청 id."""

    key: str
    rid: str
    error: str


def launch_run(registry, args: dict, *, caller, runtime, images=()) -> RunLaunch:
    """거부 판정 → `spawn(ends_after_task=True)` → 과제 요청. 회신을 기다리지 않는다.

    ``caller`` 는 부른 루프(`loop.kind.Caller`), ``runtime`` 은 그 실행 배선(`AgentRuntime`
    사전). 모델의 `agent` run(`start_run`)과 사람의 ``@agent`` 명령(`main._dispatch_agent`)이
    같이 쓴다 — 명령은 돌려받은 ``rid`` 의 회신을 기다린다."""
    from agent_cli.render import get_renderer

    task = str(args.get("task", "") or "")
    profile = str(args.get("profile", "") or "")
    refusal = run_refusal(
        task=task,
        binding=caller.binding,
        agent_name=profile,
        agent_stack=list(caller.agent_stack),
        depth=caller.depth,
        max_depth=caller.max_depth,
    )
    if refusal:
        return RunLaunch("", "", refusal)
    key, err = registry.spawn(
        profile=profile,
        instructions=str(args.get("instructions", "") or ""),
        allowed_tools=args.get("tools"),
        context_mode=str(args.get("context", "none") or "none"),
        parent_ctx=caller.ctx,
        runtime=runtime,
        agent_stack=list(caller.agent_stack),
        ends_after_task=True,
        task_summary=task,
        parent=caller.owner,
    )
    if err:
        return RunLaunch("", "", f"Delegation rejected: {err}")
    tm = registry.get(key)
    rid = item_ids.next_id(item_ids.REPLY)
    tm.task_id = rid
    # 부른 자리 — 이 스레드(부른 루프)에서 읽는다; 에이전트는 자기 스레드에서 돌아 모른다
    tm.caller_task = get_renderer().current_task_id()
    hooks = caller.hook_runner
    if hooks:
        hooks.fire(
            "OnAgentStart",
            tool_name="agent",
            tool_input=dict(args),
            turn=caller.turn,
            mcp_manager=caller.mcp_manager,
        )

        def on_task_end(result) -> None:
            hooks.fire(
                "OnAgentEnd",
                tool_name="agent",
                tool_input=dict(args),
                delegate_result=result,
                turn=caller.turn,
                mcp_manager=caller.mcp_manager,
            )

        tm.on_task_end = on_task_end
    # ``images`` (v10.149.3): 사람이 main 입력창에서 `@<profile> 과제` 에 붙인 이미지 — 과제와 함께 간다
    qerr = registry.request(
        key, task, author=caller.owner, item_id=rid, images=list(images) or None
    )
    if qerr:
        registry.kill(key, by="")  # 과제 없는 run 은 남길 이유가 없다 — 알릴 빚도 없다
        return RunLaunch("", "", f"Delegation rejected: {qerr}")
    return RunLaunch(key, rid, "")


def start_run(registry, args: dict, *, caller, runtime) -> ToolResult:
    """`agent` ``mode`` "run" 핸들러 — `launch_run` 의 결과를 도구 결과 한 줄로."""
    launched = launch_run(registry, args, caller=caller, runtime=runtime)
    if launched.error:
        return ToolResult(False, error=launched.error)
    profile = str(args.get("profile", "") or "")
    as_ = f" ({profile})" if profile else ""
    return ToolResult(
        True, output=RUN_STARTED.format(key=launched.key, as_=as_, rid=launched.rid)
    )
