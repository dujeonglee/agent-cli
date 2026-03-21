"""Context manager with structured summarization and incremental compression."""
from __future__ import annotations

from agent_cli.constants import CHARS_PER_TOKEN, CONTEXT_RESERVE_RATIO
from agent_cli.context.token_estimator import estimate_tokens_from_messages
from agent_cli.prompts.compression_prompt import (
    INCREMENTAL_UPDATE_PROMPT,
    SUMMARIZATION_PROMPT,
)
from agent_cli.providers.base import LLMProvider
from agent_cli.providers.compat import ModelCapabilities


class ContextManager:
    """Manages conversation history with automatic structured compression.

    Uses LLM-based summarization with incremental updates (pi-mono pattern).
    Adapts to model context window via ModelCapabilities.
    """

    def __init__(
        self,
        provider: LLMProvider,
        model: str,
        capabilities: ModelCapabilities,
        keep_recent: int = 4,
    ):
        self.provider = provider
        self.model = model
        self.capabilities = capabilities
        self.keep_recent = keep_recent

        # Max chars derived from context window (chars/4 heuristic, inverse)
        # Reserve 25% for system prompt + current turn
        self.max_context_chars = int(
            capabilities.context_window * CHARS_PER_TOKEN * CONTEXT_RESERVE_RATIO
        )

        self.messages: list[dict] = []
        self._summary: str | None = None

    def add(self, role: str, content: str) -> None:
        """Add a message and trigger compression if needed."""
        self.messages.append({"role": role, "content": content})
        if self._total_chars() > self.max_context_chars:
            self._compress()

    def get_messages(self) -> list[dict]:
        """Return messages with summary prepended if available."""
        msgs: list[dict] = []
        if self._summary:
            msgs.append({
                "role": "user",
                "content": f"[Previous conversation summary]\n{self._summary}",
            })
            msgs.append({
                "role": "assistant",
                "content": "Understood. I have the context from our previous conversation.",
            })
        msgs.extend(self.messages)
        return msgs

    def force_compress(self) -> None:
        """Trigger compression immediately (for overflow recovery)."""
        if len(self.messages) > self.keep_recent * 2:
            self._compress()

    def get_estimated_tokens(self) -> int:
        """Estimate current buffer size in tokens."""
        return estimate_tokens_from_messages(self.get_messages())

    def _total_chars(self) -> int:
        extra = len(self._summary) if self._summary else 0
        return extra + sum(len(m["content"]) for m in self.messages)

    def _compress(self) -> None:
        """Compress older messages into a structured summary."""
        keep = self.keep_recent * 2  # pairs of user+assistant
        if len(self.messages) <= keep:
            return

        old_msgs = self.messages[:-keep]
        kept_msgs = self.messages[-keep:]

        serialized = self._serialize_messages(old_msgs)

        if self._summary is None:
            # First compression: full summarization
            prompt_text = (
                f"Conversation to summarize:\n\n{serialized}"
            )
            system = SUMMARIZATION_PROMPT
        else:
            # Incremental update: add to existing summary
            prompt_text = INCREMENTAL_UPDATE_PROMPT.format(
                existing_summary=self._summary,
                new_messages=serialized,
            )
            system = "You are a summarization assistant. Follow the instructions exactly."

        try:
            response = self.provider.call(
                messages=[{"role": "user", "content": prompt_text}],
                system=system,
                model=self.model,
                capabilities=self.capabilities,
            )
            self._summary = response.content
            self.messages = kept_msgs
        except Exception as e:
            # Compression failed — keep messages as-is to avoid data loss
            import sys
            print(f"[warn] Context compression failed: {e}", file=sys.stderr)
            pass

    def _serialize_messages(self, messages: list[dict]) -> str:
        """Serialize messages to text format for summarization (pi-mono pattern)."""
        parts = []
        for m in messages:
            role = m.get("role", "unknown").capitalize()
            content = m.get("content", "")
            # Truncate very long tool results for summarization
            if len(content) > 2000:
                content = content[:2000] + f"\n[... {len(content) - 2000} more characters truncated]"
            parts.append(f"[{role}]: {content}")
        return "\n\n".join(parts)
