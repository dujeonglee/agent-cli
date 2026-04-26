"""Behavioral failure detectors for the recovery layer.

Detectors are *stateful across turns* (unlike parser-stage detection,
which is per-response). Owned by the AgentLoop instance — one detector
per session.

See ``docs/robust-harness/DESIGN.md`` §1 (Layer B failures) and §3.1.
"""

from __future__ import annotations

import json
from typing import Any


class ActionLoopDetector:
    """Detect consecutive identical (action, args) emissions (failure B1).

    Fires escalation level 1+ when the same (action, normalized_args)
    has been observed ``threshold`` times in a row. Subsequent identical
    observations bump the level (1, 2, 3, ...) so the playbook can
    escalate primitives. A different action or an explicit error-retry
    resets the counter and the fire count.

    The detector looks only at the model's *intent* (action + args) — it
    does not consult the model's response text or the tool result. That
    keeps it purely about "the model wants to do X with Y again".

    Threshold default is 2 (early nudge): after one repeat the next
    observation fires. The 2-fire principle mirrors human metacognition
    — repeating the same action twice should already prompt "wait, did
    I just do this?". The harness gives the model the same nudge.
    """

    def __init__(self, threshold: int = 2):
        if threshold < 2:
            raise ValueError("threshold must be ≥ 2 (one repeat is not a loop)")
        self.threshold = threshold
        self._last_signature: str | None = None
        self._consecutive_count = 0
        self._fire_count = 0

    @property
    def fire_count(self) -> int:
        """Number of times the detector has fired for the current loop."""
        return self._fire_count

    @property
    def consecutive_count(self) -> int:
        """How many consecutive emissions of the current signature so far."""
        return self._consecutive_count

    def observe(self, action: str, args: Any, *, prev_was_error: bool = False) -> int:
        """Record an action emission. Returns escalation level.

        Returns:
            0 — no fire (below threshold, or reset by an error retry)
            1 — first fire (just hit threshold)
            2 — second fire (kept emitting after the first intervention)
            3+ — subsequent fires (caller should hard-fail)

        Args:
            action: Tool name the model is about to invoke.
            args: Tool input. Normalized to a canonical JSON string for
                comparison; dict key ordering is ignored.
            prev_was_error: ``True`` when the previous tool call (with
                the same signature) returned an error. Error retries
                are legitimate — counter resets, no fire.
        """
        sig = self._canonical(action, args)

        if prev_was_error:
            # Legitimate retry after a failure — reset counter so the
            # current emission is treated as a fresh attempt. The next
            # *same-sig* emission after a successful run can still fire.
            self._last_signature = sig
            self._consecutive_count = 1
            self._fire_count = 0
            return 0

        if sig == self._last_signature:
            self._consecutive_count += 1
        else:
            self._last_signature = sig
            self._consecutive_count = 1
            self._fire_count = 0

        if self._consecutive_count >= self.threshold:
            self._fire_count += 1
            return self._fire_count
        return 0

    @staticmethod
    def _canonical(action: str, args: Any) -> str:
        """Build a hashable signature from ``action`` + ``args``.

        ``json.dumps(sort_keys=True)`` makes dict key ordering
        irrelevant. Non-JSON-serializable values fall back to ``repr``
        — the canonicalization is best-effort, not a security boundary.
        """
        try:
            args_str = json.dumps(args, sort_keys=True, default=str)
        except (TypeError, ValueError):
            args_str = repr(args)
        return f"{action}({args_str})"
