"""run 에이전트 — `agent` ``mode`` "run" 이 레지스트리에 올리는 에이전트 (docs/agent-engine/DESIGN.md).

**P2 에서 ResidentLoop 로 합친다** — 결과 = 회신, `message`/`reply` 한 쌍이 되면 이 종류의 정책
차이(주소 ``run:``·private 메일함·`wait` 제자리(WAIT_BLOCK)·run 전용 스키마·막힌 기다림)가
사라진다(설계 §8). `RunAgentLoop`·`RUN_ITEM`·`_wait_for_replies` 는 P1 임시물 목록
(`tests/test_agent_engine_transitional.py`)에 있다 — P2 가 그 테스트를 "없다" 로 뒤집는다.

run 은 상주와 같은 레지스트리에 오르는 에이전트(``ends_after_task``)다 — 상한·agents.json·화면 로스터·채널·자기 스레드를 상주와
나눠 쓰고, 과제 하나의 회신을 보내면 내려온다. 부른 쪽은 그 회신을 **막힌 채** 받아
`run_result` 어댑터로 종전과 같은 도구 결과를 만든다(P1 — 모델 바이트 불변).

- `run_agents` — 호스트: 과제마다 거부 판정(`delegation_checks`) → 레지스트리에 올림
  (`AgentRegistry.start_run_agent`, 스레드는 레지스트리의 것) → 전부 기다림 → 어댑터.
- `RunAgentLoop` — 그 에이전트 런의 종류(`PrivateLoop`). 모델이 보는 것(주소
  ``run:<run_dir>``·private 메일함·`wait` 제자리·run-only 스키마·시작 메시지 = 과제 원문·ctx
  집 ``run_…``)이 상주와 다르다 — 바이트 불변의 P1 이라 이 종류가 따로 있고, 결과가 회신이
  되는 P2 에서 상주 종류로 합친다. 화면은 상주의 채널(`begin_agent_work`)로 그린다.

정지는 호출자의 것이다: 런은 부른 루프의 stop Event 를 따른다(방 ■·Ctrl+C 가 run 을 멈춘다).
레지스트리는 세션 슬롯에서 얻는다 — 서브루프 포트의 ``agent_registry=None`` 은 "상주 모드를
쓸 수 없다" 는 권한 경계이고, run 의 수명을 맡길 곳과는 다른 말이다.
"""

from __future__ import annotations

import dataclasses
import threading
from pathlib import Path

from agent_cli.constants import RUN_OWNER_PREFIX
from agent_cli.loop.kind import Caller, LoopKindError, PrivateLoop, Start
from agent_cli.session_slots import get_agent_registry
from agent_cli.subagent.delegation_checks import run_refusal
from agent_cli.subagent.profiles import load_profile
from agent_cli.subagent.report import _generate_run_dir_name, _resolve_session_dir
from agent_cli.subagent.run_result import (
    RunReply,
    batch_tool_result,
    write_result_md_and_tool_result,
)
from agent_cli.tools.result import ToolResult

#: run 에이전트 런의 이름(채널 블록의 앵커 ``<key>#run``) — 과제가 하나라 하나다.
RUN_ITEM = "run"

NO_REGISTRY_FOR_RUN = (
    "Delegation rejected: no agent registry in this session (run agents need one)"
)


