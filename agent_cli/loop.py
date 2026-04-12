"""Agent loop: ReAct pattern with M1/M2 module integration."""

from __future__ import annotations

import json
import re
import signal
import sys
import time

from agent_cli.constants import DELEGATE_DEFAULT_TIMEOUT, OBS_SUCCESS
from agent_cli.tools.result import ToolResult

from agent_cli.context.manager import ContextManager
from agent_cli.context.overflow import is_context_overflow
from agent_cli.parsing.react_parser import parse_react
from agent_cli.prompts.system_prompt import build_system_prompt
from agent_cli.providers.base import LLMProvider
from agent_cli.providers.compat import ModelCapabilities, needs_tool_action
from agent_cli.render import (
    render_context_dump,
    render_dispatch_progress,
    render_header,
    render_turn_sep,
    render_raw,
    render_spinner_start,
    render_spinner_stop,
    render_status,
    render_step,
    render_stream_chunk,
    render_stream_end,
)
from agent_cli.tools import TOOLS, execute_tool, validate_tool_input
from agent_cli.tools.delegate import tool_delegate

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


# Checkpoint system: nudges LLM to self-assess when turn count gets high.
# _CHECKPOINT_FIRST: first check after N turns. 50 chosen because most
#   tasks complete in 10-30 turns; 50 indicates potential stuck state.
# _CHECKPOINT_INTERVAL: repeat every M turns after first. 20 gives LLM
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
        max_turns: int = 0,
        verbose: bool = False,
        ctx: ContextManager | None = None,
        suppress_output: bool = False,
        depth: int = 0,
        max_depth: int = 2,
        delegate_timeout: int = DELEGATE_DEFAULT_TIMEOUT,
        active_tools: list[str] | None = None,
        session=None,  # SessionMeta — avoid circular import
        hooks_config: dict | None = None,
        skill_name: str = "",
        skill_stack: list[str] | None = None,
        agent_stack: list[str] | None = None,
        skill_args: str = "",
        graceful_interrupt: bool = False,
        stop_event=None,
        agent_role: str = "",
        agent_name: str = "",
        mcp_manager=None,
        hook_runner=None,
    ):
        self.query = query
        self.provider = provider
        self.capabilities = capabilities
        self.model = model
        self.provider_name = provider_name
        self.base_url = base_url
        self.api_key = api_key
        self.max_turns = max_turns
        self.verbose = verbose
        self.ctx = ctx
        self.suppress_output = suppress_output
        self.depth = depth
        self.max_depth = max_depth
        self.delegate_timeout = delegate_timeout
        self.session = session
        self.hooks_config = hooks_config
        self.skill_name = skill_name
        self.skill_args = skill_args
        self.mcp_manager = mcp_manager
        self.hook_runner = hook_runner
        # Create stop_event if not provided (for Ctrl+C propagation to nested loops)
        if stop_event is None:
            import threading

            stop_event = threading.Event()
        self.stop_event = stop_event
        self.agent_role = agent_role

        # Derived state
        self.tools_list = active_tools or list(TOOLS.keys())
        # Remove "delegate" when depth >= max_depth
        if depth >= max_depth and "delegate" in self.tools_list:
            self.tools_list = [t for t in self.tools_list if t != "delegate"]
        # Remove "ask" in non-interactive mode (no ctx or suppress_output)
        if (not ctx or suppress_output) and "ask" in self.tools_list:
            self.tools_list = [t for t in self.tools_list if t != "ask"]
        # Build skill stack for recursive call prevention
        if skill_stack is None:
            skill_stack = []
        if skill_name:
            skill_stack = [*skill_stack, skill_name]
        self.skill_stack = skill_stack
        # Build agent stack for recursive call prevention
        if agent_stack is None:
            agent_stack = []
        if agent_name:
            agent_stack = [*agent_stack, agent_name]
        self.agent_stack = agent_stack

        # Loop state
        self.turn = 0
        self.tools_called: list[str] = []
        self.overflow_retried = False
        self._interrupted = False
        self._prev_sigint_handler = None
        self.graceful_interrupt = graceful_interrupt
        self.recent_tool_history: list[dict] = []
        self.messages: list[dict] = []
        self.system = ""
        # Sentinels: distinct from None (failure) and str (answer)
        self._CONTINUE = object()  # keep looping
        self._RETRY = object()  # overflow retry

    def _fire_hook(self, event: str, **kwargs):
        """Fire a hook event if runner is available. Returns HookContext or None."""
        if not self.hook_runner:
            return None
        return self.hook_runner.fire(
            event,
            messages=self.messages,
            session_dir=self.ctx.session_dir if self.ctx else None,
            turn=self.turn,
            mcp_manager=self.mcp_manager,
            **kwargs,
        )

    def _apply_system_sections(self, hook_ctx) -> None:
        """Apply dynamic system prompt sections from hook context."""
        if not hook_ctx or not hook_ctx.system_sections:
            return
        # Rebuild system prompt with dynamic sections appended
        sections_text = "\n\n".join(
            f"## {title}\n{content}"
            for title, content in hook_ctx.system_sections.items()
        )
        # Strip any previously appended dynamic sections (delimited by marker)
        marker = "\n\n<!-- HOOK_SECTIONS -->\n"
        base = self.system.split(marker)[0]
        self.system = f"{base}{marker}{sections_text}"

    def run(self):
        """Main entry point. Returns ToolResult."""
        if self.graceful_interrupt:
            self._install_signal_handler()
        try:
            self._setup()
            self._fire_hook("OnSessionStart")
            while self._should_continue():
                if self._interrupted:
                    return self._on_interrupt()
                self.turn += 1
                self._begin_turn()
                result = self._execute_turn()
                if result is not self._CONTINUE:
                    return result
            return self._on_max_turns()
        finally:
            self._fire_hook("OnSessionEnd")
            if self.graceful_interrupt:
                self._restore_signal_handler()

    def _install_signal_handler(self) -> None:
        """Install graceful SIGINT handler (1st: flag, 2nd: hard exit).

        Only installs on the main thread — signal handlers cannot be set
        from worker threads (e.g. parallel delegates).
        """
        import threading

        if threading.current_thread() is not threading.main_thread():
            return

        self._prev_sigint_handler = signal.getsignal(signal.SIGINT)

        def _handle_sigint(signum, frame):
            if self._interrupted:
                # 2nd Ctrl+C → hard exit via default handler
                signal.signal(signal.SIGINT, signal.default_int_handler)
                signal.default_int_handler(signum, frame)
            self._interrupted = True
            if self.stop_event:
                self.stop_event.set()
            print("\n⚡ Finishing current step...", file=sys.stderr)

        signal.signal(signal.SIGINT, _handle_sigint)

    def _restore_signal_handler(self) -> None:
        """Restore the previous SIGINT handler."""
        import threading

        if threading.current_thread() is not threading.main_thread():
            return
        if self._prev_sigint_handler is not None:
            signal.signal(signal.SIGINT, self._prev_sigint_handler)

    def _on_interrupt(self):
        """Handle graceful interrupt: record in ctx, return ToolResult."""

        interrupt_msg = "⚡ User interrupted. Waiting for new instructions."
        if self.ctx:
            self.ctx.add({"role": "user", "content": interrupt_msg})
        if not self.suppress_output:
            from agent_cli.render import C, console

            console.print(f"\n[{C['accent']}]⚡ Interrupted after turn {self.turn}.[/]")
        _debug_log(f"Graceful interrupt at turn {self.turn}")
        return ToolResult(False, error="Interrupted by user")

    def _setup(self) -> None:
        """Initialize system prompt and messages."""
        _set_debug_verbose(self.verbose)

        # Build system prompt with session_dir for Context Recovery Guide
        session_dir = ""
        if self.ctx:
            session_dir = str(self.ctx.session_dir)
        self.system = build_system_prompt(
            capabilities=self.capabilities,
            active_tools=self.tools_list,
            skill_stack=self.skill_stack,
            agent_role=self.agent_role,
            session_dir=session_dir,
            mcp_manager=self.mcp_manager,
        )

        if not self.suppress_output:
            render_header(
                self.provider_name,
                self.model,
                self.max_turns,
                skill_name=self.skill_name,
                skill_args=self.skill_args,
            )

        # Message setup
        if self.ctx:
            self.ctx.add({"role": "user", "content": self.query})
            self.messages = self.ctx.get_messages()
        else:
            self.messages = [{"role": "user", "content": self.query}]

    def _should_continue(self) -> bool:
        if self.stop_event and self.stop_event.is_set():
            self._interrupted = True
            return False
        return self.max_turns <= 0 or self.turn < self.max_turns

    def _begin_turn(self) -> None:
        """Render turn separator."""
        if not self.suppress_output:
            render_turn_sep(self.turn)

    def _execute_turn(self):
        """Single turn: checkpoint, hooks, LLM call, text parse, dispatch."""
        self._maybe_checkpoint()

        # PreLLMCall hook — can inject system sections and messages
        hook_ctx = self._fire_hook("PreLLMCall")
        self._apply_system_sections(hook_ctx)

        response = self._call_llm()
        if hasattr(response, "success"):
            return response  # ToolResult (LLM failure)
        if response == self._RETRY:
            return self._CONTINUE

        llm_text = response.content

        # Show token stats if available (Ollama provides eval durations)
        if not self.suppress_output and response.usage:
            _render_token_stats(response.usage, self.turn)

        # PostLLMCall hook
        self._fire_hook("PostLLMCall", llm_response=llm_text)

        if not self.suppress_output:
            render_raw(llm_text, self.turn, self.verbose)

        result = self._handle_text_path(llm_text)

        # OnTurnEnd hook
        self._fire_hook("OnTurnEnd")

        return result

    def _maybe_checkpoint(self) -> None:
        """Inject checkpoint message if turn count is high."""
        if self.turn >= _CHECKPOINT_FIRST and (
            (self.turn - _CHECKPOINT_FIRST) % _CHECKPOINT_INTERVAL == 0
        ):
            recent = self.recent_tool_history[-_CHECKPOINT_INTERVAL:]
            history_summary = "\n".join(
                f"  turn {h['turn']}: {h['tool']} → {h['result'][:100]}" for h in recent
            )
            checkpoint_msg = (
                f"[SYSTEM] CHECKPOINT — {self.turn} turns used.\n"
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
                self.ctx.add({"role": "user", "content": checkpoint_msg})
            if not self.suppress_output:
                render_status("running", f"Checkpoint at turn {self.turn}")

    def _call_llm(self):
        """LLM call with overflow retry and streaming. Returns response or sentinel."""
        # Context dump (verbose only)
        if self.verbose and not self.suppress_output:
            render_context_dump(self.messages, self.turn)
        _debug_log(
            f"LLM_CALL turn={self.turn} skill={self.skill_name or 'main'} msg_count={len(self.messages)}"
        )

        # Build streaming callback: stops spinner on first chunk, then streams
        streaming = not self.suppress_output
        spinner_active = True

        def on_chunk(text: str) -> None:
            nonlocal spinner_active
            if spinner_active:
                render_spinner_stop()
                spinner_active = False
            render_stream_chunk(text)

        if self.skill_name:
            render_spinner_start(f"skill:{self.skill_name} thinking...")
        elif not self.suppress_output:
            render_spinner_start("thinking...")
        try:
            call_kwargs: dict = {}
            if streaming:
                call_kwargs["on_chunk"] = on_chunk
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
                    render_status("running", "Context overflow — refreshing...")
                    self.messages = self.ctx.get_messages()
                    self.overflow_retried = True
                    self.turn -= 1
                    return self._RETRY
            _debug_log(
                f"LLM call failed: model={self.model} iter={self.turn} skill={self.skill_name} error={e}"
            )
            render_step(
                "error",
                f"LLM call failed (model={self.model}, iter={self.turn}): {e}",
                self.turn,
            )

            return ToolResult(False, error=f"LLM call failed: {e}")
        finally:
            if spinner_active and (self.skill_name or not self.suppress_output):
                render_spinner_stop()
            if streaming and not spinner_active:
                render_stream_end()

    def _handle_text_path(self, llm_text: str):
        """Handle text parsing response (Ollama, fallback)."""
        parsed = parse_react(llm_text)

        # 6. Thought
        if parsed.thought and not self.suppress_output:
            render_step("thought", parsed.thought, self.turn)

        # 7. Complete tool (text parsing path)
        _debug_log(f"PARSED iter={self.turn} action={parsed.action}")
        if parsed.action == "complete":
            _render_skill_progress(
                self.skill_name,
                self.turn,
                "complete",
                {},
                self.suppress_output,
                thought=parsed.thought or "",
            )
            if isinstance(parsed.action_input, dict):
                raw = parsed.action_input.get("result")
                answer = (
                    str(raw)
                    if raw
                    else "(Completed without result — model may lack capability for this task)"
                )
            elif isinstance(parsed.action_input, str):
                answer = (
                    parsed.action_input
                    or "(Completed without result — model may lack capability for this task)"
                )
            else:
                answer = (
                    str(parsed.action_input)
                    if parsed.action_input
                    else "(Completed without result — model may lack capability for this task)"
                )

            # Fulfillment guard -- no tools used yet
            if not self.tools_called and needs_tool_action(self.query):
                nudge = (
                    "You called the complete tool, but the task likely requires "
                    "tool actions (file operations, shell commands, etc.). "
                    "Please use the appropriate tools first, then call complete."
                )
                _append_observation(self.messages, self.ctx, llm_text, nudge)
                render_status(
                    "error",
                    "Answer rejected — no tool actions performed yet.",
                    self.turn,
                )
                return self._CONTINUE

            if self.ctx:
                self.ctx.add(
                    {
                        "role": "assistant",
                        "thought": parsed.thought or "",
                        "action": "complete",
                        "action_input": {"result": answer},
                    }
                )
            if not self.suppress_output:
                render_step("final", answer, self.turn)

            return ToolResult(True, output=answer)

        # 9. Detect echo-as-final-answer (common small model pattern)
        echo_answer = _try_echo_as_final(parsed.action, parsed.action_input)
        if echo_answer:
            if self.ctx:
                self.ctx.add(
                    {
                        "role": "assistant",
                        "thought": parsed.thought or "",
                        "action": "complete",
                        "action_input": {"result": echo_answer},
                    }
                )
            if not self.suppress_output:
                render_step("final", echo_answer, self.turn)

            return ToolResult(True, output=echo_answer)

        # 10. Ask tool -- prompt user for input (text parsing path)
        if parsed.action == "ask":
            questions = _extract_questions(parsed.action_input)
            if questions:
                user_response = _handle_ask(questions, self.suppress_output)
                obs_msg = f"Observation: User responded:\n{user_response}"
                _append_observation(self.messages, self.ctx, llm_text, obs_msg)
                return self._CONTINUE

        # 10b. run_skill -- intercept at loop level (text parsing path)
        if parsed.action == "run_skill":
            skill_input = (
                parsed.action_input if isinstance(parsed.action_input, dict) else {}
            )
            skill_tool_result = _handle_run_skill(
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
                graceful_interrupt=self.graceful_interrupt,
                stop_event=self.stop_event,
                hook_runner=self.hook_runner,
                mcp_manager=self.mcp_manager,
            )
            obs = skill_tool_result.output or skill_tool_result.error
            if not self.suppress_output:
                render_step("observation", obs, self.turn, tool_name="run_skill")
            obs_msg = f"Observation: {obs}"
            _append_observation(
                self.messages,
                self.ctx,
                llm_text,
                obs_msg,
                artifact=skill_tool_result.artifact,
            )
            self.tools_called.append("run_skill")
            return self._CONTINUE

        # 10c. ready_for_review -- return original query for self-check (text path)
        if parsed.action == "ready_for_review":
            summary = ""
            if isinstance(parsed.action_input, dict):
                summary = parsed.action_input.get("summary", "")
            obs = _build_review_observation(self.query, summary, ctx=self.ctx)
            _render_skill_progress(
                self.skill_name,
                self.turn,
                "ready_for_review",
                {"summary": summary},
                self.suppress_output,
                thought=parsed.thought or "",
            )
            if not self.skill_name and not self.suppress_output:
                render_step(
                    "observation",
                    obs,
                    self.turn,
                    tool_name="ready_for_review",
                )
            obs_msg = f"Observation: {obs}"
            _append_observation(self.messages, self.ctx, llm_text, obs_msg)
            return self._CONTINUE

        # 11. Tool execution (text parsing path)
        if parsed.action:
            tool_name = parsed.action
            tool_input = parsed.action_input or {}

            # Truncation guard: if JSON was repaired (truncated response),
            # strip the last element from edit_file's lines arrays
            truncation_warning = ""
            if parsed.truncated and tool_name == "edit_file":
                tool_input, truncation_warning = _sanitize_truncated_edit(tool_input)
            _render_skill_progress(
                self.skill_name,
                self.turn,
                tool_name,
                tool_input,
                self.suppress_output,
                thought=parsed.thought or "",
            )

            if not self.suppress_output:
                render_step(
                    "action",
                    "",
                    self.turn,
                    tool_name=tool_name,
                    tool_input=json.dumps(tool_input, ensure_ascii=False)
                    if isinstance(tool_input, dict)
                    else str(tool_input),
                )

            # Execute tool (shared logic -- tracks tools_called + history)
            tool_result = _execute_single_tool(
                tool_name,
                tool_input,
                self.tools_list,
                self.capabilities,
                self.provider_name,
                self.model,
                self.base_url,
                self.api_key,
                self.delegate_timeout,
                self.tools_called,
                self.recent_tool_history,
                self.turn,
                hooks_config=self.hooks_config,
                delegate_ctx=self.ctx,
                delegate_provider=self.provider,
                delegate_depth=self.depth,
                delegate_max_depth=self.max_depth,
                delegate_max_turns=self.max_turns,
                delegate_suppress=self.suppress_output,
                delegate_session=self.session,
                delegate_skill_stack=self.skill_stack,
                delegate_agent_stack=self.agent_stack,
                stop_event=self.stop_event,
                hook_runner=self.hook_runner,
                mcp_manager=self.mcp_manager,
            )

            observation = (
                tool_result.output if tool_result.success else tool_result.error
            )
            if truncation_warning:
                observation = f"{observation}\n{truncation_warning}"

            if not self.suppress_output:
                render_step(
                    "observation",
                    observation,
                    self.turn,
                    tool_name=tool_name,
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

                return ToolResult(False, error="Repeated tool call detected")

            # Inject observation with structured artifact
            obs_msg = f"Observation: {observation}"
            _append_observation(
                self.messages,
                self.ctx,
                llm_text,
                obs_msg,
                artifact=tool_result.artifact,
            )
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
                self.turn,
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
                self.turn,
            )
            retry_msg = (
                "Your response was not valid JSON. "
                "Output ONLY a JSON object: "
                '{"thought": "...", "action": "tool_name", "action_input": {...}}. '
                "No markdown fences, no extra text."
            )
        _append_observation(self.messages, self.ctx, llm_text, retry_msg)
        self.turn -= 1  # Don't count format retries
        return self._CONTINUE

    def _on_max_turns(self):
        """Handle max turns reached."""

        if not self.suppress_output:
            render_status("error", f"Max turns ({self.max_turns}) reached.")
        _debug_log(
            f"run_loop returning None: max_turns={self.max_turns} reached, skill_name={self.skill_name}"
        )
        return ToolResult(False, error=f"Max turns ({self.max_turns}) reached")


# Backward-compatible wrapper
def run_loop(
    query: str,
    provider: LLMProvider,
    capabilities: ModelCapabilities,
    model: str,
    provider_name: str = "ollama",
    base_url: str = "",
    api_key: str = "",
    max_turns: int = 0,
    verbose: bool = False,
    ctx: ContextManager | None = None,
    suppress_output: bool = False,
    depth: int = 0,
    max_depth: int = 2,
    delegate_timeout: int = DELEGATE_DEFAULT_TIMEOUT,
    active_tools: list[str] | None = None,
    session=None,  # SessionMeta — avoid circular import
    hooks_config: dict | None = None,
    skill_name: str = "",
    skill_stack: list[str] | None = None,
    agent_stack: list[str] | None = None,
    skill_args: str = "",
    graceful_interrupt: bool = False,
    stop_event=None,
    agent_role: str = "",
    agent_name: str = "",
    mcp_manager=None,
    hook_runner=None,
):
    """Run the ReAct agent loop. Returns ToolResult."""
    return AgentLoop(
        query=query,
        provider=provider,
        capabilities=capabilities,
        model=model,
        provider_name=provider_name,
        base_url=base_url,
        api_key=api_key,
        max_turns=max_turns,
        verbose=verbose,
        ctx=ctx,
        suppress_output=suppress_output,
        depth=depth,
        max_depth=max_depth,
        delegate_timeout=delegate_timeout,
        active_tools=active_tools,
        session=session,
        hooks_config=hooks_config,
        skill_name=skill_name,
        skill_stack=skill_stack,
        agent_stack=agent_stack,
        skill_args=skill_args,
        graceful_interrupt=graceful_interrupt,
        stop_event=stop_event,
        agent_role=agent_role,
        agent_name=agent_name,
        mcp_manager=mcp_manager,
        hook_runner=hook_runner,
    ).run()


def _render_token_stats(usage, turn: int) -> None:
    """Render token throughput stats when duration data is available."""
    parts = []
    if usage.ttft_ns > 0:
        parts.append(f"ttft: {usage.ttft_ns / 1e6:.0f}ms")
    if usage.input_tokens:
        if usage.prompt_eval_ns > 0:
            speed = usage.input_tokens / (usage.prompt_eval_ns / 1e9)
            parts.append(f"in: {usage.input_tokens} tok ({speed:.0f} tok/s)")
        else:
            parts.append(f"in: {usage.input_tokens} tok")
    if usage.output_tokens:
        if usage.eval_ns > 0:
            speed = usage.output_tokens / (usage.eval_ns / 1e9)
            parts.append(f"out: {usage.output_tokens} tok ({speed:.0f} tok/s)")
        else:
            parts.append(f"out: {usage.output_tokens} tok")
    if parts:
        render_status("running", " | ".join(parts), turn)


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


def _handle_ask(questions: list[str], suppress_output: bool) -> str:
    """Display all questions at once and collect a single response."""
    from agent_cli.render import C, console

    console.print(f"\n[{C['accent']}]Agent asks:[/]")
    for i, q in enumerate(questions, 1):
        if len(questions) > 1:
            console.print(f"  {i}. {q}")
        else:
            console.print(f"  {q}")
    try:
        answer = input("\nYour answer: ").strip()
    except (EOFError, KeyboardInterrupt):
        answer = "(no response)"

    q_part = "\n".join(f"Q: {q}" for q in questions)
    return f"{q_part}\nA: {answer}"


def _render_skill_progress(
    skill_name: str,
    turn: int,
    tool_name: str,
    tool_input,
    suppress_output: bool,
    thought: str = "",
) -> None:
    """Show skill/dispatch progress via renderer. Shows even when suppress_output=True (for skills)."""
    if not skill_name:
        return

    # Build action detail from tool input
    detail = ""
    if isinstance(tool_input, dict):
        if tool_name in ("read_file", "write_file", "edit_file"):
            path = tool_input.get("path", "")
            if path:
                detail = f" {path}"
        elif tool_name == "shell":
            cmd = tool_input.get("command", "")
            if cmd:
                detail = f" {cmd[:60]}"
        elif tool_name == "run_skill":
            name = tool_input.get("name", "")
            detail = f" {name}"

    render_dispatch_progress(
        label=f"skill:{skill_name}",
        turn=turn,
        tool_name=tool_name,
        detail=detail,
        thought=thought,
    )


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
    graceful_interrupt: bool = False,
    stop_event=None,
    hook_runner=None,
    mcp_manager=None,
):
    """Handle run_skill at loop level with full ctx access."""
    # Inline import: circular dependency — executor.py imports run_loop from this module
    from agent_cli.skills import load_skills
    from agent_cli.skills.executor import execute_skill

    name = skill_input.get("name", "")
    arguments = skill_input.get("arguments", "")
    # LLM might send arguments as dict instead of string
    if not isinstance(arguments, str):
        arguments = str(arguments) if arguments else ""

    if not name:
        return ToolResult(False, error="run_skill: 'name' is required.")

    # Skill stack: prevent recursive calls (A→B→A)
    if skill_stack and name in skill_stack:
        return ToolResult(
            False,
            error=f"Recursive skill call blocked: '{name}' is already in the call stack {skill_stack}.",
        )

    skills = load_skills()
    if name not in skills:
        available = ", ".join(skills.keys()) if skills else "(none)"
        return ToolResult(
            False, error=f"Skill '{name}' not found. Available: {available}"
        )

    skill = skills[name]
    if skill.disable_model_invocation:
        return ToolResult(
            False, error=f"Skill '{name}' is user-only (disable-model-invocation)."
        )

    # OnSkillStart hook
    if hook_runner:
        hook_runner.fire(
            "OnSkillStart",
            tool_name="run_skill",
            tool_input=skill_input,
            mcp_manager=mcp_manager,
        )

    render_status("running", f"Running skill: {name}...")

    try:
        from agent_cli.providers import create_provider

        provider = create_provider(provider_name, base_url, api_key)
        skill_result = execute_skill(
            skill=skill,
            arguments=arguments,
            provider=provider,
            capabilities=capabilities,
            model=model,
            provider_name=provider_name,
            base_url=base_url,
            api_key=api_key,
            suppress_output=True,
            ctx=ctx,
            session=session,
            skill_stack=skill_stack,
            graceful_interrupt=graceful_interrupt,
            stop_event=stop_event,
        )
    except Exception as e:
        _debug_log(f"run_skill({name}) exception: {e}")
        skill_result = ToolResult(False, error=f"run_skill({name}) failed: {e}")

    # OnSkillEnd hook
    if hook_runner:
        hook_runner.fire(
            "OnSkillEnd",
            tool_name="run_skill",
            tool_input=skill_input,
            skill_result=skill_result,
            mcp_manager=mcp_manager,
        )

    if isinstance(skill_result, ToolResult) and not skill_result.success:
        if skill_result.error and skill_result.error.startswith("run_skill("):
            return skill_result
        _debug_log(f"run_skill({name}) failed: {skill_result.error}")

    skill_header = f"SKILL: {name}({arguments})\n" if arguments else f"SKILL: {name}\n"
    body = skill_result.output or skill_result.error or "(skill returned no result)"
    obs = OBS_SUCCESS.format(result=f"{skill_header}{body}")

    return ToolResult(skill_result.success, output=obs, artifact=skill_result.artifact)


