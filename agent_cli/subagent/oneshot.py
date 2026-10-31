"""One-shot sub-agent — `OneShotLoop` (`LoopKind`, v10.87.0) 와 그 호스트 `tool_delegate`.

컨텍스트 모드 none(독립)·fork(대화 복사). ``tasks`` 배열: 하나면 호출 스레드에서 인라인,
여럿이면 과제별 스레드(병렬) — 실행 자리는 호스트(`tool_delegate`·`_run_parallel`)가 정하고,
한 런의 전부(가드·프로파일·자기 주소 ``run:<run_dir>``·ctx·스코프·결과 포장·정리)는
`OneShotLoop` 하나가 안다. 종전 `_run_single`·`_run_in_scope`·`runner.run_subagent_message`
세 함수가 이 클래스다.
"""

from __future__ import annotations

import dataclasses
import threading
import time
import uuid

from agent_cli.constants import RUN_OWNER_PREFIX
from agent_cli.loop.kind import Caller, LoopKindError, PrivateLoop, Start
from agent_cli.subagent.profiles import load_profile
from agent_cli.subagent.report import (
    DelegateResult,
    _extract_activity_log,
    _extract_last_actions,
    _format_delegate_output,
    _format_parallel_results,
    _generate_run_dir_name,
    _persist_run_result,
    _resolve_session_dir,
)
from agent_cli.tools.result import ToolResult


class OneShotLoop(PrivateLoop):
    """`agent` ``mode`` "run" 한 번 — 호출마다 하나.

    ``spec`` 은 과제 사전(``task``·``agent``·``instructions``·``tools``·``context``), ``caller``
    는 호출자(`Caller`). 깊이는 호출자 +1, 스택은 호출자 것에 프로파일을 더하고(엔진이
    push), 바인딩은 프로파일이 모델을 고정하면 그 복사본, 도구는 요청 ∩ 프로파일(프로파일이
    상한). 주소는 ``run:<run_dir>`` — 스코프를 여는 쪽이 디렉토리 이름을 먼저 지어 resume
    사이드카의 ``ctx_dir`` 과 맞춘다(없으면 여기서 짓는다).

    ``inline`` 이면 호출 스레드에서 CLI 들여쓰기와 함께 실시간으로, 아니면 워커 스레드에서
    (병렬 — CLI 렌더러가 모았다가 끝에 보여 준다). ``scope_extra`` 는 병렬 워커만 쓰는
    ``parent``(워커 스레드엔 바깥 스코프가 안 보인다)·``ts``(한 묶음 = 한 시작 시각).
    """

    verbose = False
    graceful_interrupt = False

    def __init__(
        self,
        spec: dict,
        caller: Caller,
        *,
        run_dir_name: str = "",
        index: int = 0,
        task_id: str = "",
        label: str = "",
        inline: bool = True,
        scope_extra: dict | None = None,
    ) -> None:
        self.spec = spec
        self.caller = caller
        self.task = str(spec.get("task", "") or "")
        self.agent_name = str(spec.get("agent", "") or "")
        self.instructions = str(spec.get("instructions", "") or "")
        self.context_mode = str(spec.get("context", "none") or "none")
        self.requested_tools = spec.get("tools")
        self.run_dir_name = run_dir_name or _generate_run_dir_name(
            self.agent_name or "task"
        )
        super().__init__(owner=f"{RUN_OWNER_PREFIX}{self.run_dir_name}")
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
        self.run_dir = None
        self._index = index
        self._task_id = task_id or f"delegate-single-{uuid.uuid4().hex}"
        self._label = label or (
            f"agent:{self.agent_name}" if self.agent_name else "agent"
        )
        self._inline = inline
        self._scope_extra = dict(scope_extra or {})

    # ── ⑫ 시작 ──
    def start(self) -> Start:
        return Start(query=self.task)

    # ── ⑬ ctx — 가드 → 프로파일 → 바인딩 고정 → 디렉토리 → ctx ──
    def make_ctx(self, start: Start):
        from agent_cli.subagent.agents_live import compose_role_prompt
        from agent_cli.subagent.runner import (
            ProfileToolsError,
            apply_role_overrides,
            create_subagent_ctx,
        )

        caller = self.caller
        if not self.task.strip():
            raise LoopKindError("Delegation rejected: empty task")
        binding = caller.binding
        if binding is None or binding.provider is None or binding.capabilities is None:
            raise LoopKindError("Delegation rejected: missing provider/capabilities")
        # 순환(A → B → A) — 이름 있는 위임만 스택에 오르니 익명은 깊이로만 막힌다.
        if self.agent_name and self.agent_name in caller.agent_stack:
            from agent_cli.recovery.recursion import format_recursion_error

            raise LoopKindError(
                format_recursion_error(
                    "agent", self.agent_name, list(caller.agent_stack)
                )
            )
        # 깊이 상한 — 엔진 필터가 상한에서 `agent` 도구를 뺐으므로 보통 디스패치는 여기
        # 오지 않는다; 직접 호출(테스트·custom tools)이 같은 문구를 받게 남긴다.
        if caller.depth >= caller.max_depth:
            from agent_cli.recovery.recursion import format_depth_limit_error

            raise LoopKindError(
                format_depth_limit_error(
                    "agent",
                    self.agent_name or "(anonymous)",
                    caller.depth,
                    caller.max_depth,
                )
            )
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
        parent_session_dir = _resolve_session_dir(caller.session, caller.ctx)
        self.run_dir = parent_session_dir / self.run_dir_name
        ctx, ctx_error = create_subagent_ctx(
            self.context_mode, caller.ctx, self.run_dir, binding=binding
        )
        if ctx is None:
            raise LoopKindError(f"Delegation rejected: {ctx_error}")
        return ctx

    def rejected(self, reason: str) -> ToolResult:
        return ToolResult(False, error=reason)

    # ── ⑭ 스코프 (카드 수명 — 단건·병렬 공용) ──
    def open_scope(self, start: Start):
        from agent_cli.render import get_renderer, render_push_depth

        renderer = get_renderer()
        renderer.begin_scope(
            task_id=self._task_id,
            kind="run",
            label=self._label,
            agent=self.agent_name,
            index=self._index,
            **self._scope_extra,
            ctx_dir=self.run_dir_name,
        )
        if self._inline:
            render_push_depth()
        return renderer

    def close_scope(self, renderer, finished, secs: float) -> None:
        from agent_cli.render import render_pop_depth

        if self._inline:
            render_pop_depth()
        ok = bool(finished is not None and finished.success)
        renderer.end_scope(
            task_id=self._task_id,
            kind="run",
            success=ok,
            duration_s=secs,
            error=(finished.error or "") if finished is not None and not ok else "",
        )

    # ── ⑰ 결과 — STATUS/RESULT 관찰 + result.md ──
    def finish(self, result, secs: float, released: str) -> ToolResult:
        ctx = self.ctx
        result_str = result.output if result.success else None
        delegate_result = DelegateResult(output=result_str, duration_secs=secs)
        delegate_result.activity_log = _extract_activity_log(ctx.get_raw_messages())
        # 턴 수 = 루프가 실제 돈 턴 (활동 로그는 상한이 있어 세면 틀린다, v9.24.1)
        delegate_result.iterations = ctx.current_turn
        if result_str is None:
            delegate_result.last_actions = _extract_last_actions(ctx.get_raw_messages())
        formatted = _format_delegate_output(delegate_result)
        if released:
            formatted = f"{formatted}\n\n{released}"
        _persist_run_result(formatted, self.run_dir)
        artifact = f"{self.run_dir_name}/"
        if result_str is not None:
            return ToolResult(
                True, output=f"STATUS: success\nRESULT:\n{formatted}", artifact=artifact
            )
        return ToolResult(
            False,
            error=f"STATUS: error\nERROR: Subagent did not complete\n{formatted}",
            artifact=artifact,
        )


