"""Shared constants for agent-cli."""

# ── Timeout values (seconds) ──────────────────
SHELL_COMMAND_TIMEOUT = 30
LLM_API_TIMEOUT = 1200
DELEGATE_DEFAULT_TIMEOUT = 300

# ── Token estimation ─────────────────────────
CHARS_PER_TOKEN = 4
OVERFLOW_RESERVE_TOKENS = 2048

# ── Observation message templates ──────────────
OBS_SUCCESS = "STATUS: success\nRESULT:\n{result}"
OBS_ERROR = "STATUS: error\nERROR: {error}"
OBS_ERROR_HINT = "STATUS: error\nERROR: {error}\nHINT: {hint}"

# ── System-injected user messages ───────────────
# These get persisted as role=user in history.jsonl but are NOT actual
# user queries — they're loop-emitted notifications/hints. `recent_exchanges`
# uses these prefixes to skip them when surfacing the resume preview.
RETRY_HINT_NO_JSON = (
    "Your response was not valid JSON. "
    "Output ONLY a JSON object: "
    '{"thought": "...", "action": "tool_name", "action_input": {...}}. '
    "No markdown fences, no extra text."
)
RETRY_HINT_NO_ACTION = (
    "Your JSON was parsed but has no action. "
    "You MUST include an action. Either use a tool: "
    '{"thought": "...", "action": "tool_name", "action_input": {...}} '
    "or complete the task: "
    '{"thought": "...", "action": "complete", "action_input": {"result": "..."}}'
)
INTERRUPT_NOTICE = "⚡ User interrupted. Waiting for new instructions."
SYSTEM_USER_PREFIXES: tuple[str, ...] = (
    "Your response was not valid JSON.",
    "Your JSON was parsed but has no action.",
    "⚡ User interrupted.",
    # B1 (action loop) interventions — both messages start with one of
    # these phrases (probe_progress / restate_task respectively).
    "You have called",
    "You were asked to:",
)
