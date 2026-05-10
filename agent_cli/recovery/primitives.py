"""Recovery primitives — pure functions that produce Intervention fragments.

A primitive takes harness-level inputs only (normalized strings, dicts) and
returns text destined for the next user-role message. Primitives must never
know which provider, model, or channel produced the upstream data —
runtime quirks are the Provider Layer's responsibility.

See ``docs/robust-harness/DESIGN.md`` §2.2 for the contract.
"""

from __future__ import annotations

# Cap echoed segment length. Head-truncate keeps the prefix where
# structural drift markers (YAML-style ``thought:`` / function-call
# ``tool(args)`` syntax / leading prose) typically appear.
ECHO_HEAD = 400


def _truncate_head(text: str, n: int = ECHO_HEAD) -> str:
    """Strip and head-truncate (keep first ``n`` chars)."""
    cleaned = text.strip() if text else ""
    if not cleaned:
        return ""
    if len(cleaned) > n:
        cleaned = cleaned[:n] + "..."
    return cleaned


def echo_prior_output(content: str = "") -> str:
    """Mirror the model's prior emitted text back at it for failure grounding.

    Returns the *body* of the echo block — a delimited section quoting the
    head of ``content``. Caller wraps with framing text appropriate to
    the failure (e.g. "Your response was not valid JSON.").

    Returns an empty string when ``content`` is empty/whitespace — the
    caller should fall back to a static reminder in that case.
    """
    content_quote = _truncate_head(content)
    if not content_quote:
        return ""
    return "\n".join(["Your prior output:", "---", content_quote, "---", ""])


# ``constrain_format_json`` / ``constrain_action_required`` lived here
# as ReAct-shape JSON reminders. They moved onto the wire-format plugin
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


def probe_progress(*, action: str, args_repr: str, repeat_count: int) -> str:
    """Nudge the model to consult its existing context (B1, level 1).

    Intent: "look at what you already have." A gentle first-level
    intervention — does NOT re-anchor the task or ask diagnostic
    questions. Just points out the loop fact and tells the model to
    re-read previous responses before deciding.

    See ``docs/robust-harness/DESIGN.md`` §2.2.
    """
    return (
        f"{_loop_observed(action, args_repr, repeat_count)}. "
        "Re-read the previous responses already in your context. "
        "Either summarize what you have learned and call complete, "
        "or take a different action."
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
            f"{_loop_observed(action, args_repr, repeat_count)} without "
            "progress. The previous nudge did not work — step back to "
            "the task itself:",
            "",
            "- Why does completing the task require this call? What "
            "information from it does the task actually need?",
            "- What information are you NOT getting from the responses? "
            "Is what you need actually here, or somewhere else?",
            "",
            "Choose your next step from that reflection, not from another "
            "retry of the same call.",
        ]
    )
