"""Agent loop: ReAct pattern with M1/M2 module integration."""

from __future__ import annotations

import signal
import sys
import threading

from agent_cli.constants import (
    DELEGATE_DEFAULT_TIMEOUT,
    INTERRUPT_NOTICE,
    OUTPUT_TRUNCATED_NOTICE,
)
from agent_cli.tools.result import ToolResult

from agent_cli.context.manager import ContextManager
from agent_cli.providers.base import LLMProvider
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.recovery.observability import (
    TurnRecorder,
)
from agent_cli.memory import consume_memory_reload
from agent_cli.render import (
    consume_directives_reload,
    notify_directives_applied,
    notify_memory_applied,
    render_system_prompt_snapshot,
    render_header,
    render_turn_sep,
    render_raw,
    render_thinking,
    render_status,
    render_step,
    render_token_usage,
)
from agent_cli.tools import TOOLS, RunContext

from agent_cli.verbose import debug_log as _debug_log, set_verbose as _set_debug_verbose
from agent_cli.wire_formats import get as _get_wire_format

# Max shrink-and-retry attempts per turn when the server rejects the
# prompt as too long (flow 2 reactive recovery). Each attempt sheds more
# history via ``ContextManager.force_fit``; the bound stops a runaway
# loop when the cache cannot shrink enough or the server keeps rejecting.
from agent_cli.loop.state import LoopConfig, LoopState, _CONTINUE, _RETRY
from agent_cli.loop.prompt import SystemPromptSvc, build_inspector_sections
from agent_cli.loop.tool_bridge import ToolBridge
from agent_cli.loop.llm import LLMCaller, _build_token_stats
from agent_cli.loop.dispatch import TurnDispatcher, _append_observation


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
        teammate_registry=None,
    ):
        # Wire format plugin — ReAct by default. Centralizes the
        # parser, recovery wording, prompt section, and lifecycle hooks
        # so adding a new format means dropping a file in
        # ``agent_cli/wire_formats/`` and re-running with
        # ``--response-format <name>``.
        if wire_format is None:
            wire_format = _get_wire_format()
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
        self.dequeue_user_message = dequeue_user_message
        self.route_message = route_message
        self.provider = provider
        self.ctx = ctx
        self.agent_role = agent_role

        # Derived wiring (finalized below, then frozen into LoopConfig)
        tools_list = active_tools or list(TOOLS.keys())
        # Remove both 'delegate' and 'run_skill' when the combined
        # call depth has reached its ceiling. Skills now count
        # toward depth (parity with delegate), so the ceiling treats
        # both kinds of nesting the same way — and the LLM never
        # sees a tool it'd be refused from. The dispatch-time
        # ``_handle_run_skill`` / ``_run_single`` checks remain as a
        # belt-and-suspenders layer for direct callers that built a
        # custom ``active_tools`` list.
        if depth >= max_depth:
            tools_list = [t for t in tools_list if t not in ("delegate", "run_skill")]
        # Remove "ask" in non-interactive mode (no ctx)
        if not ctx and "ask" in tools_list:
            tools_list = [t for t in tools_list if t != "ask"]
        # teammate 는 레지스트리가 주입된 루프(=main 부트스트랩)에서만 —
        # 서브에이전트(delegate/skill/teammate 자신)와 headless 호출자는
        # 레지스트리가 없으므로 도구 자체가 안 보인다 (P1: teammate 안
        # teammate 금지의 단일 가드; 모델이 거부당할 도구를 보지 않는다).
        if teammate_registry is None and "teammate" in tools_list:
            tools_list = [t for t in tools_list if t != "teammate"]
        # Build skill stack for recursive call prevention
        if skill_stack is None:
            skill_stack = []
        if skill_name:
            skill_stack = [*skill_stack, skill_name]
        # Build agent stack for recursive call prevention
        if agent_stack is None:
            agent_stack = []
        if agent_name:
            agent_stack = [*agent_stack, agent_name]

        # C1 PR-1: 불변 배선은 LoopConfig 로, 공유 가변 상태는 LoopState 로.
        # 기존 ``self.X`` 표면은 아래 property 브리지가 유지한다 — 메서드
        # 본문/테스트 무변경. PR-2/3 에서 협력 객체가 이 두 객체를 직접
        # 주입받으면 해당 메서드의 브리지 의존이 자연 소멸한다.
        self._config = LoopConfig(
            model=model,
            provider_name=provider_name,
            base_url=base_url,
            api_key=api_key,
            depth=depth,
            max_depth=max_depth,
            max_turns=max_turns,
            delegate_timeout=delegate_timeout,
            tools_list=tools_list,
            skill_name=skill_name,
            skill_args=skill_args,
            skill_stack=skill_stack,
            agent_stack=agent_stack,
            capabilities=capabilities,
            wire_format=wire_format,
            mcp_manager=mcp_manager,
            hook_runner=hook_runner,
            hooks_config=hooks_config,
            session=session,
            agent_role=agent_role,
            graceful_interrupt=graceful_interrupt,
            compaction_enabled=compaction_enabled,
            verbose=verbose,
            teammate_registry=teammate_registry,
        )
        self._state = LoopState(
            query=query,
            query_author=query_author,
            stop_event=stop_event if stop_event is not None else threading.Event(),
        )
        # Cumulative output tokens across this loop's turns — fed to the
        # renderer's per-turn token-usage line / web top-bar so the user
        # sees a running session total alongside the per-turn numbers.
        self._total_output_tokens = 0
        self._prev_sigint_handler = None
        # C1 PR-2: 시스템 프롬프트는 SystemPromptSvc 소유 — self.system /
        # self._system_sections 는 property 브리지.
        self._prompt = SystemPromptSvc(self._config, ctx)
        # C1 PR-3: LLM 콜은 LLMCaller 소유(overflow_retries 포함) — 위임/
        # property 가 기존 표면 유지.
        self._llm = LLMCaller(self._config, self._state, ctx, provider, self._prompt)
        # C1 PR-2: 도구 호출 브리지 — cfg/state/ctx/provider 명시 주입.
        self._tools = ToolBridge(self._config, self._state, ctx, provider)
        # Sentinels — 모듈 상수 별칭 (C1 PR-3: dispatcher/LLM 콜과 공유)
        self._CONTINUE = _CONTINUE
        self._RETRY = _RETRY

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

        # C1 PR-3: 턴 디스패치는 TurnDispatcher 소유(loop_detector 포함).
        self._dispatch = TurnDispatcher(
            self._config, self._state, ctx, self._tools, self.recorder
        )

        # Context compaction wiring (RFC docs/context-compaction/).
        # The compactor callback and the TurnRecorder are injected
        # into the ContextManager so the manager can summarise
        # evicted messages via the same provider and emit compaction
        # events for measurement. ``compaction_enabled=False`` (CLI
        # ``--no-compaction``) skips the callback registration so the
        # manager reverts to plain FIFO drop.
        if self.ctx is not None:
            self.ctx.set_recorder(self.recorder)
            if self._compaction_enabled():
                self.ctx.set_compactor(self._llm_compact_summarize)

    # ── C1 PR-1 property 브리지 ──────────────────────────────────
    # 기존 ``self.X`` 접근 표면을 유지한 채 소유권만 LoopConfig/LoopState 로
    # 옮기는 호환 shim. PR-2/3 에서 메서드가 협력 객체로 이주하며 그 객체가
    # config/state 를 직접 읽게 되면, 이주분의 브리지 의존은 소멸한다.
    # (config 필드는 재할당 금지 — setter 없음이 의도. state 필드는 가변.)

    @property
    def model(self):
        return self._config.model

    @property
    def provider_name(self):
        return self._config.provider_name

    @property
    def base_url(self):
        return self._config.base_url

    @property
    def api_key(self):
        return self._config.api_key

    @property
    def depth(self):
        return self._config.depth

    @property
    def max_depth(self):
        return self._config.max_depth

    @property
    def max_turns(self):
        return self._config.max_turns

    @property
    def delegate_timeout(self):
        return self._config.delegate_timeout

    @property
    def tools_list(self):
        return self._config.tools_list

    @property
    def skill_name(self):
        return self._config.skill_name

    @property
    def skill_args(self):
        return self._config.skill_args

    @property
    def skill_stack(self):
        return self._config.skill_stack

    @property
    def agent_stack(self):
        return self._config.agent_stack

    @property
    def capabilities(self):
        return self._config.capabilities

    @property
    def wire_format(self):
        return self._config.wire_format

    @property
    def mcp_manager(self):
        return self._config.mcp_manager

    @property
    def hook_runner(self):
        return self._config.hook_runner

    @property
    def hooks_config(self):
        return self._config.hooks_config

    @property
    def session(self):
        return self._config.session

    @property
    def graceful_interrupt(self):
        return self._config.graceful_interrupt

    @property
    def compaction_enabled(self):
        return self._config.compaction_enabled

    @property
    def verbose(self):
        return self._config.verbose

    @property
    def query(self):
        return self._state.query

    @query.setter
    def query(self, v):
        self._state.query = v

    @property
    def query_author(self):
        return self._state.query_author

    @query_author.setter
    def query_author(self, v):
        self._state.query_author = v

    @property
    def messages(self):
        return self._state.messages

    @messages.setter
    def messages(self, v):
        self._state.messages = v

    @property
    def turn(self):
        return self._state.turn

    @turn.setter
    def turn(self, v):
        self._state.turn = v

    @property
    def task_log(self):
        return self._state.task_log

    @task_log.setter
    def task_log(self, v):
        self._state.task_log = v

    @property
    def _interrupted(self):
        return self._state.interrupted

    @_interrupted.setter
    def _interrupted(self, v):
        self._state.interrupted = v

    @property
    def stop_event(self):
        return self._state.stop_event

    @stop_event.setter
    def stop_event(self, v):
        self._state.stop_event = v

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
        """LLMCaller.compact-summarize 위임 (ctx.set_compactor 등록 대상)."""
        return self._llm._llm_compact_summarize(messages)

    def _call_llm(self):
        """LLMCaller 위임 (기존 호출면 유지 — 테스트의 인스턴스 모킹 호환)."""
        return self._llm._call_llm()

    @property
    def overflow_retries(self):
        return self._llm.overflow_retries

    @overflow_retries.setter
    def overflow_retries(self, v):
        self._llm.overflow_retries = v

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

    def _rebuild_system_prompt(self) -> None:
        """SystemPromptSvc.rebuild 위임 (기존 호출면 유지)."""
        self._prompt.rebuild()

    def _apply_system_sections(self, hook_ctx) -> None:
        """SystemPromptSvc.apply_hook_sections 위임 (기존 호출면 유지)."""
        self._prompt.apply_hook_sections(hook_ctx)

    @property
    def system(self):
        return self._prompt.system

    @system.setter
    def system(self, v):
        self._prompt.system = v

    @property
    def _system_sections(self):
        return self._prompt.sections

    @_system_sections.setter
    def _system_sections(self, v):
        self._prompt.sections = v

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
                self._deliver_teammate_replies()
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
        self._rebuild_system_prompt()

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

    def _deliver_teammate_replies(self) -> None:
        """턴 경계 (teammate P1, D2): 미배달 teammate 회신을 관찰 레코드로
        주입한다 — LLM 폴링 없이 harness 가 배달. 레코드는 tool="teammate"
        + source="teammate_reply" (tool="" 는 형식-개입 마커라 금지 —
        records.is_format_intervention 오인 방지). over-cap 회신은
        build_reply_record 가 디스크 포인터로 치환(전문은 worker 가 이미
        teammates/<key>/replies/ 에 영속)."""
        registry = self._config.teammate_registry
        if registry is None:
            return
        replies = registry.drain_replies()
        if not replies:
            return
        from agent_cli.subagent.teammate import build_reply_record

        for reply in replies:
            record = build_reply_record(reply, cap=self._oversized_cap)
            self.messages.append({"role": "user", "content": record["content"]})
            if self.ctx:
                self.ctx.add(dict(record))
            render_step(
                "observation",
                record["content"],
                self.turn,
                tool_name="teammate",
                success=bool(reply.get("success")),
            )

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
        # A web edit to DIRECTIVE.md (Prompt Inspector) → rebuild the system
        # prompt so THIS call already reflects it (applies immediately at the
        # next LLM call; idle → next user query). Busts the KV cache prefix.
        # A memory add/update/delete (tool) or a web DIRECTIVE.md edit → rebuild
        # so THIS call reflects it (busts the KV prefix). Memory's `## Session
        # Memory` index is read from memory.jsonl at build time, so a rebuild is
        # all it needs to refresh.
        directives_changed = consume_directives_reload()
        memory_changed = consume_memory_reload()
        if directives_changed or memory_changed:
            self._rebuild_system_prompt()
            # Update-when-applied: push the fresh snapshot + signal open
            # inspectors so the prompt view shows the now-live sections at the
            # moment they take effect (not optimistically on save).
            render_system_prompt_snapshot(
                build_inspector_sections(self._system_sections, self.ctx), self.turn
            )
            if directives_changed:
                notify_directives_applied()
            if memory_changed:
                notify_memory_applied()
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

    def _handle_text_path(self, llm_text: str):
        return self._dispatch._handle_text_path(llm_text)

    def _task_text(self) -> str:
        return self._dispatch._task_text()

    def _dispatch_tool_with_hooks(self, tool_name: str, tool_input):
        return self._tools._dispatch_tool_with_hooks(tool_name, tool_input)

    def _tool_observation(self, tool_name: str, result, args) -> str:
        return self._tools._tool_observation(tool_name, result, args)

    def _record_tool_history(self, tool_name: str, tool_input) -> None:
        return self._tools._record_tool_history(tool_name, tool_input)

    def _run_ctx(self) -> RunContext:
        return self._tools._run_ctx()

    @property
    def _oversized_cap(self):
        return self._tools._oversized_cap

    @_oversized_cap.setter
    def _oversized_cap(self, v):
        self._tools._oversized_cap = v

    @property
    def recent_tool_history(self):
        return self._tools.recent_tool_history

    @recent_tool_history.setter
    def recent_tool_history(self, v):
        self._tools.recent_tool_history = v

    def _on_max_turns(self):
        """Handle max turns reached."""
        render_status("error", f"Max turns ({self.max_turns}) reached.")
        _debug_log(
            f"run_loop returning None: max_turns={self.max_turns} reached, skill_name={self.skill_name}"
        )
        return ToolResult(False, error=f"Max turns ({self.max_turns}) reached")


# Backward-compatible wrapper
