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
    OUTPUT_TRUNCATED_NOTICE,
)
from agent_cli.recovery.common_recovery import format_action_loop_intervention
from agent_cli.recovery.wf_recovery import (
    format_no_action_retry,
    format_no_json_retry,
)
from agent_cli.tools.result import ToolResult

from agent_cli.context.manager import ContextManager
from agent_cli.context.overflow import is_context_overflow, parse_overflow_amounts
from agent_cli.context.token_estimator import estimate_tokens
from agent_cli.prompts.system_prompt import build_system_prompt_sections
from agent_cli.providers.base import LLMProvider
from agent_cli.providers.capabilities import ModelCapabilities
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
    FAILURE_DEGENERATE,
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
    render_system_prompt_snapshot,
    render_header,
    render_turn_sep,
    render_raw,
    render_recovery,
    render_thinking,
    render_spinner_start,
    render_spinner_stop,
    render_status,
    render_step,
    render_stream_chunk,
    render_stream_end,
    render_token_usage,
    render_push_depth,
    render_pop_depth,
    render_group_start,
    render_group_end,
)
from agent_cli.tools import TOOLS, _execute_tool, infer_action
from agent_cli.tools.delegate import tool_delegate

from agent_cli.verbose import debug_log as _debug_log, set_verbose as _set_debug_verbose
from agent_cli.wire_formats import get as _get_wire_format

# Max shrink-and-retry attempts per turn when the server rejects the
# prompt as too long (flow 2 reactive recovery). Each attempt sheds more
# history via ``ContextManager.force_fit``; the bound stops a runaway
# loop when the cache cannot shrink enough or the server keeps rejecting.
_MAX_OVERFLOW_RETRIES = 5
# Compaction TARGET ratio: compact down to 80% of available headroom (leave a
# 20% margin). Distinct from manager's _COMPACTION_THRESHOLD_RATIO (0.9 = when
# to TRIGGER). Used by both the preventive (flow 1) and overflow-recovery
# (flow 2) target computations in _call_llm.
_COMPACTION_TARGET_RATIO = 0.8


