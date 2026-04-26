"""Intervention — output of a recovery primitive composition.

Carries the user-role message that should be injected into the
conversation, plus metadata (which primitives produced it) for
observability and downstream playbook decisions.

See ``docs/robust-harness/DESIGN.md`` §3.2 for the full type taxonomy.
v1 only uses ``MessageInjection``-shaped interventions; ``ParamAdjustment``
and ``StateReset`` are reserved for Step 3+.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Intervention:
    """A single recovery action targeted at the next LLM turn.

    Attributes:
        message: The user-role text to inject before the next call. Empty
            string means the caller should not inject anything (e.g.,
            when the recovery primitive composition produced no output).
        primitives: Names of the recovery primitives that composed this
            Intervention, in the order they were applied. Used by the
            observability layer to record what was tried.
    """

    message: str
    primitives: list[str] = field(default_factory=list)
