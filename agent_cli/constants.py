"""Shared constants for agent-cli."""

from agent_cli.recovery.intervention import Intervention
from agent_cli.recovery.primitives import (
    constrain_action_required,
    constrain_format_json,
    echo_prior_output,
    probe_progress,
    restate_task,
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
    # B1 (action loop) interventions — both messages start with one of
    # these phrases (probe_progress / restate_task respectively).
    "You have called",
    "You were asked to:",
)


def format_no_json_retry(*, prior_content: str = "") -> Intervention:
    """Build the Intervention for an LLM response that failed to parse as JSON.

    Composes recovery primitives: echoes the model's prior output (failure
    grounding) and reminds the model of the required JSON envelope
    (constrain). Falls back to the static ``RETRY_HINT_NO_JSON`` when no
    echoable content is available.

    Returns an :class:`Intervention` carrying both the user-role message
    to inject and the names of primitives composed (for observability).

    Keyword-only to avoid silent positional misuse.
    """
    echo = echo_prior_output(prior_content)
    if not echo:
        return Intervention(message=RETRY_HINT_NO_JSON, primitives=[])

    msg = "\n".join(
        [
            "Your response was not valid JSON.",
            "",
            echo,
            "Honor that. " + constrain_format_json(),
        ]
    )
    return Intervention(
        message=msg,
        primitives=["echo_prior_output", "constrain_format_json"],
    )


def format_no_action_retry(*, prior_content: str = "") -> Intervention:
    """Build the Intervention when JSON parsed but no action was provided.

    Same failure-grounding rationale as ``format_no_json_retry``.
    """
    echo = echo_prior_output(prior_content)
    if not echo:
        return Intervention(message=RETRY_HINT_NO_ACTION, primitives=[])

    msg = "\n".join(
        [
            "Your JSON was parsed but has no action.",
            "",
            echo,
            "Honor that. " + constrain_action_required(),
        ]
    )
    return Intervention(
        message=msg,
        primitives=["echo_prior_output", "constrain_action_required"],
    )


def format_action_loop_intervention(
    *,
    level: int,
    action: str,
    args_repr: str,
    repeat_count: int,
    task: str,
) -> Intervention | None:
    """Compose the B1 (action loop) Intervention for a given escalation level.

    Skips the temperature-down level from DESIGN.md §2.3 — temperature
    handling diverges across providers, which would leak runtime
    detail into the recovery layer. Step 4 may revisit if data shows
    benefit.

    Returns:
        Intervention for level 1 or 2; ``None`` for level ≥3 (caller
        should hard-fail with an informative error).
    """
    if level == 1:
        return Intervention(
            message=probe_progress(
                action=action,
                args_repr=args_repr,
                repeat_count=repeat_count,
            ),
            primitives=["probe_progress"],
        )
    if level == 2:
        return Intervention(
            message=restate_task(
                task=task,
                action=action,
                args_repr=args_repr,
                repeat_count=repeat_count,
            ),
            primitives=["restate_task"],
        )
    return None
