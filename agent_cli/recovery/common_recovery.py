"""Wire-format-agnostic intervention builders.

These factories compose recovery primitives whose wording does not
depend on which wire-format plugin is active — every plugin sees
identical text. Currently this is the B1 (action loop) family: the
nudge does not reference JSON, ``<tool_use>``, or any other format-
specific shape, so it stays here regardless of the chosen plugin.

WF-aware builders live in ``recovery.wf_recovery``. Splitting along
the wf-dependence axis means: when a new wire-format plugin appears,
this module needs zero edits, while ``wf_recovery`` is where any
plugin-specific recovery glue is added or audited.

The dependency direction stays one-way: ``recovery`` depends on
primitives it owns; lower layers do not depend back on ``recovery``.
"""

from __future__ import annotations

from agent_cli.recovery.intervention import Intervention
from agent_cli.recovery.primitives import probe_progress, restate_task


def format_action_loop_intervention(
    *,
    level: int,
    action: str,
    args_repr: str,
    repeat_count: int,
    task: str,
) -> Intervention | None:
    """Compose the B1 (action loop) Intervention for a given escalation level.

    Skips the temperature-down level from DESIGN.md §2.3 — temperature
    handling diverges across providers, which would leak runtime
    detail into the recovery layer. Step 4 may revisit if data shows
    benefit.

    Returns:
        Intervention for level 1 or 2; ``None`` for level ≥3 (caller
        should hard-fail with an informative error).
    """
    if level == 1:
        return Intervention(
            message=probe_progress(
                action=action,
                args_repr=args_repr,
                repeat_count=repeat_count,
            ),
            primitives=["probe_progress"],
        )
    if level == 2:
        return Intervention(
            message=restate_task(
                task=task,
                action=action,
                args_repr=args_repr,
                repeat_count=repeat_count,
            ),
            primitives=["restate_task"],
        )
    return None
