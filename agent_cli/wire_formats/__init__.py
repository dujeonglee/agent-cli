"""Pluggable wire format system.

A wire format is the on-the-wire shape of a single LLM response —
prompt rules, parser, recovery messages, prefill, and provider quirks
bundled in one module. Plugins live in
``agent_cli/wire_formats/<name>.py`` and self-register at import time
via :func:`register`.

The loop / prompts / recovery layers depend only on
:class:`WireFormat` (Protocol) and :class:`ParsedAction` (data) — they
never branch on a plugin's name. New formats are added by dropping a
file into this directory; obsolete ones are deleted by removing the
file. Main code is not edited either way.

The CLI ``--response-format <name>`` option resolves through
:func:`get`. The default ``"react"`` plugin is registered when its
module is imported (see ``agent_cli/wire_formats/react.py``).
"""

from __future__ import annotations

from agent_cli.wire_formats.base import Op, ParsedAction, ParsedTurn, WireFormat

# ── Registry ─────────────────────────────────────
_registry: dict[str, WireFormat] = {}

# Single source of truth for the default wire format — the CLI's
# --response-format default, the new-session default, and the get(None) /
# unspecified-wire fallback all resolve here. Change the default in ONE place.
#
# json_fc (2026-07-17, v6.0.0 — PHASE4): md_array 의 리네임+리셰이프 후계.
# 마크다운 헤더 envelope 제거(산문 thought + bare op 배열 — xml_fc D4 동형),
# JSON 수리 기계는 무변경 승계. bakeoff A/B(27B/35B, 140run)에서 md_array 와
# 동등(completed 100%·pf 0) 확인 후 교체. alias 없음(D8) — 구 이름
# "md_array" 는 KeyError fail-fast (MAJOR 마이그레이션 노트 참조).
# (md_array 자체는 2026-06-11 prefix_md 를 대체했던 검증 계보.)
DEFAULT_WIRE_FORMAT = "json_fc"


def register(wire_format: WireFormat) -> None:
    """Register a plugin under its ``name`` attribute.

    Idempotent on identity (re-registering the same instance is a no-op);
    raises ``ValueError`` on a name collision with a *different* instance
    so accidental shadowing is loud rather than silent.

    Plugins call this at the bottom of their module:

        register(ReActFormat())
    """
    name = wire_format.name
    existing = _registry.get(name)
    if existing is wire_format:
        return
    if existing is not None:
        raise ValueError(
            f"Wire format '{name}' is already registered to a different "
            f"instance. Each plugin module should register exactly once."
        )
    _registry[name] = wire_format


def get(name: str | None = None) -> WireFormat:
    """Return the registered plugin for ``name`` — or ``DEFAULT_WIRE_FORMAT``
    when ``name`` is None/empty (the single default source).

    Raises ``KeyError`` with the list of available names if no plugin is
    registered under ``name`` — the list is what the CLI's ``--response-format``
    option would accept.
    """
    name = name or DEFAULT_WIRE_FORMAT
    plugin = _registry.get(name)
    if plugin is None:
        available = ", ".join(sorted(_registry)) or "(none)"
        raise KeyError(
            f"Wire format '{name}' is not registered. Available: {available}."
        )
    return plugin


def list_names() -> list[str]:
    """Return the sorted list of registered plugin names.

    Used by the CLI to populate help text / validate ``--response-format``
    values.
    """
    return sorted(_registry)


# ── 모델별 바인딩 (Phase 1 — docs/multi-wire-format/DESIGN.md) ─


def wire_format_for_model(model: str) -> str | None:
    """models.json 모델 엔트리의 ``wire_format`` 바인딩 이름 (없으면 None).

    바인딩은 capabilities(모델이 뭘 할 수 있나)가 아니라 "우리가 어떤
    shape 로 말할까"라 ``ModelCapabilities`` 에 태우지 않고 모델명-키로
    직접 조회한다 — role md 의 model 오버라이드 경로는 capabilities 를
    재해석하지 않으므로, dataclass 필드로는 그 경로에 닿지 않는다.
    이름의 등록 여부는 여기서 검증하지 않는다(:func:`resolve_wire_format`
    / caller 의 ``get()`` 이 fail-fast 담당).
    """
    if not model:
        return None
    from agent_cli.config import get_model_entry

    entry = get_model_entry(model)
    if not entry:
        return None
    binding = entry.get("wire_format")
    return binding if isinstance(binding, str) and binding else None


