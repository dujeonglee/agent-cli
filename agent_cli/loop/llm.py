"""Agent loop: ReAct pattern with M1/M2 module integration."""

from __future__ import annotations

from agent_cli.context.overflow import classify_overflow
from agent_cli.context.token_estimator import estimate_tokens
from agent_cli.loop.prompt import SystemPromptSvc

# Max shrink-and-retry attempts per turn when the server rejects the
# prompt as too long (flow 2 reactive recovery). Each attempt sheds more
# history via ``ContextManager.force_fit``; the bound stops a runaway
# loop when the cache cannot shrink enough or the server keeps rejecting.
from agent_cli.loop.state import _RETRY, LoopConfig, LoopState
from agent_cli.render import (
    render_context_dump,
    render_spinner_start,
    render_spinner_stop,
    render_status,
    render_step,
    render_stream_chunk,
    render_stream_end,
    render_system_prompt_snapshot,
)
from agent_cli.tools.result import ToolResult
from agent_cli.verbose import debug_log as _debug_log

_MAX_OVERFLOW_RETRIES = 5
# Compaction TARGET ratio: compact down to this fraction of available headroom
# (default 0.8 = leave a 20% margin). Distinct from manager's
# _COMPACTION_THRESHOLD_RATIO (0.9 = when to TRIGGER). The value now lives on
# the ContextManager (``self.ctx.compaction_ratio``) so the web slider can tune
# it live; both the preventive (flow 1) and overflow-recovery (flow 2) target
# computations in _call_llm read it from there.


