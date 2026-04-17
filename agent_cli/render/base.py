"""Base renderer interface — override methods to customize output."""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod


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
            # Track last non-empty line as live status
            stripped = line.strip()
            if stripped:
                self._thread_status[tid] = stripped
        return True

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
        self, content: str, turn: int, tool_name: str | None = None
    ) -> None:
        """Tool result (observation)."""

    @abstractmethod
    def final(self, content: str, turn: int) -> None:
        """Final answer."""

    @abstractmethod
    def error(self, content: str, turn: int) -> None:
        """Error message."""

    @abstractmethod
    def raw(self, text: str, turn: int, verbose: bool) -> None:
        """Raw LLM response (verbose mode)."""

    @abstractmethod
    def status(self, state: str, message: str, turn: int = 0) -> None:
        """Status update (running/done/error)."""

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
    def spinner_start(self, message: str = "thinking...") -> None:
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
