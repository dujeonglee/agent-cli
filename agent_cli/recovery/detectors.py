"""Failure detectors for the recovery layer.

Two flavors:
- **Stateful** detectors are classes owned by the AgentLoop instance,
  carrying state across turns (e.g. ``ActionLoopDetector``).
- **Stateless** detectors are pure functions called per-decision; they
  inspect a single response/dispatch attempt without remembering
  anything (e.g. ``detect_unknown_tool``, ``detect_schema_mismatch``).

The split mirrors what each failure category actually needs — adding
ceremony (a class) for a stateless one-line check would be cargo-cult
symmetry without value. New stateful failures get classes; new
stateless ones get functions.

See ``docs/robust-harness/DESIGN.md`` §1 (Layer A & B failures) and §3.1.
"""

from __future__ import annotations

import json
from typing import Any

from agent_cli.tools.registry import validate_tool_input


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


_THOUGHT_EXEMPT_ACTIONS: frozenset[str] = frozenset({"complete"})


def detect_thought_missing(thought: Any, action: Any) -> bool:
    """Stateless A7 detector: did the model emit an action without a thought?

    Returns ``True`` when ``action`` is present (any truthy value), the
    action is not in the exempt set, and ``thought`` is missing
    (``None``) or empty/whitespace-only. The recovery layer uses this
    to short-circuit dispatch and ask the model to restate with a
    populated thought field.

    The check exists because reasoning omitted from ``thought`` is
    invisible to ``read_context`` (which keys on the field, not on raw
    content) and — more importantly — the raw response carrying that
    omission is mirrored back to the model on the next turn, where it
    becomes a transcript-internal precedent that crowds out the system
    prompt's Format Rule 1. Catching it once and asking the model to
    fix it cuts the mimicry-strengthening loop.

    ``complete`` is exempt from this check: it is the final-answer
    action, and the model's reasoning slot at that point carries no
    next-turn obligation — there is no further reasoning to
    propagate. Phase 2 bakeoff (2026-05-18) measured this exemption
    eliminates 5/5 NO_THOUGHT recoveries on qwen3.6:27b's
    ``complete_direct`` task (markdown wire format) and resolves one
    outlier on the JSON wire format, with no regression elsewhere.
    """
    if not action:
        return False  # NO_ACTION (A3) is a different label
    if action in _THOUGHT_EXEMPT_ACTIONS:
        return False
    if thought is None:
        return True
    if isinstance(thought, str) and not thought.strip():
        return True
    return False


def detect_unknown_tool(action: str, tools_list: list[str]) -> bool:
    """Stateless A4 detector: is ``action`` outside the active tool registry?

    Returns ``True`` when the action references a tool not in
    ``tools_list``. The recovery layer uses this to label the failure
    and skip dispatch — the model sees the resulting observation
    (currently the same "Unknown tool 'X'. Available: [...]" message
    produced by the leaf-level dispatch).
    """
    return bool(action) and action not in tools_list


def detect_schema_mismatch(
    action: str, action_input: Any
) -> tuple[bool, str | None, Any]:
    """Stateless A5 detector: does ``action_input`` match the tool schema?

    Returns ``(mismatched, error_message, normalized_input)``:
    - ``mismatched``: True when the input violates the schema.
    - ``error_message``: human-readable description (None on success).
    - ``normalized_input``: the (possibly auto-converted) input value
      when valid — string-to-dict promotion happens inside
      ``validate_tool_input``. Caller should use this normalized value
      for downstream dispatch.

    Wraps ``tools.registry.validate_tool_input`` so the recovery layer
    has its own vocabulary entry without owning the schema knowledge.
    """
    valid, err, normalized = validate_tool_input(action, action_input)
    if valid:
        return (False, None, normalized)
    return (True, err, normalized)


def detect_nested_envelope(result_value: Any) -> bool:
    """Stateless A6 detector: does the complete result wrap another envelope?

    Some models (notably qwen3.5/3.6 family) double-wrap the complete
    action's payload — they emit
    ``{"action":"complete","action_input":{"result":"<JSON of {result:...}>"}}``
    instead of the intended single-level envelope. The user-facing
    answer ends up prefixed with a literal ``{"result": "..."}`` artifact.

    Returns True when ``result_value`` is a string that successfully
    parses as a JSON object containing a top-level ``result`` key.
    Strings that merely *start* with ``{"result"`` but fail to parse
    are not flagged — false positives on legitimate text that happens
    to look like JSON would corrupt observability data more than
    missed cases.

    v1: detection only; the caller does NOT auto-unwrap. TurnRecord
    captures occurrences so Step 4b can decide remediation based on
    measured frequency.
    """
    if not isinstance(result_value, str):
        return False
    stripped = result_value.lstrip()
    if not stripped.startswith('{"result"'):
        return False
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return False
    return isinstance(parsed, dict) and "result" in parsed