def _build_review_observation(query: str, summary: str, ctx=None) -> str:
    """Build the observation returned by ready_for_review tool."""
    parts = [
        "--- ORIGINAL REQUEST ---",
        query,
        "--- YOUR SUMMARY ---",
        summary,
    ]
    parts.extend(
        [
            "",
            "--- REVIEW INSTRUCTIONS ---",
            "Be adversarial. Try to find gaps, not confirm success.",
            "1. List each requirement from the ORIGINAL REQUEST.",
            "2. For each requirement, check if the WORK LOG shows evidence it was completed.",
            "3. If a requirement is NOT met or evidence is missing, continue working on it.",
            "4. Only call complete if EVERY requirement has clear evidence of completion.",
        ]
    )
    return "\n".join(parts)


def _execute_single_tool(
    tool_name: str,
    tool_input,
    tools_list: list[str],
    capabilities: ModelCapabilities,
    provider_name: str = "",
    model: str = "",
    base_url: str = "",
    api_key: str = "",
    delegate_timeout: int = DELEGATE_DEFAULT_TIMEOUT,
    tools_called: list[str] | None = None,
    recent_tool_history: list[dict] | None = None,
    turn: int = 0,
    hooks_config: dict | None = None,
    delegate_ctx=None,
    delegate_provider=None,
    delegate_depth: int = 0,
    delegate_max_depth: int = 2,
    delegate_max_turns: int = 0,
    delegate_suppress: bool = False,
    delegate_session=None,
    delegate_skill_stack: list[str] | None = None,
    delegate_agent_stack: list[str] | None = None,
    stop_event=None,
    hook_runner=None,
    mcp_manager=None,
):
    """Execute a single tool: hooks, execution, tracking. Returns ToolResult."""

    _debug_log(f"TOOL turn={turn} action={tool_name} input={str(tool_input)[:200]}")

    input_dict = (
        tool_input if isinstance(tool_input, dict) else {"raw": str(tool_input)}
    )

    # PreToolUse — Python hooks (via runner) then shell hooks (backward compat)
    if hook_runner:
        pre_ctx = hook_runner.fire(
            "PreToolUse",
            tool_name=tool_name,
            tool_input=input_dict,
            turn=turn,
            mcp_manager=mcp_manager,
        )
        if pre_ctx.is_blocked:
            return ToolResult(
                False,
                error=f"Blocked by PreToolUse hook: {pre_ctx.block_reason or 'hook denied'}",
            )
        if pre_ctx.modified_input is not None:
            tool_input = pre_ctx.modified_input
            input_dict = tool_input
    if hooks_config:
        from agent_cli.hooks import run_hooks

        pre_result = run_hooks(
            "PreToolUse", tool_name, input_dict, hooks_config=hooks_config
        )
        if not pre_result.allowed:
            return ToolResult(
                False,
                error=f"Blocked by PreToolUse hook: {pre_result.stderr or 'hook denied'}",
            )
        if pre_result.updated_input is not None:
            tool_input = pre_result.updated_input

    # Delegate tool (intercepted here due to complex kwargs)
    is_delegate = tool_name == "delegate"

    if is_delegate:
        # OnDelegateStart hook
        if hook_runner:
            hook_runner.fire(
                "OnDelegateStart",
                tool_name=tool_name,
                tool_input=input_dict,
                turn=turn,
                mcp_manager=mcp_manager,
            )

        raw = tool_input if isinstance(tool_input, dict) else {"task": str(tool_input)}
        if "tasks" not in raw and "task" in raw:
            raw = {
                "tasks": [
                    {
                        "task": raw["task"],
                        "context": raw.get("context", "none"),
                        **({"tools": raw["tools"]} if raw.get("tools") else {}),
                    }
                ]
            }
        result = tool_delegate(
            args=raw,
            parent_ctx=delegate_ctx,
            provider=delegate_provider,
            model=model,
            capabilities=capabilities,
            provider_name=provider_name,
            base_url=base_url,
            api_key=api_key,
            depth=delegate_depth,
            max_depth=delegate_max_depth,
            max_turns=delegate_max_turns,
            timeout=delegate_timeout,
            suppress_output=delegate_suppress,
            session=delegate_session,
            skill_stack=delegate_skill_stack,
            agent_stack=delegate_agent_stack,
            stop_event=stop_event,
        )

        # OnDelegateEnd hook
        if hook_runner:
            hook_runner.fire(
                "OnDelegateEnd",
                tool_name=tool_name,
                tool_input=input_dict,
                delegate_result=result,
                turn=turn,
                mcp_manager=mcp_manager,
            )
    elif tool_name in tools_list:
        valid, err = validate_tool_input(tool_name, tool_input)
        if not valid:
            result = ToolResult(False, error=f"{err} Fix action_input and retry.")
        else:
            result = execute_tool(tool_name, tool_input)
    else:
        avail = ", ".join(tools_list)
        result = ToolResult(
            False, error=f"Unknown tool '{tool_name}'. Available: {avail}"
        )

    # PostToolUse — Python hooks (via runner) then shell hooks (backward compat)
    if hook_runner:
        hook_runner.fire(
            "PostToolUse",
            tool_name=tool_name,
            tool_input=input_dict,
            tool_result=result,
            turn=turn,
            mcp_manager=mcp_manager,
        )
    if hooks_config:
        from agent_cli.hooks import run_hooks

        _obs = result.output if result.success else result.error
        _evt = "PostToolUse" if result.success else "PostToolUseFailure"
        run_hooks(
            _evt, tool_name, input_dict, hooks_config=hooks_config, tool_result=_obs
        )

    # Track tool usage
    obs = result.output if result.success else result.error
    if tools_called is not None:
        tools_called.append(tool_name)
    if recent_tool_history is not None:
        recent_tool_history.append(
            {
                "tool": tool_name,
                "input": _normalize_input(tool_input),
                "result": obs[:200],
                "turn": turn,
            }
        )

    return result


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


