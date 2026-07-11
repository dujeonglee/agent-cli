"""Auto-review: parse the reviewer agent's verdict from its complete result.

The reviewer is a normal delegate agent (no loop changes). It ends with
``complete`` whose result carries a verdict signature — see ``reviewer.md``:

    VERDICT: ACCEPT
  or
    VERDICT: REJECT
    <issues to fix>

The web worker runs the reviewer after the main agent completes (when the
auto-review toggle is on), parses the result with :func:`parse_review_verdict`,
and either stops (accept) or re-injects the feedback and resumes the main agent
(reject). There is no safety cap — the user controls termination via the toggle
(toggle off to stop the review loop).
"""

from __future__ import annotations

import re

# Match a verdict line anywhere; the LAST one is the reviewer's final call.
# Lenient: case-insensitive, tolerates extra spaces after the colon.
_VERDICT_RE = re.compile(r"VERDICT:\s*(ACCEPT|REJECT)", re.IGNORECASE)


def parse_review_verdict(reviewer_output: str) -> tuple[bool, str]:
    """Parse the reviewer's complete-result string into ``(accept, feedback)``.

    - ``accept`` True iff the (last) ``VERDICT:`` line says ACCEPT.
    - ``feedback`` the actionable text after the final verdict line (empty for
      ACCEPT). When no ``VERDICT:`` line is found, defaults to
      ``(False, <raw output>)`` — quality-first (the review shouldn't silently
      pass on a malformed verdict; the user stops the loop via the toggle).
    """
    if not reviewer_output:
        return (False, "")
    matches = list(_VERDICT_RE.finditer(reviewer_output))
    if not matches:
        return (False, reviewer_output)
    last = matches[-1]
    accept = last.group(1).upper() == "ACCEPT"
    feedback = "" if accept else reviewer_output[last.end() :].strip()
    return (accept, feedback)


def record_review_observation(ctx, content: str, *, success: bool) -> None:
    """Persist an auto-review result to ``ctx`` (history.jsonl) so it survives
    resume — the live SSE card alone vanishes on reload. Mirrors the loop's
    observation record shape ({role:user, tool, success, content:"Observation:
    …"}) so ``replay_from_history`` re-renders it as an observation card. No-op
    when ``ctx`` is None (CLI / pre-session)."""
    if ctx is None:
        return
    ctx.add(
        {
            "role": "user",
            "tool": "auto-review",
            "success": success,
            "content": f"Observation: {content}",
        }
    )


# Virtual/terminal tools excluded from the "your tool calls" review listing —
# they aren't real work, just loop control. (The review-context builders below
# serve the auto-review feature via agent_cli.review.)
_REVIEW_VIRTUAL_TOOLS: frozenset[str] = frozenset({"complete", "ask"})


def _short_review_args(args, max_len: int = 80) -> str:
    """Render a tool's action_input as a compact one-liner for review injection.

    Long strings are head-truncated to 40 chars; non-scalar values
    (list / dict) collapse to ``<type>`` markers so the line stays
    short. The combined render is then capped at ``max_len``. The goal
    is "model can recognize what was called and on what target" — not
    a faithful replay.
    """
    if not isinstance(args, dict):
        s = repr(args)
        return s if len(s) <= max_len else s[: max_len - 3] + "..."
    pairs = []
    for k, v in args.items():
        if isinstance(v, str):
            v_show = v if len(v) <= 40 else v[:37] + "..."
            pairs.append(f"{k}={v_show!r}")
        elif isinstance(v, (int, float, bool)) or v is None:
            pairs.append(f"{k}={v!r}")
        else:
            pairs.append(f"{k}=<{type(v).__name__}>")
    line = ", ".join(pairs)
    if len(line) > max_len:
        line = line[: max_len - 3] + "..."
    return line


