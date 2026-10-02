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
from agent_cli.recovery.primitives import bounded_excerpt, echo_prior_output


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
    swallowed: bool = False,
    prior_is_excerpt: bool = False,
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

    ``prior_is_excerpt`` (v10.5.0): ``prior_content`` is the bounded excerpt
    a structured nudge record stored — quote it without bounding again.

    Keyword-only to avoid silent positional misuse.
    """
    wf = _resolve_dialect(dialect)
    echo = echo_prior_output(prior_content, excerpt=not prior_is_excerpt)
    if not echo and swallowed:
        # v10.1.7 (실측): 서버가 토큰은 셌는데 글을 안 줬다 — 서버측 파서가 호출
        # 블록을 삼킨 것. 같은 모양으로 다시 내면 또 삼킨다. 레지스트리가 bare
        # JSON 배열을 타 방언(json_fc)으로 건지므로 그 길을 알려 준다.
        return Intervention(
            message=(
                "The server counted output tokens but delivered NO text — a "
                "server-side parser most likely consumed your tool-call block "
                "before it reached the harness. Do not repeat the same shape. "
                "Emit the call as a bare JSON array instead: "
                '[{"action": "<tool>", ...params}] — the harness accepts it and '
                "re-renders it. (The user has been told the binding should "
                "change to json_fc on this server.)"
            ),
            primitives=["swallowed_output_hint"],
        )
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


def format_no_action_retry(
    *, prior_content: str = "", dialect=None, prior_is_excerpt: bool = False
) -> Intervention:
    """Build the Intervention when parsing succeeded but no action was provided.

    Same failure-grounding rationale as ``format_no_json_retry``.
    ``dialect`` defaults to the default dialect (DEFAULT_DIALECT) —
    see that builder's docstring for the rationale.
    """
    wf = _resolve_dialect(dialect)
    echo = echo_prior_output(prior_content, excerpt=not prior_is_excerpt)
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


# ── 구조화 넛지 레코드 (v10.5.0) ────────────────────────────────
# 저장은 방언과 독립이어야 한다: history 에는 파싱 실패의 **사실**(이유·인용·
# 진단·플래그)만 남기고, 모델이 읽을 문장은 읽는 시점의 방언이 조립한다.
# 종전엔 조립된 문장을 content 로 저장해 다른 방언으로 resume 하면 엉뚱한
# 규칙("JSON 배열로 끝내라")이 재생됐다. 라이브 메시지도 같은 함수로
# 만들므로 라이브 == 캐시 == resume 이 구성상 같다.

FORMAT_NUDGE_REASONS = ("no_json", "no_action")


def make_format_nudge(
    reason: str,
    llm_text: str,
    *,
    syntax_error: str | None = None,
    thinking_only: bool = False,
    swallowed: bool = False,
) -> dict:
    """파싱 실패 한 건의 구조화 기록 — ``{"reason", "prior", …}``.

    ``prior`` 는 실패 원문의 **경계 발췌**(v9.23.2 의 head+tail) — 원문 전체는
    저장하지 않는다(32K 폭주 교훈). 플래그·진단은 참일 때만 키가 생긴다.
    ``syntax_error`` 는 실패 시점 방언의 진단문(위치·캐럿)이다 — 원문이 없어
    다시 만들 수 없으므로 데이터로 남긴다.
    """
    if reason not in FORMAT_NUDGE_REASONS:
        raise ValueError(f"unknown format nudge reason: {reason!r}")
    cleaned = (llm_text or "").strip()
    nudge: dict = {
        "reason": reason,
        "prior": bounded_excerpt(cleaned) if cleaned else "",
    }
    if syntax_error:
        nudge["syntax_error"] = syntax_error
    if thinking_only:
        nudge["thinking_only"] = True
    if swallowed:
        nudge["swallowed"] = True
    return nudge


def build_format_nudge(nudge: dict, dialect) -> Intervention:
    """구조화 넛지 → 현재 ``dialect`` 의 문장 (라이브·캐시 렌더·resume 공용)."""
    prior = str(nudge.get("prior") or "")
    if nudge.get("reason") == "no_action":
        return format_no_action_retry(
            prior_content=prior, dialect=dialect, prior_is_excerpt=True
        )
    return format_no_json_retry(
        prior_content=prior,
        dialect=dialect,
        syntax_error=nudge.get("syntax_error") or None,
        thinking_only=bool(nudge.get("thinking_only")),
        swallowed=bool(nudge.get("swallowed")),
        prior_is_excerpt=True,
    )
