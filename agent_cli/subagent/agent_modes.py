"""`agent` 도구의 실행 — 모드 표(`_MODE_HANDLERS`)와 모드별 처리, 결과·거절 문구.

스키마·의미 검증은 `tools/agent_tool.py`, 여기는 루프(`loop.tool_bridge`)가 가로채 부르는 실행이다. 모드 처리는
레지스트리의 공개 표면(``spawn``·``request``·``resume_teammate``·``format_status``·``kill``·``get``·``instances``)만
부른다 — 레지스트리를 import 하지 않는다(타입만). run 의 정책은 잎 `run_agent` 에 있다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_cli import item_ids
from agent_cli.subagent.run_agent import start_run
from agent_cli.tools.result import ToolResult

if TYPE_CHECKING:
    from agent_cli.subagent.agents_live import AgentRegistry


# ── LLM 도구 진입점 (tool_bridge 인터셉트) ──────
#
# 모드 테이블화 (리뷰 §4.4 T3): 상주 모드별 처리는 ``_agent_<mode>`` 핸들러
# 함수 + ``_MODE_HANDLERS`` 테이블 — 종전 tool_agent 안의 if-체인.
# 모드 추가 = agent_tool.AGENT_MODES 한 줄 + 핸들러 함수 하나
# (커버리지는 tests 의 테이블↔핸들러 정합 테스트가 고정).


# ── `agent` 결과·거절 문구 (v10.85.0, agent 검토 PR B) ──────────────────────────
# 모듈 상수인 이유: ``prompts.inventory`` 가 표본으로 읽어 표기 가드(도구·인자 백틱,
# 값 큰따옴표, 호출 모양 없음, 대문자 강조 없음)가 여기까지 본다 — v10.77/10.81 규칙
# 위반 7곳·11낱말이 가드 밖이라 남아 있었다. 결과는 **사실만** 말한다(키·요청 id·
# "회신이 깨운다"); "기다리는 법"은 꼬리 `## Live Agents` 머리글이 상시로 맡고,
# 폴링 금지는 `status` 의 넛지가 맡는다(세 겹 → 한 겹, v10.73.0 자리 원칙).
SPAWN_DONE = 'spawned agent "{key}"'
SPAWN_DONE_AS = 'spawned agent "{key}" ({parts})'


def spawn_head(key: str, parts: list[str]) -> str:
    """spawn 결과 첫 줄 — 프로파일·이름이 있으면 괄호로."""
    if parts:
        return SPAWN_DONE_AS.format(key=key, parts=" · ".join(parts))
    return SPAWN_DONE.format(key=key)


def peer_address(name: str, key: str) -> str:
    """`SPAWN_PEERS` 의 ``who`` — 이름이 있으면 이름 또는 키, 값은 큰따옴표."""
    return f'"{name}" or key "{key}"' if name else f'key "{key}"'


def quoted_keys(keys: list[str]) -> str:
    return ", ".join(f'"{k}"' for k in keys)


SPAWN_DEAD_SAME_PROFILE = (
    "note: dead agent(s) with the same profile exist ({keys}) — this new spawn "
    "starts with no memory of them; to continue one of them instead, kill this "
    'and bring that one back with `mode` "resume".'
)
SPAWN_TASK_QUEUED = (
    'initial task queued as request "{rid}" — its `reply` comes back marked [{rid}] '
    "and wakes you (if it completes without one you get its run summary instead)."
)
SPAWN_NO_TASK = 'send it work with `message` (`to` "{key}", `text` the task).'
SPAWN_PEERS = (
    "Peers reach it with their `message` tool as {who}; they see the live roster "
    "in their own per-turn tail, so you need not relay keys."
)
RESUME_DONE = (
    'agent "{key}" resumed — it remembers all previous exchanges (its context was '
    "kept across death)."
)
RESUME_TASK_QUEUED = (
    'task queued as request "{rid}" — its `reply` comes back marked [{rid}] and '
    "wakes you."
)
KILL_DONE = (
    'agent "{key}" terminated. Its context is kept — `mode` "resume" brings it '
    "back with full memory; a new spawn would start without it."
)
KILL_ALREADY = (
    'agent "{key}" was already terminated — nothing to do. Its context is kept; '
    '`mode` "resume" brings it back with full memory.'
)
NO_REGISTRY = "agents are not available in this session (it has no agent registry)"


def _agent_spawn(registry, args: dict, *, caller, runtime) -> ToolResult:
    key, error = registry.spawn(
        profile=args.get("profile", ""),
        name=args.get("name", ""),
        instructions=args.get("instructions", ""),
        allowed_tools=args.get("tools"),
        context_mode=args.get("context", "none"),
        parent_ctx=caller.ctx,
        runtime=runtime,
        agent_stack=list(caller.agent_stack),
        parent=caller.owner,
    )
    if error:
        return ToolResult(False, error=f"spawn rejected: {error}")
    _parts = [p for p in (args.get("profile", ""), args.get("name", "")) if p]
    lines = [spawn_head(key, _parts)]
    # 같은 역할의 dead 가 있으면 알려준다 — "다시 시작" 의도였다면
    # 기억을 보존하는 길은 resume 이었음을 다음 선택부터 반영하도록.
    profile_arg = args.get("profile", "")
    if profile_arg:
        dead_same_role = [
            tm.key
            for tm in registry.instances()
            if tm.profile_name == profile_arg
            and tm.state == "dead"
            and tm.key != key
            and not tm.ends_after_task
        ]
        if dead_same_role:
            lines.append(
                SPAWN_DEAD_SAME_PROFILE.format(keys=quoted_keys(dead_same_role))
            )
    task = args.get("task", "")
    if task:
        rid = item_ids.next_id(item_ids.REPLY)
        err = registry.request(key, task, author=caller.owner, item_id=rid)
        if err:
            lines.append(f"initial task not queued: {err}")
        else:
            lines.append(SPAWN_TASK_QUEUED.format(rid=rid))
    else:
        lines.append(SPAWN_NO_TASK.format(key=key))
    # v10.1.0: peer 채널 안내 — 상주 에이전트끼리는 `message` 로 부르고(키나
    # 이름), 명단은 그들의 매 턴 꼬리에 있다. 실측(mgv4en): main 이 모르면
    # 플레이어들에게 main 전용 길을 가르쳐 한 턴씩 낭비됐다.
    _name = str(args.get("name", "") or "")
    lines.append(SPAWN_PEERS.format(who=peer_address(_name, key)))
    return ToolResult(True, output="\n".join(lines))


def _agent_resume(registry, args: dict, *, caller, runtime) -> ToolResult:
    key = args.get("key", "")
    err = registry.resume_teammate(key, parent_ctx=caller.ctx)
    if err:
        return ToolResult(False, error=f"resume rejected: {err}")
    lines = [RESUME_DONE.format(key=key)]
    task = args.get("task", "")
    if task:
        # spawn 처럼 요청 id 를 먼저 받아 결과에 싣는다 — 회신이 같은 id 로 온다.
        rid = item_ids.next_id(item_ids.REPLY)
        qerr = registry.request(key, task, author=caller.owner, item_id=rid)
        if qerr:
            lines.append(f"task not queued: {qerr}")
        else:
            lines.append(RESUME_TASK_QUEUED.format(rid=rid))
    return ToolResult(True, output="\n".join(lines))


def _agent_status(registry, args: dict, *, caller, runtime) -> ToolResult:
    return ToolResult(
        True, output=registry.format_status(args.get("key", ""), viewer=caller.owner)
    )


def _agent_kill(registry, args: dict, *, caller, runtime) -> ToolResult:
    key = args.get("key", "")
    tm = registry.get(key)
    if tm is not None and tm.state == "dead":
        # 멱등은 그대로(v10.84.0) — 다만 문장이 반복을 알려 준다: 벤치에서 35B 가
        # 같은 키에 kill 을 일곱 번 보내며 매번 같은 "terminated" 를 받았다.
        registry.kill(key)
        return ToolResult(True, output=KILL_ALREADY.format(key=key))
    # ✕ 와 같은 장치 — 빚진 상대 모두에게 회신이 간다. 지운 쪽은 모델(main 또는 그 에이전트)이다.
    err = registry.kill(key, by=str(caller.owner).removeprefix("agent:"))
    if err:
        return ToolResult(False, error=f"kill rejected: {err}")
    return ToolResult(True, output=KILL_DONE.format(key=key))


# 모드 → 핸들러. agent_tool.AGENT_MODES 와 1:1 — 정합은 tests 의 커버리지 테스트가 고정.
# run 의 정책(거부 판정·묶인 정지·훅)은 잎 모듈 `run_agent` 에 있다 — 레지스트리는 그
# 핸들러가 부르는 `spawn(ends_after_task=True)`·`request` 만 낸다.
_MODE_HANDLERS = {
    "run": start_run,
    "spawn": _agent_spawn,
    "status": _agent_status,
    "resume": _agent_resume,
    "kill": _agent_kill,
}


def tool_agent(
    args: dict,
    *,
    registry: AgentRegistry | None,
    caller,
    runtime: dict | None = None,
) -> ToolResult:
    """`agent` 도구 mode 디스패치 — ``_MODE_HANDLERS`` 테이블 경유. 루프(tool_bridge)가
    인터셉트해 호출자(`loop.kind.Caller` — 주소·깊이·스택·정지·ctx)와 실행 배선(runtime)을
    준다. 모든 루프가 같은 표면이다(docs/agent-engine/DESIGN.md §1) — 깊이 상한은 엔진이
    상한의 루프에서 `agent` 도구를 빼는 것으로 막는다."""
    if registry is None:
        return ToolResult(False, error=NO_REGISTRY)

    # P3: main 의 호출마다 runtime 갱신 — restore 로 되살아난 에이전트(스폰 없음)도 첫
    # 요청부터 현재 세션의 provider 배선으로 돈다. 에이전트가 부른 것은 그 에이전트가 띄운
    # 것에만 (`spawn(runtime=…)`) — 레지스트리의 기본값은 main 의 것이어야 한다.
    if runtime and caller.depth == 0:
        registry.runtime = runtime

    mode = args.get("mode", "")
    # 에이전트 주소는 어디서나 같은 규칙 — 키 또는 이름(`resolve_key`, `message` 와 같다, 감사 3-1).
    key = args.get("key")
    if mode in ("resume", "kill", "status") and isinstance(key, str) and key:
        args = {**args, "key": registry.resolve_key(key) or key}
    handler = _MODE_HANDLERS.get(mode)
    if handler is None:
        return ToolResult(
            False,
            error=(f'unknown `mode` "{mode}" — use ' + " / ".join(_MODE_HANDLERS)),
        )
    return handler(registry, args, caller=caller, runtime=runtime or registry.runtime)
