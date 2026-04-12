"""Context manager with token-budget FIFO and history.jsonl persistence.

Stores full conversation in history.jsonl (JSON Lines, append-only).
Maintains an in-memory context cache that fits within a token budget.
Converts JSON records to natural language for LLM consumption.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_cli.context.token_estimator import estimate_tokens


# ── Default token budget ─────────────────────────────────
# Used when no budget is specified and no capabilities are provided.
DEFAULT_TOKEN_BUDGET = 100_000


def compute_token_budget(context_window: int, max_output_tokens: int) -> int:
    """Compute context token budget from model capabilities.

    budget = context_window - max_output_tokens - system_prompt_reserve
    System prompt reserve is estimated at 4000 tokens.
    """
    reserve = max_output_tokens + 4000
    budget = context_window - reserve
    return max(budget, 4000)  # floor: at least 4K tokens


class ContextManager:
    """Manages conversation history with token-budget FIFO + history.jsonl.

    - Stores every message as a JSON line in history.jsonl (append-only).
    - Keeps recent messages in memory within a token budget.
    - Converts JSON records to natural language when building LLM messages.
    - On session resume, restores cache from history.jsonl tail.
    """

    def __init__(
        self,
        session_dir: Path,
        max_context_tokens: int = 0,
        *,
        resume: bool = False,
    ):
        self.session_dir = Path(session_dir)
        self.max_context_tokens = (
            max_context_tokens if max_context_tokens > 0 else DEFAULT_TOKEN_BUDGET
        )
        self._cache: list[dict] = []
        self._cache_tokens: int = 0
        self._history_path = self.session_dir / "history.jsonl"

        self.session_dir.mkdir(parents=True, exist_ok=True)

        if resume and self._history_path.is_file():
            self._restore_cache()

    # ── Public API ────────────────────────────────────

    def add(self, message: dict) -> None:
        """Add a message to cache and persist to history.jsonl.

        If adding the message exceeds the token budget, older messages
        are dropped (whole messages only, never truncated).
        """
        msg_tokens = _estimate_message_tokens(message)
        self._cache.append(message)
        self._cache_tokens += msg_tokens
        self._evict()
        self._append_to_history(message)

    def get_messages(self) -> list[dict]:
        """Return cached messages converted to natural language for LLM."""
        return [_to_natural_language(msg) for msg in self._cache]

    def get_raw_messages(self) -> list[dict]:
        """Return cached messages as raw JSON dicts (no conversion)."""
        return list(self._cache)

    def get_estimated_tokens(self) -> int:
        """Current estimated token count of the cache."""
        return self._cache_tokens

    @property
    def history_path(self) -> Path:
        """Path to this context's history.jsonl file."""
        return self._history_path

    # ── Eviction ─────────────────────────────────────

    def _evict(self) -> None:
        """Drop oldest messages until cache fits within token budget."""
        while self._cache_tokens > self.max_context_tokens and len(self._cache) > 1:
            removed = self._cache.pop(0)
            self._cache_tokens -= _estimate_message_tokens(removed)

    # ── Persistence ──────────────────────────────────

    def _append_to_history(self, message: dict) -> None:
        """Append a single JSON line to history.jsonl."""
        with open(self._history_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(message, ensure_ascii=False) + "\n")

    def _restore_cache(self) -> None:
        """Read history.jsonl and load messages within token budget."""
        with open(self._history_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        # Load from end, respecting token budget
        messages: list[dict] = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            messages.append(msg)

        # Reverse to chronological order, then add within budget
        messages.reverse()
        total = 0
        start_idx = 0
        for i, msg in enumerate(messages):
            total += _estimate_message_tokens(msg)
        # If total fits, use all; otherwise find the cut point
        if total > self.max_context_tokens:
            # Drop from the front until it fits
            running = total
            for i, msg in enumerate(messages):
                if running <= self.max_context_tokens:
                    start_idx = i
                    break
                running -= _estimate_message_tokens(msg)
            else:
                start_idx = len(messages) - 1

        self._cache = messages[start_idx:]
        self._cache_tokens = sum(_estimate_message_tokens(m) for m in self._cache)

    # ── Fork support ─────────────────────────────────

    def fork_history_to(self, target_dir: Path) -> Path:
        """Copy this context's history.jsonl to target_dir for fork mode."""
        import shutil

        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / "history.jsonl"
        if self._history_path.is_file():
            shutil.copy2(self._history_path, target_path)
        return target_path


# ── Token estimation ─────────────────────────────────


def _estimate_message_tokens(msg: dict) -> int:
    """Estimate tokens for a single message dict."""
    total = 4  # role + formatting overhead
    for key in ("content", "thought", "action_input"):
        val = msg.get(key)
        if val is None:
            continue
        if isinstance(val, str):
            total += estimate_tokens(val)
        elif isinstance(val, dict):
            total += estimate_tokens(json.dumps(val, ensure_ascii=False))
    action = msg.get("action", "")
    if action:
        total += estimate_tokens(action)
    artifact = msg.get("artifact", "")
    if artifact:
        total += estimate_tokens(artifact)
    return total


# ── Natural language conversion ───────────────────────


def _to_natural_language(msg: dict) -> dict:
    """Convert a JSON history record to a natural language message for LLM.

    Input formats (from history.jsonl):
        User input:     {"role":"user", "content":"..."}
        Tool result:    {"role":"user", "tool":"...", "args":{...}, "content":"...", "artifact":"..."}
        Assistant act:  {"role":"assistant", "thought":"...", "action":"...", "action_input":{...}}
        Complete:       {"role":"assistant", "thought":"...", "action":"complete", "action_input":{"result":"..."}}

    Output format (for chat completion):
        {"role": "user"|"assistant", "content": "...natural language..."}
    """
    role = msg.get("role", "user")

    if role == "user":
        tool = msg.get("tool")
        if tool:
            return _convert_observation(msg)
        return {"role": "user", "content": msg.get("content", "")}

    thought = msg.get("thought", "")
    action = msg.get("action", "")
    action_input = msg.get("action_input", {})

    if action == "complete":
        result = ""
        if isinstance(action_input, dict):
            result = action_input.get("result", "")
        elif isinstance(action_input, str):
            result = action_input
        if thought:
            content = f"thought: {thought}\n\n{result}"
        else:
            content = result
        return {"role": "assistant", "content": content.strip()}

    if action:
        args_summary = _summarize_action_args(action, action_input)
        parts = []
        if thought:
            parts.append(f"thought: {thought}")
        parts.append(f"action: {action}({args_summary})")
        return {"role": "assistant", "content": "\n".join(parts)}

    content = msg.get("content", thought)
    return {"role": "assistant", "content": content}


def _convert_observation(msg: dict) -> dict:
    """Convert a tool result message to natural language."""
    tool = msg.get("tool", "")
    content = msg.get("content", "")
    artifact = msg.get("artifact", "")
    args = msg.get("args", {})

    if isinstance(args, dict) and args:
        arg_summary = _summarize_tool_args(tool, args)
        header = f"[{tool}] {arg_summary}"
    else:
        header = f"[{tool}]"

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
        return args.get("agent", "")
    if tool == "run_skill":
        return args.get("name", "")
    for v in args.values():
        if isinstance(v, str) and v:
            return v[:60]
    return ""
