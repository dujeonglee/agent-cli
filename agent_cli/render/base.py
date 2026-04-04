"""Base renderer interface — override methods to customize output."""

from __future__ import annotations

from abc import ABC, abstractmethod


class Renderer(ABC):
    """Abstract base for all renderers.

    Implement a subclass and call set_renderer() to swap output style.
    Each method corresponds to a distinct UI event in the agent loop.
    """

    @abstractmethod
    def header(
        self,
        provider: str,
        model: str,
        max_iter: int,
        skill_name: str = "",
        skill_args: str = "",
    ) -> None:
        """Session or skill start banner."""

    @abstractmethod
    def iter_sep(self, iteration: int) -> None:
        """Separator between iterations."""

    @abstractmethod
    def thought(self, content: str, iteration: int) -> None:
        """LLM reasoning/thought."""

    @abstractmethod
    def action(self, tool_name: str, tool_input: str, iteration: int) -> None:
        """Tool call (action)."""

    @abstractmethod
    def observation(
        self, content: str, iteration: int, tool_name: str | None = None
    ) -> None:
        """Tool result (observation)."""

    @abstractmethod
    def final(self, content: str, iteration: int) -> None:
        """Final answer."""

    @abstractmethod
    def error(self, content: str, iteration: int) -> None:
        """Error message."""

    @abstractmethod
    def raw(self, text: str, iteration: int, verbose: bool) -> None:
        """Raw LLM response (verbose mode)."""

    @abstractmethod
    def status(self, state: str, message: str, iteration: int = 0) -> None:
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
    def context_dump(self, messages: list[dict], iteration: int) -> None:
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
        iteration: int,
        tool_name: str,
        detail: str = "",
        thought: str = "",
    ) -> None:
        """Show dispatched execution progress (skill, delegate, etc.)."""