class RunAgentLoop(PrivateLoop):
    """**P1 임시 종류 — P2 에서 ResidentLoop(ends_after_task) 로 합쳐 지운다.** 종류 정책 메서드만
    든다(정체·시작·ctx·스코프·결과 모양); 루프 본체·스레드·상한·저장은 엔진·레지스트리의 것.

    run 에이전트 하나의 런 — ``spec`` 은 과제 사전(``task``·``agent``·``instructions``·
    ``tools``·``context``), ``caller`` 는 부른 루프(`Caller`), ``tm`` 은 레지스트리의 개체.

    깊이는 호출자 +1, 스택은 호출자 것에 프로파일을 더하고(엔진이 push), 바인딩은 프로파일이
    모델을 고정하면 그 복사본, 도구는 요청 ∩ 프로파일(프로파일이 상한)."""

    verbose = False
    graceful_interrupt = False

    def __init__(
        self,
        spec: dict,
        caller: Caller,
        *,
        tm,
        run_dir: Path,
        caller_task: str,
    ) -> None:
        self.spec = spec
        self.caller = caller
        self.tm = tm
        self.task = str(spec.get("task", "") or "")
        self.agent_name = str(spec.get("agent", "") or "")
        self.instructions = str(spec.get("instructions", "") or "")
        self.context_mode = str(spec.get("context", "none") or "none")
        self.requested_tools = spec.get("tools")
        self.run_dir = run_dir
        super().__init__(owner=f"{RUN_OWNER_PREFIX}{run_dir.name}")
        self.depth = caller.depth + 1
        self.max_depth = caller.max_depth
        self.max_turns = caller.max_turns
        self.binding = caller.binding
        self.tools = self.requested_tools
        self.hooks_config = caller.hooks_config
        self.session = caller.session
        self.compaction_enabled = caller.compaction_enabled
        self.stop_event = caller.stop_event
        self.skill_stack = list(caller.skill_stack)
        self.agent_stack = list(caller.agent_stack)
        self._caller_task = caller_task

    # ── ⑫ 시작 — 과제 원문 ──
    def start(self) -> Start:
        return Start(query=self.task)

    # ── ⑬ ctx — 프로파일 → 바인딩 고정 → ctx (앞 검사는 호스트가 올리기 전에 했다) ──
    def make_ctx(self, start: Start):
        from agent_cli.subagent.agents_live import compose_role_prompt
        from agent_cli.subagent.runner import (
            ProfileToolsError,
            apply_role_overrides,
            create_subagent_ctx,
        )

        caller = self.caller
        binding = caller.binding
        agent_role = ""
        model = ""  # 프로파일 `model` 오버라이드만 — 세션 모델은 바인딩에 있다
        allowed_tools = self.requested_tools
        hooks_config = caller.hooks_config
        if self.agent_name:
            role_prompt, agent_config, error = load_profile(self.agent_name)
            if error:
                raise LoopKindError(f"Delegation rejected: {error}")
            agent_role = role_prompt
            try:
                allowed_tools, model, hooks_config = apply_role_overrides(
                    agent_config,
                    allowed_tools=allowed_tools,
                    model=binding.model,
                    hooks_config=hooks_config,
                )
            except ProfileToolsError as e:
                raise LoopKindError(f"Delegation rejected: {e}") from e
        # 인라인 지시는 프로파일 본문 뒤에 합성 — 상주 spawn 과 같은 규칙.
        self.agent_role = compose_role_prompt(agent_role, self.instructions)
        self.tools = allowed_tools
        self.hooks_config = hooks_config
        if model and model != binding.model:
            try:
                binding = binding.pinned(model)
            except KeyError as exc:
                raise LoopKindError(f"Delegation rejected: {exc.args[0]}") from exc
        self.binding = binding
        ctx, ctx_error = create_subagent_ctx(
            self.context_mode, caller.ctx, self.run_dir, binding=binding
        )
        if ctx is None:
            raise LoopKindError(f"Delegation rejected: {ctx_error}")
        self._record_on_agent(ctx)
        return ctx

    def _record_on_agent(self, ctx) -> None:
        """정해진 ctx·역할·도구·모델·훅을 레지스트리 개체에 적는다 — 로스터의 컨텍스트 크기와
        agents.json 이 읽는다(상주의 `ResidentLoop.open` 이 ``tm.ctx`` 를 적는 것과 같은 자리)."""
        tm = self.tm
        tm.ctx = ctx
        tm.role_prompt = self.agent_role
        tm.allowed_tools = self.tools
        tm.model = self.binding.model
        tm.hooks_config = self.hooks_config

    def rejected(self, reason: str) -> RunReply:
        return RunReply(refusal=reason)

    # ── ⑭ 스코프 — 상주와 같은 채널 블록, 부른 자리엔 한 줄 카드(``caller_task``) ──
    def open_scope(self, start: Start):
        from agent_cli.render import get_renderer

        renderer = get_renderer()
        renderer.begin_agent_work(
            key=self.tm.key,
            item=RUN_ITEM,
            profile=self.agent_name,
            message=self.task,
            ctx_dir=self.run_dir.name,
            caller_task=self._caller_task,
        )
        return renderer

    def close_scope(self, renderer, finished, secs: float) -> None:
        ok = bool(
            isinstance(finished, RunReply)
            and not finished.refusal
            and finished.result is not None
            and finished.result.success
        )
        error = ""
        if isinstance(finished, RunReply) and not ok:
            error = finished.refusal or (
                (finished.result.error or "") if finished.result is not None else ""
            )
        renderer.end_agent_work(
            key=self.tm.key,
            item=RUN_ITEM,
            success=ok,
            duration_s=secs,
            error=error[:200],
        )

    # ── ⑰ 회신 — 엔진 결과를 그대로 (모양은 부른 쪽의 어댑터가 정한다) ──
    def finish(self, result, secs: float, released: str) -> RunReply:
        return RunReply(
            result=result,
            ctx=self.ctx,
            run_dir=self.run_dir,
            secs=secs,
            released=released,
        )


