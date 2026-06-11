"""Shared constants for agent-cli."""

# ── Timeout values (seconds) ──────────────────
SHELL_COMMAND_TIMEOUT = 30
# LLM request timeout as a requests ``(connect, read)`` tuple:
#   - connect (30s): TCP/TLS connection establishment. Short so a DOWN /
#     unreachable server fails fast instead of blocking — ConnectTimeout is
#     retried up to AGENT_CLI_LLM_RETRY_ATTEMPTS times (default 10).
#   - read (1200s = 20min): time between bytes once connected. NOTE: requests
#     folds the HEADER wait into the read timeout (not connect), so this also
#     bounds time-to-first-token. Generous so a slow cold 27B (large prompt =
#     long TTFT) is NOT killed mid-generation. A connected-but-stalled stream
#     still waits up to read before failing.
LLM_CONNECT_TIMEOUT = 30
LLM_READ_TIMEOUT = 1200
LLM_API_TIMEOUT = (LLM_CONNECT_TIMEOUT, LLM_READ_TIMEOUT)
DELEGATE_DEFAULT_TIMEOUT = 300
# First-run capability detection probes (thinking support, JSON-format
# tolerance, context-window overflow). All run once per model and may
# incur a cold model load, so they share a generous allowance distinct
# from SHELL_COMMAND_TIMEOUT (which is for user shell commands, not
# probes).
DETECTION_PROBE_TIMEOUT = 60

# ── Token estimation ─────────────────────────
OVERFLOW_RESERVE_TOKENS = 2048

# ── Observation message templates ──────────────
OBS_SUCCESS = "STATUS: success\nRESULT:\n{result}"

# ── System-injected user messages ───────────────
# These get persisted as role=user in history.jsonl but are NOT actual
# user queries — they're loop-emitted notifications/hints.
#
# Per-format retry hints (parse-fail, no-action) live on the wire-format
# plugin: ``ReActFormat.static_retry_hint_no_*()``. The unified prefix
# list for filtering system messages out of resume previews lives at
# ``agent_cli.wire_formats.all_system_user_prefixes()``.
INTERRUPT_NOTICE = "⚡ User interrupted. Waiting for new instructions."

# Shown as an observation when a response hits the model's output-token
# limit (stop_reason == "length"). The truncated action is NOT executed
# — the loop records this so the model retries with a smaller unit.
OUTPUT_TRUNCATED_NOTICE = (
    "⚠️ Your previous response was cut off at the output-token limit, so "
    "its action was incomplete and was NOT executed. Retry with a smaller "
    "unit — e.g. build a large file incrementally with edit_file instead "
    "of one big write_file."
)
