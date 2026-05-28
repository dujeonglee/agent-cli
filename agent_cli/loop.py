"""Agent loop: ReAct pattern with M1/M2 module integration."""

from __future__ import annotations

import json
import re
import signal
import sys
import time

from agent_cli.constants import (
    DELEGATE_DEFAULT_TIMEOUT,
    INTERRUPT_NOTICE,
    OBS_SUCCESS,
)
from agent_cli.recovery.common_recovery import format_action_loop_intervention
from agent_cli.recovery.wf_recovery import (
    format_no_action_retry,
    format_no_json_retry,
)
from agent_cli.tools.result import ToolResult

from agent_cli.context.manager import ContextManager
from agent_cli.context.overflow import is_context_overflow
from agent_cli.prompts.system_prompt import build_system_prompt
from agent_cli.providers.base import LLMProvider
from agent_cli.providers.compat import ModelCapabilities
from agent_cli.recovery.detectors import (
    ActionLoopDetector,
    detect_nested_envelope,
    detect_schema_mismatch,
    detect_thought_missing,
    detect_unknown_tool,
    unwrap_nested_envelope,
)
from agent_cli.recovery.observability import (
    FAILURE_ACTION_LOOP,
    FAILURE_NESTED_ENVELOPE,
    FAILURE_NO_ACTION,
    FAILURE_NO_JSON,
    FAILURE_NO_OUTPUT,
    FAILURE_NO_THOUGHT,
    FAILURE_SCHEMA_MISMATCH,
    FAILURE_UNKNOWN_TOOL,
    TurnRecorder,
)
from agent_cli.render import (
    render_context_dump,
    render_header,
    render_turn_sep,
    render_raw,
    render_thinking,
    render_spinner_start,
    render_spinner_stop,
    render_status,
    render_step,
    render_stream_chunk,
    render_stream_end,
    render_push_depth,
    render_pop_depth,
    render_group_start,
    render_group_end,
)
from agent_cli.tools import TOOLS, _execute_tool
from agent_cli.tools.delegate import tool_delegate

