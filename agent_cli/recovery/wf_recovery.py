"""Wire-format-dependent intervention builders.

These factories compose recovery primitives with wording sourced from
the active wire-format plugin (``failure_framing_*``,
``constraint_reminder_*``, ``static_retry_hint_*``). When a new plugin
is added, this module is the audit point: every wf-aware composer
lives here and pulls strings off the plugin's :class:`WireFormat`
Protocol, so the per-plugin text stays inside the plugin file and the
composition stays here.

WF-agnostic builders live in ``recovery.common_recovery``. The split
along the wf-dependence axis lets a new plugin land without touching
``common_recovery``, while changes to recovery wording for one plugin
ripple through this file alone.

The dependency direction stays one-way: ``recovery`` depends on
primitives it owns; lower layers do not depend back on ``recovery``.
"""

from __future__ import annotations

from agent_cli.recovery.intervention import Intervention
from agent_cli.recovery.primitives import echo_prior_output
from agent_cli.wire_formats import get as _get_wire_format


def _resolve_wire_format(wire_format):
    """Backward-compat fallback to the default wire format (DEFAULT_WIRE_FORMAT).

    The recovery package's format-agnostic boundary is preserved by
    ``recovery/__init__.py`` not re-exporting this module: only callers
    who explicitly import ``recovery.wf_recovery`` pull in the
    wire_formats dependency. The format-aware nature of this module is
    therefore self-evident at the import site, no lazy indirection
    required.
    """
    if wire_format is not None:
        return wire_format
    return _get_wire_format()


def format_no_json_retry(*, prior_content: str = "", wire_format=None) -> Intervention:
    """Build the Intervention for an LLM response that failed to parse.

    Composes recovery primitives: echoes the model's prior output (failure
    grounding) and reminds the model of the required envelope
    (constrain). Falls back to the plugin's static "no JSON" hint when no
    echoable content is available.

    ``wire_format`` selects which envelope wording to use. Omitting it
    falls back to the default wire format (DEFAULT_WIRE_FORMAT) so existing callers
    (the loop's pre-Step-6 call sites, every test in
    ``test_retry_builders``) keep their original behavior bit-for-bit.

    Returns an :class:`Intervention` carrying both the user-role message
    to inject and the names of primitives composed (for observability).

    Keyword-only to avoid silent positional misuse.
    """
    wf = _resolve_wire_format(wire_format)
    echo = echo_prior_output(prior_content)
    if not echo:
        return Intervention(message=wf.static_retry_hint_no_json(), primitives=[])

    msg = "\n".join(
        [
            wf.failure_framing_parse_fail(),
            "",
            echo,
            "Honor that. " + wf.constraint_reminder_call(),
        ]
    )
    return Intervention(
        message=msg,
        primitives=["echo_prior_output", "constrain_format_json"],
    )


def format_no_action_retry(
    *, prior_content: str = "", wire_format=None
) -> Intervention:
    """Build the Intervention when parsing succeeded but no action was provided.

    Same failure-grounding rationale as ``format_no_json_retry``.
    ``wire_format`` defaults to the default wire format (DEFAULT_WIRE_FORMAT) —
    see that builder's docstring for the rationale.
    """
    wf = _resolve_wire_format(wire_format)
    echo = echo_prior_output(prior_content)
    if not echo:
        return Intervention(message=wf.static_retry_hint_no_action(), primitives=[])

    msg = "\n".join(
        [
            wf.failure_framing_no_action(),
            "",
            echo,
            "Honor that. " + wf.constraint_reminder_action_required(),
        ]
    )
    return Intervention(
        message=msg,
        primitives=["echo_prior_output", "constrain_action_required"],
    )
