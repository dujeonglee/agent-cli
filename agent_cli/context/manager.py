"""Context manager with FIFO message queue and history.jsonl persistence.

Stores full conversation in history.jsonl (JSON Lines, append-only).
Maintains an in-memory FIFO cache of the last N messages.
Converts JSON records to natural language for LLM consumption.
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path


# ── Default FIFO size ──────────────────────────���─────
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
        session_dir: Path,
        fifo_size: int = DEFAULT_FIFO_SIZE,
        *,
        resume: bool = False,
    ):
        self.session_dir = Path(session_dir)
        self.fifo_size = fifo_size if fifo_size > 0 else DEFAULT_FIFO_SIZE
        self._cache: deque[dict] = deque(maxlen=fifo_size)
        self._history_path = self.session_dir / "history.jsonl"

        self.session_dir.mkdir(parents=True, exist_ok=True)

        if resume and self._history_path.is_file():
            self._restore_cache()

    # ── Public API ────────────────────────────────────

    def add(self, message: dict) -> None:
        """Add a message to cache and persist to history.jsonl.

        Args:
            message: A dict with at minimum {"role": "user"|"assistant", ...}.
                     See DESIGN.md section 4.5 for full schema.
        """
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

    def get_estimated_tokens(self) -> int:
        """Rough token estimate from cache."""
        total_chars = sum(
            len(str(m.get("content", ""))) + len(str(m.get("thought", "")))
            for m in self._cache
        )
        return total_chars // 4 + 1

    @property
    def history_path(self) -> Path:
        """Path to this context's history.jsonl file."""
        return self._history_path

    # ── Persistence ──��────────────────────────────────

    def _append_to_history(self, message: dict) -> None:
        """Append a single JSON line to history.jsonl."""
        with open(self._history_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(message, ensure_ascii=False) + "\n")

    def _restore_cache(self) -> None:
        """Read history.jsonl and load the last N messages into cache."""
        with open(self._history_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        tail = lines[-self.fifo_size :] if len(lines) > self.fifo_size else lines
        for line in tail:
            line = line.strip()
            if not line:
                continue
            try:
                self._cache.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # ── Fork support ─────���────────────────────────────

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
