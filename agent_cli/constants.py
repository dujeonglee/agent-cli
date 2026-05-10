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
# user queries — they're loop-emitted notifications/hints.
#
# Per-format retry hints (parse-fail, no-action) live on the wire-format
# plugin: ``ReActFormat.static_retry_hint_no_*()``. The unified prefix
# list for filtering system messages out of resume previews lives at
# ``agent_cli.wire_formats.all_system_user_prefixes()``.
INTERRUPT_NOTICE = "⚡ User interrupted. Waiting for new instructions."