from agent_cli.verbose import debug_log as _debug_log, set_verbose as _set_debug_verbose
from agent_cli.wire_formats import get as _get_wire_format


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
        record_turns: bool = True,
        wire_format=None,
        compaction_enabled: bool = True,
    ):
        # Wire format plugin — ReAct by default. Centralizes the
        # parser, recovery wording, prompt section, and lifecycle hooks
        # so adding a new format means dropping a file in
        # ``agent_cli/wire_formats/`` and re-running with
        # ``--response-format <name>``.
        if wire_format is None:
            wire_format = _get_wire_format("react")
        self.wire_format = wire_format

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
        # Remove both 'delegate' and 'run_skill' when the combined
        # call depth has reached its ceiling. Skills now count
        # toward depth (parity with delegate), so the ceiling treats
        # both kinds of nesting the same way — and the LLM never
        # sees a tool it'd be refused from. The dispatch-time
        # ``_handle_run_skill`` / ``_run_single`` checks remain as a
        # belt-and-suspenders layer for direct callers that built a
        # custom ``active_tools`` list.
        if depth >= max_depth:
            self.tools_list = [
                t for t in self.tools_list if t not in ("delegate", "run_skill")
            ]
        # Remove "ask" in non-interactive mode (no ctx)
        if not ctx and "ask" in self.tools_list:
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

        # Observability — per-turn record. Disabled when no session
        # (headless / subagent) or when user opted out.
        self.recorder = TurnRecorder(
            session_dir=(self.ctx.session_dir if self.ctx else None),
            enabled=record_turns,
        )

        # B1 (action loop) detector. Threshold=2 fires on the second
        # consecutive identical (action, args). Escalation count
        # selects the playbook column (1=probe_progress,
        # 2=restate_task, 3+=hard fail).
        self.loop_detector = ActionLoopDetector(threshold=2)

        # Context compaction wiring (RFC docs/context-compaction/).
        # The compactor callback and the TurnRecorder are injected
        # into the ContextManager so the manager can summarise
        # evicted messages via the same provider and emit compaction
        # events for measurement. ``compaction_enabled=False`` (CLI
        # ``--no-compaction``) skips the callback registration so the
        # manager reverts to plain FIFO drop.
        self.compaction_enabled = compaction_enabled
        if self.ctx is not None:
            self.ctx.set_recorder(self.recorder)
            if self._compaction_enabled():
                self.ctx.set_compactor(self._llm_compact_summarize)

    def _compaction_enabled(self) -> bool:
        """Resolve the effective compaction-enabled flag (NFR-CC-5).

        Order of precedence:
          1. ``AGENT_CLI_COMPACTION`` env var (operator-level kill
             switch, wins over constructor flag).
          2. ``compaction_enabled`` constructor flag (CLI
             ``--no-compaction``).
        """
        import os

        env = os.environ.get("AGENT_CLI_COMPACTION", "").strip().lower()
        if env in ("off", "false", "0", "disabled", "no"):
            return False
        return self.compaction_enabled

    def _llm_compact_summarize(self, messages: list[dict]) -> str:
        """Compactor callback: call the main provider with a
        summarisation system prompt + the evicted messages, return
        the summary text. Raised exceptions are converted to
        ``CompactionError`` inside ContextManager._summarize_messages.

        ``messages`` arrives in chat-ready ``{role, content}`` form —
        ContextManager._compact does the natural-language conversion
        via the wire_format plugin before calling here.

        Capabilities are overridden for this one call:
          - ``supports_structured_output=False`` — we want a plain text
            summary, not a JSON object. The agent-loop's normal ReAct
            calls force JSON; here we explicitly opt out.
          - ``supports_thinking=False`` — summarisation doesn't benefit
            from a reasoning trace, and the thinking tokens would
            consume the response budget without ending up in the
            persisted summary.
        """
        from dataclasses import replace

        summarisation_prompt = (
            "Summarise the conversation below concisely. Preserve "
            "(a) the user's original intent, (b) key actions taken "
            "(tools used, files touched), (c) decisions made, (d) "
            "outcomes / discoveries. Stay under 2000 tokens. Plain text."
        )
        summary_capabilities = replace(
            self.capabilities,
            supports_structured_output=False,
            supports_thinking=False,
        )
        response = self.provider.call(
            messages=messages,
            system=summarisation_prompt,
            model=self.model,
            capabilities=summary_capabilities,
        )
        return response.content if hasattr(response, "content") else str(response)

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
            # Loop exited. Distinguish "interrupted between turns"
            # (stop_event set after the previous turn finished — the body's
            # _interrupted check never re-runs) from a real max_turns hit.
            # Without this branch, a Ctrl+C that arrives mid-turn is reported
            # as "Max turns (0) reached" once the turn wraps up, which is
            # misleading when max_turns is unset.
            if self._interrupted:
                return self._on_interrupt()
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

        interrupt_msg = INTERRUPT_NOTICE
        if self.ctx:
            self.ctx.add({"role": "user", "content": interrupt_msg})
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
            agent_stack=self.agent_stack,
            agent_role=self.agent_role,
            session_dir=session_dir,
            mcp_manager=self.mcp_manager,
            wire_format=self.wire_format,
            depth=self.depth,
            max_depth=self.max_depth,
        )

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
        render_turn_sep(self.turn)

    def _execute_turn(self):
        """Single turn: hooks, LLM call, text parse, dispatch."""
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
        if response.usage:
            _render_token_stats(response.usage, self.turn, self.verbose)

        # PostLLMCall hook
        self._fire_hook("PostLLMCall", llm_response=llm_text)

        render_raw(llm_text, self.turn, self.verbose)
        if self.verbose and response.thinking:
            render_thinking(response.thinking, self.turn)

        result = self._handle_text_path(llm_text)

        # OnTurnEnd hook
        self._fire_hook("OnTurnEnd")

        return result

    def _call_llm(self):
        """LLM call with overflow retry and streaming. Returns response or sentinel."""
        # Context dump (verbose only)
        if self.verbose:
            render_context_dump(self.messages, self.turn)
        _debug_log(
            f"LLM_CALL turn={self.turn} skill={self.skill_name or 'main'} msg_count={len(self.messages)}"
        )

        # Build streaming callback: stops spinner on first chunk, then streams
        spinner_active = True

        def on_chunk(text: str) -> None:
            nonlocal spinner_active
            if spinner_active:
                render_spinner_stop()
                spinner_active = False
            render_stream_chunk(text)

        if self.skill_name:
            render_spinner_start(f"skill:{self.skill_name}")
        else:
            render_spinner_start()
        # Plugin-defined provider hints (e.g. envelope formats need
        # ``skip_json_format=True`` so Ollama doesn't force ``{`` as the
        # first token, which would conflict with the ``<tool_use>``
        # envelope opening). ReAct returns ``{}`` so the call path is
        # byte-equivalent for the default plugin.
        extra_call_kwargs = self.wire_format.provider_call_kwargs()

        # Plugin-defined prefill: a string the provider sees as the start
        # of an assistant turn. Forces the model to continue from there,
        # producing the wire format from the very first generated token.
        # ReAct returns "" (no prefill — its prior already produces ReAct
        # shape). Envelope plugins return e.g. ``<tool_use id="r1" action="``
        # so the model emits the tool name next. Empty string => behaviour
        # is identical to the pre-plugin path.
        prefill = self.wire_format.prefill()
        call_messages = self.messages
        if prefill:
            call_messages = [
                *self.messages,
                {"role": "assistant", "content": prefill},
            ]
        try:
            response = self.provider.call(
                messages=call_messages,
                system=self.system,
                model=self.model,
                capabilities=self.capabilities,
                on_chunk=on_chunk,
                **extra_call_kwargs,
            )
            # Stitch the prefill back onto the response so downstream
            # parsers see a complete emission. Use dataclasses.replace
            # to keep usage / thinking / stop_reason intact.
            if prefill:
                from dataclasses import replace as _replace

                response = _replace(response, content=prefill + response.content)
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
            if spinner_active:
                render_spinner_stop()
            else:
                render_stream_end()

    def _handle_text_path(self, llm_text: str):
        """Handle text parsing response (Ollama, fallback).

        Recovery primitives consume only the emitted text (``llm_text``)
        — the thinking channel is intentionally excluded from the
        recovery path (see ``docs/robust-harness/DESIGN.md`` §2.2).

        TurnRecord is emitted exactly once per call, regardless of which
        terminal branch is taken (success/retry/exception). Branches
        that fire an Intervention mutate ``outcome`` (failure_signal +
        primitives) before returning, and the trailing finally writes
        the record.
        """
        parsed = self.wire_format.parse(llm_text)

        # Classify outcome early; the dispatch body may mutate this
        # dict to reflect a B1 (action loop) detection that is only
        # known after we see the chosen action.
        if parsed.parse_stage == 0:
            # Split A1 into two sub-modes — empty/whitespace-only output
            # vs non-empty content that drifted from JSON. The recovery
            # path is identical (RETRY_HINT_NO_JSON fallback in both),
            # but the labels separate two operationally different
            # failure shapes for analysis (DESIGN.md §1, A1a vs A1b).
            if not (llm_text or "").strip():
                initial_signal = FAILURE_NO_OUTPUT
            else:
                initial_signal = FAILURE_NO_JSON
        elif not parsed.action:
            initial_signal = FAILURE_NO_ACTION
        else:
            initial_signal = None
        outcome: dict = {"failure_signal": initial_signal, "primitives": []}

        try:
            return self._dispatch_text_path(llm_text, parsed, outcome)
        finally:
            self.recorder.record(
                model=self.model,
                parse_stage=parsed.parse_stage,
                failure_signal=outcome["failure_signal"],
                primitives_applied=outcome["primitives"],
            )

    def _dispatch_text_path(self, llm_text: str, parsed, outcome: dict):
        """Body of the text-parsing path. Returns a ToolResult or a sentinel.

        ``outcome`` is a mutable dict owned by the caller. Branches
        that fire an Intervention update ``outcome["failure_signal"]``
        and/or ``outcome["primitives"]`` before returning so the
        caller's ``finally`` block records what happened.
        """

        # A7 NO_THOUGHT — action present but thought missing. Retry
        # before dispatch so the omission does not enter the transcript
        # as a precedent for future turns (mimicry-strengthening loop:
        # the raw response is mirrored back on the next turn and
        # crowds out the system prompt's Format Rule 1).
        if self.wire_format.thought_required and detect_thought_missing(
            parsed.thought, parsed.action
        ):
            # ``thought_required`` is False on plugins where the thought
            # is preceding free text rather than a schema field — for
            # those, missing thought is not a drift signal.
            _debug_log(
                f"NO_THOUGHT: action={parsed.action!r}, thought={parsed.thought!r}"
            )
            render_status(
                "error",
                "Response missing thought. Retrying...",
                self.turn,
            )
            # ReAct-only: format_no_thought_retry lives on the plugin,
            # not in recovery/builders, because it has no meaning when
            # ``thought_required`` is False (envelope plugins).
            intervention = self.wire_format.format_no_thought_retry(
                prior_content=llm_text
            )
            _append_observation(
                self.messages,
                self.ctx,
                self.wire_format,
                llm_text,
                intervention.message,
                tool_name="",
                success=False,
            )
            outcome["failure_signal"] = FAILURE_NO_THOUGHT
            outcome["primitives"] = list(intervention.primitives)
            self.turn -= 1
            return self._CONTINUE

        # 6. Thought
        if parsed.thought:
            render_step("thought", parsed.thought, self.turn)

        # 7. Complete tool (text parsing path)
        _debug_log(f"PARSED iter={self.turn} action={parsed.action}")
        if parsed.action == "complete":
            if isinstance(parsed.action_input, dict):
                raw = parsed.action_input.get("result")
                answer = (
                    str(raw)
                    if raw
                    else "(Completed without result — model may lack capability for this task)"
                )
            elif isinstance(parsed.action_input, str):
                raw = parsed.action_input
                answer = (
                    parsed.action_input
                    or "(Completed without result — model may lack capability for this task)"
                )
            else:
                raw = None
                answer = (
                    str(parsed.action_input)
                    if parsed.action_input
                    else "(Completed without result — model may lack capability for this task)"
                )

            # A6 (Nested envelope) — detection records the signal for
            # observability AND we unwrap one level so the user-facing
            # answer doesn't carry a literal ``{"result": "..."}`` prefix.
            # Single-level only (recursive nesting indicates a different
            # bug worth surfacing). ``raw`` may be from ``parsed.action_
            # input`` (dict path) or the input itself (str path); both
            # surface as the same artifact, so we re-derive ``answer``
            # from the unwrapped value.
            if detect_nested_envelope(raw):
                outcome["failure_signal"] = FAILURE_NESTED_ENVELOPE
                unwrapped = unwrap_nested_envelope(raw)
                if unwrapped != raw:
                    answer = unwrapped or answer

            if self.ctx:
                self.ctx.add(
                    {
                        "role": "assistant",
                        "thought": parsed.thought or "",
                        "action": "complete",
                        "action_input": {"result": answer},
                    }
                )
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
            render_step("final", echo_answer, self.turn)

            return ToolResult(True, output=echo_answer)

        # 10. Ask tool -- prompt user for input (text parsing path)
        if parsed.action == "ask":
            questions = _extract_questions(parsed.action_input)
            if questions:
                # Emit the action step so out-of-band renderers (web)
                # replace their streaming card with a structured
                # ``assistant_turn``. Without this, the raw-JSON
                # streaming card stays on screen and the next turn's
                # stream chunks visually append to it — the user sees
                # consecutive assistant emissions glued together.
                render_step(
                    "action",
                    "",
                    self.turn,
                    tool_name="ask",
                    tool_input=json.dumps(parsed.action_input, ensure_ascii=False)
                    if isinstance(parsed.action_input, dict)
                    else str(parsed.action_input),
                )
                user_response = _handle_ask(questions)
                obs_msg = f"Observation: User responded:\n{user_response}"
                _append_observation(
                    self.messages,
                    self.ctx,
                    self.wire_format,
                    llm_text,
                    obs_msg,
                    tool_name="ask",
                    success=True,
                )
                return self._CONTINUE

        # 10b. run_skill -- intercept at loop level (text parsing path)
        if parsed.action == "run_skill":
            skill_input = (
                parsed.action_input if isinstance(parsed.action_input, dict) else {}
            )
            # Same reason as ``ask`` above — close out the streaming
            # card before the (often long-running) skill subprocess
            # starts emitting its own events.
            render_step(
                "action",
                "",
                self.turn,
                tool_name="run_skill",
                tool_input=json.dumps(skill_input, ensure_ascii=False),
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
                parent_hooks_config=self.hooks_config,
                parent_depth=self.depth,
                max_depth=self.max_depth,
            )
            obs = skill_tool_result.output or skill_tool_result.error
            render_step(
                "observation",
                obs,
                self.turn,
                tool_name="run_skill",
                success=skill_tool_result.success,
            )
            obs_msg = f"Observation: {obs}"
            _append_observation(
                self.messages,
                self.ctx,
                self.wire_format,
                llm_text,
                obs_msg,
                tool_name="run_skill",
                success=skill_tool_result.success,
                artifact=skill_tool_result.artifact,
            )
            return self._CONTINUE

        # 10c. ready_for_review -- return original query for self-check (text path)
        if parsed.action == "ready_for_review":
            summary = ""
            if isinstance(parsed.action_input, dict):
                summary = parsed.action_input.get("summary", "")
            # Same streaming-card concern as ``ask`` / ``run_skill``.
            # Skill-mode skips the observation render below, so we
            # only emit the action step at top level too — keeps the
            # nested skill output clean.
            if not self.skill_name:
                render_step(
                    "action",
                    "",
                    self.turn,
                    tool_name="ready_for_review",
                    tool_input=json.dumps(parsed.action_input, ensure_ascii=False)
                    if isinstance(parsed.action_input, dict)
                    else str(parsed.action_input or {}),
                )
            obs = _build_review_observation(self.query, summary, ctx=self.ctx)
            if not self.skill_name:
                render_step(
                    "observation",
                    obs,
                    self.turn,
                    tool_name="ready_for_review",
                    success=True,
                )
            obs_msg = f"Observation: {obs}"
            _append_observation(
                self.messages,
                self.ctx,
                self.wire_format,
                llm_text,
                obs_msg,
                tool_name="ready_for_review",
                success=True,
            )
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

            # B1 (action loop) detection — observe BEFORE dispatch so a
            # repeated call doesn't pay the cost of the redundant tool
            # run. Counter resets after a tool error so legitimate
            # retries don't false-positive.
            prev_was_error = bool(
                self.recent_tool_history
                and self.recent_tool_history[-1].get("tool") == tool_name
                and self.recent_tool_history[-1].get("success") is False
            )
            loop_level = self.loop_detector.observe(
                tool_name, tool_input, prev_was_error=prev_was_error
            )
            if loop_level >= 1:
                outcome["failure_signal"] = FAILURE_ACTION_LOOP
                args_repr = (
                    json.dumps(tool_input, sort_keys=True, ensure_ascii=False)
                    if isinstance(tool_input, dict)
                    else str(tool_input)
                )
                intervention = format_action_loop_intervention(
                    level=loop_level,
                    action=tool_name,
                    args_repr=args_repr,
                    repeat_count=self.loop_detector.consecutive_count,
                    task=self.query,
                )
                if intervention is None:
                    # Level ≥3: recovery exhausted — hard fail with a
                    # message that cites which primitives were already
                    # tried so the user knows we did not give up early.
                    _debug_log(
                        f"Loop hard-fail: {tool_name} input={args_repr[:100]} "
                        f"level={loop_level} skill_name={self.skill_name}"
                    )
                    render_status(
                        "error",
                        f"Action loop unresolved: {tool_name} repeated; "
                        "tried probe_progress and restate_task without "
                        "recovery. Stopping.",
                    )
                    return ToolResult(
                        False,
                        error=(
                            "Action loop unresolved: probe_progress and "
                            "restate_task did not break the repetition."
                        ),
                    )
                # Level 1 or 2: inject Intervention, skip dispatch,
                # let the next turn try again with the new context.
                render_status(
                    "error",
                    f"Action loop detected ({tool_name}, level {loop_level}). "
                    "Nudging model.",
                    self.turn,
                )
                _append_observation(
                    self.messages,
                    self.ctx,
                    self.wire_format,
                    llm_text,
                    intervention.message,
                    tool_name=tool_name,
                    success=False,
                )
                outcome["primitives"] = list(intervention.primitives)
                self.turn -= 1  # Don't count loop nudges as user-facing turns
                return self._CONTINUE

            render_step(
                "action",
                "",
                self.turn,
                tool_name=tool_name,
                tool_input=json.dumps(tool_input, ensure_ascii=False)
                if isinstance(tool_input, dict)
                else str(tool_input),
            )

            # A4 (Unknown tool) — pre-dispatch detection. Skips _dispatch_tool_with_hooks
            # entirely so the recovery layer is the single source of truth for
            # this failure mode (DESIGN.md §4 invariant: same primitive shape
            # across reused failures). The error message is the same one the
            # leaf-level dispatch would have produced — primitive extraction
            # for "did you mean" suggestions is deferred to Step 4b once
            # observability data shows whether it improves recovery.
            if detect_unknown_tool(tool_name, self.tools_list):
                outcome["failure_signal"] = FAILURE_UNKNOWN_TOOL
                avail = ", ".join(self.tools_list)
                err_msg = f"Unknown tool '{tool_name}'. Available: {avail}"
                render_step(
                    "observation",
                    err_msg,
                    self.turn,
                    tool_name=tool_name,
                    success=False,
                )
                _append_observation(
                    self.messages,
                    self.ctx,
                    self.wire_format,
                    llm_text,
                    f"Observation: {err_msg}",
                    tool_name=tool_name,
                    success=False,
                )
                return self._CONTINUE

            # A5 (Schema mismatch) — pre-dispatch detection. Same rationale
            # as A4: single source of truth in the recovery layer. The
            # detector also normalizes the input (string→dict promotion)
            # when valid; we use the normalized value if present.
            mismatched, schema_err, normalized = detect_schema_mismatch(
                tool_name, tool_input
            )
            if mismatched:
                outcome["failure_signal"] = FAILURE_SCHEMA_MISMATCH
                err_msg = f"{schema_err} Fix action_input and retry."
                render_step(
                    "observation",
                    err_msg,
                    self.turn,
                    tool_name=tool_name,
                    success=False,
                )
                _append_observation(
                    self.messages,
                    self.ctx,
                    self.wire_format,
                    llm_text,
                    f"Observation: {err_msg}",
                    tool_name=tool_name,
                    success=False,
                )
                return self._CONTINUE
            tool_input = normalized  # use post-normalization input for dispatch

            # Execute tool (method tracks self.recent_tool_history,
            # uses self.* for provider/ctx/hooks/etc.)
            tool_result = self._dispatch_tool_with_hooks(tool_name, tool_input)

            observation = (
                tool_result.output if tool_result.success else tool_result.error
            )
            if truncation_warning:
                observation = f"{observation}\n{truncation_warning}"

            render_step(
                "observation",
                observation,
                self.turn,
                tool_name=tool_name,
                success=tool_result.success,
            )

            # Inject observation with structured artifact
            obs_msg = f"Observation: {observation}"
            _append_observation(
                self.messages,
                self.ctx,
                self.wire_format,
                llm_text,
                obs_msg,
                tool_name=tool_name,
                success=tool_result.success,
                artifact=tool_result.artifact,
            )
            return self._CONTINUE

        # 12. Missing action or parse failure -- retry with appropriate hint.
        # Echo the model's failed output back as failure grounding (content
        # shows structural drift: YAML-style keys, function-call syntax,
        # bare prose). Thinking-channel echo is excluded from v1 — see
        # docs/robust-harness/DESIGN.md §2.2.
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
            intervention = format_no_action_retry(
                prior_content=llm_text, wire_format=self.wire_format
            )
        else:
            # JSON parse failed entirely
            _debug_log(f"JSON parse failed (stage={parsed.parse_stage}):\n{llm_text}")
            render_status(
                "error",
                "Invalid JSON response. Retrying...",
                self.turn,
            )
            intervention = format_no_json_retry(
                prior_content=llm_text, wire_format=self.wire_format
            )
        _append_observation(
            self.messages,
            self.ctx,
            self.wire_format,
            llm_text,
            intervention.message,
            tool_name="",
            success=False,
        )
        # Surface composed primitive names to the enclosing _handle_text_path
        # so the trailing finally-block records them.
        outcome["primitives"] = list(intervention.primitives)
        self.turn -= 1  # Don't count format retries
        return self._CONTINUE

    def _dispatch_tool_with_hooks(self, tool_name: str, tool_input):
        """Orchestrator: pre-hooks → invoke → guards → post-hooks → record.

        Preconditions (enforced by ``_dispatch_text_path``):
            - ``tool_name`` is a valid name in ``self.tools_list`` (A4
              already checked).
            - ``tool_input`` matches the tool's schema (A5 already checked).

        The body is intentionally a 5-line recipe; each stage is a
        single-purpose helper. See the helpers' docstrings for what
        each stage owns.
        """
        _debug_log(
            f"TOOL turn={self.turn} action={tool_name} input={str(tool_input)[:200]}"
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
                f"TOOL EXCEPTION turn={self.turn} tool={tool_name}\n{_tb.format_exc()}"
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
        if self.hook_runner:
            pre_ctx = self.hook_runner.fire(
                "PreToolUse",
                tool_name=tool_name,
                tool_input=input_dict,
                turn=self.turn,
                mcp_manager=self.mcp_manager,
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

        if self.hooks_config:
            from agent_cli.hooks import run_hooks

            pre_result = run_hooks(
                "PreToolUse", tool_name, input_dict, hooks_config=self.hooks_config
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
        if self.hook_runner:
            self.hook_runner.fire(
                "OnDelegateStart",
                tool_name="delegate",
                tool_input=input_dict,
                turn=self.turn,
                mcp_manager=self.mcp_manager,
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
            parent_ctx=self.ctx,
            provider=self.provider,
            model=self.model,
            capabilities=self.capabilities,
            provider_name=self.provider_name,
            base_url=self.base_url,
            api_key=self.api_key,
            depth=self.depth,
            max_depth=self.max_depth,
            max_turns=self.max_turns,
            timeout=self.delegate_timeout,
            session=self.session,
            skill_stack=self.skill_stack,
            agent_stack=self.agent_stack,
            stop_event=self.stop_event,
            hooks_config=self.hooks_config,
        )

        if self.hook_runner:
            self.hook_runner.fire(
                "OnDelegateEnd",
                tool_name="delegate",
                tool_input=input_dict,
                delegate_result=result,
                turn=self.turn,
                mcp_manager=self.mcp_manager,
            )
        return result

    # ── 3. Regular tool dispatch ───────────────────────────────────
    def _invoke_regular(self, tool_name: str, tool_input) -> ToolResult:
        """Dispatch a non-delegate tool via the registry.

        Recovery layer (A4/A5 detectors in ``_dispatch_text_path``) has
        already validated tool_name + action_input. The leaf primitive
        ``_execute_tool`` trusts that contract and would raise KeyError
        on a missing name.
        """
        session_dir = self.ctx.session_dir if self.ctx else None
        return _execute_tool(tool_name, tool_input, session_dir=session_dir)

    # ── 4. PostToolUse hooks ───────────────────────────────────────
    def _run_post_hooks(
        self, tool_name: str, input_dict: dict, result: ToolResult
    ) -> None:
        """Fire PostToolUse hooks (runner + shell config). Pure side
        effect — never modifies ``result``.

        Failure runs route to ``PostToolUseFailure`` for the shell-config
        path so policy hooks can react differently to errors.
        """
        if self.hook_runner:
            self.hook_runner.fire(
                "PostToolUse",
                tool_name=tool_name,
                tool_input=input_dict,
                tool_result=result,
                turn=self.turn,
                mcp_manager=self.mcp_manager,
            )
        if self.hooks_config:
            from agent_cli.hooks import run_hooks

            _obs = result.output if result.success else result.error
            _evt = "PostToolUse" if result.success else "PostToolUseFailure"
            run_hooks(
                _evt,
                tool_name,
                input_dict,
                hooks_config=self.hooks_config,
                tool_result=_obs,
            )

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
                "turn": self.turn,
                "success": result.success,
            }
        )

    def _on_max_turns(self):
        """Handle max turns reached."""
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
    record_turns: bool = True,
    wire_format=None,
    compaction_enabled: bool = True,
):
    """Run the agent loop with the given wire-format plugin. Returns ToolResult.

    ``wire_format`` accepts a registered plugin name (str) or a
    ``WireFormat`` instance directly. ``None`` falls back to the
    "react" plugin so existing callers don't need to change.
    """
    if isinstance(wire_format, str):
        wire_format = _get_wire_format(wire_format)
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
        record_turns=record_turns,
        wire_format=wire_format,
        compaction_enabled=compaction_enabled,
    ).run()


