"""Recovery primitives — pure functions that produce Intervention fragments.

A primitive takes harness-level inputs only (normalized strings, dicts) and
returns text destined for the next user-role message. Primitives must never
know which provider, model, or channel produced the upstream data —
runtime quirks are the Provider Layer's responsibility.

See ``docs/robust-harness/DESIGN.md`` §2.2 for the contract.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

#: Longest prior output quoted whole (v9.23.2). Past this the echo keeps the
#: head and the tail and says how much it dropped.
ECHO_MAX_CHARS = 2000
ECHO_HEAD_CHARS = 1400
ECHO_TAIL_CHARS = 600


#: Scalars longer than this are shown as ``<N chars>`` in :func:`summarize_call`
#: — the summary names the fields, it does not repeat a 2 KB file body.
CALL_VALUE_MAX_CHARS = 40


def summarize_value(value: object) -> str:
    """One token per argument value for :func:`summarize_call`.

    Short strings verbatim (JSON-quoted), long strings as ``<N chars>``,
    arrays as ``[n items]``, objects as ``{n keys}``, other scalars as their
    JSON literal.
    """
    if isinstance(value, str):
        if len(value) <= CALL_VALUE_MAX_CHARS:
            return json.dumps(value, ensure_ascii=False)
        return f"<{len(value):,} chars>"
    if isinstance(value, list):
        return f"[{len(value)} item{'' if len(value) == 1 else 's'}]"
    if isinstance(value, dict):
        return f"{{{len(value)} key{'' if len(value) == 1 else 's'}}}"
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return json.dumps(value)
    return str(value)


def summarize_call(
    tool_name: str,
    tool_input: object,
    *,
    missing: Sequence[str] = (),
    unknown: Sequence[str] = (),
    hint: str | None = None,
) -> str:
    """One line that shows the model the call it just sent (v10.37.0).

    ``You sent: `edit_file` with `op` "replace", `pos` "12#AB", `lines` [1 item]
    — missing: `path`. Re-emit the whole call with every required field.``

    Why it exists: a call rejected before dispatch (A4/A5) is not stored, so
    the rejection text is the model's only copy. Text dialects also get
    :func:`echo_prior_output`, but under native function calling the prose
    is empty and the model saw *only* "Missing required field(s) … path" —
    with nothing to compare against it re-sent the same call four times
    (Harbor regex-log, v10.36.0). Values are summarized
    (:func:`summarize_value`) so the line stays one line.

    ``hint`` overrides the trailing instruction; by default it follows from
    what is wrong (missing fields → re-emit whole call; otherwise fix the
    named argument).
    """
    # v10.77.0 표기 규칙: 호출 모양(`tool(k=v)`)이 아니라 문장 — 도구·인자는
    # 백틱, 값은 ``summarize_value`` 그대로(문자열은 따옴표).
    if isinstance(tool_input, dict) and tool_input:
        args = " with " + ", ".join(
            f"`{k}` {summarize_value(v)}" for k, v in tool_input.items()
        )
    elif isinstance(tool_input, dict) or tool_input is None:
        args = " with no arguments"
    else:
        args = f" with {summarize_value(tool_input)}"
    line = f"You sent: `{tool_name}`{args}"
    gaps = []
    if missing:
        gaps.append("missing: " + ", ".join(f"`{m}`" for m in missing))
    if unknown:
        gaps.append("unknown: " + ", ".join(f"`{u}`" for u in unknown))
    if gaps:
        line += " — " + "; ".join(gaps)
    if hint is None:
        hint = (
            "Re-emit the whole call with every required field."
            if missing
            else "Fix the argument named above and re-emit the whole call."
        )
    return f"{line}. {hint}"


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
    # v10.77.0: 호출 모양 대신 도구 이름 + 인자 객체 (예시 규칙 v10.75.1 과 같음).
    # 접두 "You have called" 는 fold 판정이 쓰므로 그대로.
    return f"You have called `{action}` with {args_repr} {repeat_count} times in a row"


def probe_progress(
    *,
    action: str,
    args_repr: str,
    repeat_count: int,
    prior_result: str,
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

    ``prior_result`` is always what the call returned when it last ran:
    the caller fires this level only for a repeat of an *executed* call
    (a repeat of a call rejected before dispatch is a retry, not a loop).

    See ``docs/robust-harness/DESIGN.md`` §2.2.
    """
    return (
        f"{_loop_observed(action, args_repr, repeat_count)}. "
        "The previous call already returned:\n---\n"
        f"{bounded_excerpt(prior_result)}\n---\n"
        "Take a different action based on that result. "
        "Call `complete` only if the task itself is finished, "
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
