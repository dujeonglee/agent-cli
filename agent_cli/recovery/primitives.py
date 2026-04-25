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


def constrain_format_json() -> str:
    """Remind the model that the next response must be a bare JSON object."""
    return (
        "Output ONLY a JSON object: "
        '{"thought": "...", "action": "tool_name", "action_input": {...}}. '
        "No markdown fences, no extra text."
    )


def constrain_action_required() -> str:
    """Remind the model that an action field is mandatory."""
    return (
        "You MUST include an action. Either use a tool: "
        '{"thought": "...", "action": "tool_name", "action_input": {...}} '
        "or complete the task: "
        '{"thought": "...", "action": "complete", "action_input": {"result": "..."}}'
    )
