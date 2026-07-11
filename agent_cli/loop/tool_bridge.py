"""Agent loop: ReAct pattern with M1/M2 module integration."""

from __future__ import annotations

import json

from agent_cli.tools.result import ToolResult

from agent_cli.context.token_estimator import estimate_tokens
from agent_cli.tools import TOOLS, RunContext, _execute_tool
from agent_cli.tools.base import default_oversized_nudge
from agent_cli.tools.delegate import tool_delegate

from agent_cli.verbose import debug_log as _debug_log

# Max shrink-and-retry attempts per turn when the server rejects the
# prompt as too long (flow 2 reactive recovery). Each attempt sheds more
# history via ``ContextManager.force_fit``; the bound stops a runaway
# loop when the cache cannot shrink enough or the server keeps rejecting.
from agent_cli.loop.state import LoopConfig, LoopState


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
            if tool_name == "delegate":
                result = self._invoke_delegate(tool_input, input_dict)
            else:
                result = self._invoke_regular(tool_name, tool_input)
        except Exception as e:  # noqa: BLE001 — safety net by design
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

    # ── 2. Delegate dispatch (with delegate-specific hooks) ────────
    def _invoke_delegate(self, tool_input, input_dict: dict) -> ToolResult:
        """OnDelegateStart hook → tool_delegate(...) → OnDelegateEnd hook.

        The kwargs threaded into ``tool_delegate`` are too provider/
        identity-specific to fit the generic ``_execute_tool`` path,
        which is why delegate is intercepted here.
        """
        if self.cfg.hook_runner:
            self.cfg.hook_runner.fire(
                "OnDelegateStart",
                tool_name="delegate",
                tool_input=input_dict,
                turn=self.state.turn,
                mcp_manager=self.cfg.mcp_manager,
            )

        raw = tool_input if isinstance(tool_input, dict) else {"task": str(tool_input)}
        # Flat-native delegate (consolidation Step 3): a single op IS the flat
        # task spec — wrap the whole dict as a one-element tasks list so all
        # fields (task / context / tools / agent) survive. A parallel batch
        # arrives already shaped as ``{tasks:[...]}`` (assembled by the loop's
        # ``_dispatch_parallel_batch``) and passes through untouched.
        if "tasks" not in raw:
            raw = {"tasks": [raw]}
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
            timeout=self.cfg.delegate_timeout,
            session=self.cfg.session,
            skill_stack=self.cfg.skill_stack,
            agent_stack=self.cfg.agent_stack,
            stop_event=self.state.stop_event,
            hooks_config=self.cfg.hooks_config,
            compaction_enabled=self.cfg.compaction_enabled,
        )

        if self.cfg.hook_runner:
            self.cfg.hook_runner.fire(
                "OnDelegateEnd",
                tool_name="delegate",
                tool_input=input_dict,
                delegate_result=result,
                turn=self.state.turn,
                mcp_manager=self.cfg.mcp_manager,
            )
        return result

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
        """Dispatch a non-delegate tool via the registry.

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