def _render_token_stats(usage, turn: int, verbose: bool = False) -> None:
    """Render token throughput stats when duration data is available.

    When verbose is False, append a short hint pointing at --verbose so users
    know raw LLM responses are available on demand (the per-turn `📄 raw ...`
    line is suppressed in non-verbose mode to keep the stream uncluttered).
    """
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
    # Anthropic prompt cache visibility — only render when non-zero so
    # other providers' summary lines stay unchanged.
    if usage.cache_read_input_tokens:
        parts.append(f"cache hit: {usage.cache_read_input_tokens} tok")
    if usage.cache_creation_input_tokens:
        parts.append(f"cache write: {usage.cache_creation_input_tokens} tok")
    if not parts:
        return
    msg = " | ".join(parts)
    if not verbose:
        msg += "  (use --verbose to view raw response)"
    render_status("running", msg, turn)


# Fields a model might use to wrap a question's text inside a dict —
# e.g. `questions=[{"question": "..."}]` drift observed with qwen3.6
# in S25FE-kernel session 1776954600. Checked in priority order, so a
# `question` key wins over `text` when both are present.
_QUESTION_TEXT_KEYS = ("question", "text", "content", "q")


def _extract_question_text(item) -> str | None:
    """Pull the text out of a single question item, or return None if
    the item can't be interpreted as a question.

    Strings are returned as-is (when non-empty). Dicts are probed for
    one of the known text-bearing field names. Anything else — nested
    lists, numbers, dicts without a recognizable text field, dicts
    whose value is itself a non-string — returns None so the caller
    can drop the item rather than rendering a raw repr.
    """
    if isinstance(item, str):
        return item if item else None
    if isinstance(item, dict):
        for key in _QUESTION_TEXT_KEYS:
            v = item.get(key)
            if isinstance(v, str) and v:
                return v
    return None