class LLMCaller:
    """LLM 콜 소유자 (C1 PR-3 승격 클러스터) — 스트리밍 호출, 예방 압축
    (ensure_within)·오버플로 재시도 카운터, 압축 요약 콜.

    의존은 명시 주입 5종: cfg/state/ctx/provider/prompt(SystemPromptSvc).
    전유 상태 ``overflow_retries`` 소유. 턴 오케스트레이션(_execute_turn:
    훅 발화·truncation 처리·dispatch 진입)은 AgentLoop 에 남는다 — 이
    객체는 "한 번의 호출"만 안다.
    """

    def __init__(
        self,
        config: LoopConfig,
        state: LoopState,
        ctx,
        provider,
        prompt: SystemPromptSvc,
    ) -> None:
        self.cfg = config
        self.state = state
        self.ctx = ctx
        self.provider = provider
        self.prompt = prompt
        # Reactive context-overflow recovery (flow 2) 카운터 — 현재 턴에서
        # 축소-재시도한 횟수. _MAX_OVERFLOW_RETRIES 로 유계.
        self.overflow_retries = 0
        # Size of the last session-state block, reserved from the next turn's
        # compaction budget (see _call_llm).
        self._state_tokens = 0

    def _build_session_state(self, budget: int) -> str:
        """The volatile state block appended to the last message each turn —
        context pressure, turn budget, the live agent roster and the session
        memory index (see ``prompts/session_state.py`` for why the tail).

        Assembled HERE rather than in ContextManager because this is where the
        inputs live: the turn counter (state), the agent registry (config) and
        the model's budget. Reuses the same section renderers the system prompt
        used before the move, so the model sees identical headings.

        ``budget`` is the live compaction target, so the "context is nearly
        full" warning fires a turn or two BEFORE ``ensure_within`` actually
        starts dropping history — early enough for the model to save something
        with ``memory``."""
        from agent_cli.memory import render_index
        from agent_cli.prompts.session_state import build_session_state
        from agent_cli.prompts.system_prompt import build_live_agents_section

        agents = ""
        # Same gate the system-prompt section used: no ``agent`` tool, no
        # roster to advertise.
        if self.cfg.agent_registry is not None and "agent" in self.cfg.tools_list:
            agents = build_live_agents_section(
                self.cfg.agent_registry, include_state=True
            )
        memory = ""
        if self.ctx is not None and self.ctx.session_dir:
            memory = render_index(self.ctx.session_dir)
        return build_session_state(
            used_tokens=self.ctx.get_estimated_tokens() if self.ctx else 0,
            budget_tokens=budget,
            turn=self.state.turn,
            max_turns=self.cfg.max_turns,
            agents=agents,
            memory=memory,
        )

    def _interrupt_check(self) -> bool:
        """Zero-arg predicate the provider polls per chunk to break a
        mid-generation stream (Ctrl+C / web stop)."""
        ev = self.state.stop_event
        return bool(ev and ev.is_set())

    def _call_llm(self):
        """LLM call with overflow retry and streaming. Returns response or sentinel."""
        # flow 1 — preventive compaction before the call. The threshold
        # uses the live system-prompt size and the model's window, so it
        # reflects real headroom (not a fixed budget). ``sys_tokens`` is
        # reused below to reconcile the cache against the server's actual
        # input count once the call succeeds.
        sys_tokens = estimate_tokens(self.prompt.system) if self.ctx else 0
        if self.ctx:
            target = max(
                int(
                    (
                        self.cfg.capabilities.context_window
                        - sys_tokens
                        # The session-state block rides at the TAIL of the
                        # messages, so it is part of the request but not of
                        # ``system``. Reserve the previous turn's size — it is
                        # stable turn to turn (the memory index grows slowly),
                        # and it must be built AFTER ensure_within to report
                        # post-compaction numbers, which is after this budget
                        # is needed.
                        - self._state_tokens
                        - self.cfg.capabilities.max_output_tokens
                    )
                    * self.ctx.compaction_ratio
                ),
                1,
            )
            self.ctx.ensure_within(target)
            state = self._build_session_state(target)
            self._state_tokens = estimate_tokens(state)
            self.ctx.set_session_state(state)
            self.state.messages = self.ctx.get_messages()

        # Context dump (verbose only)
        if self.cfg.verbose:
            render_context_dump(self.state.messages, self.state.turn)
        _debug_log(
            f"LLM_CALL turn={self.state.turn} skill={self.cfg.skill_name or 'main'} msg_count={len(self.state.messages)}"
        )

        # Build streaming callback: stops spinner on first chunk, then streams
        spinner_active = True

        def on_chunk(text: str) -> None:
            nonlocal spinner_active
            if spinner_active:
                render_spinner_stop()
                spinner_active = False
            render_stream_chunk(text)

        if self.cfg.skill_name:
            render_spinner_start(f"skill:{self.cfg.skill_name}")
        else:
            render_spinner_start()
        # Prompt Inspector snapshot: what THIS call's system prompt looks
        # like, as named sections. No-op for CLI renderers (store-only on
        # web), so per-turn cost is negligible.
        render_system_prompt_snapshot(self.prompt.sections, self.state.turn)

        # Plugin-defined provider hints. The wire plugin decides them from
        # the model's capabilities — e.g. ``json_mode``: ReAct requests it
        # iff the model supports structured output, json_fc never does
        # (markdown shape). This is the single wire ⨯ capability decision
        # point, so the provider never combines the two itself.
        extra_call_kwargs = self.cfg.wire_format.provider_call_kwargs(
            self.cfg.capabilities
        )

        # Plugin-defined prefill: a string the provider sees as the start
        # of an assistant turn. Forces the model to continue from there,
        # producing the wire format from the very first generated token.
        # ReAct returns "" (no prefill — its prior already produces ReAct
        # shape). Envelope plugins return e.g. ``<tool_use id="r1" action="``
        # so the model emits the tool name next. Empty string => behaviour
        # is identical to the pre-plugin path.
        prefill = self.cfg.wire_format.prefill()
        call_messages = self.state.messages
        if prefill:
            call_messages = [
                *self.state.messages,
                {"role": "assistant", "content": prefill},
            ]
        try:
            response = self.provider.call(
                messages=call_messages,
                system=self.prompt.system,
                model=self.cfg.model,
                capabilities=self.cfg.capabilities,
                on_chunk=on_chunk,
                degeneration_check=self.cfg.wire_format.is_degenerate,
                # 게이트 문자는 wire shape 소유(P0-4) — 커스텀 플러그인 폴백 "#".
                degeneration_trigger=getattr(
                    self.cfg.wire_format, "degeneration_trigger", "#"
                ),
                interrupt_check=self._interrupt_check,
                # 세션 thinking 오버라이드(web UI) — 공유 ctx 라 이 콜이 즉시 반영.
                request_overrides=(self.ctx.thinking_override if self.ctx else None),
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
            overflow_amounts = classify_overflow(e)
            if (
                overflow_amounts is not None
                and self.ctx
                and self.overflow_retries < _MAX_OVERFLOW_RETRIES
            ):
                # Reactive recovery (flow 2): the server rejected the
                # prompt as too long. Trust its count over our local
                # estimate, shrink the cache toward the limit, and retry.
                # ``classify_overflow`` gates on HTTP 400/413 so a non-overflow
                # error never reaches force_fit (AUDIT L-1); a None result
                # falls through to the normal failure path below.
                actual, limit = overflow_amounts
                budget = self.ctx.max_context_tokens
                target = int((limit or budget) * self.ctx.compaction_ratio)
                if self.ctx.force_fit(target, actual_tokens=actual):
                    self.overflow_retries += 1
                    render_status(
                        "running",
                        f"Context overflow — shrinking and retrying "
                        f"({self.overflow_retries}/{_MAX_OVERFLOW_RETRIES})...",
                    )
                    self.state.messages = self.ctx.get_messages()
                    self.state.turn -= 1
                    return _RETRY
                # force_fit could not shrink further (only the anchor
                # remains) — fall through to a clean failure.
            _debug_log(
                f"LLM call failed: model={self.cfg.model} iter={self.state.turn} skill={self.cfg.skill_name} error={e}"
            )
            render_step(
                "error",
                f"LLM call failed (model={self.cfg.model}, iter={self.state.turn}): {e}",
                self.state.turn,
            )

            return ToolResult(False, error=f"LLM call failed: {e}")
        finally:
            if spinner_active:
                render_spinner_stop()
            else:
                render_stream_end()

    # ── C1 PR-3: 턴 디스패치는 TurnDispatcher 소유 — 기존 호출면 위임.

    def _llm_compact_summarize(self, messages: list[dict]) -> str:
        """Compactor callback: call the main provider with a
        summarisation system prompt + the evicted messages, return
        the summary text. Raised exceptions are converted to
        ``CompactionError`` inside ContextManager._summarize_messages.

        ``messages`` arrives in chat-ready ``{role, content}`` form —
        ContextManager._compact does the natural-language conversion
        via the wire_format plugin before calling here.

        Capabilities are overridden for this one call:
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
            self.cfg.capabilities,
            supports_thinking=False,
        )
        response = self.provider.call(
            messages=messages,
            system=summarisation_prompt,
            model=self.cfg.model,
            capabilities=summary_capabilities,
        )
        return response.content if hasattr(response, "content") else str(response)


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
