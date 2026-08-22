"""Agent loop: ReAct pattern with M1/M2 module integration."""

from __future__ import annotations

import json

from agent_cli.context.token_estimator import estimate_tokens

# Max shrink-and-retry attempts per turn when the server rejects the
# prompt as too long (flow 2 reactive recovery). Each attempt sheds more
# history via ``ContextManager.force_fit``; the bound stops a runaway
# loop when the cache cannot shrink enough or the server keeps rejecting.
from agent_cli.loop.state import LoopConfig, LoopState
from agent_cli.tools import TOOLS, RunContext, _execute_tool
from agent_cli.tools.base import default_oversized_nudge
from agent_cli.tools.result import ToolResult
from agent_cli.verbose import debug_log as _debug_log


def _normalize_input(tool_input) -> str:
    """Normalize tool input to a comparable string."""
    if isinstance(tool_input, dict):
        return json.dumps(tool_input, sort_keys=True, ensure_ascii=False)
    return str(tool_input)


class ToolBridge:
    """도구 호출 브리지 — hooks·invoke(delegate/regular)·RunContext·
    결과→관찰 seam 의 소유자 (C1 PR-2 승격 클러스터).

    의존은 명시 주입 4종뿐: ``cfg``(LoopConfig, 읽기전용)·``state``
    (LoopState, 공유 가변)·``ctx``(ContextManager)·``provider``. 전유 상태
    (``_oversized_cap``/``_run_ctx_cache``/``recent_tool_history``)는 이
    객체가 소유하고, AgentLoop 는 property/위임 메서드로 기존 표면을
    유지한다. 단독 생성 가능 = 단위 테스트에 AgentLoop 불필요.
    """

    def __init__(self, config: LoopConfig, state: LoopState, ctx, provider) -> None:
        self.cfg = config
        self.state = state
        self.ctx = ctx
        self.provider = provider
        # Oversized-observation cap: a single tool observation larger than
        # this many tokens is replaced (at the result→observation seam) with
        # a narrow-it nudge. context_window / 10; 0 = disabled (headless).
        caps = config.capabilities
        self._oversized_cap = (
            caps.context_window // 10
            if caps and getattr(caps, "context_window", 0)
            else 0
        )
        # Lazy one-shot cache for _run_ctx() — see its docstring.
        self._run_ctx_cache: RunContext | None = None
        self.recent_tool_history: list[dict] = []

    def _dispatch_tool_with_hooks(self, tool_name: str, tool_input):
        """Orchestrator: pre-hooks → invoke → guards → post-hooks → record.

        Preconditions (enforced by ``_dispatch_op``):
            - ``tool_name`` is a valid name in ``self.cfg.tools_list`` (A4
              already checked).
            - ``tool_input`` matches the tool's schema (A5 already checked).

        The body is intentionally a 5-line recipe; each stage is a
        single-purpose helper. See the helpers' docstrings for what
        each stage owns.
        """
        # Wire keys are namespaced ``{tool}_{param}``. Strip to standard
        # keys once here so delegate's direct ``tool_delegate()`` call and
        # the hooks all see standard keys; ``_execute_tool``'s own strip is
        # then a no-op on the already-stripped result.
        if tool_name in TOOLS and isinstance(tool_input, dict):
            tool_input = TOOLS[tool_name].strip_prefix(tool_input)
        _debug_log(
            f"TOOL turn={self.state.turn} action={tool_name} input={str(tool_input)[:200]}"
        )

        input_dict = (
            tool_input if isinstance(tool_input, dict) else {"raw": str(tool_input)}
        )

        # 1. Pre-hooks (may block or modify input)
        blocked, tool_input, input_dict = self._run_pre_hooks(
            tool_name, tool_input, input_dict
        )
        if blocked is not None:
            return blocked

        # 2/3. Dispatch (delegate special-case or regular).
        #
        # Safety net: a tool that raises instead of returning a
        # ``ToolResult`` would otherwise propagate up through
        # ``_execute_turn`` → ``run()`` and either kill the worker
        # thread (web) or the whole process (chat / run). Worse, the
        # LLM never sees the failure as an Observation it could
        # recover from. Catch ``Exception`` here and convert to
        # ``ToolResult(False, error=...)`` so the rest of the
        # pipeline (post-hooks, history recording, observation
        # injection) treats it as a normal tool failure and the LLM
        # gets a chance to retry on the next turn.
        #
        # Deliberately catches ``Exception``, NOT ``BaseException``:
        # ``KeyboardInterrupt`` / ``SystemExit`` must still propagate
        # so a user Ctrl+C exits the loop cleanly. Per-tool error
        # paths (e.g. ``edit_file`` returning ``RuntimeError`` as a
        # ToolResult) are unchanged — they hit the normal return
        # path and never reach this except. The net here is for the
        # *unexpected* — a tool author bug, a malformed input that
        # slipped past pre-validation, an underlying library raising
        # ``TypeError`` from inside ``re.py``, etc.
        try:
            if tool_name == "agent":
                result = self._invoke_agent(tool_input, input_dict)
            else:
                result = self._invoke_regular(tool_name, tool_input)
        except Exception as e:
            import traceback as _tb

            # Full traceback to debug log so diagnosis isn't lost.
            # LLM-facing message stays short: it has to fit a single
            # Observation card without ballooning context, and the
            # model recovers from intent, not stack frames.
            _debug_log(
                f"TOOL EXCEPTION turn={self.state.turn} tool={tool_name}\n{_tb.format_exc()}"
            )
            result = ToolResult(
                False,
                error=(
                    f"Tool '{tool_name}' raised "
                    f"{type(e).__name__}: {e}. "
                    f"This is likely a malformed input or an internal "
                    f"tool error. Review the action_input shape and "
                    f"retry, or try a different approach if the same "
                    f"input keeps failing."
                ),
            )

        # 4. PostToolUse hooks
        self._run_post_hooks(tool_name, input_dict, result)

        # 5. Append to recent_tool_history (B1 detector input)
        self._record_tool_history(tool_name, tool_input, result)

        return result

    def dispatch_edit_batch(self, path: str, edits: list) -> ToolResult:
        """같은 파일 edit_file 배치 디스패치 — 단건 경로와 **동일한 훅/이력
        계약** (P0-2). 종전 dispatch 가 ``apply_edits_batch`` 를 직접 불러
        Pre/PostToolUse 훅과 ``recent_tool_history``(B1 입력)가 배치에서만
        통째로 빠졌다(훅으로 금지한 편집이 배치로는 무사통과하던 정책 구멍).

        배치 의미는 보존한다:
          - **적용은 1회** (``apply_edits_batch`` — 단일 읽기→겹침 거부→
            bottom-up 적용→단일 쓰기, all-or-nothing) + **관찰 1건**.
          - **PreToolUse 는 edit 별** 발화(각 edit 이 블록/수정 대상) —
            하나라도 블록되면 **배치 전체 미적용**(원자성 유지) 후 단건과
            동일 형태의 블록 ToolResult 반환. 단건 계약과 동형으로, 블록 시
            post-hook/이력은 기록하지 않는다.
          - **PostToolUse 는 1회**(물리 실행이 1회 — 포매터류 훅이 edit 수만큼
            중복 실행되지 않게), **이력은 edit 별**(B1 이 각 편집을 보게).
          - 예외 안전망·문구는 단건 오케스트레이터와 동일.
        """
        from agent_cli.tools.edit_file import apply_edits_batch

        processed: list = []
        for args in edits:
            if isinstance(args, dict):
                args = TOOLS["edit_file"].strip_prefix(args)
            input_dict = args if isinstance(args, dict) else {"raw": str(args)}
            blocked, args, input_dict = self._run_pre_hooks(
                "edit_file", args, input_dict
            )
            if blocked is not None:
                # all-or-nothing: 어느 edit 도 적용하지 않았음을 명시.
                blocked.error += (
                    f" (batch of {len(edits)} same-file edits — none applied)"
                )
                return blocked
            processed.append(args if isinstance(args, dict) else input_dict)

        try:
            result = apply_edits_batch(path, processed)
        except Exception as e:  # 단건 경로와 동일 안전망 (KeyboardInterrupt 전파)
            import traceback as _tb

            _debug_log(
                f"TOOL EXCEPTION turn={self.state.turn} tool=edit_file(batch)\n"
                f"{_tb.format_exc()}"
            )
            result = ToolResult(
                False,
                error=(
                    f"Tool 'edit_file' raised "
                    f"{type(e).__name__}: {e}. "
                    f"This is likely a malformed input or an internal "
                    f"tool error. Review the action_input shape and "
                    f"retry, or try a different approach if the same "
                    f"input keeps failing."
                ),
            )

        first = processed[0] if processed else {}
        self._run_post_hooks("edit_file", first, result)
        for args in processed:
            self._record_tool_history("edit_file", args, result)
        return result

    # ── 1. PreToolUse hooks ────────────────────────────────────────
    def _run_pre_hooks(
        self, tool_name: str, tool_input, input_dict: dict
    ) -> tuple[ToolResult | None, object, dict]:
        """Fire PreToolUse hooks (Python runner first, then shell config).

        Returns ``(block_result, tool_input, input_dict)``:
            - ``block_result`` is None when both hook layers pass; if
              non-None the orchestrator must early-return with it.
            - ``tool_input`` is unchanged unless a hook called
              ``modify_input``/``updated_input``; ``input_dict`` mirrors
              that change when the new input is itself a dict.
        """
        if self.cfg.hook_runner:
            pre_ctx = self.cfg.hook_runner.fire(
                "PreToolUse",
                tool_name=tool_name,
                tool_input=input_dict,
                turn=self.state.turn,
                mcp_manager=self.cfg.mcp_manager,
            )
            if pre_ctx.is_blocked:
                return (
                    ToolResult(
                        False,
                        error=f"Blocked by PreToolUse hook: {pre_ctx.block_reason or 'hook denied'}",
                    ),
                    tool_input,
                    input_dict,
                )
            if pre_ctx.modified_input is not None:
                tool_input = pre_ctx.modified_input
                input_dict = tool_input

        if self.cfg.hooks_config:
            from agent_cli.hooks import run_hooks

            pre_result = run_hooks(
                "PreToolUse", tool_name, input_dict, hooks_config=self.cfg.hooks_config
            )
            if not pre_result.allowed:
                return (
                    ToolResult(
                        False,
                        error=f"Blocked by PreToolUse hook: {pre_result.stderr or 'hook denied'}",
                    ),
                    tool_input,
                    input_dict,
                )
            if pre_result.updated_input is not None:
                tool_input = pre_result.updated_input

        return None, tool_input, input_dict

    # ── 2. Agent dispatch (5.0.0 통합) ──────────────────────────────
    @staticmethod
    def _run_spec(args: dict) -> dict:
        """agent run op → 일회성 실행 spec (exec 의 task 스펙 shape)."""
        return {
            "task": args.get("task", ""),
            "context": args.get("context", "none"),
            "tools": args.get("tools"),
            "agent": args.get("profile", ""),
            "instructions": args.get("instructions", ""),
        }

    def _invoke_agent(self, tool_input, input_dict: dict) -> ToolResult:
        """agent 인터셉트 — 제네릭 execute 경로에 없는 provider/identity
        배선이 필요해 여기서 가로챈다.

        - ``{"tasks":[...]}`` : 배치 디스패처가 조립한 run fan-out →
          일회성 병렬 엔진(tool_delegate 경로)으로. OnAgentStart/End 훅이
          엔진 실행을 감싼다 (구 delegate 훅 승계 — run 에만 발화;
          상주 모드는 일반 PreToolUse/PostToolUse 로 충분).
        - ``engine="oneshot"`` 모드(run) 단건 : 같은 엔진의 단건 경로.
        - 그 외(상주 모드) : tool_agent(레지스트리) — 레지스트리 없는
          루프에서는 tool_agent 가 "main 전용" 에러로 거부 (모드 축소).

        라우팅은 ``AGENT_MODES`` 테이블의 ``engine`` 필드에서 파생 (모드
        테이블화 — 종전 ``mode == "run"`` 문자열 분기). 미지의 모드는
        registry 경로로 흘러 tool_agent 의 unknown-mode 에러가 처리
        (루프 경로에서는 validate 가 A5 로 먼저 거른다 — 여기 도달은
        직접 호출자/테스트뿐).
        """
        from agent_cli.subagent.agents_live import tool_agent

        # 함수-로컬 import — 호출 시점에 oneshot 모듈 attr 을 읽는 테스트
        # DI seam (모듈-레벨 바인딩이면 monkeypatch 가 안 닿는다).
        from agent_cli.subagent.oneshot import tool_delegate
        from agent_cli.tools.agent_tool import AGENT_MODES

        args = tool_input if isinstance(tool_input, dict) else {"mode": str(tool_input)}

        spec = AGENT_MODES.get(args.get("mode"))
        if "tasks" in args or (spec is not None and spec.engine == "oneshot"):
            raw = (
                {"tasks": args["tasks"]}
                if "tasks" in args
                else {"tasks": [self._run_spec(args)]}
            )
            if self.cfg.hook_runner:
                self.cfg.hook_runner.fire(
                    "OnAgentStart",
                    tool_name="agent",
                    tool_input=input_dict,
                    turn=self.state.turn,
                    mcp_manager=self.cfg.mcp_manager,
                )
            result = tool_delegate(
                args=raw,
                parent_ctx=self.ctx,
                provider=self.provider,
                model=self.cfg.model,
                capabilities=self.cfg.capabilities,
                provider_name=self.cfg.provider_name,
                base_url=self.cfg.base_url,
                api_key=self.cfg.api_key,
                depth=self.cfg.depth,
                max_depth=self.cfg.max_depth,
                max_turns=self.cfg.max_turns,
                timeout=self.cfg.agent_timeout,
                session=self.cfg.session,
                skill_stack=self.cfg.skill_stack,
                agent_stack=self.cfg.agent_stack,
                stop_event=self.state.stop_event,
                hooks_config=self.cfg.hooks_config,
                compaction_enabled=self.cfg.compaction_enabled,
            )
            if self.cfg.hook_runner:
                self.cfg.hook_runner.fire(
                    "OnAgentEnd",
                    tool_name="agent",
                    tool_input=input_dict,
                    delegate_result=result,
                    turn=self.state.turn,
                    mcp_manager=self.cfg.mcp_manager,
                )
            return result

        from agent_cli.runtime import AgentRuntime

        return tool_agent(
            args,
            registry=self.cfg.agent_registry,
            parent_ctx=self.ctx,
            # 단일 정의 (v8.39.0 조립기) — 종전 13키 dict 리터럴 3벌 중 하나.
            runtime=AgentRuntime.from_loop_config(self.cfg, self.provider).as_dict(),
        )

    # ── per-call loop context (execute + render seams share it) ─────
    def _run_ctx(self) -> RunContext:
        """The :class:`RunContext` for the CURRENT tool call — the single
        per-call bundle handed to both tool surfaces (``run``/``_run`` and
        ``render_oversized``): the session dir, the oversized cap, and the tool
        names callable in this loop. One construction point keeps the two seams
        from drifting on what "this call's context" is.

        Built lazily ONCE and cached: all three inputs are immutable after
        ``__init__`` (``tools_list`` is only depth/ctx-stripped there), and
        ``RunContext`` is frozen — so re-building it (plus the ``frozenset``)
        on every tool call and again on every oversized render was pure
        waste. If a per-call-VARYING field ever joins RunContext, drop this
        cache."""
        cached = getattr(self, "_run_ctx_cache", None)
        if cached is None:
            cached = RunContext(
                session_dir=self.ctx.session_dir if self.ctx else None,
                oversized_cap=self._oversized_cap,
                tools_available=frozenset(self.cfg.tools_list),
            )
            self._run_ctx_cache = cached
        return cached

    # ── 3. Regular tool dispatch ───────────────────────────────────
    def _invoke_regular(self, tool_name: str, tool_input) -> ToolResult:
        """Dispatch a non-agent tool via the registry.

        Recovery layer (A4/A5 detectors in ``_dispatch_op``) has
        already validated tool_name + action_input. The leaf primitive
        ``_execute_tool`` trusts that contract and would raise KeyError
        on a missing name.
        """
        return _execute_tool(tool_name, tool_input, ctx=self._run_ctx())

    # ── 4. PostToolUse hooks ───────────────────────────────────────
    def _run_post_hooks(
        self, tool_name: str, input_dict: dict, result: ToolResult
    ) -> None:
        """Fire PostToolUse hooks (runner + shell config). Pure side
        effect — never modifies ``result``.

        Failure runs route to ``PostToolUseFailure`` for the shell-config
        path so policy hooks can react differently to errors.
        """
        if self.cfg.hook_runner:
            self.cfg.hook_runner.fire(
                "PostToolUse",
                tool_name=tool_name,
                tool_input=input_dict,
                tool_result=result,
                turn=self.state.turn,
                mcp_manager=self.cfg.mcp_manager,
            )
        if self.cfg.hooks_config:
            from agent_cli.hooks import run_hooks

            _obs = result.output if result.success else result.error
            _evt = "PostToolUse" if result.success else "PostToolUseFailure"
            run_hooks(
                _evt,
                tool_name,
                input_dict,
                hooks_config=self.cfg.hooks_config,
                tool_result=_obs,
            )

    # ── result → observation seam (per-tool render + oversized cap) ──
    def _tool_observation(self, tool_name: str, result: ToolResult, args) -> str:
        """Turn a tool's ToolResult into the observation body that enters
        context. Two per-tool surfaces meet here: ``render_observation``
        (how this tool formats its result — default output/error) and
        ``apply_oversized_cap`` (whether the cap applies — default True).
        An over-cap body is replaced with a narrow-it nudge. Tools not in the
        registry (none today) fall back to the default render + cap on."""
        tool = TOOLS.get(tool_name)
        safe_args = args if isinstance(args, dict) else {}
        if tool is not None:
            body = tool.render_observation(result, safe_args)
            cap_on = tool.apply_oversized_cap
        else:
            body = result.output if result.success else result.error
            cap_on = True
        if cap_on and self._oversized_cap:
            tokens = estimate_tokens(body)
            if tokens > self._oversized_cap:
                # Per-tool over-cap policy (default = generic narrow-it nudge).
                # ``tools_available`` lets a tool tailor guidance to what the
                # model can actually call (e.g. delegate fan-out only when
                # delegate is not depth-stripped from this loop).
                if tool is not None:
                    return tool.render_oversized(
                        result,
                        safe_args,
                        body=body,
                        tokens=tokens,
                        ctx=self._run_ctx(),
                    )
                return default_oversized_nudge(tool_name, tokens, self._oversized_cap)
        return body

    # ── 5. recent_tool_history append ──────────────────────────────
    def _record_tool_history(
        self, tool_name: str, tool_input, result: ToolResult
    ) -> None:
        """Append one row to ``self.recent_tool_history`` (B1 action-loop
        detector reads from it). Records both successes and failures so
        the detector sees the same call repeated in error loops.
        """
        obs = result.output if result.success else result.error
        self.recent_tool_history.append(
            {
                "tool": tool_name,
                "input": _normalize_input(tool_input),
                "result": obs[:200],
                "turn": self.state.turn,
                "success": result.success,
            }
        )