def _extract_questions(action_input) -> list[str]:
    """Extract a list of question strings from an `ask` tool input,
    tolerating the various shapes models emit:

    - {"questions": ["a", "b"]}                 — the canonical form
    - {"questions": "single"}                   — single-string variant
    - {"question": "legacy"}                    — older singular field
    - "direct question"                         — bare string
    - ["q1", "q2"]                              — bare list
    - {"questions": [{"question": "..."}, ...]} — list of dict items
      with a nested text field (qwen3.6 drift)
    - {"questions": {"question": "..."}}        — single dict wrapper

    Dict items without a recognizable text field drop silently instead
    of rendering as `str(dict)` repr noise.
    """
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
        return [t for q in raw_questions if (t := _extract_question_text(q))]
    if isinstance(raw_questions, dict):
        text = _extract_question_text(raw_questions)
        return [text] if text else []
    return []


def _handle_ask(questions: list[str]) -> str:
    """Display all questions at once and collect a single response."""
    import re

    from agent_cli.render import get_renderer

    # Strip existing leading "1.", "2)", "- ", etc. so our numbering isn't doubled
    def _strip_leading_marker(q: str) -> str:
        return re.sub(r"^\s*(?:\d+[.):]|[-*•])\s+", "", q)

    # Respect nested depth prefix (so ask inside skill/delegate aligns with │)
    renderer = get_renderer()
    prefix = getattr(renderer, "_prefix", "")

    # Announce the questions through the renderer instead of writing
    # to ``console`` directly. CLI renderers print the colored block;
    # WebRenderer no-ops because the same text reaches the UI via
    # ``prompt_user(context=...)`` below, and a duplicate emission
    # would just bleed terminal noise into the web-launch terminal.
    cleaned_questions = [_strip_leading_marker(q) for q in questions]
    renderer.announce_ask(cleaned_questions, prefix=prefix)
    # Plain-text mirror of the announcement above — passed to
    # ``prompt_user`` as ``context`` so out-of-band renderers (web)
    # can surface the question alongside the input affordance. CLI
    # renderers ignore it; ``announce_ask`` is what the terminal
    # user sees with colour.
    if len(questions) > 1:
        context_lines = [
            f"{i}. {_strip_leading_marker(q)}" for i, q in enumerate(questions, 1)
        ]
    else:
        context_lines = [_strip_leading_marker(questions[0])]
    context_text = "Agent asks:\n" + "\n".join(f"  {line}" for line in context_lines)
    # Route through the renderer so paste and """ ... """ multiline work
    # at the CLI and a web renderer can serve the same prompt as a form
    # without the loop knowing the difference. ``prompt_user`` propagates
    # EOF / Ctrl+C — caller policy is "(no response)" so the assistant
    # gets a stable answer slot even when the user bails.
    from agent_cli.render import get_renderer

    try:
        answer = get_renderer().prompt_user(
            f"{prefix}\n{prefix}Your answer: ",
            multiline=True,
            continuation=f"{prefix}... ",
            context=context_text,
        )
    except (EOFError, KeyboardInterrupt):
        answer = "(no response)"

    q_part = "\n".join(f"Q: {_strip_leading_marker(q)}" for q in questions)
    return f"{q_part}\nA: {answer}"


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
    parent_hooks_config: dict | None = None,
    parent_depth: int = 0,
    max_depth: int = 2,
):
    """Handle run_skill at loop level with full ctx access."""
    # Inline import: circular dependency — executor.py imports run_loop from this module
    from agent_cli.recovery.recursion import (
        format_depth_limit_error,
        format_recursion_error,
    )
    from agent_cli.skills import load_skills
    from agent_cli.skills.executor import execute_skill

    name = skill_input.get("name", "")
    arguments = skill_input.get("arguments", "")
    # LLM might send arguments as dict instead of string
    if not isinstance(arguments, str):
        arguments = str(arguments) if arguments else ""

    if not name:
        return ToolResult(False, error="run_skill: 'name' is required.")

    # Cycle check (A → B → A). Stack lookup is O(N) but the stack
    # is bounded by ``max_depth`` so this is effectively constant.
    if skill_stack and name in skill_stack:
        return ToolResult(
            False,
            error=format_recursion_error("skill", name, list(skill_stack)),
        )

    # Depth ceiling — belt-and-suspenders. The AgentLoop init has
    # already removed ``run_skill`` from the tools_list when we hit
    # the limit, so a model going through the normal dispatch path
    # never reaches this branch. Direct callers (tests, custom
    # active_tools, future integrations) hit it here with the same
    # message the LLM would otherwise see.
    if parent_depth >= max_depth:
        return ToolResult(
            False,
            error=format_depth_limit_error("skill", name, parent_depth, max_depth),
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

    render_group_start(f"skill:{name}", icon="🪄")
    render_push_depth()
    t0 = time.monotonic()

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
            max_depth=max_depth,
            ctx=ctx,
            session=session,
            skill_stack=skill_stack,
            graceful_interrupt=graceful_interrupt,
            stop_event=stop_event,
            parent_hooks_config=parent_hooks_config,
            parent_depth=parent_depth,
        )
    except Exception as e:
        _debug_log(f"run_skill({name}) exception: {e}")
        skill_result = ToolResult(False, error=f"run_skill({name}) failed: {e}")
    finally:
        render_pop_depth()
        render_group_end(
            f"skill:{name}",
            success=skill_result.success if skill_result else False,
            duration_s=time.monotonic() - t0,
        )

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
    """Build the observation returned by ready_for_review tool.

    The observation re-injects the original request (often pushed out of
    recency by long transcripts), pairs it with the model's self-summary,
    and asks the model to write out a per-requirement check against its
    previous Observations. The structured "Format your review like this"
    block forces the self-review to be *generated* rather than asserted
    in one line — small models that follow output templates also tend
    to follow the reasoning the template implies.
    """
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
            "",
            "1. List each requirement from the ORIGINAL REQUEST.",
            "2. For each requirement, check your previous Observations in this "
            "conversation for evidence it was completed.",
            "3. If a requirement is NOT met or evidence is missing, continue "
            "working on it.",
            "4. Only call complete if EVERY requirement has clear evidence of "
            "completion.",
            "",
            "Format your review like this:",
            "Requirement 1: <short paraphrase of the requirement>",
            "  -> [DONE | MISSING]: <evidence from an Observation, or what "
            "is still needed>",
            "Requirement 2: ...",
            "Decision: complete | continue",
        ]
    )
    return "\n".join(parts)


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


