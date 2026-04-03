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
        self._original_max_context_chars = self.max_context_chars
        self._compress_failures = 0
        self._max_compress_failures = 3

    def add(self, role: str, content: str) -> None:
        """Add a message and trigger compression if needed."""
        self.messages.append({"role": role, "content": content})
        self._msg_chars += len(content)
        if self._total_chars() > self.max_context_chars:
            self._compress()

    def get_messages(self) -> list[dict]:
        """Return messages with summary and scratchpad context prepended."""
        msgs: list[dict] = []

        # Scratchpad anchor: injected unless inside a skill execution
        # (skill internal loops should not see the outer task's scratchpad)
        if not self._skill_name:
            scratchpad_block = self._build_scratchpad_block()
        else:
            scratchpad_block = ""
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
                    "content": (
                        "This conversation is being continued from earlier context "
                        "that was compressed. The summary below covers the prior "
                        "portion of the conversation.\n\n"
                        f"Summary:\n{self._summary}\n\n"
                        "Recent messages are preserved verbatim below. "
                        "Continue the conversation from where it left off — "
                        "do not acknowledge the summary, do not recap what was "
                        "happening, and do not ask clarifying questions about "
                        "prior context. Resume directly."
                    ),
                }
            )
            msgs.append(
                {
                    "role": "assistant",
                    "content": "Understood. Resuming where we left off.",
                }
            )
        msgs.extend(self.messages)
        return msgs

    def force_compress(self, user_instruction: str = "") -> None:
        """Trigger compression immediately (for overflow recovery or /compact)."""
        if len(self.messages) > self.keep_recent * 2:
            self._compress(user_instruction=user_instruction)

    def get_estimated_tokens(self) -> int:
        """Estimate current buffer size in tokens."""
        return estimate_tokens_from_messages(self.get_messages())

    def _total_chars(self) -> int:
        extra = len(self._summary) if self._summary else 0
        return extra + self._msg_chars

    def _compress(self, user_instruction: str = "") -> None:
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

        if user_instruction:
            system += f"\n\n## Additional Instruction\n{user_instruction}"

        try:
            response = self.provider.call(
                messages=[{"role": "user", "content": prompt_text}],
                system=system,
                model=self.model,
                capabilities=self.capabilities,
                skip_json_format=True,
            )
            self._summary = response.content
            # Add artifact recovery hint after compaction
            artifact_hint = (
                "[Context was compressed. Previous tool outputs are no longer "
                "in conversation history. Check the scratchpad Progress section "
                "for artifact paths — use read_file to reload details if needed.]"
            )
            self._summary = f"{self._summary}\n\n{artifact_hint}"
            self.messages = kept_msgs
            self._msg_chars = sum(len(m["content"]) for m in kept_msgs)
            # Reset failure tracking on success
            self._compress_failures = 0
            self.max_context_chars = self._original_max_context_chars
        except Exception as e:
            self._compress_failures += 1
            import sys

            if self._compress_failures <= self._max_compress_failures:
                # Raise threshold temporarily, capped at 2x original
                new_limit = min(
                    int(self.max_context_chars * 1.3),
                    self._original_max_context_chars * 2,
                )
                self.max_context_chars = new_limit
                print(
                    f"[warn] Context compression failed ({self._compress_failures}/"
                    f"{self._max_compress_failures}): {e}",
                    file=sys.stderr,
                )
            else:
                # Too many failures — alert user via Rich console
                from agent_cli.render import console, C

                console.print(
                    f"[{C['error']}]Context compression failed "
                    f"{self._compress_failures} times. "
                    f"Context may grow unbounded.[/]"
                )

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

    def init_task(self) -> None:
        """Initialize scratchpad."""
        from agent_cli.context.scratchpad import init_scratchpad

        init_scratchpad(self._scratchpad_dir)

    def _build_scratchpad_block(self) -> str:
        """Build the scratchpad context block for injection into messages."""
        from agent_cli.context.scratchpad import (
            load_scratchpad,
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

        # 2. Artifact injection — disabled for now.
        # Artifacts are saved to disk for persistence/recovery but NOT injected
        # into context. Raw tool output (hashlines, STATUS: prefixes) confuses LLM
        # when mixed with conversation. Scratchpad progress references are sufficient.
        # TODO: Re-enable with proper summarization (not raw tool output).

        return "\n\n---\n\n".join(parts) if parts else ""

    def get_budget_info(self) -> dict:
        """Return current token budget allocation (for /ctx_window display)."""
        return {
            "mode": "scratchpad",
            "budget": self._budget.to_dict(),
            "turn_count": self._turn_count,
        }
