"""Agent loop: ReAct pattern with M1/M2 module integration."""

from __future__ import annotations

import json
import re
import time

from agent_cli.constants import OBS_SUCCESS, OBS_ERROR, OBS_ERROR_HINT

from agent_cli.context.manager import ContextManager
from agent_cli.context.overflow import check_preemptive_overflow, is_context_overflow
from agent_cli.parsing.react_parser import parse_react
from agent_cli.prompts.system_prompt import build_system_prompt
from agent_cli.providers.base import LLMProvider
from agent_cli.providers.compat import ModelCapabilities, needs_tool_action
from agent_cli.render import (
    render_context_dump,
    render_header,
    render_iter_sep,
    render_raw,
    render_status,
    render_step,
)
from agent_cli.tools import TOOLS, execute_tool, validate_tool_input
from agent_cli.tools.delegate import tool_delegate
from agent_cli.tools.registry import convert_to_anthropic_tools, convert_to_openai_tools
from agent_cli.tools.truncation import get_truncation_config, truncate_output

_debug_verbose = False


def _set_debug_verbose(v: bool) -> None:
    """Enable/disable debug logging to stderr."""
    global _debug_verbose
    _debug_verbose = v


def _debug_log(msg: str) -> None:
    """Print debug message to stderr (only when verbose mode is on)."""
    if not _debug_verbose:
        return
    import sys

    print(f"[debug {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr)


# Checkpoint system: nudges LLM to self-assess when iteration count gets high.
# _CHECKPOINT_FIRST: first check after N iterations. 50 chosen because most
#   tasks complete in 10-30 iterations; 50 indicates potential stuck state.
# _CHECKPOINT_INTERVAL: repeat every M iterations after first. 20 gives LLM
#   enough room to recover without being too aggressive.
# At each checkpoint, the last _CHECKPOINT_INTERVAL tool calls are shown.
_CHECKPOINT_FIRST = 50
_CHECKPOINT_INTERVAL = 20
assert _CHECKPOINT_FIRST >= _CHECKPOINT_INTERVAL, (
    f"CHECKPOINT_FIRST ({_CHECKPOINT_FIRST}) must be >= "
    f"CHECKPOINT_INTERVAL ({_CHECKPOINT_INTERVAL})"
)


class AgentLoop:
    """Encapsulates the ReAct agent loop state and execution."""

    def __init__(
        self,
        query: str,
        provider: LLMProvider,
        capabilities: ModelCapabilities,
        model: str,
        provider_name: str = "ollama",
        base_url: str = "",
        api_key: str = "",
        max_iter: int = 0,
        verbose: bool = False,
        ctx: ContextManager | None = None,
        quiet: bool = False,
        depth: int = 0,
        max_depth: int = 2,
        delegate_timeout: int = 300,
        active_tools: list[str] | None = None,
        plan_context: str | None = None,
        session=None,  # SessionMeta — avoid circular import
        hooks_config: dict | None = None,
        skill_name: str = "",
        skill_stack: list[str] | None = None,
        skill_args: str = "",
    ):
        self.query = query
        self.provider = provider
        self.capabilities = capabilities
        self.model = model
        self.provider_name = provider_name
        self.base_url = base_url
        self.api_key = api_key
        self.max_iter = max_iter
        self.verbose = verbose
        self.ctx = ctx
        self.quiet = quiet
        self.depth = depth
        self.max_depth = max_depth
        self.delegate_timeout = delegate_timeout
        self.plan_context = plan_context
        self.session = session
        self.hooks_config = hooks_config
        self.skill_name = skill_name
        self.skill_args = skill_args

        # Derived state
        self.include_delegate = depth < max_depth
        self.tools_list = active_tools or list(TOOLS.keys())
        # Remove "ask" in non-interactive mode (no ctx)
        if not ctx and "ask" in self.tools_list:
            self.tools_list = [t for t in self.tools_list if t != "ask"]
        # Build skill stack for recursive call prevention
        if skill_stack is None:
            skill_stack = []
        if skill_name:
            skill_stack = [*skill_stack, skill_name]
        self.skill_stack = skill_stack

        # Loop state
        self.iteration = 0
        self.tools_called: list[str] = []
        self.overflow_retried = False
        self.recent_tool_history: list[dict] = []
        self.messages: list[dict] = []
        self.system = ""
        # Sentinels: distinct from None (failure) and str (answer)
        self._CONTINUE = object()  # keep looping
        self._RETRY = object()  # overflow retry

    def run(self) -> str | None:
        """Main entry point -- replaces run_loop body.

        Returns the final answer string, or None if max iterations reached.
        """
        self._setup()
        while self._should_continue():
            self.iteration += 1
            self._begin_iteration()
            result = self._execute_iteration()
            if result is not self._CONTINUE:
                return result
        return self._on_max_iter()

    def _setup(self) -> None:
        """Initialize system prompt, scratchpad, messages."""
        _set_debug_verbose(self.verbose)

        # Note: run_skill stays in tools_list -- skill_stack prevents recursion
        # System prompt hides skills already in the stack from LLM
        self.system = build_system_prompt(
            capabilities=self.capabilities,
            active_tools=self.tools_list,
            include_delegate=self.include_delegate,
            plan_context=self.plan_context,
            skill_stack=self.skill_stack,
        )

        if not self.quiet:
            render_header(
                self.provider_name,
                self.model,
                self.max_iter,
                skill_name=self.skill_name,
                skill_args=self.skill_args,
            )

        # Log query to session (skip for skill internal loops)
        if self.session and self.depth == 0 and not self.skill_name:
            _log_to_session(
                self.session,
                {"iter": 0, "action": "query", "observation": self.query},
            )

        # Scratchpad: auto-init on first run, set skill context if inside a skill
        if self.ctx:
            from agent_cli.context.scratchpad import load_scratchpad

            if not load_scratchpad(self.ctx._scratchpad_dir):
                self.ctx.init_task(self.query)
            # Set or clear skill context (for artifact subdirectory routing)
            self.ctx.set_skill_context(
                skill_name=self.skill_name,
                parent_turn=self.ctx._turn_count if self.skill_name else 0,
            )

        # Message setup
        if self.ctx:
            self.ctx.add("user", self.query)
            self.messages = self.ctx.get_messages()
        else:
            self.messages = [{"role": "user", "content": self.query}]

    def _should_continue(self) -> bool:
        return self.max_iter <= 0 or self.iteration < self.max_iter

    def _begin_iteration(self) -> None:
        """Scratchpad begin_turn, skill progress, iter sep."""
        # Scratchpad: begin turn for each iteration
        if self.ctx:
            self.ctx.begin_turn(self.query)
        # Skill progress: shown per-tool after LLM response (see _render_skill_progress)
        if not self.quiet:
            render_iter_sep(self.iteration)

    def _execute_iteration(self) -> str | None:
        """Single iteration: checkpoint, overflow, LLM call, dispatch."""
        # 1. Checkpoint: nudge LLM if running too long
        self._maybe_checkpoint()

        # 2. Preemptive overflow check
        if check_preemptive_overflow(self.messages, self.capabilities):
            if self.ctx:
                render_status("running", "Compressing context (preemptive)...")
                self.ctx.force_compress()
                self.messages = self.ctx.get_messages()
            else:
                # Single-shot: trim oldest observations
                self.messages = _trim_old_observations(self.messages, self.capabilities)

        # 2. Prepare tools for native tool calling
        call_kwargs: dict = {}
        if self.capabilities.supports_tool_calling:
            if self.provider_name == "anthropic":
                call_kwargs["tools"] = convert_to_anthropic_tools(
                    self.tools_list, include_delegate=self.include_delegate
                )
            elif self.provider_name == "openai":
                call_kwargs["tools"] = convert_to_openai_tools(
                    self.tools_list, include_delegate=self.include_delegate
                )

        # 3. LLM call
        response = self._call_llm(call_kwargs)
        if response is None:
            return None  # LLM failed
        if response == self._RETRY:
            return self._CONTINUE  # Overflow retry

        llm_text = response.content

        if not self.quiet:
            render_raw(llm_text, self.iteration, self.verbose)

        # 4. Native tool calling path (Anthropic/OpenAI)
        if response.tool_calls:
            return self._handle_native_path(response, llm_text)

        # 5. Text parsing path (Ollama, fallback)
        return self._handle_text_path(llm_text)

    def _maybe_checkpoint(self) -> None:
        """Inject checkpoint message if iteration count is high."""
        if self.iteration >= _CHECKPOINT_FIRST and (
            (self.iteration - _CHECKPOINT_FIRST) % _CHECKPOINT_INTERVAL == 0
        ):
            recent = self.recent_tool_history[-_CHECKPOINT_INTERVAL:]
            history_summary = "\n".join(
                f"  iter {h['iter']}: {h['tool']} → {h['result'][:100]}" for h in recent
            )
            checkpoint_msg = (
                f"[SYSTEM] CHECKPOINT — {self.iteration} iterations used.\n"
                f"Recent tool calls:\n{history_summary}\n\n"
                f"You MUST now do ONE of:\n"
                f'1. Use the complete tool: {{"thought": "...", "action": "complete", "action_input": {{"result": "your result"}}}}\n'
                f"2. If genuinely incomplete, explain what SPECIFIC step remains and do it.\n\n"
                f"Do NOT call echo, cat, or any tool just to confirm completion.\n"
                f"Do NOT repeat previous tool calls.\n"
                f"If you already completed the task, call the complete tool IMMEDIATELY."
            )
            self.messages.append({"role": "user", "content": checkpoint_msg})
            if self.ctx:
                self.ctx.add("user", checkpoint_msg)
            if not self.quiet:
                render_status("running", f"Checkpoint at iteration {self.iteration}")

    def _call_llm(self, call_kwargs: dict):
        """LLM call with overflow retry. Returns response or None on failure."""
        # Context dump (verbose only)
        if self.verbose and not self.quiet:
            render_context_dump(self.messages, self.iteration)
        _debug_log(
            f"LLM_CALL iter={self.iteration} skill={self.skill_name or 'main'} msg_count={len(self.messages)}"
        )

        try:
            response = self.provider.call(
                messages=self.messages,
                system=self.system,
                model=self.model,
                capabilities=self.capabilities,
                **call_kwargs,
            )
            return response
        except Exception as e:
            if is_context_overflow(str(e)):
                if self.ctx and not self.overflow_retried:
                    render_status("running", "Context overflow — compressing...")
                    self.ctx.force_compress()
                    self.messages = self.ctx.get_messages()
                    self.overflow_retried = True
                    self.iteration -= 1
                    return self._RETRY
            _debug_log(
                f"LLM call failed: {e} skill_name={self.skill_name} iter={self.iteration}"
            )
            render_step("error", f"LLM call failed: {e}", self.iteration)
            return None

    def _handle_native_path(self, response, llm_text: str) -> str | None:
        """Handle native tool calling response (Anthropic/OpenAI)."""
        if len(response.tool_calls) == 1:
            first_toolcall = response.tool_calls[0]

            # 4a. Complete tool -> extract result and return
            if first_toolcall["name"] == "complete":
                _render_skill_progress(
                    self.skill_name,
                    self.iteration,
                    "complete",
                    {},
                    self.quiet,
                    thought=llm_text[:100],
                )
                answer = (
                    first_toolcall.get("input", {}).get("result") or "(completed)"
                    if isinstance(first_toolcall.get("input"), dict)
                    else "(completed)"
                )
                # Fulfillment guard
                if not self.tools_called and needs_tool_action(self.query):
                    render_status(
                        "error",
                        "Answer rejected — no tool actions performed yet.",
                        self.iteration,
                    )
                    # Fall through to execute as normal tool (will fail gracefully)
                else:
                    _log_tool_to_session(
                        self.session,
                        self.depth,
                        self.iteration,
                        "complete",
                        answer,
                    )
                    # Scratchpad: save complete result
                    if self.ctx:
                        self.ctx.end_turn(
                            content=answer,
                            tags=_build_artifact_tags("complete", {}, self.skill_name),
                            summary=_build_artifact_summary("complete", {}, answer),
                        )
                    if not self.quiet:
                        render_step("final", answer, self.iteration)
                    return answer

            # 4b. Ask tool -- prompt user (native path)
            if first_toolcall["name"] == "ask":
                questions = _extract_questions(first_toolcall.get("input"))
                if questions:
                    user_response = _handle_ask(questions, self.quiet)
                    obs_msg = f"User responded:\n{user_response}"
                    _append_native_observation(
                        self.messages,
                        self.ctx,
                        self.provider_name,
                        response,
                        [{"tool_call": first_toolcall, "output": obs_msg}],
                    )
                    return self._CONTINUE

            # 4c. run_skill -- intercept at loop level (needs ctx)
            if first_toolcall["name"] == "run_skill":
                skill_input = (
                    first_toolcall.get("input", {})
                    if isinstance(first_toolcall.get("input"), dict)
                    else {}
                )
                obs = _handle_run_skill(
                    skill_input,
                    self.provider_name,
                    self.base_url,
                    self.api_key,
                    self.capabilities,
                    self.model,
                    self.ctx,
                    self.session,
                    self.skill_name,
                    skill_stack=self.skill_stack,
                )
                if not self.quiet:
                    render_step(
                        "observation",
                        obs,
                        self.iteration,
                        tool_name="run_skill",
                    )
                _append_native_observation(
                    self.messages,
                    self.ctx,
                    self.provider_name,
                    response,
                    [{"tool_call": first_toolcall, "output": obs}],
                )
                self.tools_called.append("run_skill")
                return self._CONTINUE

            # 4c-2. read_artifact -- needs ctx for scratchpad_dir
            if first_toolcall["name"] == "read_artifact":
                art_input = (
                    first_toolcall.get("input", {})
                    if isinstance(first_toolcall.get("input"), dict)
                    else {}
                )
                from agent_cli.tools.read_artifact import tool_read_artifact

                art_result = tool_read_artifact(art_input, ctx=self.ctx)
                obs = art_result.output if art_result.success else art_result.error
                if not self.quiet:
                    render_step(
                        "observation",
                        obs,
                        self.iteration,
                        tool_name="read_artifact",
                    )
                _append_native_observation(
                    self.messages,
                    self.ctx,
                    self.provider_name,
                    response,
                    [{"tool_call": first_toolcall, "output": obs}],
                )
                self.tools_called.append("read_artifact")
                return self._CONTINUE

            # 4d. Echo-as-final-answer pattern
            echo_answer = _try_echo_as_final(
                first_toolcall["name"], first_toolcall["input"]
            )
            if echo_answer:
                if self.ctx:
                    self.ctx.end_turn(
                        content=echo_answer,
                        tags=_build_artifact_tags("complete", {}, self.skill_name),
                        summary=_build_artifact_summary("complete", {}, echo_answer),
                    )
                if not self.quiet:
                    render_step("final", echo_answer, self.iteration)
                return echo_answer

        observations = []
        for tc in response.tool_calls:
            tool_name = tc["name"]
            tool_input = tc["input"]
            _render_skill_progress(
                self.skill_name,
                self.iteration,
                tool_name,
                tool_input,
                self.quiet,
                thought=llm_text[:100],
            )

            if not self.quiet:
                render_step(
                    "action",
                    "",
                    self.iteration,
                    tool_name=tool_name,
                    tool_input=json.dumps(tool_input, ensure_ascii=False)
                    if isinstance(tool_input, dict)
                    else str(tool_input),
                )

            # Execute tool (shared logic -- tracks tools_called + history)
            obs = _execute_single_tool(
                tool_name,
                tool_input,
                self.tools_list,
                self.include_delegate,
                self.capabilities,
                self.provider_name,
                self.model,
                self.base_url,
                self.api_key,
                self.delegate_timeout,
                self.tools_called,
                self.recent_tool_history,
                self.iteration,
                hooks_config=self.hooks_config,
            )

            if not self.quiet:
                render_step("observation", obs, self.iteration, tool_name=tool_name)
            observations.append({"tool_call": tc, "output": obs})

            # Scratchpad: save tool result as artifact
            if self.ctx:
                self.ctx.end_turn(
                    content=obs,
                    tags=_build_artifact_tags(tool_name, tool_input, self.skill_name),
                    summary=_build_artifact_summary(tool_name, tool_input, obs),
                )

            _log_tool_to_session(
                self.session,
                self.depth,
                self.iteration,
                tool_name,
                obs,
                action_input=_normalize_input(tool_input),
            )

        # Repeated call detection
        if _detect_repeated_calls(self.recent_tool_history):
            last = self.recent_tool_history[-1]
            _debug_log(
                f"Repeated call: {last['tool']} input={last['input'][:100]} skill_name={self.skill_name}"
            )
            render_status(
                "error",
                f"Repeated call detected: {last['tool']} called "
                f"{_REPEAT_THRESHOLD} times with same input. Stopping.",
            )
            return ""  # Non-None to exit loop

        # Format messages based on provider
        _append_native_observation(
            self.messages, self.ctx, self.provider_name, response, observations
        )
        return self._CONTINUE

    def _handle_text_path(self, llm_text: str) -> str | None:
        """Handle text parsing response (Ollama, fallback)."""
        parsed = parse_react(llm_text)

        # 6. Thought
        if parsed.thought and not self.quiet:
            render_step("thought", parsed.thought, self.iteration)

        # 7. Complete tool (text parsing path)
        if parsed.action == "complete":
            _render_skill_progress(
                self.skill_name,
                self.iteration,
                "complete",
                {},
                self.quiet,
                thought=parsed.thought or "",
            )
            if isinstance(parsed.action_input, dict):
                answer = parsed.action_input.get("result") or "(completed)"
            elif isinstance(parsed.action_input, str):
                answer = parsed.action_input or "(completed)"
            else:
                answer = "(completed)"

            # Fulfillment guard -- check BEFORE rendering
            if not self.tools_called and needs_tool_action(self.query):
                nudge = (
                    "You called the complete tool, but the task likely requires "
                    "tool actions (file operations, shell commands, etc.). "
                    "Please use the appropriate tools first, then call complete."
                )
                _append_text_observation(self.messages, self.ctx, llm_text, nudge)
                render_status(
                    "error",
                    "Answer rejected — no tool actions performed yet.",
                    self.iteration,
                )
                return self._CONTINUE

            _log_tool_to_session(
                self.session,
                self.depth,
                self.iteration,
                "complete",
                answer,
                thought=parsed.thought or "",
            )

            # Scratchpad: save complete result
            if self.ctx:
                self.ctx.end_turn(
                    content=answer,
                    tags=_build_artifact_tags("complete", {}, self.skill_name),
                    summary=_build_artifact_summary("complete", {}, answer),
                )
            if not self.quiet:
                render_step("final", answer, self.iteration)
            return answer

        # 9. Detect echo-as-final-answer (common small model pattern)
        echo_answer = _try_echo_as_final(parsed.action, parsed.action_input)
        if echo_answer:
            _log_tool_to_session(
                self.session,
                self.depth,
                self.iteration,
                "complete (echo)",
                echo_answer,
                thought=parsed.thought or "",
            )
            if self.ctx:
                self.ctx.end_turn(
                    content=echo_answer,
                    tags=_build_artifact_tags("complete", {}, self.skill_name),
                    summary=_build_artifact_summary("complete", {}, echo_answer),
                )
            if not self.quiet:
                render_step("final", echo_answer, self.iteration)
            return echo_answer

        # 10. Ask tool -- prompt user for input (text parsing path)
        if parsed.action == "ask":
            questions = _extract_questions(parsed.action_input)
            if questions:
                user_response = _handle_ask(questions, self.quiet)
                obs_msg = f"Observation: User responded:\n{user_response}\n\nContinue. Respond with JSON only."
                _append_text_observation(self.messages, self.ctx, llm_text, obs_msg)
                _log_tool_to_session(
                    self.session,
                    self.depth,
                    self.iteration,
                    "ask",
                    user_response,
                    thought=parsed.thought or "",
                )
                return self._CONTINUE

        # 10b. run_skill -- intercept at loop level (text parsing path)
        if parsed.action == "run_skill":
            skill_input = (
                parsed.action_input if isinstance(parsed.action_input, dict) else {}
            )
            obs = _handle_run_skill(
                skill_input,
                self.provider_name,
                self.base_url,
                self.api_key,
                self.capabilities,
                self.model,
                self.ctx,
                self.session,
                self.skill_name,
                skill_stack=self.skill_stack,
            )
            if not self.quiet:
                render_step("observation", obs, self.iteration, tool_name="run_skill")
            obs_msg = f"Observation: {obs}\n\nContinue with the next step. Respond with JSON only."
            _append_text_observation(self.messages, self.ctx, llm_text, obs_msg)
            self.tools_called.append("run_skill")
            _log_tool_to_session(
                self.session,
                self.depth,
                self.iteration,
                "run_skill",
                obs,
                thought=parsed.thought or "",
                action_input=_normalize_input(skill_input),
            )
            return self._CONTINUE

        # 10c. read_artifact -- needs ctx (text parsing path)
        if parsed.action == "read_artifact":
            art_input = (
                parsed.action_input if isinstance(parsed.action_input, dict) else {}
            )
            from agent_cli.tools.read_artifact import tool_read_artifact

            art_result = tool_read_artifact(art_input, ctx=self.ctx)
            obs = art_result.output if art_result.success else art_result.error
            if not self.quiet:
                render_step(
                    "observation",
                    obs,
                    self.iteration,
                    tool_name="read_artifact",
                )
            obs_msg = f"Observation: {obs}\n\nContinue with the next step. Respond with JSON only."
            _append_text_observation(self.messages, self.ctx, llm_text, obs_msg)
            self.tools_called.append("read_artifact")
            return self._CONTINUE

        # 11. Tool execution (text parsing path)
        if parsed.action:
            tool_name = parsed.action
            tool_input = parsed.action_input or {}
            _render_skill_progress(
                self.skill_name,
                self.iteration,
                tool_name,
                tool_input,
                self.quiet,
                thought=parsed.thought or "",
            )

            if not self.quiet:
                render_step(
                    "action",
                    "",
                    self.iteration,
                    tool_name=tool_name,
                    tool_input=json.dumps(tool_input, ensure_ascii=False)
                    if isinstance(tool_input, dict)
                    else str(tool_input),
                )

            # Execute tool (shared logic -- tracks tools_called + history)
            observation = _execute_single_tool(
                tool_name,
                tool_input,
                self.tools_list,
                self.include_delegate,
                self.capabilities,
                self.provider_name,
                self.model,
                self.base_url,
                self.api_key,
                self.delegate_timeout,
                self.tools_called,
                self.recent_tool_history,
                self.iteration,
                hooks_config=self.hooks_config,
            )

            if not self.quiet:
                render_step(
                    "observation",
                    observation,
                    self.iteration,
                    tool_name=tool_name,
                )

            # Scratchpad: save tool result as artifact
            if self.ctx:
                self.ctx.end_turn(
                    content=observation,
                    tags=_build_artifact_tags(tool_name, tool_input, self.skill_name),
                    summary=_build_artifact_summary(tool_name, tool_input, observation),
                )

            # Repeated call detection
            if _detect_repeated_calls(self.recent_tool_history):
                last = self.recent_tool_history[-1]
                _debug_log(
                    f"Repeated call: {last['tool']} input={last['input'][:100]} skill_name={self.skill_name}"
                )
                render_status(
                    "error",
                    f"Repeated call detected: {last['tool']} called "
                    f"{_REPEAT_THRESHOLD} times with same input. Stopping.",
                )
                return None

            _log_tool_to_session(
                self.session,
                self.depth,
                self.iteration,
                tool_name,
                observation,
                thought=parsed.thought or "",
                action_input=_normalize_input(tool_input),
            )

            # Inject observation
            obs_msg = f"Observation: {observation}\n\nContinue with the next step. Respond with JSON only."
            _append_text_observation(self.messages, self.ctx, llm_text, obs_msg)
            return self._CONTINUE

        # 12. Missing action or parse failure -- retry with appropriate hint
        if parsed.parse_stage > 0:
            # JSON parsed OK but no action -- LLM forgot to include action
            _debug_log(
                f"No action in parsed JSON (stage={parsed.parse_stage}):\n{llm_text}"
            )
            render_status(
                "error",
                "Response has no action. Retrying...",
                self.iteration,
            )
            retry_msg = (
                "Your JSON was parsed but has no action. "
                "You MUST include an action. Either use a tool: "
                '{"thought": "...", "action": "tool_name", "action_input": {...}} '
                "or complete the task: "
                '{"thought": "...", "action": "complete", "action_input": {"result": "..."}}'
            )
        else:
            # JSON parse failed entirely
            _debug_log(f"JSON parse failed (stage={parsed.parse_stage}):\n{llm_text}")
            render_status(
                "error",
                "Invalid JSON response. Retrying...",
                self.iteration,
            )
            retry_msg = (
                "Your response was not valid JSON. "
                "Output ONLY a JSON object: "
                '{"thought": "...", "action": "tool_name", "action_input": {...}}. '
                "No markdown fences, no extra text."
            )
        _append_text_observation(self.messages, self.ctx, llm_text, retry_msg)
        self.iteration -= 1  # Don't count format retries
        return self._CONTINUE

    def _on_max_iter(self) -> None:
        """Handle max iterations reached."""
        if not self.quiet:
            render_status("error", f"Max iterations ({self.max_iter}) reached.")
        _debug_log(
            f"run_loop returning None: max_iter={self.max_iter} reached, skill_name={self.skill_name}"
        )
        return None


# Backward-compatible wrapper
def run_loop(
    query: str,
    provider: LLMProvider,
    capabilities: ModelCapabilities,
    model: str,
    provider_name: str = "ollama",
    base_url: str = "",
    api_key: str = "",
    max_iter: int = 0,
    verbose: bool = False,
    ctx: ContextManager | None = None,
    quiet: bool = False,
    depth: int = 0,
    max_depth: int = 2,
    delegate_timeout: int = 300,
    active_tools: list[str] | None = None,
    plan_context: str | None = None,
    session=None,  # SessionMeta — avoid circular import
    hooks_config: dict | None = None,
    skill_name: str = "",
    skill_stack: list[str] | None = None,
    skill_args: str = "",
) -> str | None:
    """Run the ReAct agent loop.

    Returns the final answer string, or None if max iterations reached.
    """
    return AgentLoop(
        query=query,
        provider=provider,
        capabilities=capabilities,
        model=model,
        provider_name=provider_name,
        base_url=base_url,
        api_key=api_key,
        max_iter=max_iter,
        verbose=verbose,
        ctx=ctx,
        quiet=quiet,
        depth=depth,
        max_depth=max_depth,
        delegate_timeout=delegate_timeout,
        active_tools=active_tools,
        plan_context=plan_context,
        session=session,
        hooks_config=hooks_config,
        skill_name=skill_name,
        skill_stack=skill_stack,
        skill_args=skill_args,
    ).run()


def _extract_questions(action_input) -> list[str]:
    """Extract questions list from ask tool input, handling all formats."""
    if isinstance(action_input, dict):
        raw_questions = action_input.get("questions") or action_input.get("question")
    elif isinstance(action_input, str):
        raw_questions = action_input
    elif isinstance(action_input, list):
        raw_questions = action_input
    else:
        return []
    # Normalize to list
    if isinstance(raw_questions, str):
        return [raw_questions] if raw_questions else []
    if isinstance(raw_questions, list):
        return [str(q) for q in raw_questions if q]
    return []


def _handle_ask(questions: list[str], quiet: bool) -> str:
    """Display questions to the user and collect responses."""
    responses = []
    for q in questions:
        try:
            answer = input(f"\nAgent asks: {q}\nYour answer: ").strip()
        except (EOFError, KeyboardInterrupt):
            answer = "(no response)"
        responses.append(f"Q: {q}\nA: {answer}")
    return "\n".join(responses)


def _render_skill_progress(
    skill_name: str,
    iteration: int,
    tool_name: str,
    tool_input,
    quiet: bool,
    thought: str = "",
) -> None:
    """Show skill progress: thought first, then action."""
    if not skill_name or not quiet:
        return
    from agent_cli.render import C, console

    # Build action detail from tool input
    detail = ""
    if isinstance(tool_input, dict):
        if tool_name in ("read_file", "write_file", "edit_file"):
            path = tool_input.get("path", "")
            if path:
                detail = f" {path.split('/')[-1]}"
        elif tool_name == "shell":
            cmd = tool_input.get("command", "")
            if cmd:
                detail = f" {cmd[:60]}"
        elif tool_name == "run_skill":
            name = tool_input.get("name", "")
            detail = f" {name}"

    # Line 1: thought (full text)
    if thought:
        t = thought.replace("\n", " ").strip()
        console.print(
            f"  [{C['muted']}]skill:{skill_name} [{iteration}] 💭 {t}[/]",
            highlight=False,
        )

    # Line 2: action
    if tool_name == "complete":
        console.print(
            f"  [{C['muted']}]skill:{skill_name}[/]"
            f" [{C['accent']}][{iteration}] ✅ {tool_name}{detail}[/]",
            highlight=False,
        )
    else:
        console.print(
            f"  [{C['muted']}]skill:{skill_name}"
            f" [{iteration}] ⚡ {tool_name}:{detail}[/]",
            highlight=False,
        )


def _build_internal_skill_summary(ctx, turn_before: int) -> str:
    """Build a summary of internal skill calls that happened since turn_before."""
    if not ctx:
        return ""
    from agent_cli.context.scratchpad import build_artifact_index

    index = build_artifact_index(ctx._scratchpad_dir)
    internal = []
    for a in index:
        if a.turn <= turn_before:
            continue
        if "complete" not in a.tags:
            continue
        skill_tag = next((t for t in a.tags if t.startswith("skill:")), None)
        if skill_tag:
            internal.append(f"- run_skill({skill_tag[6:]}): {a.summary}")

    if not internal:
        return ""
    return "\n[Internal skill calls during this execution:]\n" + "\n".join(internal)


def _handle_run_skill(
    skill_input: dict,
    provider_name: str,
    base_url: str,
    api_key: str,
    capabilities: ModelCapabilities,
    model: str,
    ctx,
    session,
    parent_skill_name: str = "",
    skill_stack: list[str] | None = None,
) -> str:
    """Handle run_skill at loop level with full ctx access."""
    from agent_cli.skills import load_skills
    from agent_cli.skills.executor import execute_skill

    name = skill_input.get("name", "")
    arguments = skill_input.get("arguments", "")
    # LLM might send arguments as dict instead of string
    if not isinstance(arguments, str):
        arguments = str(arguments) if arguments else ""

    if not name:
        return OBS_ERROR.format(error="run_skill: 'name' is required.")

    # Skill stack: prevent recursive calls (A→B→A)
    if skill_stack and name in skill_stack:
        return OBS_ERROR.format(
            error=f"Recursive skill call blocked: '{name}' is already in the call stack {skill_stack}."
        )

    skills = load_skills()
    if name not in skills:
        available = ", ".join(skills.keys()) if skills else "(none)"
        return OBS_ERROR.format(
            error=f"Skill '{name}' not found. Available: {available}"
        )

    skill = skills[name]
    if skill.disable_model_invocation:
        return OBS_ERROR.format(
            error=f"Skill '{name}' is user-only (disable-model-invocation)."
        )

    # Set skill context for subdirectory routing
    if ctx:
        ctx.set_skill_context(skill_name=name, parent_turn=ctx._turn_count)

    render_status("running", f"Running skill: {name}...")
    turn_before = ctx._turn_count if ctx else 0

    try:
        from agent_cli.providers import create_provider

        provider = create_provider(provider_name, base_url, api_key)
        result = execute_skill(
            skill=skill,
            arguments=arguments,
            provider=provider,
            capabilities=capabilities,
            model=model,
            provider_name=provider_name,
            base_url=base_url,
            api_key=api_key,
            quiet=True,
            ctx=ctx,
            session=session,
            skill_stack=skill_stack,
        )
    except Exception as e:
        result = None
        _debug_log(f"run_skill({name}) exception: {e}")
        obs = OBS_ERROR.format(error=f"run_skill({name}) failed: {e}")
    else:
        if not result:
            _debug_log(
                f"run_skill({name}) returned {repr(result)} (inner loop stopped without complete)"
            )
        skill_header = (
            f"SKILL: {name}({arguments})\n" if arguments else f"SKILL: {name}\n"
        )
        body = result or "(skill returned no result)"
        internal = _build_internal_skill_summary(ctx, turn_before)
        obs = OBS_SUCCESS.format(result=f"{skill_header}{body}{internal}")
    finally:
        # Reset skill context
        if ctx:
            ctx.set_skill_context()

    return obs


def _build_artifact_summary(tool_name: str, tool_input, obs: str = "") -> str:
    """Build a human-readable summary for scratchpad progress."""
    if tool_name == "complete":
        # Preview of the result
        preview = obs[:80].replace("\n", " ").strip()
        return f"Task completed: {preview}"

    if isinstance(tool_input, dict):
        if tool_name in ("read_file", "write_file", "edit_file"):
            filepath = tool_input.get("path", "")
            if filepath:
                # Count lines in observation
                lines = obs.count("\n") + 1 if obs else 0
                fname = filepath.split("/")[-1]
                return f"{tool_name}: {fname} ({lines}줄)"
        if tool_name == "shell":
            cmd = tool_input.get("command", "")
            if cmd:
                return f"shell: {cmd[:60]}"
        if tool_name == "delegate":
            task = tool_input.get("task", "")
            return f"delegate: {task[:80]}"

    return f"{tool_name} executed"


def _build_artifact_tags(tool_name: str, tool_input, skill_name: str = "") -> list[str]:
    """Build artifact tags from tool context."""
    tags = [tool_name]

    # Extract filepath for file tools
    if isinstance(tool_input, dict):
        filepath = tool_input.get("path", "")
        if filepath and tool_name in ("read_file", "write_file", "edit_file"):
            tags.append(filepath)

    # Add skill tag if inside a skill
    if skill_name:
        tags.append(f"skill:{skill_name}")

    return tags


def _log_to_session(session, entry: dict) -> None:
    """Append an iteration entry to the session log (no-op if no session)."""
    if session is None:
        return
    from agent_cli.context.session import append_log

    entry["ts"] = time.time()
    append_log(session, entry)


def _log_tool_to_session(
    session,
    depth: int,
    iteration: int,
    action: str,
    observation: str,
    thought: str = "",
    action_input: str = "",
) -> None:
    """Log a tool execution to session (depth==0 only)."""
    if depth != 0:
        return
    entry: dict = {
        "iter": iteration,
        "action": action,
        "observation": observation[:500],
    }
    if thought:
        entry["thought"] = thought
    if action_input:
        entry["action_input"] = action_input
    _log_to_session(session, entry)


def _execute_single_tool(
    tool_name: str,
    tool_input,
    tools_list: list[str],
    include_delegate: bool,
    capabilities: ModelCapabilities,
    provider_name: str = "",
    model: str = "",
    base_url: str = "",
    api_key: str = "",
    delegate_timeout: int = 300,
    tools_called: list[str] | None = None,
    recent_tool_history: list[dict] | None = None,
    iteration: int = 0,
    hooks_config: dict | None = None,
) -> str:
    """Execute a single tool, track history, and return observation string."""
    obs = _do_execute_tool(
        tool_name,
        tool_input,
        tools_list,
        include_delegate,
        capabilities,
        provider_name,
        model,
        base_url,
        api_key,
        delegate_timeout,
        hooks_config=hooks_config,
    )

    # Track tool usage
    if tools_called is not None:
        tools_called.append(tool_name)
    if recent_tool_history is not None:
        recent_tool_history.append(
            {
                "tool": tool_name,
                "input": _normalize_input(tool_input),
                "result": obs[:200],
                "iter": iteration,
            }
        )

    return obs


# Repeated call detection: if the same tool is called with identical input
# N times consecutively, assume the LLM is stuck and force exit.
# 3 chosen as minimum to distinguish genuine retries from loops.
_REPEAT_THRESHOLD = 3

# Regex: simple echo with no pipes, redirects, subshells, or chaining
_ECHO_FINAL_RE = re.compile(
    r'^echo\s+["\']?(.+?)["\']?\s*$',
    re.DOTALL,
)


def _try_echo_as_final(tool_name: str, tool_input) -> str | None:
    """Detect 'echo ...' shell calls that are actually final answers.

    Small models often use shell echo instead of the complete tool.
    Only matches simple echo commands with no pipes, redirects, or chaining.
    """
    if tool_name != "shell" or not isinstance(tool_input, dict):
        return None
    cmd = tool_input.get("command", "").strip()
    # Reject if command has pipes, redirects, semicolons, &&, || etc.
    if any(c in cmd for c in ["|", ">", "<", ";", "&&", "||", "`", "$("]):
        return None
    m = _ECHO_FINAL_RE.match(cmd)
    if m:
        return m.group(1).strip().strip("'\"")
    return None


def _normalize_input(tool_input) -> str:
    """Normalize tool input to a comparable string."""
    if isinstance(tool_input, dict):
        return json.dumps(tool_input, sort_keys=True, ensure_ascii=False)
    return str(tool_input)


def _detect_repeated_calls(history: list[dict]) -> bool:
    """Return True if last N calls are identical (same tool + same input)."""
    if len(history) < _REPEAT_THRESHOLD:
        return False
    recent = history[-_REPEAT_THRESHOLD:]
    first = (recent[0]["tool"], recent[0]["input"])
    return all((h["tool"], h["input"]) == first for h in recent)


def _do_execute_tool(
    tool_name: str,
    tool_input,
    tools_list: list[str],
    include_delegate: bool,
    capabilities: ModelCapabilities,
    provider_name: str = "",
    model: str = "",
    base_url: str = "",
    api_key: str = "",
    delegate_timeout: int = 300,
    hooks_config: dict | None = None,
) -> str:
    """Core tool execution logic (no tracking)."""
    # PreToolUse hook
    input_dict = (
        tool_input if isinstance(tool_input, dict) else {"raw": str(tool_input)}
    )
    if hooks_config:
        from agent_cli.hooks import run_hooks

        pre_result = run_hooks(
            "PreToolUse", tool_name, input_dict, hooks_config=hooks_config
        )
        if not pre_result.allowed:
            return OBS_ERROR.format(
                error=f"Blocked by PreToolUse hook: {pre_result.stderr or 'hook denied'}"
            )
        if pre_result.updated_input is not None:
            tool_input = pre_result.updated_input

    if tool_name == "delegate" and include_delegate:
        result = tool_delegate(
            args=tool_input
            if isinstance(tool_input, dict)
            else {"task": str(tool_input)},
            provider=provider_name,
            model=model,
            base_url=base_url,
            api_key=api_key,
            timeout=delegate_timeout,
        )
        if result.success:
            _run_post_hook(hooks_config, tool_name, input_dict, result.output)
            return result.output
        else:
            obs = OBS_ERROR_HINT.format(
                error=result.error, hint="Check task description and try again."
            )
            _run_post_failure_hook(hooks_config, tool_name, input_dict, obs)
            return obs

    if tool_name in tools_list:
        valid, err = validate_tool_input(tool_name, tool_input)
        if not valid:
            return OBS_ERROR_HINT.format(error=err, hint="Fix action_input and retry.")
        result = execute_tool(tool_name, tool_input)
        if result.success:
            cfg = get_truncation_config(capabilities, tool_name)
            obs = OBS_SUCCESS.format(result=truncate_output(result.output, cfg))
            _run_post_hook(hooks_config, tool_name, input_dict, obs)
            return obs
        else:
            obs = OBS_ERROR_HINT.format(
                error=result.error, hint="Check parameters and try again."
            )
            _run_post_failure_hook(hooks_config, tool_name, input_dict, obs)
            return obs

    avail = ", ".join(tools_list) + (", delegate" if include_delegate else "")
    return OBS_ERROR.format(error=f"Unknown tool '{tool_name}'. Available: {avail}")


def _run_post_hook(hooks_config, tool_name, input_dict, obs):
    """Fire PostToolUse hook if configured."""
    if hooks_config:
        from agent_cli.hooks import run_hooks

        run_hooks(
            "PostToolUse",
            tool_name,
            input_dict,
            hooks_config=hooks_config,
            tool_result=obs,
        )


def _run_post_failure_hook(hooks_config, tool_name, input_dict, obs):
    """Fire PostToolUseFailure hook if configured."""
    if hooks_config:
        from agent_cli.hooks import run_hooks

        run_hooks(
            "PostToolUseFailure",
            tool_name,
            input_dict,
            hooks_config=hooks_config,
            tool_result=obs,
        )


def _format_tool_call_messages(
    provider_name: str,
    response,
    observations: list[dict],
) -> list[dict]:
    """Format tool call results as provider-specific messages."""
    if provider_name == "anthropic":
        return _format_anthropic_tool_messages(response, observations)
    elif provider_name == "openai":
        return _format_openai_tool_messages(response, observations)
    else:
        # Fallback: generic observation format
        obs_text = "\n\n".join(o["output"] for o in observations)
        return [
            {"role": "assistant", "content": response.content or ""},
            {
                "role": "user",
                "content": f"Observation: {obs_text}\n\nContinue. Respond with JSON only.",
            },
        ]


def _format_anthropic_tool_messages(response, observations: list[dict]) -> list[dict]:
    """Format for Anthropic: assistant content blocks + tool_result user message."""
    # Build assistant message with text + tool_use blocks
    assistant_content = []
    if response.content:
        assistant_content.append({"type": "text", "text": response.content})
    for tc in response.tool_calls or []:
        assistant_content.append(
            {
                "type": "tool_use",
                "id": tc["id"],
                "name": tc["name"],
                "input": tc["input"],
            }
        )

    # Build tool result user message
    tool_results = []
    for obs in observations:
        tool_results.append(
            {
                "type": "tool_result",
                "tool_use_id": obs["tool_call"]["id"],
                "content": obs["output"],
            }
        )

    return [
        {"role": "assistant", "content": assistant_content},
        {"role": "user", "content": tool_results},
    ]


def _format_openai_tool_messages(response, observations: list[dict]) -> list[dict]:
    """Format for OpenAI: assistant with tool_calls + tool role messages."""
    # Build assistant message with tool_calls
    assistant_msg = {
        "role": "assistant",
        "content": response.content or None,
        "tool_calls": [
            {
                "id": tc["id"],
                "type": "function",
                "function": {
                    "name": tc["name"],
                    "arguments": json.dumps(tc["input"]),
                },
            }
            for tc in response.tool_calls or []
        ],
    }

    # Build tool result messages
    result_msgs = [
        {
            "role": "tool",
            "tool_call_id": obs["tool_call"]["id"],
            "content": obs["output"],
        }
        for obs in observations
    ]

    return [assistant_msg] + result_msgs


def _append_native_observation(
    messages: list[dict],
    ctx,
    provider_name: str,
    response,
    observations: list[dict],
) -> None:
    """Native tool calling: format messages + extend + sync ctx."""
    new_msgs = _format_tool_call_messages(provider_name, response, observations)
    messages.extend(new_msgs)
    if ctx:
        for m in new_msgs:
            ctx.add(
                m["role"],
                m.get("content", "")
                if isinstance(m.get("content"), str)
                else json.dumps(m.get("content", "")),
            )


def _append_text_observation(
    messages: list[dict],
    ctx,
    llm_text: str,
    obs_msg: str,
) -> None:
    """Text parsing: append assistant + observation + sync ctx."""
    messages.append({"role": "assistant", "content": llm_text})
    messages.append({"role": "user", "content": obs_msg})
    if ctx:
        ctx.add("assistant", llm_text)
        ctx.add("user", obs_msg)


def _trim_old_observations(
    messages: list[dict], capabilities: ModelCapabilities
) -> list[dict]:
    """Trim oldest observation messages in single-shot mode to fit context."""
    if len(messages) <= 3:
        return messages
    # Keep first (query) and last 4 messages, trim middle
    keep_last = min(4, len(messages) - 1)
    return [messages[0]] + messages[-keep_last:]