def _append_observation(
    messages: list[dict],
    ctx,
    wire_format,
    llm_text: str,
    obs_msg: str,
    *,
    tool_name: str,
    success: bool,
    artifact: str = "",
) -> None:
    """Text parsing: append assistant + observation + sync ctx.

    For the in-memory messages list (sent to LLM), the wire_format plugin
    decides what the assistant turn looks like as next-turn prior. ReAct
    returns ``llm_text`` unchanged — raw IS the wire shape. Envelope
    formats may re-render so the model's own prior teaches the envelope
    instead of reinforcing whatever drift happened mid-turn.

    For history.jsonl (via ctx.add), the wire_format plugin shapes the
    assistant record (e.g. ReAct splits ``thought / action / action_input``
    into top-level fields) so the on-disk history retains structured form.

    The observation entry stores ``tool`` (the tool that ran, or an
    empty string for format-retry interventions) and ``success`` (so
    the web renderer's ``replay_from_history`` can re-emit the same
    ✓/✗ shape a live observation event has). The presence of the
    ``tool`` key — not its truthiness — distinguishes a tool result
    from a plain user chat turn, so empty-string ``tool_name`` (used
    by format-retry paths) still routes through ``observation()``.
    """
    messages.append(
        {
            "role": "assistant",
            "content": wire_format.normalize_assistant_for_messages(llm_text),
        }
    )
    messages.append({"role": "user", "content": obs_msg})
    if ctx:
        ctx.add(wire_format.serialize_assistant_for_history(llm_text))
        obs_entry = {
            "role": "user",
            "tool": tool_name,
            "success": success,
            "content": obs_msg,
        }
        if artifact:
            obs_entry["artifact"] = artifact
        ctx.add(obs_entry)
