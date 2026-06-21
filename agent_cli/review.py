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


def build_reviewer_task(task_text: str, final_answer: str, ctx=None) -> str:
    """Assemble the reviewer delegate's task prompt: WHAT to review.

    The reviewer's *system* prompt (reviewer.md) owns HOW to review and the
    VERDICT format; this is just the material — the original request, the
    finishing agent's final answer, and the factual list of tool calls it made
    (the reviewer reads the actual files to verify). Reuses
    ``_format_tool_calls_for_review`` (kept across the ready_for_review
    removal). Lazy import — review is a leaf module and loop.py is heavy."""
    from agent_cli.loop import _format_tool_calls_for_review

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

    Loop: review → accept stops; reject resumes the main agent with the
    feedback and reviews again. No safety cap — the user stops it via the
    toggle (decision: keep reviewing until accepted)."""

    def _emit(event, detail=""):
        if render:
            render(event, detail)

    while is_enabled():
        _emit("review_start")
        reviewer_task = build_reviewer_task(task_text, final_answer, ctx)
        verdict_text = spawn_reviewer(reviewer_task)
        accept, feedback = parse_review_verdict(verdict_text)
        if accept:
            _emit("accept")
            return
        _emit("reject", feedback)
        # Reject: hand the feedback back to the main agent, which fixes the work
        # and completes again; review the new result on the next iteration.
        final_answer = resume_main(feedback)