def _sanitize_truncated_edit(tool_input: dict) -> tuple[dict, str]:
    """Strip the last line from the last edit when response was truncated.

    Returns (sanitized_input, warning_message).
    """
    edits = tool_input.get("edits", [])
    if not edits:
        return tool_input, ""

    total = len(edits)
    last_edit = edits[-1]
    lines = last_edit.get("lines", [])

    if lines:
        last_edit = {**last_edit, "lines": lines[:-1]}
        edits = edits[:-1] + [last_edit]
        tool_input = {**tool_input, "edits": edits}

        # If the last edit has no lines left, drop the entire edit
        if not last_edit["lines"] and last_edit.get("op") == "replace":
            edits = edits[:-1]
            tool_input = {**tool_input, "edits": edits}

    applied = len(tool_input.get("edits", []))
    warning = (
        f"[warn] Response was truncated. "
        f"Applied {applied} of {total} edits (last line dropped). "
        f"Re-read the file to verify and complete remaining edits."
    )
    return tool_input, warning


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


def _append_observation(
    messages: list[dict],
    ctx,
    llm_text: str,
    obs_msg: str,
    artifact: str = "",
) -> None:
    """Text parsing: append assistant + observation + sync ctx.

    For the in-memory messages list (sent to LLM), raw JSON is kept as-is
    since it's the format the LLM produced and expects to see.

    For history.jsonl (via ctx.add), assistant messages are parsed into
    structured dicts so _to_natural_language can convert them properly.
    """
    messages.append({"role": "assistant", "content": llm_text})
    messages.append({"role": "user", "content": obs_msg})
    if ctx:
        ctx.add(_parse_assistant_for_history(llm_text))
        obs_entry = {"role": "user", "content": obs_msg}
        if artifact:
            obs_entry["artifact"] = artifact
        ctx.add(obs_entry)


def _parse_assistant_for_history(llm_text: str) -> dict:
    """Parse raw LLM JSON text into a structured dict for history.jsonl.

    Converts: '{"thought":"...", "action":"...", "action_input":{...}}'
    Into:     {"role":"assistant", "thought":"...", "action":"...", "action_input":{...}}
    """
    try:
        data = json.loads(llm_text)
        if isinstance(data, dict) and ("thought" in data or "action" in data):
            data["role"] = "assistant"
            return data
    except (json.JSONDecodeError, TypeError):
        pass
    # Fallback: plain content
    return {"role": "assistant", "content": llm_text}