@dataclasses.dataclass
class _Started:
    """과제 하나를 올린 결과 — 거부문이 있으면 올리지 못했다. 아니면 개체와, 그 스레드가
    남기는 회신(또는 런을 깬 예외)."""

    refusal: str = ""
    tm: object = None
    reply: RunReply | None = None
    error: BaseException | None = None


def _start_one(spec: dict, caller: Caller, registry, caller_task: str) -> _Started:
    """과제 하나를 거부 판정하고, 통과하면 레지스트리에 올려 그 스레드를 띄운다."""
    task = str(spec.get("task", "") or "")
    agent_name = str(spec.get("agent", "") or "")
    refusal = run_refusal(
        task=task,
        binding=caller.binding,
        agent_name=agent_name,
        agent_stack=list(caller.agent_stack),
        depth=caller.depth,
        max_depth=caller.max_depth,
    )
    if refusal:
        return _Started(refusal=refusal)
    if registry is None:
        return _Started(refusal=NO_REGISTRY_FOR_RUN)
    run_dir = _resolve_session_dir(caller.session, caller.ctx) / _generate_run_dir_name(
        agent_name or "task"
    )
    started = _Started()

    def body(tm) -> None:
        kind = RunAgentLoop(
            spec, caller, tm=tm, run_dir=run_dir, caller_task=caller_task
        )
        try:
            started.reply = kind.run()
        except BaseException as exc:  # 부른 쪽이 받는다(단건은 다시 던진다)
            started.error = exc

    tm, err = registry.start_run_agent(
        profile=agent_name,
        task=task,
        context_mode=str(spec.get("context", "none") or "none"),
        home_dir=run_dir,
        body=body,
    )
    if tm is None:
        return _Started(refusal=f"Delegation rejected: {err}")
    started.tm = tm
    return started


def _wait_for_replies(started: list[_Started], stop_event) -> None:
    """올린 run 에이전트들의 회신을 막힌 채 기다린다(그 스레드의 끝 = 회신 뒤 내려옴).
    기다리는 중에 이 스레드가 끊기면(CLI Ctrl+C) 런들에 정지를 알리고 그대로 던진다 —
    종전엔 같은 스레드에서 돌아 그 예외가 곧 런을 끊었다."""
    try:
        for s in started:
            if s.tm is not None:
                s.tm.worker.join()
    except BaseException:
        stop_event.set()
        raise


def run_agents(args: dict, caller: Caller) -> ToolResult:
    """`agent` ``mode`` "run" 의 호스트 — ``tasks`` 하나면 그 회신의 결과, 여럿이면 묶음.

    run 에이전트는 각자 자기 스레드에서 돈다(상주와 같다). 런의 stop Event 는 호출자의 것이고,
    없으면(루프 밖 호출) 이 묶음 하나가 같이 쓰는 새 Event 다."""
    from agent_cli.render import get_renderer

    tasks = args.get("tasks", [])
    if not tasks:
        return ToolResult(False, error="Delegation rejected: empty tasks array")
    # LLM 이 ["task text"] 로 보내기도 한다
    tasks = [{"task": t} if isinstance(t, str) else t for t in tasks]
    if caller.stop_event is None:
        caller = dataclasses.replace(caller, stop_event=threading.Event())
    registry = get_agent_registry()
    caller_task = get_renderer().current_task_id()
    started = [_start_one(spec, caller, registry, caller_task) for spec in tasks]
    _wait_for_replies(started, caller.stop_event)
    if len(started) == 1:
        only = started[0]
        if only.refusal:
            return ToolResult(False, error=only.refusal)
        if only.error is not None:
            raise only.error
        return write_result_md_and_tool_result(only.reply)
    results: list[ToolResult | None] = []
    for s in started:
        if s.refusal:
            results.append(ToolResult(False, error=s.refusal))
        elif s.reply is None:
            results.append(None)  # 회신 없이 끝난 스레드 — 종전 문구
        else:
            results.append(write_result_md_and_tool_result(s.reply))
    return batch_tool_result(tasks, results)