def _run_parallel(task_specs: list[dict], caller: Caller) -> ToolResult:
    """과제 여럿을 과제별 스레드로 — 호스트. UI(Live 패널·과제별 캡처·묶음 틀)는 렌더러의
    것이고 여기는 `OneShotLoop` 를 스레드마다 하나씩 만들어 돌릴 뿐이다."""
    from agent_cli.render import get_renderer

    results: list[ToolResult | None] = [None] * len(task_specs)
    if caller.stop_event is None:
        caller = dataclasses.replace(caller, stop_event=threading.Event())

    renderer = get_renderer()
    # 여기(띄우는 스레드)서 잡아 모든 워커에 준다: 워커의 스코프 스택은 비어 있어 자기가
    # 스킬 안에서 시작됐는지 못 보고(`parent`), 묶음은 시작 시각 하나를 나눠 쓴다(`ts` —
    # 워커마다 찍으면 ms 차이로 스윔레인이 순차 실행처럼 그린다). 끝 시각은 워커별.
    parent_scope = renderer.current_scope()
    batch_ts = time.time()

    def worker(index: int, spec: dict) -> None:
        # uuid4 — 스레드 id 는 재사용되어 이전 호출의 카드와 겹친다.
        task_id = f"delegate-{index}-{uuid.uuid4().hex}"
        results[index] = OneShotLoop(
            spec,
            caller,
            index=index,
            task_id=task_id,
            label=spec.get("task", "") or spec.get("agent", "") or f"task #{index + 1}",
            inline=False,
            scope_extra={"parent": parent_scope, "ts": batch_ts},
        ).run()

    threads = []
    for i, spec in enumerate(task_specs):
        t = threading.Thread(target=worker, args=(i, spec), daemon=True)
        threads.append(t)
        t.start()
    try:
        for t in threads:
            t.join()
    finally:
        # Live 영역 뒤 터미널 상태 복구(CJK 입력의 readline 혼선 방지) — 실제 TTY 일 때만.
        # web 백그라운드는 stdin 이 분리돼 non-TTY 에서 tcflush 가 OSError(ENODEV).
        try:
            import sys
            import termios

            if sys.stdin is not None and sys.stdin.isatty():
                termios.tcflush(sys.stdin, termios.TCIFLUSH)
        except (ImportError, OSError, ValueError):
            pass

    return _format_parallel_results(task_specs, results)


def tool_delegate(args: dict, caller: Caller) -> ToolResult:
    """`agent` ``mode`` "run" 의 호스트 — ``tasks`` 하나면 이 스레드에서 인라인(CLI 가 중첩
    들여쓰기로 실시간 스트리밍, web 은 접히는 카드 하나), 여럿이면 병렬."""
    tasks = args.get("tasks", [])
    if not tasks:
        return ToolResult(False, error="Delegation rejected: empty tasks array")
    # LLM 이 ["task text"] 로 보내기도 한다
    tasks = [{"task": t} if isinstance(t, str) else t for t in tasks]
    if len(tasks) == 1:
        return OneShotLoop(tasks[0], caller, inline=True).run()
    return _run_parallel(tasks, caller)
