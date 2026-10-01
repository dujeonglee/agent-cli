"""Wire-format-dependent intervention builders.

These factories compose recovery primitives with wording sourced from
the active dialect plugin (``failure_framing_*``,
``constraint_reminder_*``, ``static_retry_hint_*``). When a new plugin
is added, this module is the audit point: every wf-aware composer
lives here and pulls strings off the plugin's :class:`DialectBase`
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

from agent_cli.dialects import get as _get_dialect
from agent_cli.recovery.intervention import Intervention
from agent_cli.recovery.primitives import echo_prior_output


def _resolve_dialect(dialect):
    """Backward-compat fallback to the default dialect (DEFAULT_DIALECT).

    The recovery package's format-agnostic boundary is preserved by
    ``recovery/__init__.py`` not re-exporting this module: only callers
    who explicitly import ``recovery.dialect_recovery`` pull in the
    dialects dependency. The format-aware nature of this module is
    therefore self-evident at the import site, no lazy indirection
    required.
    """
    if dialect is not None:
        return dialect
    return _get_dialect()


def format_no_json_retry(
    *,
    prior_content: str = "",
    dialect=None,
    syntax_error: str | None = None,
    thinking_only: bool = False,
) -> Intervention:
    """Build the Intervention for an LLM response that failed to parse.

    Composes recovery primitives: echoes the model's prior output (failure
    grounding) and reminds the model of the required envelope
    (constrain). Falls back to the plugin's static "no JSON" hint when no
    echoable content is available.

    ``dialect`` selects which envelope wording to use. Omitting it
    falls back to the default dialect (DEFAULT_DIALECT) so existing callers
    (the loop's pre-Step-6 call sites, every test in
    ``test_retry_builders``) keep their original behavior bit-for-bit.

    ``syntax_error`` (from ``DialectBase.diagnose_syntax_error``) is the
    optional "where it broke" pointer — message + line/column + caret. When
    present it is embedded as its own block after the framing, so the model
    is told the exact fault, not just "not valid JSON". Omitting it (the
    default) leaves the message bit-for-bit identical to before.

    Returns an :class:`Intervention` carrying both the user-role message
    to inject and the names of primitives composed (for observability).

    Keyword-only to avoid silent positional misuse.
    """
    wf = _resolve_dialect(dialect)
    echo = echo_prior_output(prior_content)
    if not echo and thinking_only:
        # v10.1.4 (실측 NO_OUTPUT): content 는 비었는데 사고 채널에는 글이 있다 —
        # 호출이 사고 안에 갇혔거나(미닫힘 <think>) 사고로 예산을 다 썼다.
        # "비었다" 가 아니라 **왜** 비었는지 말해야 다음 턴이 달라진다.
        return Intervention(
            message=(
                f"{wf.failure_framing_parse_fail()} Your whole reply stayed in the "
                "thinking channel — nothing reached the harness. Finish thinking "
                "first (close it), then write the call AFTER it, outside any "
                f"thinking tags. {wf.constraint_reminder_call()}"
            ),
            primitives=["thinking_only_hint", "constrain_format_json"],
        )
    if not echo:
        return Intervention(message=wf.static_retry_hint_no_json(), primitives=[])

    parts = [wf.failure_framing_parse_fail()]
    primitives = ["echo_prior_output", "constrain_format_json"]
    if syntax_error:
        parts += [syntax_error]
        primitives = ["diagnose_json_error", *primitives]
    parts += ["", echo, "Honor that. " + wf.constraint_reminder_call()]

    return Intervention(message="\n".join(parts), primitives=primitives)


def format_no_action_retry(*, prior_content: str = "", dialect=None) -> Intervention:
    """Build the Intervention when parsing succeeded but no action was provided.

    Same failure-grounding rationale as ``format_no_json_retry``.
    ``dialect`` defaults to the default dialect (DEFAULT_DIALECT) —
    see that builder's docstring for the rationale.
    """
    wf = _resolve_dialect(dialect)
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
