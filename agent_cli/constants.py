"""Shared constants for agent-cli."""

# ── Timeout values (seconds) ──────────────────
SHELL_COMMAND_TIMEOUT = 30
# LLM request timeout as a requests ``(connect, read)`` tuple.
#   - connect (30s): TCP/TLS connection establishment. Short so a DOWN /
#     unreachable server fails fast — ConnectTimeout is retried up to
#     AGENT_CLI_LLM_RETRY_ATTEMPTS times (default 10).
#   - read: time between bytes once connected.
# Two profiles:
#   - LLM_API_TIMEOUT (non-streaming, read=1200s): the post() reads the whole
#     body, so the read timeout is the full-generation idle bound — generous so
#     a slow cold 27B isn't killed mid-generation.
#   - LLM_STREAM_TIMEOUT (streaming, read=30s): the post() only reads the
#     response HEADERS, so 30s bounds the header wait (a broken server that
#     never responds fails fast + retries, instead of the old ~20min hang) AND
#     interrupt during the header wait. After post() returns, the provider
#     RESETS the socket read timeout to patient (LLM_READ_TIMEOUT) so body reads
#     don't inherit the 30s — the poll-loop idle detector owns body stalls. (A
#     single socket timeout can't be both short-for-header and patient-for-body;
#     the reset is how we get both. Empirically verified; best-effort with a
#     fallback to the configured read timeout if the urllib3 socket is
#     unreachable.)
LLM_CONNECT_TIMEOUT = 30
LLM_READ_TIMEOUT = 1200
LLM_STREAM_READ_TIMEOUT = 30
LLM_API_TIMEOUT = (LLM_CONNECT_TIMEOUT, LLM_READ_TIMEOUT)
LLM_STREAM_TIMEOUT = (LLM_CONNECT_TIMEOUT, LLM_STREAM_READ_TIMEOUT)
# Streaming idle/stall handling (poll-loop on the patient body socket):
#   - every STREAM_IDLE_THRESHOLD seconds with no token, render a notice
#     (visible "still waiting" feedback) — resets when a token arrives.
#   - after STREAM_IDLE_MAX_TICKS consecutive idle intervals (20*30s = 10min of
#     total silence) the connection is closed and the request re-sent
#     (StreamIdleTimeout), up to STREAM_MAX_RECONNECTS times before hard-fail.
#     A re-send RESTARTS generation (no server-side resume); a 10-min-silent
#     stream is dead anyway. Interrupt is independent (polled every 0.2s).
STREAM_IDLE_THRESHOLD = 30
STREAM_IDLE_MAX_TICKS = 20
STREAM_MAX_RECONNECTS = 3
# P3 (v8.55.0): 스트림 무진전(no-token) 한도의 기본/상한. 유휴는 "마지막
# **진전**(실제 토큰/종결 이벤트) 이후"로 잰다 — keep-alive 프레임은 진전이
# 아니다(omlx hang 실측 2회: keep-alive 가 종전 줄-기준 감지를 무력화).
# 사용자 값은 ctx.stream_idle_timeout_s(web "Stall" / env
# AGENT_CLI_STREAM_IDLE_TIMEOUT_S) → CallSettings 로 매 콜 전달, 0=감지 끔.
DEFAULT_STREAM_IDLE_TIMEOUT_S = STREAM_IDLE_THRESHOLD * STREAM_IDLE_MAX_TICKS
STREAM_IDLE_TIMEOUT_MAX_S = 3600
AGENT_DEFAULT_TIMEOUT = 300
# First-run capability detection probes (thinking support, JSON-format
# tolerance, context-window overflow). All run once per model and may
# incur a cold model load, so they share a generous allowance distinct
# from SHELL_COMMAND_TIMEOUT (which is for user shell commands, not
# probes).
DETECTION_PROBE_TIMEOUT = 60

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

# Injected as a role=user notice immediately BEFORE a mid-run queued user
# request (web steering, ``LoopState._inject_queued_messages``). Without it the
# model tends to answer only the most-recent user turn and silently drop an
# earlier request it had not answered yet (e.g. it was still doing a tool call
# for it when the next request was queued). Its prefix is registered in
# ``wire_formats._FORMAT_AGNOSTIC_USER_PREFIXES`` so resume/history filtering
# excludes the notice from previews.
QUEUED_REQUEST_NOTICE = (
    "⚡ Another user request arrived while you were mid-task. Address EVERY "
    "outstanding request in this run — including any earlier one you have not "
    "answered yet — instead of replying only to the most recent and skipping "
    "the rest."
)

# Shown as an observation when a response hits the model's output-token
# limit (stop_reason == "length"). The truncated action is NOT executed
# — the loop records this so the model retries with a smaller unit.
OUTPUT_TRUNCATED_NOTICE = (
    "⚠️ Your previous response was cut off at the output-token limit, so "
    "its action was incomplete and was NOT executed. Retry with a smaller "
    "unit — e.g. build a large file incrementally with edit_file instead "
    "of one big write_file."
)
