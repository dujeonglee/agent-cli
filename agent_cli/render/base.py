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
from dataclasses import dataclass, field


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

    def begin_delegate_task(
        self,
        *,
        task_id: str,
        index: int,
        agent: str,
        task_text: str,
    ) -> None:
        """Mark the current thread as a delegate worker. No-op for CLI."""

    def end_delegate_task(
        self,
        *,
        task_id: str,
        success: bool,
        duration_s: float,
        error: str = "",
    ) -> None:
        """Mark the current thread as leaving its delegate context.
        No-op for CLI."""

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

    @abstractmethod
    def turn_sep(self, turn: int) -> None:
        """Separator between turns."""

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

    @abstractmethod
    def raw(self, text: str, turn: int, verbose: bool) -> None:
        """Raw LLM response (verbose mode)."""

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

    @abstractmethod
    def model_detected(
        self, model: str, capabilities, provider: str, saved_path: str
    ) -> None:
        """Newly detected model info."""

    @abstractmethod
    def model_loaded(self, model: str, capabilities) -> None:
        """Loaded model one-liner."""

    @abstractmethod
    def context_dump(self, messages: list[dict], turn: int) -> None:
        """Debug context window dump."""

    @abstractmethod
    def spinner_start(self, message: str = "") -> None:
        """Start a spinner animation (e.g. during LLM call)."""

    @abstractmethod
    def spinner_stop(self) -> None:
        """Stop the spinner animation."""

    @abstractmethod
    def dispatch_progress(
        self,
        label: str,
        turn: int,
        tool_name: str,
        detail: str = "",
        thought: str = "",
    ) -> None:
        """Show dispatched execution progress (skill, delegate, etc.)."""

    def stream_chunk(self, text: str) -> None:
        """Render a streaming chunk from LLM response. Default: no-op."""

    def stream_end(self) -> None:
        """Signal end of streaming. Default: no-op."""

    def group_start(self, label: str, icon: str = "") -> None:
        """Start a nested block (skill/delegate). Default: no-op."""

    def group_end(
        self, label: str, success: bool = True, duration_s: float = 0
    ) -> None:
        """End a nested block. Default: no-op."""

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
        different consumers want different policy (chat REPL ends the
        session, setup wizard aborts, ask tool substitutes a
        fallback). ``default`` covers only the empty-submission case.

        Args:
            prompt: Prompt string shown to the user.
            default: Returned when the user submits empty (whitespace-
                only) input. Lets callers mirror the bracketed-default
                pattern (``"  Size [4096]: "``) without inspecting
                emptiness afterwards. NOT a fallback for EOF / Ctrl+C.
            multiline: ``True`` enables multi-line submission (triple-
                quote blocks, paste detection) — chat REPL and the
                ``ask`` tool answer use this. ``False`` reads a single
                line — setup wizard prompts use this.
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

    def can_confirm(self) -> bool:
        """Whether an interactive confirmation can actually be shown to
        the user right now.

        The dangerous-shell guard calls this before prompting: a renderer
        that can't surface a prompt (no TTY, no connected client) reports
        ``False`` so the caller refuses the command with a clear error
        instead of hanging on input that will never arrive. Default
        ``True`` — most renderers are attached to a live interactive
        surface; those whose ability depends on runtime state (a terminal,
        an open connection) override this.
        """
        return True

    @abstractmethod
    def confirm(
        self,
        prompt: str,
        options: list[ConfirmOption],
        *,
        default_key: str,
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

        Returns:
            ``(key, comment)``. ``key`` is one of ``options``' keys
            (or ``default_key`` on no match). ``comment`` is the rest
            of the typed line (everything after the first token),
            stripped, empty when no comment was given.
        """
