"""Context manager with FIFO message queue and history.jsonl persistence.

Stores full conversation in history.jsonl (JSON Lines, append-only).
Maintains an in-memory FIFO cache of the last N messages.
Converts JSON records to natural language for LLM consumption.

No LLM-based compression. No scratchpad. No artifact injection.
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path


# ── Default FIFO size ────────────────────────────────
DEFAULT_FIFO_SIZE = 100


class ContextManager:
    """Manages conversation history with FIFO + history.jsonl persistence.

    - Stores every message as a JSON line in history.jsonl (append-only).
    - Keeps the last N messages in memory for fast access.
    - Converts JSON records to natural language when building LLM messages.
    - On session resume, restores cache from history.jsonl tail.
    """

    def __init__(
        self,
        # New API: session_dir as first positional arg
        # Legacy API: provider, model, capabilities as first 3 positional args
        *args,
        session_dir: Path | None = None,
        fifo_size: int = DEFAULT_FIFO_SIZE,
        resume: bool = False,
        # Legacy kwargs
        provider=None,
        model: str = "",
        capabilities=None,
        keep_recent: int = 4,
        session_id: str | None = None,
        scratchpad_base: Path | None = None,
        scratchpad_dir: Path | None = None,
    ):
        # Detect call style from positional args
        if args:
            first = args[0]
            if isinstance(first, Path) or (
                isinstance(first, str) and "/" in str(first)
            ):
                # New API: ContextManager(session_dir, fifo_size=...)
                session_dir = Path(first)
                if len(args) > 1:
                    fifo_size = args[1]
            else:
                # Legacy API: ContextManager(provider, model, capabilities, ...)
                provider = first
                if len(args) > 1:
                    model = args[1]
                if len(args) > 2:
                    capabilities = args[2]
                if len(args) > 3:
                    pass  # Legacy: keep_recent ignored

        # Resolve session_dir from legacy params if not provided directly
        if session_dir is None:
            if scratchpad_dir is not None:
                session_dir = Path(scratchpad_dir)
            elif session_id is not None:
                base = scratchpad_base or Path(".agent-cli")
                session_dir = base / "sessions" / session_id
            else:
                raise ValueError(
                    "session_dir or session_id is required for ContextManager."
                )

        self.session_dir = Path(session_dir)
        self.fifo_size = fifo_size
        self._cache: deque[dict] = deque(maxlen=fifo_size)
        self._history_path = self.session_dir / "history.jsonl"

        # Legacy attributes (for callers that still reference them)
        self.provider = provider
        self.model = model
        self.capabilities = capabilities
        self.messages: list[dict] = []  # Legacy: some code reads ctx.messages

        # Ensure session directory exists
        self.session_dir.mkdir(parents=True, exist_ok=True)

        if resume and self._history_path.is_file():
            self._restore_cache()

    # ── Public API ────────────────────────────────────

    def add(self, message_or_role, content: str | None = None) -> None:
        """Add a message to cache and persist to history.jsonl.

        Supports two call styles:
            add({"role": "user", "content": "hello"})   # New API (dict)
            add("user", "hello")                         # Legacy API (role, content)
        """
        if isinstance(message_or_role, str):
            # Legacy: add("role", "content")
            message = {"role": message_or_role, "content": content or ""}
        else:
            message = message_or_role
        self._cache.append(message)
        self._append_to_history(message)

    def get_messages(self) -> list[dict]:
        """Return cached messages converted to natural language for LLM.

        Returns a list of {"role": ..., "content": ...} dicts suitable
        for chat completion API calls.
        """
        return [_to_natural_language(msg) for msg in self._cache]

    def get_raw_messages(self) -> list[dict]:
        """Return cached messages as raw JSON dicts (no conversion)."""
        return list(self._cache)

    @property
    def history_path(self) -> Path:
        """Path to this context's history.jsonl file."""
        return self._history_path

    # ── Persistence ───────────────────────────────────

    def _append_to_history(self, message: dict) -> None:
        """Append a single JSON line to history.jsonl."""
        with open(self._history_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(message, ensure_ascii=False) + "\n")

    def _restore_cache(self) -> None:
        """Read history.jsonl and load the last N messages into cache."""
        lines: list[str] = []
        with open(self._history_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        # Take last N lines
        tail = lines[-self.fifo_size :] if len(lines) > self.fifo_size else lines
        for line in tail:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                self._cache.append(msg)
            except json.JSONDecodeError:
                continue

    # ── Legacy API bridge ──────────────────────────────
    # These methods maintain backward compatibility with old ContextManager API.
    # They will be removed once all callers are migrated to the new API.

    def add_legacy(self, role: str, content: str) -> None:
        """Legacy add(role, content) → new add(dict).

        Stores as a simple {"role": ..., "content": ...} message.
        Callers should migrate to add(dict) with structured fields.
        """
        self.add({"role": role, "content": content})

    def force_compress(self, user_instruction: str = "") -> None:
        """No-op. FIFO replaces compression."""
        pass

    def get_estimated_tokens(self) -> int:
        """Rough token estimate from cache."""
        total_chars = sum(
            len(str(m.get("content", ""))) + len(str(m.get("thought", "")))
            for m in self._cache
        )
        return total_chars // 4 + 1

    def begin_turn(self, query: str, tags: list[str] | None = None) -> dict:
        """No-op. Scratchpad removed."""
        return {}

    def end_turn(self, **kwargs) -> None:
        """No-op. Scratchpad removed."""
        return None

    def init_task(self) -> None:
        """No-op. Scratchpad removed."""
        pass

    def set_dispatch_context(self, name: str = "", parent_step: int = 0) -> None:
        """No-op. Scratchpad removed."""
        pass

    @property
    def _scratchpad_dir(self) -> Path:
        """Legacy alias for session_dir."""
        return self.session_dir

    @property
    def _step_count(self) -> int:
        """Legacy stub. Always 0."""
        return 0

    @staticmethod
    def _extract_files_touched(messages: list[dict]) -> tuple[set[str], set[str]]:
        """Legacy stub. Returns empty sets."""
        return set(), set()

    def get_budget_info(self) -> dict:
        """Legacy stub."""
        return {"mode": "fifo", "fifo_size": self.fifo_size}

    # ── Fork support ──────────────────────────────────

    def fork_history_to(self, target_dir: Path) -> Path:
        """Copy this context's history.jsonl to target_dir for fork mode.

        Returns the path to the copied history.jsonl.
        """
        import shutil

        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / "history.jsonl"
        if self._history_path.is_file():
            shutil.copy2(self._history_path, target_path)
        return target_path


# ── Natural language conversion ───────────────────────


def _to_natural_language(msg: dict) -> dict:
    """Convert a JSON history record to a natural language message for LLM.

    Input formats (from history.jsonl):
        User input:     {"role":"user", "content":"..."}
        Tool result:    {"role":"user", "tool":"read_file", "args":{...}, "content":"...", "artifact":"..."}
        Assistant act:  {"role":"assistant", "thought":"...", "action":"...", "action_input":{...}}
        Complete:       {"role":"assistant", "thought":"...", "action":"complete", "action_input":{"result":"..."}}

    Output format (for chat completion):
        {"role": "user"|"assistant", "content": "...natural language..."}
    """
    role = msg.get("role", "user")

    # User messages
    if role == "user":
        tool = msg.get("tool")
        if tool:
            return _convert_observation(msg)
        return {"role": "user", "content": msg.get("content", "")}

    # Assistant messages
    thought = msg.get("thought", "")
    action = msg.get("action", "")
    action_input = msg.get("action_input", {})

    if action == "complete":
        # Final answer: thought + result, no action wrapper
        result = ""
        if isinstance(action_input, dict):
            result = action_input.get("result", "")
        elif isinstance(action_input, str):
            result = action_input
        content = f"{thought}. {result}" if thought else result
        return {"role": "assistant", "content": content.strip()}

    if action:
        # Tool call: thought + action summary
        args_summary = _summarize_action_args(action, action_input)
        content = (
            f"{thought}. → {action}({args_summary})"
            if thought
            else f"→ {action}({args_summary})"
        )
        return {"role": "assistant", "content": content.strip()}

    # Plain assistant message (fallback)
    content = msg.get("content", thought)
    return {"role": "assistant", "content": content}


def _convert_observation(msg: dict) -> dict:
    """Convert a tool result message to natural language."""
    tool = msg.get("tool", "")
    content = msg.get("content", "")
    artifact = msg.get("artifact", "")
    args = msg.get("args", {})

    # Build header
    if isinstance(args, dict) and args:
        # Pick the most relevant arg for summary
        arg_summary = _summarize_tool_args(tool, args)
        header = f"[{tool}] {arg_summary}"
    else:
        header = f"[{tool}]"

    # Build body
    parts = [header]
    if content:
        parts.append(content)
    if artifact:
        parts.append(f"→ {artifact}")

    return {"role": "user", "content": "\n".join(parts)}


def _summarize_action_args(action: str, action_input) -> str:
    """Summarize action_input for the → action(...) display."""
    if not isinstance(action_input, dict):
        return str(action_input)[:80] if action_input else ""

    if action in ("read_file", "write_file", "edit_file"):
        return action_input.get("path", "")
    if action == "shell":
        cmd = action_input.get("command", "")
        return cmd[:60] if cmd else ""
    if action == "delegate":
        tasks = action_input.get("tasks", [])
        if tasks and isinstance(tasks, list):
            first = tasks[0] if isinstance(tasks[0], dict) else {}
            agent = first.get("agent", "")
            task = first.get("task", "")[:40]
            if len(tasks) > 1:
                return f'{agent}, "{task}" +{len(tasks) - 1} more'
            return f'{agent}, "{task}"'
        return ""
    if action == "run_skill":
        name = action_input.get("name", "")
        arguments = action_input.get("arguments", "")
        return f"{name}({arguments})" if arguments else name

    # Generic: first string value
    for v in action_input.values():
        if isinstance(v, str) and v:
            return v[:60]
    return ""


def _summarize_tool_args(tool: str, args: dict) -> str:
    """Summarize tool args for the [{tool}] header."""
    if tool in ("read_file", "write_file", "edit_file"):
        return args.get("path", "")
    if tool == "shell":
        return args.get("command", "")[:60]
    if tool == "delegate":
        agent = args.get("agent", "")
        return agent
    if tool == "run_skill":
        return args.get("name", "")
    # Generic
    for v in args.values():
        if isinstance(v, str) and v:
            return v[:60]
    return ""
