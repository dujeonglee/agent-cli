"""Shared constants for agent-cli."""

from agent_cli.recovery.primitives import (
    constrain_action_required,
    constrain_format_json,
    echo_prior_output,
)

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
)


def format_no_json_retry(*, prior_content: str = "") -> str:
    """Build the retry hint shown when an LLM response failed to parse as JSON.

    Composes recovery primitives: echoes the model's prior output (failure
    grounding) and reminds the model of the required JSON envelope
    (constrain). Falls back to the static ``RETRY_HINT_NO_JSON`` when no
    prior content is available — preserves the existing retry path.

    Keyword-only to avoid silent positional misuse.
    """
    echo = echo_prior_output(prior_content)
    if not echo:
        return RETRY_HINT_NO_JSON

    return "\n".join(
        [
            "Your response was not valid JSON.",
            "",
            echo,
            "Honor that. " + constrain_format_json(),
        ]
    )


def format_no_action_retry(*, prior_content: str = "") -> str:
    """Build the retry hint shown when JSON parsed but no action was provided.

    Same failure-grounding rationale as ``format_no_json_retry``.
    """
    echo = echo_prior_output(prior_content)
    if not echo:
        return RETRY_HINT_NO_ACTION

    return "\n".join(
        [
            "Your JSON was parsed but has no action.",
            "",
            echo,
            "Honor that. " + constrain_action_required(),
        ]
    )
