"""Actionable error messages for recursion + depth-limit blocks.

When the loop blocks a ``run_skill`` / ``delegate`` call (either
because the target is already on the call stack, or because the
combined call depth has hit ``max_depth``), the LLM sees this string
as an Observation. A bare "blocked" message leaves the model
guessing; without an explicit recovery menu it often retries the
same call or stalls.

Two formatters here, both wired into the two block sites
(``_handle_run_skill`` and ``_run_single``):

  * :func:`format_recursion_error` — cycle detection (target already
    in stack). Recovery is task-shaped: don't re-enter, complete
    with what you have, or ask the user.

  * :func:`format_depth_limit_error` — hard ceiling. Recovery is
    structural: break the task into independent steps, or finish
    the current level before any further nesting.
"""

from __future__ import annotations


def format_recursion_error(kind: str, name: str, stack: list[str]) -> str:
    """A skill/agent the model just tried to invoke is already on the
    call stack — a cycle. Block + tell the model how to recover.

    ``kind`` is ``"skill"`` or ``"agent"`` for the message tense.
    ``stack`` is the live stack as ``["foo", "bar"]``; rendered with
    ``→`` so the chain reads left-to-right at a glance.
    """
    chain = " → ".join(stack) if stack else "(empty)"
    return (
        f"Recursive {kind} call blocked: '{name}' is already on the call "
        f"stack ({chain}). Re-entering it would loop without progress. "
        f"Recover by one of:\n"
        f"  (1) try a different approach that does not call '{name}' again,\n"
        f"  (2) finish the current task with what you already have via "
        f"'complete',\n"
        f"  (3) use 'ask' if you need user guidance to choose a path."
    )


def format_depth_limit_error(
    kind: str, name: str, current_depth: int, max_depth: int
) -> str:
    """The combined skill + delegate call depth has hit
    ``max_depth``. Block + explain the structural fix (this isn't
    something the model can retry around — the task is too deep).

    ``current_depth`` is the *parent* depth (the one we're trying to
    descend from). The next level would be ``current_depth + 1``, so
    we report that figure for clarity in the message.
    """
    return (
        f"Maximum call depth reached: cannot enter {kind} '{name}' — that "
        f"would put the chain at depth {current_depth + 1}, beyond the "
        f"configured limit of {max_depth}. Skill and delegate hops share "
        f"this counter. Recover by one of:\n"
        f"  (1) finish the current level with 'complete' before any further "
        f"nesting,\n"
        f"  (2) restructure the task into independent steps that can each "
        f"run at the top level,\n"
        f"  (3) tell the user the task is too deep — they may bump the "
        f"limit with '--max-depth N' (currently {max_depth})."
    )


__all__ = ["format_recursion_error", "format_depth_limit_error"]
