"""Shared constants for agent-cli."""

# ── Timeout values (seconds) ──────────────────
SHELL_COMMAND_TIMEOUT = 30
LLM_API_TIMEOUT = 600
OLLAMA_DETECT_TIMEOUT = 10
DELEGATE_DEFAULT_TIMEOUT = 300

# ── Context window thresholds ─────────────────
SMALL_MODEL_CONTEXT = 8192
MEDIUM_MODEL_CONTEXT = 32768

# ── Token estimation ─────────────────────────
CHARS_PER_TOKEN = 4
OVERFLOW_RESERVE_TOKENS = 2048
CONTEXT_RESERVE_RATIO = 0.95  # Reserve 5% for system prompt + current turn

# ── Observation message templates ──────────────
OBS_SUCCESS = "STATUS: success\nRESULT:\n{result}"
OBS_ERROR = "STATUS: error\nERROR: {error}"
OBS_ERROR_HINT = "STATUS: error\nERROR: {error}\nHINT: {hint}"

# ── Plan step status ──────────────────────────
STEP_PENDING = "pending"
STEP_IN_PROGRESS = "in_progress"
STEP_DONE = "done"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"