def try_foreign_parse(bound: WireFormat, llm_text: str):
    """foreign-format 구제 (Phase 3 — multi-wire-format DESIGN §9).

    바인딩 포맷(``bound``)이 0-op 로 읽은 emission 을 **타 등록 포맷**
    파서로 시도해, action-보유 ops 를 내는 첫 포맷의
    ``(ParsedTurn, format_name)`` 을 반환 (없으면 None). 실측 근거:
    35B bakeoff 0-op 캡처의 17%가 md_array 회귀 (PHASE2.md §8).

    - 순서: ``DEFAULT_WIRE_FORMAT`` 먼저 (누출 최빈 — 모델들이 가장 많이
      노출된 포맷), 나머지는 이름순 (결정적).
    - 수용 게이트 = ops 존재 + action 보유 op 존재: 각 파서의 자체 가드
      (md_array 헤더리스 경로의 ``"action"`` 키 요구, xml_fc 의 라인-앵커
      등록-도구명)와 합쳐져 산문 오인을 차단한다.
    - 포맷 간 코드 결합 없음 — 각 플러그인은 서로를 모르고, 조합은
      레지스트리(여기)가 소유 (self-contained 불변식 유지).
    - caller(dispatch)는 구제 turn 을 corrected_record 로 직렬화해 prior 가
      **바인딩 포맷의 캐노니컬 shape** 로 재렌더되게 한다 — 누출 raw 의
      재공급(mimicry 강화) 없이 다음 턴부터 모델을 교정.
    """
    if not (llm_text or "").strip():
        return None
    names = [DEFAULT_WIRE_FORMAT] + [
        n for n in sorted(_registry) if n != DEFAULT_WIRE_FORMAT
    ]
    for name in names:
        plugin = _registry.get(name)
        if plugin is None or plugin is bound:
            continue
        try:
            turn = plugin.parse_turn(llm_text)
        except Exception:
            continue  # 타 파서의 예외가 구제 시도를 죽이면 안 됨
        # 수용 게이트: stage 1(정상)·2(수리)만 — stage 3(regex 긁기)은
        # 키메라(예4 실측)에서 action 이름만 긁고 input 을 잃는 저신뢰
        # 조각이라 cross-format 추측으로는 배제. op 는 action + dict input
        # 둘 다 있어야 유효 (input 없는 조각 op 로 도구를 잘못 쏘지 않게).
        if turn.parse_stage in (1, 2) and any(
            op.action and isinstance(op.action_input, dict) for op in turn.ops
        ):
            return turn, name
    return None


def resolve_wire_format(
    *,
    explicit: str | None,
    session_format: str | None,
    model: str = "",
) -> WireFormat:
    """해석 체인: 명시 플래그 > resume 세션 메타 > 모델 바인딩 > DEFAULT.

    - ``explicit``: 사용자가 직접 준 ``--response-format`` (미지정 = None).
      사용자의 말이 항상 최우선 (D1).
    - ``session_format``: resume 세션 메타의 ``response_format`` — 세션이
      그 포맷으로 축적한 transcript 와의 정합 우선. 바인딩이 나중에
      바뀌어도 기존 세션은 기록된 포맷으로 안정 resume (새 바인딩은
      새 세션부터).
    - ``model``: 해석된 모델명 — models.json 바인딩 조회용.

    unknown 이름은 어느 소스든 ``KeyError`` (D2: 조용한 폴백은 "바인딩
    됐다고 믿는" 오진을 만든다 — fail-fast). 전부 None 이면
    ``DEFAULT_WIRE_FORMAT`` — 종전과 바이트 동일 경로.
    """
    name = explicit or session_format or wire_format_for_model(model)
    return get(name)


# ── Format-agnostic system-injected user-message prefixes ─────
# Used by ``all_system_user_prefixes`` below. These three are emitted
# by code paths that don't belong to any single wire format:
#   - ``"⚡ User interrupted."`` — Ctrl-C handler in the loop.
#   - ``"You have called"`` — B1 (action loop) probe_progress primitive.
#   - ``"You were asked to:"`` — B1 restate_task primitive.
# Format-specific framings (parse-fail / no-action / no-thought
# retry messages) live in each plugin's ``system_user_prefixes()`` and
# are unioned at consume time.
_FORMAT_AGNOSTIC_USER_PREFIXES: tuple[str, ...] = (
    "⚡ User interrupted.",
    "You have called",
    "You were asked to:",
)


def all_system_user_prefixes() -> tuple[str, ...]:
    """Return every prefix that marks a user-role message as system-injected.

    The single entry point for code that needs to filter system notices
    out of conversation history (resume preview, telemetry, anything
    that reads ``history.jsonl``). Returned tuple = format-agnostic
    prefixes + every registered plugin's ``system_user_prefixes()``.

    Order is not significant — callers use ``any(startswith(p) for p in …)``.
    """
    plugin_prefixes: tuple[str, ...] = ()
    for name in sorted(_registry):
        plugin_prefixes += _registry[name].system_user_prefixes()
    return _FORMAT_AGNOSTIC_USER_PREFIXES + plugin_prefixes


__all__ = [
    "ParsedAction",
    "ParsedTurn",
    "Op",
    "WireFormat",
    "register",
    "get",
    "list_names",
    "wire_format_for_model",
    "resolve_wire_format",
    "try_foreign_parse",
    "all_system_user_prefixes",
]


# ── Builtin plugin registration ──────────────────────────────
# Plugins shipped with agent-cli register at package-import time so
# ``get("react")`` works out of the box. The import is at the bottom
# (not the top) so the ``register`` symbol it depends on is already
# defined when ``react.py`` is loaded — the alternative (top-level
# import + explicit register call) would fail because ``react`` would
# not yet see ``register`` in this module's namespace.
def _register_builtin_plugins() -> None:
    from agent_cli.wire_formats.json_fc import JsonFcFormat
    from agent_cli.wire_formats.react import ReActFormat
    from agent_cli.wire_formats.xml_fc import XmlFcFormat

    register(ReActFormat())
    register(JsonFcFormat())  # default — md_array 후계 (PHASE4, bakeoff 게이트 통과)
    register(XmlFcFormat())  # 태그-파라미터 (multi-wire-format PHASE2)


_register_builtin_plugins()