def _format_tool_calls_for_review(ctx, max_calls: int = 30) -> str:
    """Build the ``--- YOUR TOOL CALLS ---`` section for review injection.

    Returns "" (no section emitted) when ctx is None, has no
    assistant tool calls, or only virtual tools were used. Virtual
    tools (``complete`` / ``ask``) are filtered — they don't
    represent work the model has done.

    The section gives the model a *factual* list of what it actually
    invoked, independent of whether the corresponding Observations
    have been evicted by context FIFO. The model can then dispute or
    confirm its summary against this list before calling ``complete``.

    When the count exceeds ``max_calls``, the most recent
    ``max_calls`` entries are kept (most relevant to "is the work
    done?") and a header note records the omission.
    """
    if ctx is None:
        return ""
    try:
        raw = ctx.get_raw_messages()
    except Exception:
        return ""

    # iter_record_ops reads BOTH assistant record shapes (multi-op ``ops``
    # + legacy singular ``action``) — reading only the top-level ``action``
    # here silently skipped every op turn once md_array/react started
    # storing ``ops`` records, leaving this section permanently empty.
    from agent_cli.context.manager import iter_record_ops

    calls = []
    for msg in raw:
        for action, args in iter_record_ops(msg):
            if action in _REVIEW_VIRTUAL_TOOLS:
                continue
            calls.append(f"- {action}({_short_review_args(args)})")

    if not calls:
        return ""

    total = len(calls)
    if total > max_calls:
        calls = calls[-max_calls:]
        header = f"--- YOUR TOOL CALLS (last {max_calls} of {total}) ---"
    else:
        header = "--- YOUR TOOL CALLS ---"

    return "\n".join([header, *calls])


def build_reviewer_task(task_text: str, final_answer: str, ctx=None) -> str:
    """Assemble the reviewer delegate's task prompt: WHAT to review.

    The reviewer's *system* prompt (reviewer.md) owns HOW to review and the
    VERDICT format; this is just the material — the original request, the
    finishing agent's final answer, and the factual list of tool calls it made
    (the reviewer reads the actual files to verify). Reuses
    ``_format_tool_calls_for_review`` (kept across the ready_for_review
    removal). Lazy import — review is a leaf module and loop.py is heavy."""
    parts = [
        "Another agent has finished the task below and called complete. Review "
        "whether the delivered work actually fulfills it, then return your "
        "VERDICT (ACCEPT / REJECT) as instructed.",
        "",
        "--- ORIGINAL REQUEST ---",
        task_text,
        "",
        "--- THE AGENT'S FINAL ANSWER ---",
        final_answer or "(no final answer)",
    ]
    tool_calls = _format_tool_calls_for_review(ctx)
    if tool_calls:
        parts.extend(["", tool_calls])
    return "\n".join(parts)


def run_auto_review(
    task_text: str,
    final_answer: str,
    ctx,
    *,
    is_enabled,
    spawn_reviewer,
    resume_main,
    render=None,
    is_interrupted=None,
) -> None:
    """Drive the post-completion review loop. Dependencies are injected so the
    loop logic is unit-testable; the web worker supplies the real ones:

    - ``is_enabled() -> bool``  — the auto-review toggle (checked each round, so
      toggling off mid-loop stops it).
    - ``spawn_reviewer(task) -> str`` — run the reviewer delegate, return its
      complete result.
    - ``resume_main(feedback) -> str`` — inject feedback into the main session
      and resume the main agent, returning its NEW final answer.
    - ``render(event, detail="")`` — optional progress hook so the verdict is
      surfaced to the MAIN conversation (otherwise the reviewer's verdict lives
      only inside the delegate group card and the user never sees the outcome).
      Events: ``review_start`` (a round began), ``accept`` (passed),
      ``reject`` (detail = the feedback shown before the rework).
    - ``is_interrupted() -> bool`` — optional; True once the user hits Stop. An
      interrupt cancels the in-flight reviewer/resume, leaving a malformed
      verdict that would parse as REJECT and loop forever — so we MUST break the
      loop on interrupt rather than feed garbage back as feedback.

    Loop: review → accept stops; reject resumes the main agent with the
    feedback and reviews again. No safety cap — the user stops it via the toggle
    or an interrupt (decision: keep reviewing until accepted)."""

    def _emit(event, detail=""):
        if render:
            render(event, detail)

    def _interrupted():
        return bool(is_interrupted and is_interrupted())

    while is_enabled():
        if _interrupted():
            return
        _emit("review_start")
        reviewer_task = build_reviewer_task(task_text, final_answer, ctx)
        verdict_text = spawn_reviewer(reviewer_task)
        # An interrupt during the reviewer run leaves verdict_text malformed;
        # don't parse it as a reject and resume — stop the loop instead.
        if _interrupted():
            return
        accept, feedback = parse_review_verdict(verdict_text)
        if accept:
            _emit("accept")
            return
        _emit("reject", feedback)
        # Reject: hand the feedback back to the main agent, which fixes the work
        # and completes again; review the new result on the next iteration.
        final_answer = resume_main(feedback)
