"""Recovery primitive toolbox for the robust-harness design.

See ``docs/robust-harness/DESIGN.md`` for the conceptual model. Primitives
are pure, harness-level functions: they take normalized inputs (strings,
dicts, registries) and return Intervention text fragments. They never
reference provider names, model names, or channel names — that
abstraction lives in the Provider Layer.
"""

from agent_cli.recovery.detectors import ActionLoopDetector
from agent_cli.recovery.intervention import Intervention
from agent_cli.recovery.observability import (
    FAILURE_ACTION_LOOP,
    FAILURE_NO_ACTION,
    FAILURE_NO_JSON,
    TurnRecord,
    TurnRecorder,
)
from agent_cli.recovery.primitives import (
    constrain_action_required,
    constrain_format_json,
    echo_prior_output,
    probe_progress,
    restate_task,
)

__all__ = [
    "echo_prior_output",
    "constrain_format_json",
    "constrain_action_required",
    "probe_progress",
    "restate_task",
    "Intervention",
    "TurnRecord",
    "TurnRecorder",
    "ActionLoopDetector",
    "FAILURE_NO_JSON",
    "FAILURE_NO_ACTION",
    "FAILURE_ACTION_LOOP",
]