class AgentLoop:
    """Encapsulates the ReAct agent loop state and execution."""

    def __init__(
        self,
        query: str,
        provider: LLMProvider,
        capabilities: ModelCapabilities,
        model: str,
        provider_name: str = "openai",
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
        dequeue_user_message=None,
        route_message=None,
        query_author: str | None = None,
        agent_role: str = "",
        agent_name: str = "",
        mcp_manager=None,
        hook_runner=None,
        record_turns: bool = True,
        record_raw_failures: bool | None = None,
        wire_format=None,
        compaction_enabled: bool = True,
    ):
        # Wire format plugin — ReAct by default. Centralizes the
        # parser, recovery wording, prompt section, and lifecycle hooks
        # so adding a new format means dropping a file in
        # ``agent_cli/wire_formats/`` and re-running with
        # ``--response-format <name>``.
        if wire_format is None:
            wire_format = _get_wire_format()
        self.wire_format = wire_format

        self.query = query
        # Web multi-user intake. ``query_author`` = nickname of whoever sent the
        # run-STARTING message (None for CLI / single user). ``dequeue_user_message``
        # pulls ONE queued message at each turn boundary; ``route_message(text)``
        # routes it through the SAME command path as a run-starter (``/sh``,
        # ``/compact``, ``@agent``, ``/skill``) and returns True when it handled
        # the message — so an injected command behaves identically to one typed
        # at run-start, instead of leaking in as literal chat text. A plain chat
        # message (route_message returns False / None) is injected as a steering
        # user turn. ``task_log`` accumulates EVERY user request (starter +
        # injected) so recovery / review reference the full set of asks.
        self.query_author = query_author
        self.dequeue_user_message = dequeue_user_message
        self.route_message = route_message
        self.task_log: list[str] = []
        self.provider = provider
        self.capabilities = capabilities
        # Oversized-observation cap: a single tool observation larger than
        # this many tokens is replaced (at the result→observation seam) with
        # a narrow-it nudge instead of crowding out the context. context_window
        # / 10 — large outputs degrade response quality, so we steer the model
        # to fetch narrower instead of dumping. 0 = disabled (headless/test
        # paths with no capabilities). Per-tool opt-out via Tool.apply_oversized_cap.
        self._oversized_cap = (
            capabilities.context_window // 10
            if capabilities and getattr(capabilities, "context_window", 0)
            else 0
        )
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
        # Reactive context-overflow recovery (flow 2): how many times we
        # have shrunk-and-retried for the CURRENT turn. Bounded by
        # ``_MAX_OVERFLOW_RETRIES`` so a pathological case (the cache
        # cannot shrink enough, or the server keeps rejecting) fails
        # cleanly instead of looping forever.
        self.overflow_retries = 0
        # Cumulative output tokens across this loop's turns — fed to the
        # renderer's per-turn token-usage line / web top-bar so the user
        # sees a running session total alongside the per-turn numbers.
        self._total_output_tokens = 0
        self._interrupted = False
        self._prev_sigint_handler = None
        self.graceful_interrupt = graceful_interrupt
        self.recent_tool_history: list[dict] = []
        self.messages: list[dict] = []
        # Named system-prompt sections — single source of truth for
        # ``self.system`` (always derived via join) and the web Prompt
        # Inspector snapshot. Populated by _setup; updated by
        # _apply_system_sections.
        self._system_sections: list[tuple[str, str]] = []
        self.system = ""
        # Sentinels: distinct from None (failure) and str (answer)
        self._CONTINUE = object()  # keep looping
        self._RETRY = object()  # overflow retry

        # Observability — per-turn record. Disabled when no session
        # (headless / subagent) or when user opted out.
        #
        # raw_failures capture: debug-only dump of failed turns' raw
        # response (recovery-rule analysis). Off by default; opt in via the
        # AGENT_CLI_RECORD_RAW_FAILURES env var so every entry point
        # (run / chat / web) is covered without a per-command CLI flag.
        if record_raw_failures is None:
            import os

            record_raw_failures = os.environ.get(
                "AGENT_CLI_RECORD_RAW_FAILURES", ""
            ).strip().lower() in ("1", "true", "on", "yes")
        self.recorder = TurnRecorder(
            session_dir=(self.ctx.session_dir if self.ctx else None),
            enabled=record_turns,
            record_raw=record_raw_failures,
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
            "You are compacting an agent's working transcript so it can "
            "continue the task with this summary in place of the raw "
            "history. Write a plain-text summary under these headings "
            "(omit a heading if it has no content):\n\n"
            "TASK — the user's original request(s) and intent.\n"
            "STATE — what is currently true: progress so far, what works.\n"
            "DONE — actions taken: tools used and exact file paths / "
            "symbols touched, and what changed.\n"
            "PENDING — work not yet finished and the next step that was "
            "about to run.\n"
            "DECISIONS — choices made and why, including alternatives "
            "rejected.\n"
            "FAILURES — approaches that failed or errors encountered, so "
            "they are not retried.\n"
            "FACTS — keep verbatim: paths, identifiers, commands, config "
            "values, signatures, error strings, and any user "
            "correction/preference.\n\n"
            "Rules: use ONLY information present in the transcript — do not "
            "invent or assume. Keep exact identifiers verbatim (paths, "
            "names, numbers, commands). Be concise; stay under 2000 tokens. "
            "Plain text only — no JSON, no code fences except to quote a "
            "short critical snippet."
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
        """Apply dynamic system prompt sections from hook context.

        ``self._system_sections`` is the single source of truth;
        ``self.system`` is always derived by joining it — so the inspector
        view and the string the LLM receives cannot drift. Re-applying
        replaces the previous hook sections (idempotent across turns). The
        ``<!-- HOOK_SECTIONS -->`` marker keeps the joined string identical
        to the historical format (and signals "dynamic from here" to a
        reader); it rides on the first hook section's text.
        """
        if not hook_ctx or not hook_ctx.system_sections:
            return
        # Callers that set ``self.system`` directly (tests, embedders) without
        # going through _setup get a single seeded section so the
        # single-source invariant still holds.
        if not self._system_sections and self.system:
            self._system_sections = [("Base", self.system)]
        static = [s for s in self._system_sections if not s[0].startswith("Hook: ")]
        hook_sections = [
            (f"Hook: {title}", f"## {title}\n{content}")
            for title, content in hook_ctx.system_sections.items()
        ]
        first_name, first_text = hook_sections[0]
        hook_sections[0] = (
            first_name,
            f"<!-- HOOK_SECTIONS -->\n{first_text}",
        )
        self._system_sections = static + hook_sections
        self.system = "\n\n".join(t for _, t in self._system_sections)

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
                # Stamp the turn ABOUT to run so history records (any injected
                # messages + this turn's action/observation) carry it.
                if self.ctx:
                    self.ctx.set_turn(self.turn + 1)
                self._inject_queued_messages()
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
        """Handle graceful interrupt: record as an observation, return ToolResult.

        Recorded as a tool-style observation (``tool="interrupt"``) rather
        than a bare ``role=user`` message. Two reasons:
          - The transcript no longer shows two consecutive user turns
            (interrupt notice + the next real instruction); the notice
            renders as ``[interrupt] …`` via ``_convert_observation``.
          - ``recent_exchanges`` skips any ``role=user`` entry carrying a
            ``tool`` field, so the notice is excluded from resume previews
            without relying on the legacy prefix match.
        Shared by both surfaces: Ctrl+C (chat) and the web stop button
        both land here through ``stop_event`` → ``_should_continue``.
        """
        if self.ctx:
            self.ctx.add(
                {
                    "role": "user",
                    "tool": "interrupt",
                    "success": False,
                    "content": INTERRUPT_NOTICE,
                }
            )
        # Surface through the renderer (CLI console / web SSE), not a
        # direct console.print — the latter leaked the notice to the web
        # server's terminal. Mirror the run_skill path: only the
        # top level renders, so a nested skill interrupt doesn't double-
        # print under its parent.
        if not self.skill_name:
            render_step(
                "observation",
                INTERRUPT_NOTICE,
                self.turn,
                tool_name="interrupt",
                success=False,
            )
        _debug_log(f"Graceful interrupt at turn {self.turn}")
        return ToolResult(False, error="Interrupted by user")

    def _setup(self) -> None:
        """Initialize system prompt and messages."""
        _set_debug_verbose(self.verbose)

        # Build system prompt with session_dir for Context Recovery Guide.
        # Built as named sections — the joined string is what the LLM gets
        # (byte-identical to the old single-string build), and the section
        # list feeds the web Prompt Inspector via the renderer snapshot.
        session_dir = ""
        if self.ctx:
            session_dir = str(self.ctx.session_dir)
        self._system_sections = build_system_prompt_sections(
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
        self.system = "\n\n".join(t for _, t in self._system_sections)

        render_header(
            self.provider_name,
            self.model,
            self.max_turns,
            skill_name=self.skill_name,
            skill_args=self.skill_args,
        )

        # Message setup. The run-starting query is added through the SAME path
        # as turn-boundary injections (single labeling + task-log + ctx point).
        self.task_log = []
        if self.ctx is None:
            self.messages = []
        self._add_user_message(self.query, self.query_author)

    def _add_user_message(self, text: str, author: str | None = None) -> None:
        """Add a user message to the conversation + task log.

        Shared by the run-starter (``_setup``) and the plain-chat branch of
        turn-boundary injection so the ``[author]: text`` labeling, task-log
        accumulation, and ctx/messages update live in ONE place. ``author``
        (a nickname) is prefixed only when truthy — CLI / single-user stays raw.
        """
        labeled = f"[{author}]: {text}" if author else text
        self.task_log.append(labeled)
        # ``author`` rides along (when present) so the history enrich can
        # attribute the query + strip the label for the search surface. The
        # cache / LLM path ignores the extra key.
        record = {"role": "user", "content": labeled}
        if author:
            record["author"] = author
        if self.ctx:
            self.ctx.add(record)
            self.messages = self.ctx.get_messages()
        else:
            self.messages.append(record)

    def _should_continue(self) -> bool:
        if self.stop_event and self.stop_event.is_set():
            self._interrupted = True
            return False
        return self.max_turns <= 0 or self.turn < self.max_turns

    def _task_text(self) -> str:
        """All user requests this run (first query + injected), for recovery /
        review anchoring. Falls back to the raw query if the log is empty."""
        return "\n".join(self.task_log) if self.task_log else self.query

    def _inject_queued_messages(self) -> None:
        """Turn boundary (web): pull ONE queued user message and process it the
        SAME way a run-starter is processed. No-op when no callback (CLI) or the
        queue is empty.

        A command (``/sh``, ``/compact``, ``@agent``, ``/skill``) is routed via
        ``route_message`` exactly as at run-start — so it executes instead of
        leaking in as literal chat. Its effect lands in the shared ctx (e.g. a
        ``@agent`` delegate result, a ``/compact``), so we refresh ``messages``
        and record the ask in the task log. A plain chat message falls through
        to ``_add_user_message`` as a steering injection."""
        if self.dequeue_user_message is None:
            return
        item = self.dequeue_user_message()
        if not item:
            return
        text = item.get("text") or ""
        author = item.get("nickname")
        labeled = f"[{author}]: {text}" if author else text
        # Echo the dequeued message as a conversation card BEFORE routing —
        # mirrors the worker's run-starter echo so an injected command/chat
        # shows the same way it would at run-start (web renderer only;
        # push_user_message is a web affordance, CLI/minimal don't inject).
        from agent_cli.render import get_renderer

        renderer = get_renderer()
        if hasattr(renderer, "push_user_message"):
            renderer.push_user_message(labeled)
        if self.route_message is not None and self.route_message(text):
            # Routed as a command — record the ask; ctx may have changed.
            self.task_log.append(labeled)
            if self.ctx:
                self.messages = self.ctx.get_messages()
            return
        self._add_user_message(text, author)

    def _interrupt_check(self) -> bool:
        """Zero-arg predicate the provider polls per chunk to break a
        mid-generation stream (Ctrl+C / web stop). Both surfaces signal
        through ``stop_event``; the streaming loop — which owns the HTTP
        response and runs on the reading thread — closes it itself."""
        return bool(self.stop_event and self.stop_event.is_set())

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

        # User interrupted mid-generation (Ctrl+C / web stop): the provider
        # broke the stream. The partial text was streamed to the UI but never
        # added to ctx, so discard it — route straight to the interrupt
        # handler (records the interrupt notice, not the partial) instead of
        # parsing/dispatching a half-written action. A stream that COMPLETED
        # before the interrupt was noticed has no "interrupted" stop_reason and
        # flows normally; the turn-boundary check then stops the next turn
        # (graceful "finish current step" for already-done generation).
        if getattr(response, "stop_reason", None) == "interrupted":
            return self._on_interrupt()

        llm_text = response.content

        # Show token stats if available (providers report eval durations)
        if response.usage:
            self._total_output_tokens += response.usage.output_tokens or 0
            stats = _build_token_stats(
                response.usage,
                self.capabilities.context_window,
                self._total_output_tokens,
            )
            render_token_usage(stats, self.turn, self.verbose)

        # PostLLMCall hook
        self._fire_hook("PostLLMCall", llm_response=llm_text)

        render_raw(llm_text, self.turn, self.verbose)
        if self.verbose and response.thinking:
            render_thinking(response.thinking, self.turn)

        # Output-truncation guard: when the response hit the model's
        # output-token limit (``stop_reason == "length"``), its action is
        # incomplete — a half-written file (write_file), a truncated
        # command (shell), or a clipped answer (complete). Do NOT dispatch
        # it; record a notice so the model retries with a smaller unit.
        # (continuation — resuming the cut-off output — is a follow-up.)
        if getattr(response, "stop_reason", None) == "length":
            result = self._on_output_truncated(llm_text)
            self._fire_hook("OnTurnEnd")
            return result

        result = self._handle_text_path(llm_text)

        # OnTurnEnd hook
        self._fire_hook("OnTurnEnd")

        return result

    def _on_output_truncated(self, llm_text: str):
        """Handle a response cut off at ``max_output_tokens``.

        The (incomplete) assistant text is still recorded so the model
        sees what it was mid-way through, paired with an observation that
        says the action was NOT executed and to retry smaller. Returns
        ``_CONTINUE`` so the loop gives the model another turn.
        """
        _append_observation(
            self.messages,
            self.ctx,
            self.wire_format,
            llm_text,
            f"Observation: {OUTPUT_TRUNCATED_NOTICE}",
            tool_name="output_truncated",
            success=False,
            turn=self.turn,
            render=not self.skill_name,
        )
        _debug_log(
            f"Output truncated (stop_reason=length) at turn {self.turn}; "
            "action not dispatched"
        )
        return self._CONTINUE

    def _call_llm(self):
        """LLM call with overflow retry and streaming. Returns response or sentinel."""
        # flow 1 — preventive compaction before the call. The threshold
        # uses the live system-prompt size and the model's window, so it
        # reflects real headroom (not a fixed budget). ``sys_tokens`` is
        # reused below to reconcile the cache against the server's actual
        # input count once the call succeeds.
        sys_tokens = estimate_tokens(self.system) if self.ctx else 0
        if self.ctx:
            target = max(
                int(
                    (
                        self.capabilities.context_window
                        - sys_tokens
                        - self.capabilities.max_output_tokens
                    )
                    * _COMPACTION_TARGET_RATIO
                ),
                1,
            )
            self.ctx.ensure_within(target)
            self.messages = self.ctx.get_messages()

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
        # Prompt Inspector snapshot: what THIS call's system prompt looks
        # like, as named sections. No-op for CLI renderers (store-only on
        # web), so per-turn cost is negligible.
        render_system_prompt_snapshot(
            build_inspector_sections(self._system_sections, self.ctx), self.turn
        )

        # Plugin-defined provider hints. The wire plugin decides them from
        # the model's capabilities — e.g. ``json_mode``: ReAct requests it
        # iff the model supports structured output, md_array never does
        # (markdown shape). This is the single wire ⨯ capability decision
        # point, so the provider never combines the two itself.
        extra_call_kwargs = self.wire_format.provider_call_kwargs(self.capabilities)

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
                degeneration_check=self.wire_format.is_degenerate,
                interrupt_check=self._interrupt_check,
                **extra_call_kwargs,
            )
            # Stitch the prefill back onto the response so downstream
            # parsers see a complete emission. Use dataclasses.replace
            # to keep usage / thinking / stop_reason intact.
            if prefill:
                from dataclasses import replace as _replace

                response = _replace(response, content=prefill + response.content)
            # flow 1 (part B) — re-anchor the cache to the server's actual
            # input count so the chars/4 estimate can't compound across
            # turns. usage covers system+messages; ctx subtracts the same
            # sys_tokens used for the threshold above. No-op without usage.
            if self.ctx and response.usage:
                self.ctx.reconcile_actual_tokens(
                    response.usage.total_input_tokens, system_tokens=sys_tokens
                )
            # A successful call means we're no longer in overflow for this
            # turn — reset the counter so a later turn gets a fresh budget
            # of shrink-and-retry attempts.
            self.overflow_retries = 0
            return response
        except Exception as e:
            if (
                is_context_overflow(str(e))
                and self.ctx
                and self.overflow_retries < _MAX_OVERFLOW_RETRIES
            ):
                # Reactive recovery (flow 2): the server rejected the
                # prompt as too long. Trust its count over our local
                # estimate, shrink the cache toward the limit, and retry.
                actual, limit = parse_overflow_amounts(str(e))
                budget = self.ctx.max_context_tokens
                target = int((limit or budget) * _COMPACTION_TARGET_RATIO)
                if self.ctx.force_fit(target, actual_tokens=actual):
                    self.overflow_retries += 1
                    render_status(
                        "running",
                        f"Context overflow — shrinking and retrying "
                        f"({self.overflow_retries}/{_MAX_OVERFLOW_RETRIES})...",
                    )
                    self.messages = self.ctx.get_messages()
                    self.turn -= 1
                    return self._RETRY
                # force_fit could not shrink further (only the anchor
                # remains) — fall through to a clean failure.
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
        """Handle text parsing response (non-JSON fallback).

        Recovery primitives consume only the emitted text (``llm_text``)
        — the thinking channel is intentionally excluded from the
        recovery path (see ``docs/robust-harness/DESIGN.md`` §2.2).

        TurnRecord is emitted exactly once per call, regardless of which
        terminal branch is taken (success/retry/exception). Branches
        that fire an Intervention mutate ``outcome`` (failure_signal +
        primitives) before returning, and the trailing finally writes
        the record.
        """
        turn = self.wire_format.parse_turn(llm_text)

        # Recover dropped action names (parse_stage 3) — the dropped-action
        # recovery SEAM: an op's action slot is empty but its action_input
        # survived (parse-preservation invariant), so ``infer_action`` tries to
        # resolve the tool from the input shape. A successful inference is
        # flagged so the observation step rewrites the prior + history to the
        # corrected shape (no raw-drift mimicry) and the TurnRecorder logs it.
        #
        # As of consolidation Step 3 every builtin tool is flat-native, so the
        # current prefix-based resolver returns None for builtin payloads (this
        # hook + infer_action stay live for a FUTURE prefixed tool/format; a
        # flat action-less payload like ``{path}`` is ambiguous → NO_ACTION,
        # the documented extension point for a future schema-based resolver).
        # Ambiguous/none leaves it to the NO_ACTION recovery below.
        #
        # Gated on ``action_required``: when the wire format requires an
        # explicit action (action_required=True), a dropped action is a
        # drift to be corrected by the model, so we skip inference and fall
        # through to the NO_ACTION recovery below. When False (the namespaced
        # format — react), the action is recoverable from the
        # preserved action_input, so we infer it. Mirror of how
        # ``thought_required`` gates the NO_THOUGHT recovery.
        action_inferred = False
        if not self.wire_format.action_required:
            for op in turn.ops:
                if not op.action and isinstance(op.action_input, dict):
                    inferred = infer_action(op.action_input)
                    if inferred:
                        op.action = inferred
                        action_inferred = True

        # Classify outcome early; the dispatch body may mutate this
        # dict to reflect a B1 (action loop) detection that is only
        # known after we see the chosen action.
        # Degeneration is a GENERATION-level pathology — the stream ran away
        # repeating the wire shape instead of terminating — so it is logically
        # PRIOR to, and a more specific cause than, the parse-level symptom (a
        # runaway naturally fails to parse and would otherwise be mislabeled
        # NO_JSON). Checked FIRST. Recovery is driven by ``turn.parse_stage``
        # (in ``_recover_unparsed``), NOT by this label, so relabeling a
        # stage-0 runaway DEGENERATE changes only the telemetry signal
        # (turns.jsonl) — the recovery path is unchanged. Empty output is not a
        # runaway (``is_degenerate("")`` is False) so it still falls through to
        # NO_OUTPUT below.
        if self.wire_format.is_degenerate(llm_text):
            initial_signal = FAILURE_DEGENERATE
        elif turn.parse_stage == 0:
            # Split A1 into two sub-modes — empty/whitespace-only output
            # vs non-empty content that drifted from JSON. The recovery
            # path is identical (RETRY_HINT_NO_JSON fallback in both),
            # but the labels separate two operationally different
            # failure shapes for analysis (DESIGN.md §1, A1a vs A1b).
            if not (llm_text or "").strip():
                initial_signal = FAILURE_NO_OUTPUT
            else:
                initial_signal = FAILURE_NO_JSON
        elif not any(op.action for op in turn.ops):
            initial_signal = FAILURE_NO_ACTION
        else:
            initial_signal = None
        outcome: dict = {
            "failure_signal": initial_signal,
            "primitives": ["action_inferred"] if action_inferred else [],
            "action_inferred": action_inferred,
        }

        try:
            return self._dispatch_turn(llm_text, turn, outcome)
        finally:
            self.recorder.record(
                model=self.model,
                parse_stage=turn.parse_stage,
                failure_signal=outcome["failure_signal"],
                primitives_applied=outcome["primitives"],
                raw=llm_text,
            )

    def _dispatch_turn(self, llm_text: str, turn, outcome: dict):
        """Turn-level dispatch: guards, then the ops in array order.

        ``turn`` is a ``ParsedTurn``. Single-action formats produce 0 or 1
        ops (the default ``parse_turn`` wrapper), so for them this reproduces
        the pre-multi-op behaviour exactly. ``outcome`` is a mutable dict
        owned by the caller (``_handle_text_path``); branches that fire an
        Intervention update it before returning so the trailing finally
        records what happened.
        """
        first_action = next((op.action for op in turn.ops if op.action), None)

        # A7 NO_THOUGHT — action present but thought missing. Retry
        # before dispatch so the omission does not enter the transcript
        # as a precedent for future turns (mimicry-strengthening loop:
        # the raw response is mirrored back on the next turn and
        # crowds out the system prompt's Format Rule 1).
        if self.wire_format.thought_required and detect_thought_missing(
            turn.thought, first_action
        ):
            # ``thought_required`` is False on plugins where the thought
            # is preceding free text rather than a schema field — for
            # those, missing thought is not a drift signal.
            _debug_log(f"NO_THOUGHT: action={first_action!r}, thought={turn.thought!r}")
            # ReAct-only: format_no_thought_retry lives on the plugin,
            # not in recovery/builders, because it has no meaning when
            # ``thought_required`` is False (envelope plugins).
            intervention = self.wire_format.format_no_thought_retry(
                prior_content=llm_text
            )
            render_recovery(llm_text, intervention.message, "no thought", self.turn)
            _append_observation(
                self.messages,
                self.ctx,
                self.wire_format,
                llm_text,
                intervention.message,
                tool_name="",
                success=False,
                turn=self.turn,
                render=False,  # render_recovery already surfaced it
            )
            outcome["failure_signal"] = FAILURE_NO_THOUGHT
            outcome["primitives"] = list(intervention.primitives)
            self.turn -= 1
            return self._CONTINUE

        # 6. Thought
        if turn.thought:
            render_step("thought", turn.thought, self.turn)

        # No usable ops at all (parse failure / no action recovered, including
        # a thought-only turn) — straight to recovery. md_array completes via
        # an explicit `complete` op, so a thought-only emission is a NO_ACTION
        # nudge, not a silent completion.
        if not turn.ops:
            return self._recover_unparsed(llm_text, turn, outcome)

        # Dispatch ops in array order (sequential — observations append in
        # order). Single-action formats have exactly ONE op and take the
        # legacy path (accumulate=None → _dispatch_op appends its own
        # observation and returns, byte-identical to pre-multi-op).
        #
        # N ops (multi-op formats): regular tool ops execute and ACCUMULATE
        # into one combined observation (run-all; any-fail ⇒ the combined
        # observation is marked failed so the model retries the failed op).
        # A turn-ending branch (complete / run_skill / guard
        # intervention / recovery) flushes whatever already ran first so
        # executed work isn't lost, then returns. ``ask`` is NOT turn-ending —
        # it produces an observation (the user's reply) and accumulates like a
        # normal tool, so several ``ask`` ops batch (each prompts in turn) just
        # like a read/shell batch.
        if len(turn.ops) == 1:
            return self._dispatch_op(llm_text, turn, turn.ops[0], outcome)

        results: list[dict] = []
        ops = turn.ops
        i = 0
        while i < len(ops):
            op = ops[i]
            # Turn-ending special actions: flush accumulated results BEFORE
            # the branch runs so its observation lands after the work done
            # so far (chronological order for the model).
            if op.action in ("complete", "run_skill"):
                self._flush_op_results(llm_text, results)
                results = []
                return self._dispatch_op(llm_text, turn, op, outcome)
            # Parallel batch: a run of ≥2 consecutive ops of the SAME
            # parallel_safe tool dispatches concurrently into one combined
            # observation (delegate: independent subagents). A lone
            # parallel_safe op falls through to the normal per-op path so it
            # keeps its B1/A4/A5 guards. Mutating tools (parallel_safe=False)
            # always take the sequential per-op path — order is their
            # correctness guarantee (write→edit same file, mkdir→touch).
            tool = TOOLS.get(op.action) if op.action else None
            # Same-file edit batch: a run of ≥2 consecutive edit_file ops on the
            # SAME path is applied together against ONE original read (all refs
            # resolved before any write, bottom-up, all-or-nothing) so a later
            # op's hashline ref doesn't go stale from an earlier op's line shift.
            # A lone edit_file, or edits on different paths, take the normal
            # per-op path. Only consecutive same-path edits group — interleaving
            # another tool (e.g. write→edit) breaks the run, preserving order.
            if op.action == "edit_file" and isinstance(op.action_input, dict):
                path = op.action_input.get("path")
                j = i
                while (
                    j < len(ops)
                    and ops[j].action == "edit_file"
                    and isinstance(ops[j].action_input, dict)
                    and ops[j].action_input.get("path") == path
                ):
                    j += 1
                if j - i > 1:
                    self._dispatch_edit_batch(
                        llm_text, turn, ops[i:j], outcome, accumulate=results
                    )
                    i = j
                    continue
            if tool is not None and tool.parallel_safe:
                j = i
                while j < len(ops) and ops[j].action == op.action:
                    j += 1
                if j - i > 1:
                    self._dispatch_parallel_batch(
                        llm_text, turn, ops[i:j], outcome, accumulate=results
                    )
                    i = j
                    continue
            r = self._dispatch_op(llm_text, turn, op, outcome, accumulate=results)
            if r is not None:
                # Guard/recovery fired inside the op (B1/A4/A5/no-action):
                # its intervention observation is already appended; flush the
                # accumulated work after it (rare mid-array edge — order is
                # intervention-first, results still preserved).
                self._flush_op_results(llm_text, results)
                return r
            i += 1
        self._flush_op_results(llm_text, results)
        return self._CONTINUE

    def _flush_op_results(self, llm_text: str, results: list[dict]) -> None:
        """Append ONE combined observation for accumulated op results.

        Per-op header lines (``[i/N] tool — OK/FAILED``) frame each op's
        output; turn success = all ops succeeded (any-fail ⇒ failed so the
        model retries the failed op next turn). No-op when nothing ran.
        """
        if not results:
            return
        n = len(results)
        parts = []
        for i, r in enumerate(results, start=1):
            status = "OK" if r["success"] else "FAILED"
            parts.append(f"[{i}/{n}] {r['tool_name']} — {status}\n{r['observation']}")
        combined = "\n\n".join(parts)
        all_ok = all(r["success"] for r in results)
        _append_observation(
            self.messages,
            self.ctx,
            self.wire_format,
            llm_text,
            f"Observation: {combined}",
            tool_name=_combined_tool_label([r["tool_name"] for r in results]),
            success=all_ok,
            turn=self.turn,
        )

    def _dispatch_parallel_batch(
        self, llm_text, turn, batch_ops, outcome, *, accumulate
    ):
        """Dispatch a run of ≥2 consecutive parallel_safe ops concurrently,
        appending ONE combined result to *accumulate*.

        Only ``delegate`` is wired today (the sole ``parallel_safe`` tool): each
        op's flat input becomes one task spec → ``tool_delegate({tasks:[...]})``
        → ``_run_parallel`` (real threading). This is what makes the prompt's
        "several delegate ops in one turn run in parallel" actually true (the
        N-op loop is otherwise sequential).

        A future read-only ``parallel_safe`` tool with no internal concurrent
        engine would fan its ops over a thread-pool of per-op ``run()`` calls
        in the extension slot below — not wired (no other tool opts in).
        """
        tool_name = batch_ops[0].action
        # Render one action card per op (the model's flat emission, pre-wrap),
        # matching the single-op render so the UI shows every delegate op.
        for op in batch_ops:
            disp = op.action_input if op.action_input is not None else {}
            render_step(
                "action",
                "",
                self.turn,
                tool_name=tool_name,
                tool_input=json.dumps(disp, ensure_ascii=False)
                if isinstance(disp, dict)
                else str(disp),
            )

        if tool_name != "delegate":
            # Extension slot: a parallel_safe tool with no internal concurrent
            # engine (e.g. a future read-only read_file/code_index opt-in) would
            # fan its ops out over a thread-pool of per-op run() calls here.
            raise NotImplementedError(
                f"parallel_safe batch dispatch not wired for {tool_name!r}; only "
                "delegate has an internal concurrent engine (_run_parallel)."
            )

        # delegate: assemble each flat op into one task spec and run the batch
        # through the existing parallel engine (one combined observation).
        specs = [
            TOOLS["delegate"].strip_prefix(op.action_input or {}) for op in batch_ops
        ]
        result = self._dispatch_tool_with_hooks("delegate", {"tasks": specs})
        observation = self._tool_observation("delegate", result, {"tasks": specs})
        # Rendered from storage by _flush_op_results' _append_observation
        # (combined card), matching ctx + resume — no separate pre-render.
        accumulate.append(
            {
                "tool_name": tool_name,
                "observation": observation,
                "success": result.success,
            }
        )

    def _dispatch_edit_batch(self, llm_text, turn, batch_ops, outcome, *, accumulate):
        """Apply a run of ≥2 consecutive same-path edit_file ops as ONE batch,
        appending ONE combined result to *accumulate*.

        Calls the pure ``apply_edits_batch`` (resolve all refs against one
        original read → reject overlaps → bottom-up apply → one write,
        all-or-nothing). Single-direction call: the loop hands it ``(path,
        edits)`` and gets a ToolResult back — edit_file knows nothing of the
        loop. Each op's flat input also gets rendered as its own action card,
        matching the single-op render.
        """
        from agent_cli.tools.edit_file import apply_edits_batch

        for op in batch_ops:
            disp = op.action_input if isinstance(op.action_input, dict) else {}
            render_step(
                "action",
                "",
                self.turn,
                tool_name="edit_file",
                tool_input=json.dumps(disp, ensure_ascii=False),
            )

        path = batch_ops[0].action_input.get("path")
        edits = [op.action_input for op in batch_ops]
        result = apply_edits_batch(path, edits)
        observation = self._tool_observation(
            "edit_file", result, batch_ops[0].action_input
        )
        accumulate.append(
            {
                "tool_name": "edit_file",
                "observation": observation,
                "success": result.success,
            }
        )

    def _dispatch_op(self, llm_text: str, turn, op, outcome: dict, accumulate=None):
        """Dispatch ONE op of a turn. Returns a ToolResult or a sentinel.

        Carries the pre-multi-op per-action body unchanged: special actions
        (complete / ask / run_skill), then B1/A4/A5 guards
        and tool execution, then the no-action fall-through recovery.

        ``accumulate`` (multi-op N-op path only): a list to collect this op's
        execution record into instead of appending its own observation —
        the caller combines all records into one observation. Returns
        ``None`` in that case ("executed, keep going"); every other branch
        returns a ToolResult/sentinel as before.
        """
        # 7. Complete tool (text parsing path)
        _debug_log(f"PARSED iter={self.turn} action={op.action}")
        if op.action == "complete":
            if isinstance(op.action_input, dict):
                raw = op.action_input.get("result")
                answer = (
                    str(raw)
                    if raw
                    else "(Completed without result — model may lack capability for this task)"
                )
            elif isinstance(op.action_input, str):
                raw = op.action_input
                answer = (
                    op.action_input
                    or "(Completed without result — model may lack capability for this task)"
                )
            else:
                raw = None
                answer = (
                    str(op.action_input)
                    if op.action_input
                    else "(Completed without result — model may lack capability for this task)"
                )

            # A6 (Nested envelope) — detection records the signal for
            # observability AND we unwrap one level so the user-facing
            # answer doesn't carry a literal ``{"result": "..."}`` prefix.
            # Single-level only (recursive nesting indicates a different
            # bug worth surfacing). ``raw`` may be from ``op.action_
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
                    self.wire_format.serialize_terminal_for_history(
                        turn.thought or "", answer
                    )
                )
            render_step("final", answer, self.turn)

            return ToolResult(True, output=answer)

        # 9. Detect echo-as-final-answer (common small model pattern)
        echo_answer = _try_echo_as_final(op.action, op.action_input)
        if echo_answer:
            if self.ctx:
                self.ctx.add(
                    self.wire_format.serialize_terminal_for_history(
                        turn.thought or "", echo_answer
                    )
                )
            render_step("final", echo_answer, self.turn)

            return ToolResult(True, output=echo_answer)

        # 10. Ask tool -- prompt user for input (text parsing path)
        if op.action == "ask":
            questions = _extract_questions(op.action_input)
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
                    tool_input=json.dumps(op.action_input, ensure_ascii=False)
                    if isinstance(op.action_input, dict)
                    else str(op.action_input),
                )
                user_response = _handle_ask(questions)
                # ``ask`` is a normal observation-producing op (the user's
                # reply is the observation), not a terminal. In a multi-op turn
                # it accumulates like read/shell so consecutive asks batch into
                # the one combined observation; alone it appends its own.
                if accumulate is not None:
                    accumulate.append(
                        {
                            "tool_name": "ask",
                            "observation": f"User responded:\n{user_response}",
                            "success": True,
                        }
                    )
                    return None
                obs_msg = f"Observation: User responded:\n{user_response}"
                _append_observation(
                    self.messages,
                    self.ctx,
                    self.wire_format,
                    llm_text,
                    obs_msg,
                    tool_name="ask",
                    success=True,
                    turn=self.turn,
                    render=False,  # the answer is surfaced by the input UI
                )
                return self._CONTINUE

        # 10b. run_skill -- intercept at loop level (text parsing path)
        if op.action == "run_skill":
            skill_input = op.action_input if isinstance(op.action_input, dict) else {}
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
                compaction_enabled=self.compaction_enabled,
            )
            obs = skill_tool_result.output or skill_tool_result.error
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
                turn=self.turn,
            )
            return self._CONTINUE

        # 11. Tool execution (text parsing path)
        if op.action:
            tool_name = op.action
            tool_input = op.action_input or {}

            # Multi-op formats emit flat single-target ops (one file / edit /
            # query / task per op); the tool re-wraps that into its canonical
            # prefixed input so the validate → strip → run pipeline below is
            # unchanged. Single-action formats bypass this (their input is
            # already canonical).
            if (
                getattr(self.wire_format, "multi_op", False)
                and tool_name in TOOLS
                and isinstance(tool_input, dict)
            ):
                tool_input = TOOLS[tool_name].wrap_single_op(tool_input)

            # Truncation guard: if JSON was repaired (truncated response),
            # strip the last element from edit_file's lines arrays
            truncation_warning = ""
            if op.truncated and tool_name == "edit_file":
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
                    task=self._task_text(),
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
                render_recovery(
                    llm_text,
                    intervention.message,
                    f"action loop ({tool_name}, level {loop_level})",
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
                    turn=self.turn,
                    render=False,  # render_recovery already surfaced it
                )
                outcome["primitives"] = list(intervention.primitives)
                self.turn -= 1  # Don't count loop nudges as user-facing turns
                return self._CONTINUE

            # Render the model's ACTUAL emission, not the dispatch-canonical
            # form. `wrap_single_op` above re-wrapped the flat op into the
            # tool's prefixed/batch shape (e.g. read_file `{path}` →
            # `{read_file_reads:[...]}`) so the validate→strip→run pipeline is
            # unchanged — but showing THAT misrepresents what the model wrote
            # and diverges from history.jsonl / resume-replay (which store the
            # raw op). The dispatch keeps `tool_input` (wrapped); only the card
            # shows `op.action_input` (pre-wrap).
            display_input = op.action_input if op.action_input is not None else {}
            render_step(
                "action",
                "",
                self.turn,
                tool_name=tool_name,
                tool_input=json.dumps(display_input, ensure_ascii=False)
                if isinstance(display_input, dict)
                else str(display_input),
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
                render_recovery(
                    llm_text, f"Observation: {err_msg}", "unknown tool", self.turn
                )
                _append_observation(
                    self.messages,
                    self.ctx,
                    self.wire_format,
                    llm_text,
                    f"Observation: {err_msg}",
                    tool_name=tool_name,
                    success=False,
                    turn=self.turn,
                    render=False,  # render_recovery already surfaced it
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
                render_recovery(
                    llm_text, f"Observation: {err_msg}", "schema mismatch", self.turn
                )
                _append_observation(
                    self.messages,
                    self.ctx,
                    self.wire_format,
                    llm_text,
                    f"Observation: {err_msg}",
                    tool_name=tool_name,
                    success=False,
                    turn=self.turn,
                    render=False,  # render_recovery already surfaced it
                )
                return self._CONTINUE
            tool_input = normalized  # use post-normalization input for dispatch

            # Execute tool (method tracks self.recent_tool_history,
            # uses self.* for provider/ctx/hooks/etc.)
            tool_result = self._dispatch_tool_with_hooks(tool_name, tool_input)

            observation = self._tool_observation(tool_name, tool_result, tool_input)
            if truncation_warning:
                observation = f"{observation}\n{truncation_warning}"

            # N-op accumulate mode: record the execution for the caller's
            # combined observation instead of appending one here. The render
            # happens once, from storage, in _append_observation (single-op
            # below, or _flush_op_results for the combined) — so the live card
            # matches ctx + resume. Oversized bodies are already nudge-capped
            # by _tool_observation above.
            if accumulate is not None:
                accumulate.append(
                    {
                        "tool_name": tool_name,
                        "observation": observation,
                        "success": tool_result.success,
                    }
                )
                return None

            # Inject observation with structured artifact. On an
            # action-name correction, rewrite the assistant prior + history
            # to the corrected wire shape so neither the next turn nor a
            # resume re-feeds the raw drift (mimicry-strengthening).
            obs_msg = f"Observation: {observation}"
            corrected = None
            if outcome.get("action_inferred"):
                corrected = {
                    "role": "assistant",
                    "thought": turn.thought or "",
                    "action": op.action,
                    "action_input": op.action_input,
                }
            _append_observation(
                self.messages,
                self.ctx,
                self.wire_format,
                llm_text,
                obs_msg,
                tool_name=tool_name,
                success=tool_result.success,
                artifact=tool_result.artifact,
                corrected_record=corrected,
                turn=self.turn,
            )
            return self._CONTINUE

        # No usable action on this op — fall through to recovery.
        return self._recover_unparsed(llm_text, turn, outcome)

    def _recover_unparsed(self, llm_text: str, turn, outcome: dict):
        """Missing action or parse failure — retry with the appropriate hint.

        Echoes the model's failed output back as failure grounding (content
        shows structural drift: YAML-style keys, function-call syntax,
        bare prose). Thinking-channel echo is excluded from v1 — see
        docs/robust-harness/DESIGN.md §2.2.
        """
        if turn.parse_stage > 0:
            # Parsed OK but no action -- LLM forgot to include the action
            _debug_log(
                f"No action in parsed JSON (stage={turn.parse_stage}):\n{llm_text}"
            )
            intervention = format_no_action_retry(
                prior_content=llm_text, wire_format=self.wire_format
            )
            recovery_reason = "no action"
        else:
            # JSON parse failed entirely
            _debug_log(f"JSON parse failed (stage={turn.parse_stage}):\n{llm_text}")
            syntax_error = self.wire_format.diagnose_syntax_error(llm_text)
            intervention = format_no_json_retry(
                prior_content=llm_text,
                wire_format=self.wire_format,
                syntax_error=syntax_error,
            )
            recovery_reason = "invalid JSON"
        render_recovery(llm_text, intervention.message, recovery_reason, self.turn)
        _append_observation(
            self.messages,
            self.ctx,
            self.wire_format,
            llm_text,
            intervention.message,
            tool_name="",
            success=False,
            turn=self.turn,
            render=False,  # render_recovery already surfaced it
        )
        # Surface composed primitive names to the enclosing _handle_text_path
        # so the trailing finally-block records them.
        outcome["primitives"] = list(intervention.primitives)
        self.turn -= 1  # Don't count format retries
        return self._CONTINUE

    def _dispatch_tool_with_hooks(self, tool_name: str, tool_input):
        """Orchestrator: pre-hooks → invoke → guards → post-hooks → record.

        Preconditions (enforced by ``_dispatch_op``):
            - ``tool_name`` is a valid name in ``self.tools_list`` (A4
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
            compaction_enabled=self.compaction_enabled,
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

        Recovery layer (A4/A5 detectors in ``_dispatch_op``) has
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

    # ── result → observation seam (per-tool render + oversized cap) ──
    def _tool_observation(self, tool_name: str, result: ToolResult, args) -> str:
        """Turn a tool's ToolResult into the observation body that enters
        context. Two per-tool surfaces meet here: ``render_observation``
        (how this tool formats its result — default output/error) and
        ``apply_oversized_cap`` (whether the cap applies — default True).
        An over-cap body is replaced with a narrow-it nudge. Tools not in the
        registry (none today) fall back to the default render + cap on."""
        tool = TOOLS.get(tool_name)
        if tool is not None:
            body = tool.render_observation(
                result, args if isinstance(args, dict) else {}
            )
            cap_on = tool.apply_oversized_cap
        else:
            body = result.output if result.success else result.error
            cap_on = True
        if cap_on and self._oversized_cap:
            tokens = estimate_tokens(body)
            if tokens > self._oversized_cap:
                return _render_oversized_nudge(tool_name, tokens, self._oversized_cap)
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
    provider_name: str = "openai",
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
    dequeue_user_message=None,
    route_message=None,
    query_author: str | None = None,
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
    default wire format so existing callers don't need to change.
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
        dequeue_user_message=dequeue_user_message,
        route_message=route_message,
        query_author=query_author,
        stop_event=stop_event,
        agent_role=agent_role,
        agent_name=agent_name,
        mcp_manager=mcp_manager,
        hook_runner=hook_runner,
        record_turns=record_turns,
        wire_format=wire_format,
        compaction_enabled=compaction_enabled,
    ).run()


def _build_token_stats(usage, context_window: int, total_out: int) -> dict:
    """Build the render-agnostic token-usage payload for one turn.

    Pure data — the renderer (CLI line / web top-bar) decides how to
    show it. ``"in"`` is ``usage.total_input_tokens`` (non-cached input +
    cache writes + reads) = the whole prompt's context occupancy, so the
    ctx% readout is correct even on an Anthropic prompt-cache hit (where
    bare ``input_tokens`` would exclude the cached portion and under-report).
    ``"in_speed"`` deliberately uses bare ``input_tokens`` — prefill only
    processes the non-cached tokens, so that's the true prefill tok/s.
    ``cache_read``/``cache_write`` are surfaced separately as a breakdown.
    Speeds/ttft are included when the provider reported durations (omlx and
    other OpenAI-compatible servers do; the Anthropic streaming path leaves
    them 0 → omitted by the renderer).
    """
    return {
        "in": usage.total_input_tokens,
        "out": usage.output_tokens,
        "in_speed": (
            usage.input_tokens / (usage.prompt_eval_ns / 1e9)
            if usage.prompt_eval_ns > 0
            else 0
        ),
        "out_speed": (
            usage.output_tokens / (usage.eval_ns / 1e9) if usage.eval_ns > 0 else 0
        ),
        "ttft_ms": usage.ttft_ns / 1e6 if usage.ttft_ns > 0 else 0,
        "cache_read": usage.cache_read_input_tokens,
        "cache_write": usage.cache_creation_input_tokens,
        "context_window": context_window,
        "total_out": total_out,
    }


def build_inspector_sections(system_sections, ctx):
    """Prompt Inspector sections = system-prompt sections + compaction-
    injected context (running summary + touched-file list).

    The summary and file list are injected as ``role=user`` messages right
    after the system prompt (``ContextManager.get_messages``), so they are
    NOT part of ``self.system`` — but they DO consume the context window and
    shape the turn, so the inspector surfaces them as clearly-labelled extra
    sections. Returns a NEW list; never mutates ``system_sections`` (which
    is the single source ``self.system`` derives from).
    """
    sections = list(system_sections)
    if ctx is None:
        return sections
    summary = getattr(ctx, "summary", "")
    if summary:
        sections.append(("⊙ Compaction summary (user-injected)", summary))
    file_list = getattr(ctx, "file_list", None) or []
    if file_list:
        listing = "\n".join(f"- {p}" for p in file_list)
        sections.append(("⊙ Files touched (user-injected)", listing))
    return sections


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
    if not renderer.can_prompt():
        # No interactive channel right now (non-TTY CLI / no connected web
        # client) → don't block forever on an answer that can't arrive;
        # give the model a stable answer slot. ``prompt_user`` itself
        # serializes + pauses any Live panel, so a delegate worker thread
        # is fine when a channel IS available.
        answer = "(no response)"
    else:
        try:
            answer = renderer.prompt_user(
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
    compaction_enabled: bool = True,
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
            compaction_enabled=compaction_enabled,
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


# Virtual/terminal tools excluded from the "your tool calls" review listing —
# they aren't real work, just loop control. (The review-context builders below
# are retained for the auto-review feature; see _build_review_observation.)
_REVIEW_VIRTUAL_TOOLS: frozenset[str] = frozenset({"complete", "ask"})


def _short_review_args(args, max_len: int = 80) -> str:
    """Render a tool's action_input as a compact one-liner for review injection.

    Long strings are head-truncated to 40 chars; non-scalar values
    (list / dict) collapse to ``<type>`` markers so the line stays
    short. The combined render is then capped at ``max_len``. The goal
    is "model can recognize what was called and on what target" — not
    a faithful replay.
    """
    if not isinstance(args, dict):
        s = repr(args)
        return s if len(s) <= max_len else s[: max_len - 3] + "..."
    pairs = []
    for k, v in args.items():
        if isinstance(v, str):
            v_show = v if len(v) <= 40 else v[:37] + "..."
            pairs.append(f"{k}={v_show!r}")
        elif isinstance(v, (int, float, bool)) or v is None:
            pairs.append(f"{k}={v!r}")
        else:
            pairs.append(f"{k}=<{type(v).__name__}>")
    line = ", ".join(pairs)
    if len(line) > max_len:
        line = line[: max_len - 3] + "..."
    return line


def _format_tool_calls_for_review(ctx, max_calls: int = 30) -> str:
    """Build the ``--- YOUR TOOL CALLS ---`` section for review injection.

    Returns "" (no section emitted) when ctx is None, has no
    assistant tool calls, or only virtual tools were used. Virtual
    tools (``complete`` / ``ask``) are filtered — they don't
    represent work the model has done.

    The section gives the model a *factual* list of what it actually
    invoked, independent of whether the corresponding Observations
    have been evicted by context FIFO. The model can then dispute or
    confirm its summary against this list before calling ``complete``.

    When the count exceeds ``max_calls``, the most recent
    ``max_calls`` entries are kept (most relevant to "is the work
    done?") and a header note records the omission.
    """
    if ctx is None:
        return ""
    try:
        raw = ctx.get_raw_messages()
    except Exception:
        return ""

    calls = []
    for msg in raw:
        if msg.get("role") != "assistant":
            continue
        action = msg.get("action")
        if not action or action in _REVIEW_VIRTUAL_TOOLS:
            continue
        args = msg.get("action_input") or {}
        calls.append(f"- {action}({_short_review_args(args)})")

    if not calls:
        return ""

    total = len(calls)
    if total > max_calls:
        calls = calls[-max_calls:]
        header = f"--- YOUR TOOL CALLS (last {max_calls} of {total}) ---"
    else:
        header = "--- YOUR TOOL CALLS ---"

    return "\n".join([header, *calls])


def _build_review_observation(query: str, summary: str, ctx=None) -> str:
    """Build a review-context block: original request + summary + tool calls.

    Retained for the auto-review feature (PR2) — assembles the material a
    reviewer needs. (Formerly the observation returned by the removed
    ``ready_for_review`` tool.)

    The block re-injects the original request (often pushed out of
    recency by long transcripts), pairs it with the model's self-summary,
    optionally appends a factual list of tool calls extracted from
    ``ctx`` (so the review survives Observation eviction by context
    FIFO), and asks the model to write out a per-requirement check
    against its previous Observations. The structured "Format your
    review like this" block forces the self-review to be *generated*
    rather than asserted in one line — small models that follow output
    templates also tend to follow the reasoning the template implies.
    """
    parts = [
        "--- ORIGINAL REQUEST ---",
        query,
        "--- YOUR SUMMARY ---",
        summary,
    ]
    tool_calls_block = _format_tool_calls_for_review(ctx)
    if tool_calls_block:
        parts.extend(["", tool_calls_block])
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
    """Strip the last (likely incomplete) line from a truncated edit_file op.

    edit_file is flat-native (consolidation Step 3) — one op = one edit. When
    the response was cut off mid-emission the final ``lines`` element is
    probably a partial line, so drop it and warn the model to re-read and
    finish the edit. Returns (sanitized_input, warning_message).
    """
    lines = tool_input.get("lines")
    if not lines:
        return tool_input, ""

    sanitized = {**tool_input, "lines": lines[:-1]}
    warning = (
        "[warn] Response was truncated — the last (incomplete) line of this "
        "edit was dropped. Re-read the file to verify and complete the edit."
    )
    return sanitized, warning


def _normalize_input(tool_input) -> str:
    """Normalize tool input to a comparable string."""
    if isinstance(tool_input, dict):
        return json.dumps(tool_input, sort_keys=True, ensure_ascii=False)
    return str(tool_input)


def _combined_tool_label(names: list[str]) -> str:
    """Run-length-compress a multi-op turn's tool names for the combined
    observation's label: ``["shell"] + ["write_file"]*12`` → ``shell+write_file×12``
    instead of a 137-char ``shell+write_file+write_file+...`` that overflows the
    line. Consecutive same-tool ops collapse to ``tool×N``; order is preserved
    (non-adjacent repeats stay separate runs)."""
    out: list[str] = []
    i = 0
    while i < len(names):
        j = i
        while j < len(names) and names[j] == names[i]:
            j += 1
        out.append(names[i] if j - i == 1 else f"{names[i]}×{j - i}")
        i = j
    return "+".join(out)


def _render_oversized_nudge(tool_name: str, tokens: int, cap: int) -> str:
    """The observation body substituted for an over-cap tool output. The full
    output is NOT added to context — oversized tool output crowds out reasoning
    and lowers quality — so we steer the model to re-request a narrower slice.
    Generic for now (tunable per-tool later via ``Tool.render_observation``)."""
    return (
        f"[{tool_name or 'tool'}: output too large — ~{tokens:,} tokens "
        f"> cap {cap:,} (context_window/10). NOT added to context; the call "
        f"itself succeeded. Large outputs crowd out reasoning and lower quality. "
        f"Re-request a narrower slice: read a specific line range or symbols, add "
        f"a LIMIT / tighter filter, or pipe through `head`/`grep`. To keep a full "
        f"large result, write it to a file (e.g. `… | tee /tmp/out.txt`) then read "
        f"specific parts with read_file.]"
    )


def _append_observation(
    messages: list[dict],
    ctx,
    wire_format,
    llm_text: str,
    obs_msg: str,
    *,
    tool_name: str,
    success: bool,
    turn: int = 0,
    artifact: str = "",
    corrected_record: dict | None = None,
    render: bool = True,
) -> None:
    """Text parsing: append assistant + observation + sync ctx.

    The next-turn prior (the in-memory ``messages`` assistant turn) is
    ALWAYS the rendered history record — ``render_assistant_from_history``
    of either the corrected record or the serialized one. Because the
    record is sanitized at save time (``serialize_assistant_for_history``
    cleans both the structured thought and the bare-content fallback via
    ``sanitize_thought``), the prior never carries a wire sentinel the model
    leaked mid-turn. Re-feeding such drift would strengthen mimicry (next
    turn's prior, or a resumed session's restored prior, teaches "repeating
    the shape / dropping the action is fine") — the format-runaway root
    cause, and the same class of failure the NO_THOUGHT retry avoids. This
    unifies the live prior with the resume prior (both go through render) and
    with the action-inferred correction, which already rendered its record.

    ``corrected_record`` (set after an action-name correction, where
    ``infer_action`` recovered a dropped action from the action_input key
    prefixes) supplies the structured record directly instead of re-parsing
    the raw. The correction stays traceable via the TurnRecorder
    (``parse_stage=3`` + ``action_inferred``).

    For history.jsonl (via ctx.add), the same record is stored, so the
    on-disk history retains structured form.

    The observation entry stores ``tool`` (the tool that ran, or an
    empty string for format-retry interventions) and ``success`` (so
    the web renderer's ``replay_from_history`` can re-emit the same
    ✓/✗ shape a live observation event has). The presence of the
    ``tool`` key — not its truthiness — distinguishes a tool result
    from a plain user chat turn, so empty-string ``tool_name`` (used
    by format-retry paths) still routes through ``observation()``.
    """
    if corrected_record is not None:
        history_record = corrected_record
    else:
        history_record = wire_format.serialize_assistant_for_history(llm_text)
    prior_content = wire_format.render_assistant_from_history(history_record)["content"]

    messages.append({"role": "assistant", "content": prior_content})
    messages.append({"role": "user", "content": obs_msg})
    stored_content = obs_msg
    if ctx:
        ctx.add(history_record)
        obs_entry = {
            "role": "user",
            "tool": tool_name,
            "success": success,
            "content": obs_msg,
        }
        if artifact:
            obs_entry["artifact"] = artifact
        stored = ctx.add(obs_entry)
        # ctx.add returns the stored (possibly spilled) message; tolerate a
        # ctx stub that returns None (some tests) by keeping obs_msg.
        if isinstance(stored, dict):
            stored_content = stored.get("content", obs_msg)

    # Single render point for observations: render what was STORED so the live
    # web/CLI card matches ctx and resume. ``render=False`` for recovery paths,
    # which already surface the intervention via ``render_recovery`` (no
    # double-render).
    if render:
        display = stored_content
        if isinstance(display, str) and display.startswith("Observation: "):
            display = display[len("Observation: ") :]
        render_step("observation", display, turn, tool_name=tool_name, success=success)
