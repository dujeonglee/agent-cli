"""Context manager with structured summarization and incremental compression.

Supports optional scratchpad + artifact persistence for long-running tasks.
Scratchpad content survives compaction as a context anchor.
"""

from __future__ import annotations

from pathlib import Path

from agent_cli.constants import CHARS_PER_TOKEN
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

    Optional scratchpad mode: when enabled, maintains a persistent
    scratchpad.md and per-turn artifacts that survive compaction.
    """

    def __init__(
        self,
        provider: LLMProvider,
        model: str,
        capabilities: ModelCapabilities,
        keep_recent: int = 4,
        session_id: str | None = None,
        scratchpad_base: Path | None = None,
        scratchpad_dir: Path | None = None,
    ):
        if session_id is None and scratchpad_dir is None:
            raise ValueError(
                "session_id is required for ContextManager. "
                "Pass session_id to create a session-scoped scratchpad."
            )

        self.provider = provider
        self.model = model
        self.capabilities = capabilities
        self.keep_recent = keep_recent

        self.messages: list[dict] = []
        self._summary: str | None = None
        self._msg_chars: int = 0  # Running character count for O(1) add()

        # Scratchpad integration (always active, session-scoped)
        if scratchpad_dir:
            self._scratchpad_dir = scratchpad_dir
        else:
            from agent_cli.context.scratchpad import session_scratchpad_dir

            base = scratchpad_base or Path(".agent-cli")
            self._scratchpad_dir = session_scratchpad_dir(session_id, base)

        self._turn_count = 0
        self._skill_name = ""
        self._skill_parent_turn = 0

        from agent_cli.context.scratchpad import ContextBudget

        self._budget = ContextBudget.for_model(capabilities.context_window)
        # Max chars for conversation = budget's conversation allocation
        self.max_context_chars = int(self._budget.conversation_tokens * CHARS_PER_TOKEN)

    def add(self, role: str, content: str) -> None:
        """Add a message and trigger compression if needed."""
        self.messages.append({"role": role, "content": content})
        self._msg_chars += len(content)
        if self._total_chars() > self.max_context_chars:
            self._compress()

    def get_messages(self) -> list[dict]:
        """Return messages with summary and scratchpad context prepended."""
        msgs: list[dict] = []

        # Scratchpad anchor: always injected first (survives compaction)
        scratchpad_block = self._build_scratchpad_block()
        if scratchpad_block:
            msgs.append({"role": "user", "content": scratchpad_block})
            msgs.append(
                {
                    "role": "assistant",
                    "content": (
                        "Understood. I have the scratchpad context "
                        "and will avoid repeating completed work."
                    ),
                }
            )

        if self._summary:
            msgs.append(
                {
                    "role": "user",
                    "content": f"[Previous conversation summary]\n{self._summary}",
                }
            )
            msgs.append(
                {
                    "role": "assistant",
                    "content": "Understood. I have the context from our previous conversation.",
                }
            )
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
        return extra + self._msg_chars

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
            prompt_text = f"Conversation to summarize:\n\n{serialized}"
            system = SUMMARIZATION_PROMPT
        else:
            # Incremental update: add to existing summary
            prompt_text = INCREMENTAL_UPDATE_PROMPT.format(
                existing_summary=self._summary,
                new_messages=serialized,
            )
            system = (
                "You are a summarization assistant. Follow the instructions exactly."
            )

        try:
            response = self.provider.call(
                messages=[{"role": "user", "content": prompt_text}],
                system=system,
                model=self.model,
                capabilities=self.capabilities,
            )
            self._summary = response.content
            self.messages = kept_msgs
            self._msg_chars = sum(len(m["content"]) for m in kept_msgs)
        except Exception as e:
            # Compression failed — temporarily raise threshold to avoid retry loop
            import sys

            print(f"[warn] Context compression failed: {e}", file=sys.stderr)
            self.max_context_chars = int(self.max_context_chars * 1.5)

    def _serialize_messages(self, messages: list[dict]) -> str:
        """Serialize messages to text format for summarization (pi-mono pattern)."""
        parts = []
        for m in messages:
            role = m.get("role", "unknown").capitalize()
            content = m.get("content", "")
            # Truncate very long tool results for summarization
            if len(content) > 2000:
                content = (
                    content[:2000]
                    + f"\n[... {len(content) - 2000} more characters truncated]"
                )
            parts.append(f"[{role}]: {content}")
        return "\n\n".join(parts)

    # ── Scratchpad integration ────────────────────────────────

    def set_skill_context(self, skill_name: str = "", parent_turn: int = 0) -> None:
        """Set skill context for artifact subdirectory routing."""
        self._skill_name = skill_name
        self._skill_parent_turn = parent_turn

    def begin_turn(self, query: str, tags: list[str] | None = None) -> dict:
        """Begin a turn: load scratchpad + select relevant artifacts.

        Returns a dict with context info for debugging/logging.
        """
        self._turn_count += 1
        self._current_tags = tags or []

        return {
            "scratchpad_loaded": True,
            "artifacts_loaded": 0,
            "budget": self._budget.to_dict(),
        }

    def end_turn(
        self,
        content: str,
        tags: list[str] | None = None,
        summary: str = "",
        decision: str | None = None,
    ) -> str | None:
        """End a turn: save artifact + update scratchpad.

        Returns the artifact path if saved, None otherwise.
        """
        from agent_cli.context.scratchpad import (
            append_decision,
            append_progress,
            save_artifact,
        )

        # Extract skill_name from tags if present
        skill_name = ""
        if tags:
            for t in tags:
                if t.startswith("skill:"):
                    skill_name = t[6:]
                    break

        # Save result as artifact (always)
        artifact_path = None
        if content:
            artifact_path = save_artifact(
                turn=self._turn_count,
                content=content,
                tags=tags,
                summary=summary,
                base=self._scratchpad_dir,
                skill_name=skill_name,
                parent_turn=self._skill_parent_turn,
            )

        # Update scratchpad progress
        if summary:
            append_progress(
                turn=self._turn_count,
                summary=summary,
                artifact_path=artifact_path,
                base=self._scratchpad_dir,
            )

        # Record decision if any
        if decision:
            append_decision(
                turn=self._turn_count,
                decision=decision,
                base=self._scratchpad_dir,
            )

        return artifact_path

    def init_task(self, goal: str) -> None:
        """Initialize scratchpad for a new task."""
        from agent_cli.context.scratchpad import init_scratchpad

        init_scratchpad(goal, self._scratchpad_dir)

    def _build_scratchpad_block(self) -> str:
        """Build the scratchpad context block for injection into messages."""
        from agent_cli.context.scratchpad import (
            build_artifact_index,
            load_artifact,
            load_scratchpad,
            select_artifacts,
        )

        parts = []

        # 1. Scratchpad (always loaded)
        scratchpad = load_scratchpad(self._scratchpad_dir)
        if scratchpad:
            # Truncate if exceeds budget
            max_chars = self._budget.scratchpad_tokens * CHARS_PER_TOKEN
            if len(scratchpad) > max_chars:
                scratchpad = scratchpad[:max_chars] + "\n[... scratchpad truncated]"
            parts.append(f"[Scratchpad — persistent task context]\n{scratchpad}")

        # 2. Selected artifacts (within budget)
        index = build_artifact_index(self._scratchpad_dir)
        if index:
            current_tags = getattr(self, "_current_tags", [])
            selected = select_artifacts(
                index=index,
                current_tags=current_tags,
                budget_tokens=self._budget.artifact_tokens,
            )
            for meta in selected:
                _, body = load_artifact(meta.path)
                if body:
                    header = (
                        f"[Artifact {meta.entry_id}] {meta.summary}"
                        if meta.summary
                        else f"[Artifact {meta.entry_id}]"
                    )
                    parts.append(f"{header}\n{body}")

        return "\n\n---\n\n".join(parts) if parts else ""

    def get_budget_info(self) -> dict:
        """Return current token budget allocation (for /ctx_window display)."""
        return {
            "mode": "scratchpad",
            "budget": self._budget.to_dict(),
            "turn_count": self._turn_count,
        }
