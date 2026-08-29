"""Session-state block — volatile state appended to the LAST message.

Why the tail (v8.46.0). Everything the model reads is one token sequence, and
providers cache a KV *prefix*: reuse ends at the first token that differs from
the previous call. So WHERE volatile text sits decides what it costs.

- In the system prompt (where ``Session Memory`` and the live agent roster used
  to live) a change invalidates the prefix from that section onward — which is
  the rest of the system prompt PLUS the entire conversation. One ``memory
  add`` mid-session forced a full re-prefill of tens of thousands of tokens.
  That pressure is why the roster deliberately carried membership only and no
  busy/idle state, and why ``_build_environment_section`` omits the date.
- Right after the USER'S REQUEST looks tempting but is the worst place in an
  agent loop: the request sits near the FRONT (one query, then N turns of
  assistant/observation grow after it), so a per-turn block there invalidates
  every turn that follows it.
- At the TAIL it is free. Turn N ends with ``… obs_{N-1} + STATE_N``; turn N+1
  is ``… obs_{N-1}, assistant_N, obs_N + STATE_{N+1}``. The prefix match ends
  at ``obs_{N-1}`` — exactly where it would have ended anyway, because
  ``assistant_N`` and ``obs_N`` are new tokens regardless. The only thing lost
  is the previous block's own ~50 cached tokens.

The tail is also where recency attention is strongest, which is what this
content wants: it is the state the model should be deciding against RIGHT NOW.

Delivery is by appending to the last message rather than adding a new one:
providers hand ``messages`` to the server verbatim (``providers/anthropic.py``
sends the list as-is), so an extra trailing ``role=user`` message would create
consecutive same-role turns whose handling differs per provider. Appending
keeps the message count unchanged. Same mechanism as ``_OBS_COMPLETE_NUDGE``:
applied at feed time in ``ContextManager.get_messages``, on a copy, and NEVER
persisted to history.jsonl (a stored block would re-feed stale numbers every
turn afterwards and pollute resume previews).
"""

from __future__ import annotations

#: Report context pressure only past this fraction of the budget. Below it the
#: number is noise the model would pay attention tokens for and act on wrongly;
#: above it, it is the one signal that changes what a good agent does next.
#: Kept OFF by default at low usage rather than always-on for a second reason:
#: telling a model it is running out of room invites premature ``complete``
#: (the same failure ``_OBS_COMPLETE_NUDGE`` had to be measured against), so
#: the warning is phrased as an action to take, not as a shortage to react to.
COMPACTION_WARN_RATIO = 0.75

#: Marker line that opens the block. Public so callers (and tests) can
#: locate the boundary between the conversation and the appended state.
SESSION_STATE_HEADER = (
    "── session state (context only — not part of the conversation) ──"
)


def _context_line(used: int, budget: int, turn: int, max_turns: int) -> str:
    parts = []
    if budget > 0:
        pct = min(100, round(used * 100 / budget))
        parts.append(f"context: ~{used:,} / {budget:,} tokens ({pct}%)")
    elif used:
        parts.append(f"context: ~{used:,} tokens")
    if turn:
        parts.append(f"turn {turn}" + (f"/{max_turns}" if max_turns else ""))
    return " · ".join(parts)


def build_session_state(
    *,
    used_tokens: int = 0,
    budget_tokens: int = 0,
    turn: int = 0,
    max_turns: int = 0,
    agents: str = "",
    memory: str = "",
) -> str:
    """Render the block, or ``""`` when there is nothing worth saying.

    ``agents`` / ``memory`` are the already-rendered sections
    (``build_live_agents_section(include_state=True)`` / ``memory.render_index``)
    — passed in rather than fetched here so this stays a pure function and the
    model keeps seeing the SAME headings it saw when these lived in the system
    prompt (nothing to re-learn from the move).
    """
    blocks = [b for b in (agents.strip(), memory.strip()) if b]
    ctx_line = _context_line(used_tokens, budget_tokens, turn, max_turns)
    if not ctx_line and not blocks:
        return ""

    lines = [SESSION_STATE_HEADER]
    if ctx_line:
        lines.append(ctx_line)
    for b in blocks:
        lines.append("")
        lines.append(b)

    if budget_tokens > 0 and used_tokens >= budget_tokens * COMPACTION_WARN_RATIO:
        lines.append("")
        lines.append(
            "⚠ Context is nearly full. Older turns will be summarised away soon "
            "— anything you must not lose (a failure you should not repeat, a "
            "decision and its reason, a hard-won fact) should go into "
            "memory(mode=add) NOW, while you still have it. Keep working; this "
            "is not a reason to finish early."
        )
    return "\n".join(lines)
