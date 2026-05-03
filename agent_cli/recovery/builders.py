"""Intervention builders — compose recovery primitives into Interventions.

These factories live in the recovery layer (not ``constants``) to keep
the dependency direction one-way: ``recovery`` depends on primitives it
owns; lower layers do not depend back on ``recovery``. Earlier history
placed them in ``constants.py`` and produced an inverted-layer cycle
(see docs/robust-harness/REMAINING_DEBT.md history for context).
"""

from __future__ import annotations

from agent_cli.constants import RETRY_HINT_NO_ACTION, RETRY_HINT_NO_JSON
from agent_cli.recovery.intervention import Intervention
from agent_cli.recovery.primitives import (
    constrain_action_required,
    constrain_format_json,
    echo_prior_output,
    probe_progress,
    restate_task,
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


def format_no_thought_retry(*, prior_content: str = "") -> Intervention:
    """Build the Intervention for a JSON envelope that has an action but
    no thought.

    Same failure-grounding rationale as ``format_no_action_retry`` —
    echo the model's prior output so it sees its own omission, then
    restate the constraint. The constraint is inlined here rather than
    promoted to a primitive: ``constrain_thought_required`` would have
    exactly one caller in v1, which violates the "primitive reused by
    ≥2 failures" anti-patchwork invariant in DESIGN.md §4. Promote it
    only when a second caller appears.
    """
    constraint = (
        "Your JSON must include a 'thought' field stating purpose "
        "(what you want to achieve) and reason (why this specific action). "
        "Do not omit it."
    )
    echo = echo_prior_output(prior_content)
    if not echo:
        return Intervention(
            message="Your JSON was missing the 'thought' field. " + constraint,
            primitives=[],
        )

    msg = "\n".join(
        [
            "Your JSON was missing the 'thought' field.",
            "",
            echo,
            "Honor that. " + constraint,
        ]
    )
    return Intervention(
        message=msg,
        primitives=["echo_prior_output"],
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
