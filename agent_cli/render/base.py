"""Base renderer interface — override methods to customize output and input.

A renderer is the agent loop's window onto the user: it both emits events
(prompts, observations, status) and reads the user's responses (chat
queries, confirmations, ask-tool answers). Wrapping input here means a
web-UI renderer can satisfy the same Protocol just by streaming events
over SSE and receiving form submissions, with no change to the loop.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field

# Serializes every interactive user read (``confirm`` y/n/a AND ``ask``'s
# free-text ``prompt_user``) across threads. Parallel delegate runs each
# task in its own worker thread; without one shared lock two workers could
# prompt at once and a single answer could be consumed by the wrong one.
# Re-entrant so a caller that already holds it (e.g. the dangerous-shell
# guard re-checking its allowlist around ``confirm``) can nest the read.
interactive_lock = threading.RLock()


@dataclass(frozen=True)
class ConfirmOption:
    """One choice in a multi-option confirmation.

    Used by :meth:`Renderer.confirm` to describe each selectable
    option without committing the renderer to a particular UI shape
    (CLI: typed token; web: button). ``aliases`` lists additional
    accepted typed tokens for CLI ergonomics (e.g. ``"yes"`` for the
    ``"y"`` key) — case-insensitive match. Web renderers typically
    only render ``label`` and submit ``key``.
    """

    key: str
    label: str
    aliases: tuple[str, ...] = field(default_factory=tuple)


class Renderer(ABC):
    """Abstract base for all renderers.

    Implement a subclass and call set_renderer() to swap output style.
    Each method corresponds to a distinct UI event in the agent loop.

    Built-in support for:
    - Nested rendering via push_depth/pop_depth for skills/delegates
    - Thread-local output capture for parallel delegate buffering
    """

    def __init__(self) -> None:
        self._depth: int = 0
        self._captures: dict[int, list[str]] = {}  # thread_id → captured lines
        self._thread_status: dict[int, str] = {}  # thread_id → last status line
        self._capture_lock = threading.Lock()
        # Per-thread prompt provenance, so an interactive confirm/ask can
        # show WHO is asking and WHY. The loop calls thought()/action() on
        # the same thread right before a tool triggers confirm/ask, and
        # delegate workers register their agent label — all keyed by
        # ``threading.get_ident()``.
        self._thread_agent: dict[int, str] = {}  # tid → delegate label
        self._thread_thought: dict[int, str] = {}  # tid → last reasoning
        self._thread_action: dict[int, str] = {}  # tid → last action line

    # ── Depth (nesting) ──────────────────────────────

    @property
    def depth(self) -> int:
        return self._depth

    def push_depth(self) -> None:
        """Increase nesting depth (enter skill/delegate)."""
        self._depth += 1

    def pop_depth(self) -> None:
        """Decrease nesting depth (exit skill/delegate)."""
        if self._depth > 0:
            self._depth -= 1

    # ── Capture (parallel buffering) ─────────────────

    def start_capture(self) -> None:
        """Start capturing output for the current thread."""
        with self._capture_lock:
            self._captures[threading.get_ident()] = []

    def stop_capture(self) -> list[str]:
        """Stop capturing and return collected lines for the current thread."""
        with self._capture_lock:
            tid = threading.get_ident()
            self._thread_status.pop(tid, None)
            return self._captures.pop(tid, [])

    def get_thread_status(self, tid: int) -> str:
        """Return the last captured status line for a given thread."""
        with self._capture_lock:
            return self._thread_status.get(tid, "")

    @property
    def is_capturing(self) -> bool:
        """True if the current thread is in capture mode."""
        return threading.get_ident() in self._captures

    def _capture_line(self, line: str) -> bool:
        """Append line to capture buffer if capturing. Returns True if captured."""
        tid = threading.get_ident()
        with self._capture_lock:
            buf = self._captures.get(tid)
            if buf is None:
                return False
            buf.append(line)
        return True

    def set_thread_status(self, status: str) -> None:
        """Update the live status line for the current thread."""
        tid = threading.get_ident()
        with self._capture_lock:
            if tid in self._captures:  # only when capturing
                self._thread_status[tid] = status

    # ── Prompt provenance (who/why behind a confirm or ask) ──

    def set_thread_agent(self, label: str) -> None:
        """Tag the current thread with a delegate label (or clear it with
        ``""``). Surfaced by ``confirm`` / ``prompt_user`` so the user can
        tell which agent is asking."""
        tid = threading.get_ident()
        if label:
            self._thread_agent[tid] = label
        else:
            self._thread_agent.pop(tid, None)

    def note_thought(self, content: str) -> None:
        """Record the current thread's latest reasoning (first line kept
        for the compact prompt header)."""
        self._thread_thought[threading.get_ident()] = content.strip()

    def note_action(self, tool_name: str, tool_input: str) -> None:
        """Record the current thread's latest action."""
        line = f"{tool_name} → {tool_input}" if tool_input else tool_name
        self._thread_action[threading.get_ident()] = line

    def begin_prompt_scope(self, scope_id: str, label: str = "") -> None:
        """프롬프트 인스펙터 스코프 push (v4.52.0) — 중첩 루프(skill 등)가
        호출자 스레드에서 돌 때 자기 스코프를 선언. Default no-op (C8 규율 —
        인스펙터 없는 렌더러는 무관)."""

    def end_prompt_scope(self, scope_id: str) -> None:
        """스코프 pop + 동적 컨텍스트 고정. Default no-op."""

    def note_scope_ctx(self, ctx) -> None:
        """현재 스코프의 ContextManager 를 인스펙터에 등록 — 동적 컨텍스트
        on-demand 조회용. Default no-op."""

    def note_system_prompt(self, sections: list[tuple[str, str]], turn: int) -> None:
        """Record the system prompt (as named sections) sent to the LLM this
        turn. Concrete no-op — the CLI renderer has no use for it; the web
        renderer overrides to keep the latest snapshot for the Prompt
        Inspector (`GET /api/debug/prompt`). Called once per LLM call, so an
        override must be cheap (store, don't render)."""

    def prompt_meta(self) -> dict[str, str]:
        """Provenance for an interactive prompt on the current thread:
        ``agent`` (delegate label, empty for the main agent), ``reasoning``
        (last thought), ``action`` (last action line)."""
        tid = threading.get_ident()
        return {
            "agent": self._thread_agent.get(tid, ""),
            "reasoning": self._thread_thought.get(tid, ""),
            "action": self._thread_action.get(tid, ""),
        }

    def _format_prompt_meta(self, *, include_action: bool) -> str:
        """One compact block naming the asking agent + its reasoning (and
        action for confirm), or ``""`` when there's nothing to show. Used
        by CLI renderers to print a header before the prompt.

        Gated on an agent label: the main agent already prints its
        thought/action inline right above the prompt, so a header would
        just duplicate it. Delegate workers (whose output is captured into
        a panel, not shown inline at prompt time) are exactly the case the
        user can't otherwise attribute — so the header fires there."""
        meta = self.prompt_meta()
        if not meta["agent"]:
            return ""
        lines: list[str] = [f"↳ from [{meta['agent']}]"]
        if meta["reasoning"]:
            first = meta["reasoning"].split("\n", 1)[0]
            lines.append(f"  💭 {first}")
        if include_action and meta["action"]:
            lines.append(f"  ⚡ {meta['action']}")
        return "\n".join(lines)

    # ── Ask-tool announcement ────────────────────────
    #
    # The ``ask`` tool's "Agent asks:" header + question list is a UI
    # concern too. CLI surfaces need to print the questions before
    # reading stdin (terminals don't echo the prompt context back).
    # Web surfaces don't because ``prompt_user(context=...)`` already
    # carries the question text into the form. The loop calls this
    # before ``prompt_user``; the renderer decides what to draw.

    def announce_ask(self, questions: list[str], *, prefix: str = "") -> None:
        """Announce the ask-tool questions before the input prompt.

        Default: no-op. WebRenderer keeps the default — the same text
        already arrives at the UI via the ``context`` argument of
        ``prompt_user``, so a duplicate emission would just be noise.
        MinimalRenderer overrides to print a colored block.
        """

    # ── Parallel delegate lifecycle ──────────────────
    #
    # Out-of-band surfaces (web) need to know when a worker thread
    # enters / leaves a delegate-parallel context so they can route
    # the thread's subsequent emits into a dedicated UI group instead
    # of interleaving them on the main timeline. CLI renderers don't
    # need this — ``rich.Live`` polls ``get_thread_status`` directly
    # — so the base implementations are concrete no-ops. WebRenderer
    # overrides to map ``thread_id → task_id`` and emit SSE markers.

    def begin_scope(
        self,
        *,
        task_id: str,
        kind: str = "run",
        label: str = "",
        agent: str = "",
        index: int = 0,
        parent: str | None = None,
        ts: float | None = None,
        ctx_dir: str | None = None,
    ) -> None:
        """Enter a nested execution scope — the single path for BOTH a skill
        subloop (``kind="skill"``) and a delegate / one-shot worker
        (``kind="run"``). On web this opens ONE ``scope_start`` card + routes
        the scope's inner events to it (fixes: ``/orchestrate`` used to emit a
        different, un-handled event and drew no card). CLI branches on ``kind``
        (skill bracket vs delegate Live panel). No-op default.

        ``parent`` — the ENCLOSING scope's ``task_id``, or ``""`` for a
        top-level scope under main. Leave it ``None`` (the default) when the
        scope opens on the same thread as its caller: the renderer then reads
        the parent off the thread's scope stack. Pass it explicitly only when
        that inference is impossible — a parallel worker calls this from its
        OWN thread, whose stack is empty, so the spawning code captures the
        parent (``current_scope()``) and hands it over.

        ``ctx_dir`` — the scope's OWN context directory, relative to the
        session dir (``skill_…``, ``run_…``, ``agents/<key>``). Sub-agents keep
        their turns in their own ``history.jsonl`` there — main's history holds
        only the final observation — so without this link a resumed session can
        rebuild the scope's card but not the turns inside it. Callers that name
        the directory AFTER opening the scope must name it first and pass it to
        both sides.

        ``ts`` — the scope's canonical start time (epoch seconds). Leave it
        ``None`` to have the emit point stamp "now". A parallel BATCH passes one
        shared value for all its workers: they start within milliseconds of each
        other, and stamping each thread separately scattered a simultaneous
        fan-out across several rows of the swimlane's event-ordinal axis, which
        reads as sequential. Shared ts → one row → one fork."""

    def end_scope(
        self,
        *,
        task_id: str,
        kind: str = "run",
        success: bool = True,
        duration_s: float = 0.0,
        error: str = "",
    ) -> None:
        """Leave a scope opened by :meth:`begin_scope`. No-op default."""

    def current_scope(self) -> str:
        """The calling thread's innermost open scope ``task_id`` (``""`` = main).

        Read it BEFORE spawning worker threads to capture the parent a worker
        can't infer from its own (empty) stack. Default ``""`` — only WebRenderer
        tracks scope identity, and a CLI caller passing that "" through to
        ``begin_scope(parent=...)`` is indistinguishable from top-level, which
        is what the CLI renders anyway (depth comes from push_depth)."""
        return ""

    def begin_agent_work(
        self, *, key: str, seq: int, profile: str, message: str
    ) -> None:
        """teammate worker 가 request 1건 처리를 시작 (teammate P1).

        delegate 의 begin_delegate_task 와 달리 **프롬프트 스코프를
        건드리지 않는다** — 스코프는 worker 가 begin_prompt_scope(key) 로
        상시 보유. 이 표면은 요청별 표시(웹 SSE 카드 라우팅 / CLI 출력
        캡처)만 담당. 기본 no-op."""

    def end_agent_work(
        self,
        *,
        key: str,
        seq: int,
        success: bool,
        duration_s: float,
        error: str = "",
    ) -> None:
        """teammate request 1건 처리 종료 — begin_agent_work 과 짝.
        기본 no-op."""

    def update_prompt_section(self, scope: str, name: str, text: str) -> None:
        """저장된 시스템 프롬프트 스냅샷의 섹션 1개를 외과적으로 갱신
        (teammate 멤버십의 즉시 인스펙터 반영 — v4.61.0). text 가 빈
        문자열이면 섹션 제거. 기본 no-op (web 전용 표면)."""

    def agent_roster(self, roster: list) -> None:
        """teammate 목록/상태 스냅샷 (P4) — spawn/kill/상태 전이마다 호출.
        web 은 sticky 브로드캐스트(대화 창 목록), CLI 는 no-op."""

    def agent_message(
        self,
        *,
        key: str,
        direction: str,
        author: str,
        text: str,
        seq: int = 0,
        success: bool = True,
        to: str = "main",
        ts: float | str | None = None,
    ) -> None:
        """teammate 대화 창의 메시지 1건 (P4). direction: "in"(요청/답변
        수신) | "out"(회신) | "question"(ask). ``to`` = 수신자("main" 또는
        "user:닉네임" 등 — @agt 명령/창 개입 문답의 라우팅 표시). ``ts``
        (5.13) = resume 재생 시 원래 발생 시각. 기본 no-op."""

    def clear_agent_conversation(self, key: str) -> None:
        """한 teammate 의 대화 기록을 표면에서 정리 (5.13, kill 시). web 은
        replay 버퍼 정리 + 라이브 창 비움, CLI 는 no-op."""

    def agent_mail_hint(self, *, key: str, kind: str, text: str) -> None:
        """상주 에이전트 mailbox 도착 힌트 (배달과 별개의 라이브 신호 —
        도착 순간에만 의미). ``kind``: "reply"|"question"|"died" 등,
        ``text``: 이미 조립된 사람용 한 줄. **기본 구현은 ``status`` 로 위임**
        해 CLI·커스텀 렌더러의 종전 동작을 보존한다. web 은 이 메서드를
        전용 transient ``agent_mail`` 이벤트로 override 해 프론트 인라인
        라인으로 표시 — ``status`` 이벤트엔 프론트 리스너가 없어 웹에선
        드롭됐기 때문(필수 구현 아님: 기본값 존재)."""
        self.status("running", text)

    # ── Abstract render methods ──────────────────────

    @abstractmethod
    def header(
        self,
        provider: str,
        model: str,
        max_turns: int,
        skill_name: str = "",
        skill_args: str = "",
    ) -> None:
        """Session or skill start banner."""

    def turn_sep(self, turn: int) -> None:
        """Separator between turns. Default no-op (C8) — web 이 실증한
        정답(턴 정보가 이벤트에 실림); 시각 구분이 필요한 렌더러만 override."""

    @abstractmethod
    def thought(self, content: str, turn: int) -> None:
        """LLM reasoning/thought."""

    @abstractmethod
    def action(self, tool_name: str, tool_input: str, turn: int) -> None:
        """Tool call (action)."""

    @abstractmethod
    def observation(
        self,
        content: str,
        turn: int,
        tool_name: str | None = None,
        success: bool = True,
    ) -> None:
        """Tool result (observation).

        `success` is the authoritative outcome from `ToolResult.success`
        and drives the ✓/✗ icon and `success`/`error` label."""

    @abstractmethod
    def final(self, content: str, turn: int) -> None:
        """Final answer."""

    @abstractmethod
    def error(self, content: str, turn: int) -> None:
        """Error message."""

    def recovery(
        self,
        raw_emission: str,
        intervention_message: str,
        reason: str,
        turn: int,
    ) -> None:
        """Recovery lifecycle: the model's emission failed to parse or
        validate, so the loop feeds an ``intervention`` back and retries.

        Default — mark the format error and surface the intervention (what
        was fed back to the model) as its own observation, so the failed
        emission, the intervention, and the retry each read as distinct
        steps rather than one growing blob. The web renderer overrides this
        to first *finalize* the live streaming card as a failed emission
        (otherwise the next turn's stream appends to the same card).
        ``raw_emission`` is the rejected text — already shown via
        :meth:`raw` in verbose CLI; web uses it to close the stream card.
        """
        self.status("error", f"format error ({reason}) — retrying", turn)
        self.observation(intervention_message, turn, None, success=False)

    def compaction(
        self,
        *,
        phase: str,
        old_tokens: int = 0,
        new_tokens: int = 0,
        evicted_count: int = 0,
        reason: str = "",
    ) -> None:
        """Context-compaction lifecycle (``start`` / ``done`` / ``warning``).

        Default — route through :meth:`status` so CLI/minimal renderers
        print a one-line notice. The web renderer overrides this to emit a
        dedicated structured ``compaction`` SSE event the frontend renders
        as an inline conversation line (the generic ``status`` event has no
        frontend listener). Unknown ``phase`` is silent rather than raising
        — this is a UX path and a typo must not break the agent loop.
        """
        if phase == "start":
            self.status(
                "info",
                f"Compacting context ({old_tokens:,} tokens, "
                f"{evicted_count} messages → summary)",
                0,
            )
        elif phase == "done":
            self.status(
                "info",
                f"Compaction done ({old_tokens:,} → {new_tokens:,} tokens)",
                0,
            )
        elif phase == "warning":
            self.status(
                "warning",
                f"Context compaction failed ({reason}); using FIFO drop instead.",
                0,
            )

    def raw(self, text: str, turn: int, verbose: bool) -> None:
        """Raw LLM response (verbose mode). Default no-op (C8) — 디버그
        전용 대용량 덤프라 표출은 opt-in."""

    def thinking(self, text: str, turn: int) -> None:
        """Reasoning content from a separate API field (verbose mode).

        Default no-op so existing plugin renderers keep working without
        forced overrides. Override to surface provider-side reasoning
        (e.g. Anthropic thinking blocks, OpenAI reasoning).
        """

    @abstractmethod
    def status(self, state: str, message: str, turn: int = 0) -> None:
        """Status update (running/done/error)."""

    def token_usage(self, stats: dict, turn: int, verbose: bool = False) -> None:
        """Per-turn token usage: in/out tokens (+speed), context-window
        occupancy %, and cumulative session output. ``stats`` is the
        render-agnostic dict from ``loop._build_token_stats``.

        Non-abstract with a no-op default so custom ``render/<name>.py``
        renderers keep working without implementing it; MinimalRenderer
        (CLI line) and WebRenderer (top-bar SSE) override.
        """

    def model_detected(
        self, model: str, capabilities, provider: str, saved_path: str
    ) -> None:
        """Newly detected model info. Default (C8): 코어 ``status`` 위임 —
        최소 구현(코어 7)만으로도 정보가 표출된다. 리치 표(스펙 카드)가
        필요한 렌더러만 override."""
        self.status(
            "done",
            f"model detected: {model} (provider={provider}, saved={saved_path})",
        )

    def model_loaded(self, model: str, capabilities) -> None:
        """Loaded model one-liner. Default (C8): 코어 ``status`` 위임."""
        self.status("done", f"model loaded: {model}")

    def context_dump(self, messages: list[dict], turn: int) -> None:
        """Debug context window dump. Default no-op (C8) — 디버그 전용."""

    def consume_directives_dirty(self) -> bool:
        """Atomically read+clear the 'DIRECTIVE.md was edited via the web
        Prompt Inspector' flag. The loop checks it each LLM call to rebuild its
        system prompt. Only the web renderer tracks it; others never edit."""
        return False

    def broadcast_directives_changed(self) -> None:
        """Tell open Prompt Inspectors to re-fetch (editor + prompt view). Fired
        on save (editor sync) and when the edit is actually applied (prompt
        view reflects the now-live directive). No-op off the web renderer."""

    def broadcast_memory_changed(self) -> None:
        """Tell open Prompt Inspectors to re-fetch the prompt view after a
        `memory` op changed the `## Session Memory` index. Memory has no editor
        (unlike directives), so this refreshes the prompt only. No-op off web."""

    def spinner_start(self, message: str = "") -> None:
        """Start a spinner animation (e.g. during LLM call). Default
        no-op (C8) — 장식; 애니메이션 가능한 매체만 override."""

    def spinner_stop(self) -> None:
        """Stop the spinner animation. Default no-op (C8)."""

    def dispatch_progress(
        self,
        label: str,
        turn: int,
        tool_name: str,
        detail: str = "",
        thought: str = "",
    ) -> None:
        """Show dispatched execution progress (skill, delegate, etc.).
        Default no-op (C8) — 진행 장식; 코어 action/observation 이 본 정보를
        이미 나름."""

    def stream_chunk(self, text: str) -> None:
        """Render a streaming chunk from LLM response. Default: no-op."""

    def stream_end(self) -> None:
        """Signal end of streaming. Default: no-op."""

    # ── User input ──────────────────────────────────

    @abstractmethod
    def prompt_user(
        self,
        prompt: str,
        *,
        default: str = "",
        multiline: bool = True,
        continuation: str = "... ",
        context: str = "",
    ) -> str:
        """Read free-form text input from the user.

        ``EOFError`` / ``KeyboardInterrupt`` propagate to the caller —
        different consumers want different policy (the setup wizard
        aborts, the ask tool substitutes a fallback). ``default`` covers
        only the empty-submission case.

        Args:
            prompt: Prompt string shown to the user.
            default: Returned when the user submits empty (whitespace-
                only) input. Lets callers mirror the bracketed-default
                pattern (``"  Size [4096]: "``) without inspecting
                emptiness afterwards. NOT a fallback for EOF / Ctrl+C.
            multiline: ``True`` enables multi-line submission (triple-
                quote blocks, paste detection) — the ``ask`` tool answer
                uses this. ``False`` reads a single line — setup wizard
                prompts use this.
            continuation: Prompt prefix shown for subsequent lines of
                a multi-line block. Ignored when ``multiline=False``.
            context: Optional pre-input announcement (e.g. the ``ask``
                tool's question list) that the renderer may surface
                alongside the input affordance. CLI renderers typically
                ignore this — they already print such announcements
                via ``console.print`` for color. Out-of-band UIs (web)
                use it to attach the question to the input form so
                the user doesn't have to scroll back.

        Returns:
            The stripped user input, or ``default`` on empty input.
        """

    def can_prompt(self) -> bool:
        """Whether an interactive prompt — a ``confirm`` y/n/a or ``ask``'s
        free-text question — can actually be shown to the user right now.

        Callers check this before blocking on a read: a renderer that
        can't surface a prompt (no TTY, no connected client) reports
        ``False`` so the caller refuses / substitutes a default instead of
        hanging on input that will never arrive. Default ``True`` — most
        renderers are attached to a live interactive surface; those whose
        ability depends on runtime state (a terminal, an open connection)
        override this.
        """
        return True

    @contextmanager
    def _prompt_display_guard(self):
        """Context wrapping a blocking user read so the renderer can quiet
        any output that would clash with it.

        Default no-op. ``MinimalRenderer`` overrides it to pause an active
        Rich ``Live`` region (spinner / parallel-delegate panel) that would
        otherwise repaint over the prompt; ``WebRenderer`` needs nothing
        (its prompt is an out-of-band SSE event).
        """
        yield

    def _guarded_read(self, read):
        """Run a blocking user read under the shared serialization lock and
        the renderer's display guard. ``confirm`` and ``prompt_user`` both
        route their read through here so they serialize against each other
        (one outstanding prompt at a time → answers can't cross wires) and
        share one display-quieting policy. ``read`` is a zero-arg callable
        performing the actual blocking read; its return value is passed
        through, its exceptions (EOF/abort) propagate to the caller."""
        with interactive_lock, self._prompt_display_guard():
            return read()

    @abstractmethod
    def confirm(
        self,
        prompt: str,
        options: list[ConfirmOption],
        *,
        default_key: str,
        command: str | None = None,
        danger_spans: list[tuple[int, int]] | None = None,
    ) -> tuple[str, str]:
        """Ask the user to pick one of ``options`` and optionally add
        a free-text comment.

        CLI implementations parse the first token of the typed line
        against each option's ``key`` and ``aliases`` (case-
        insensitive). Web implementations render one button per
        option and submit ``(key, comment)`` directly.

        Args:
            prompt: Prompt string shown to the user.
            options: Options the user can pick from. Must be non-empty.
            default_key: Returned when the user submits empty input or
                EOF, and when the typed token matches no option key /
                alias.
            command: Optional command/action text to display prominently
                alongside the prompt (the dangerous-shell guard passes the
                command here instead of embedding it in ``prompt``). ``None``
                → nothing extra shown, prompt-only (path-escape confirm).
            danger_spans: Optional ``(start, end)`` character ranges within
                ``command`` to highlight as dangerous (the triggered keyword
                tokens). Renderers paint these ranges — bold-red in the CLI,
                a ``.danger`` span on the web — and leave the rest plain. The
                ranges are pre-computed at the origin so renderers never
                re-derive token boundaries.

        Returns:
            ``(key, comment)``. ``key`` is one of ``options``' keys
            (or ``default_key`` on no match). ``comment`` is the rest
            of the typed line (everything after the first token),
            stripped, empty when no comment was given.
        """
