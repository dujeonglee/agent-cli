"""Recovery primitives — pure functions that produce Intervention fragments.

A primitive takes harness-level inputs only (normalized strings, dicts) and
returns text destined for the next user-role message. Primitives must never
know which provider, model, or channel produced the upstream data —
runtime quirks are the Provider Layer's responsibility.

See ``docs/robust-harness/DESIGN.md`` §2.2 for the contract.
"""

from __future__ import annotations

#: Longest prior output quoted whole (v9.23.2). Past this the echo keeps the
#: head and the tail and says how much it dropped.
ECHO_MAX_CHARS = 2000
ECHO_HEAD_CHARS = 1400
ECHO_TAIL_CHARS = 600


def bounded_excerpt(text: str) -> str:
    """``text`` whole when short, else head + ``[… N characters omitted …]`` +
    tail. Both ends survive because a format failure can sit at either one:
    a cut-off JSON looks fine at the head, a prose drift shows only at the
    tail."""
    if len(text) <= ECHO_MAX_CHARS:
        return text
    omitted = len(text) - ECHO_HEAD_CHARS - ECHO_TAIL_CHARS
    return (
        text[:ECHO_HEAD_CHARS]
        + f"\n[… {omitted} characters omitted …]\n"
        + text[-ECHO_TAIL_CHARS:]
    )


def echo_prior_output(content: str, *, excerpt: bool = True) -> str:
    """Mirror the model's prior emitted text back at it for failure grounding.

    Returns the *body* of the echo block — a delimited section quoting
    ``content`` (bounded, see :func:`bounded_excerpt`). Caller wraps with
    framing text appropriate to the failure (e.g. "Your response was not
    valid JSON.").

    Returns an empty string when ``content`` is empty / whitespace —
    the caller should fall back to a static reminder in that case.

    **Bounded since v9.23.2.** The echo used to quote the output whole on the
    theory that recovery is rare and cheap. The failures that need it most
    are the long ones: a 32K-character runaway (Harbor extract-elf, Qwen3.8-
    Flash-Next) was stored AND quoted whole, the model then imitated the
    broken fragment for 22 turns. The failed emission itself is no longer
    stored on these paths (``store_emission=False`` — "retries are not
    recorded", v9.21), so this quote is the only copy; head + tail keeps
    both places a format error can hide.

    ``excerpt=False`` (v10.5.0): ``content`` is already a bounded excerpt
    (the structured nudge record stores it that way) — quote it as is.
    Bounding twice would cut the head+tail form a second time.
    """
    cleaned = content.strip() if content else ""
    if not cleaned:
        return ""
    body = bounded_excerpt(cleaned) if excerpt else cleaned
    return f"Your prior output:\n---\n{body}\n---\n"


# ``constrain_format_json`` / ``constrain_action_required`` lived here
# as ReAct-shape JSON reminders. They moved onto the dialect plugin
# in Step 7: ``ReActFormat.constraint_reminder_call()`` /
# ``constraint_reminder_action_required()``. recovery/primitives.py
# now holds only format-agnostic primitives — ``echo_prior_output`` for
# failure grounding and the B1 (action loop) nudges.


def _loop_observed(action: str, args_repr: str, repeat_count: int) -> str:
    """One sentence stating the loop fact.

    Shared by all B1-related primitives so the wording stays consistent
    across escalation levels. The body that follows is
    primitive-specific.
    """
    return f"You have called {action}({args_repr}) {repeat_count} times in a row"


def probe_progress(
    *,
    action: str,
    args_repr: str,
    repeat_count: int,
    prior_result: str | None = None,
) -> str:
    """Hand the model what the repeated call already returned (B1, level 1).

    Intent: "you already have this." A gentle first-level intervention —
    does NOT re-anchor the task or ask diagnostic questions. States the
    loop fact, quotes the previous call's result (``prior_result``, bounded
    like :func:`echo_prior_output`) so the model need not hunt for it, and
    asks for a different action. ``complete`` is mentioned only as
    conditional on the task being finished (v10.32.0): the earlier wording
    offered "summarize and call complete" as the first of two equal exits,
    and a Qwen3.6-35B Harbor trial took it two turns after the nudge with
    a known defect left in the deliverable.

    ``prior_result`` is None when the caller cannot pair the repeat with an
    executed call (e.g. the earlier emission was rejected before dispatch);
    the text then says the result is already in context.

    See ``docs/robust-harness/DESIGN.md`` §2.2.
    """
    head = f"{_loop_observed(action, args_repr, repeat_count)}. "
    if prior_result is None:
        body = "Its result is already in your context. "
    else:
        body = (
            "The previous call already returned:\n---\n"
            f"{bounded_excerpt(prior_result)}\n---\n"
        )
    return (
        head + body + "Take a different action based on that result. "
        "Call complete only if the task itself is finished, "
        "not to end the repetition."
    )


def restate_task(*, task: str, action: str, args_repr: str, repeat_count: int) -> str:
    """Re-anchor the task with causal/gap diagnostics (B1, level 2).

    Intent: "look at what the task actually needs, and what is
    missing." Used when ``probe_progress`` did not break the loop.
    Re-pins the user's original goal and asks the model to reflect on:
    1. The causal connection between the looped action and the task.
    2. The information gap the loop is failing to fill.
    """
    return "\n".join(
        [
            "You were asked to:",
            "---",
            task.strip(),
            "---",
            "",
            (
                f"{_loop_observed(action, args_repr, repeat_count)} without "
                "progress. The previous nudge did not work — step back to "
                "the task itself:"
            ),
            "",
            (
                "- Why does completing the task require this call? What "
                "information from it does the task actually need?"
            ),
            (
                "- What information are you NOT getting from the responses? "
                "Is what you need actually here, or somewhere else?"
            ),
            "",
            (
                "Choose your next step from that reflection, not from another "
                "retry of the same call."
            ),
        ]
    )
