"""Dialects — the on-the-wire shape of a single LLM response.

A dialect bundles prompt rules, parser, decoding grammar, recovery messages and
history round-trip for one tool-call shape. Since Phase 5 a dialect is **data**:
a :class:`~agent_cli.dialects.spec.DialectSpec` in ``agent_cli/dialects/specs/``
driven by the single :class:`~agent_cli.dialects.engine.Dialect` engine. The
loop / prompts / recovery layers depend only on :class:`DialectBase` (the
protocol) and :class:`ParsedAction` (data) — they never branch on a name.

The CLI ``--dialect <name>`` option resolves through :func:`get`. 내장
방언(json_fc·xml_fc·hermes_json·glm_argkey)은 패키지 import 시 등록된다.
"""

from __future__ import annotations

from agent_cli.dialects.base import DialectBase, Op, ParsedAction, ParsedTurn

# ── Registry ─────────────────────────────────────
_registry: dict[str, DialectBase] = {}

# Single source of truth for the default dialect — the CLI's
# --dialect default, the new-session default, and the get(None) /
# unspecified-wire fallback all resolve here. Change the default in ONE place.
#
# json_fc (2026-07-17, v6.0.0 — PHASE4): md_array 의 리네임+리셰이프 후계.
# 마크다운 헤더 envelope 제거(산문 thought + bare op 배열 — xml_fc D4 동형),
# JSON 수리 기계는 무변경 승계. bakeoff A/B(27B/35B, 140run)에서 md_array 와
# 동등(completed 100%·pf 0) 확인 후 교체. alias 없음(D8) — 구 이름
# "md_array" 는 KeyError fail-fast (MAJOR 마이그레이션 노트 참조).
# (md_array 자체는 2026-06-11 prefix_md 를 대체했던 검증 계보.)
DEFAULT_DIALECT = "json_fc"


def register(dialect: DialectBase) -> None:
    """Register a plugin under its ``name`` attribute.

    Idempotent on identity (re-registering the same instance is a no-op);
    raises ``ValueError`` on a name collision with a *different* instance
    so accidental shadowing is loud rather than silent.

    Plugins call this at the bottom of their module:

        register(Dialect(JSON_FC))
    """
    name = dialect.name
    existing = _registry.get(name)
    if existing is dialect:
        return
    if existing is not None:
        raise ValueError(
            f"Dialect '{name}' is already registered to a different "
            f"instance. Each plugin module should register exactly once."
        )
    _registry[name] = dialect


def get(name: str | None = None) -> DialectBase:
    """Return the registered plugin for ``name`` — or ``DEFAULT_DIALECT``
    when ``name`` is None/empty (the single default source).

    Raises ``KeyError`` with the list of available names if no plugin is
    registered under ``name`` — the list is what the CLI's ``--dialect``
    option would accept.
    """
    name = name or DEFAULT_DIALECT
    plugin = _registry.get(name)
    if plugin is None:
        available = ", ".join(sorted(_registry)) or "(none)"
        raise KeyError(f"Dialect '{name}' is not registered. Available: {available}.")
    return plugin


def list_names() -> list[str]:
    """Return the sorted list of registered plugin names.

    Used by the CLI to populate help text / validate ``--dialect``
    values.
    """
    return sorted(_registry)


# ── 모델별 바인딩 (Phase 1 — docs/dialects/DESIGN.md) ─


def dialect_for_model(model: str) -> str | None:
    """models.json 모델 엔트리의 ``dialect`` 바인딩 이름 (없으면 None).

    바인딩은 capabilities(모델이 뭘 할 수 있나)가 아니라 "우리가 어떤
    shape 로 말할까"라 ``ModelCapabilities`` 에 태우지 않고 모델명-키로
    직접 조회한다 — role md 의 model 오버라이드 경로는 capabilities 를
    재해석하지 않으므로, dataclass 필드로는 그 경로에 닿지 않는다.
    이름의 등록 여부는 여기서 검증하지 않는다(:func:`resolve_dialect`
    / caller 의 ``get()`` 이 fail-fast 담당).
    """
    if not model:
        return None
    from agent_cli.config import get_model_entry

    entry = get_model_entry(model)
    if not entry:
        return None
    # v10.4.0: ``dialect`` 키만 읽는다 — 옛 ``wire_format`` 키는 바인딩이 아니다.
    binding = entry.get("dialect")
    return binding if isinstance(binding, str) and binding else None


def try_foreign_parse(bound: DialectBase, llm_text: str):
    """foreign-format 구제 (Phase 3 — dialects DESIGN §9).

    바인딩 포맷(``bound``)이 0-op 로 읽은 emission 을 **타 등록 포맷**
    파서로 시도해, action-보유 ops 를 내는 첫 포맷의
    ``(ParsedTurn, format_name)`` 을 반환 (없으면 None). 실측 근거:
    35B bakeoff 0-op 캡처의 17%가 md_array 회귀 (PHASE2.md §8).

    - 순서: ``DEFAULT_DIALECT`` 먼저 (누출 최빈 — 모델들이 가장 많이
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
    names = [DEFAULT_DIALECT] + [n for n in sorted(_registry) if n != DEFAULT_DIALECT]
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


_override: str | None = None


def set_dialect_override(name: str | None) -> None:
    """``--dialect`` 를 프로세스 전체에 강제한다 — main 이 부트 때 한 번 호출.

    플래그는 세션(프로세스) 단위의 사용자 결정이라 main 과 모든
    서브에이전트가 같은 값을 본다. None 이면 강제 없음.
    """
    global _override
    _override = name


class DialectUnbound(KeyError):
    """모델에 방언이 묶여 있지 않다 — 해석 체인의 두 소스가 모두 비었다."""

    def __init__(self, model: str):
        from agent_cli.config import _GLOBAL_MODELS_PATH

        super().__init__(
            f"No dialect for model '{model or '(none)'}'. Set \"dialect\" on its "
            f"entry in {_GLOBAL_MODELS_PATH} (one of: {', '.join(list_names())}) "
            f"or pass --dialect."
        )


def resolve_dialect(model: str) -> DialectBase:
    """해석 체인 (v10.3.0): ``--dialect`` 강제 > models.json 모델 바인딩.

    둘 다 없으면 :class:`DialectUnbound` — 기본값은 없다. 방언은 모델에
    묶인 설정이고, 묶이지 않은 모델은 사용자가 묶어야 한다. unknown
    이름은 어느 소스든 ``KeyError`` (D2: 조용한 폴백 금지). main 과
    서브에이전트가 같은 함수를 부르므로 체인도 하나다.
    """
    name = _override or dialect_for_model(model)
    if name is None:
        raise DialectUnbound(model)
    return get(name)


# ── Format-agnostic system-injected user-message prefixes ─────
# Used by ``all_system_user_prefixes`` below. These three are emitted
# by code paths that don't belong to any single dialect:
#   - ``"⚡ User interrupted."`` — Ctrl-C handler in the loop.
#   - ``"You have called"`` — B1 (action loop) probe_progress primitive.
#   - ``"You were asked to:"`` — B1 restate_task primitive.
# Format-specific framings (parse-fail / no-action / no-thought
# retry messages) live in each plugin's ``system_user_prefixes()`` and
# are unioned at consume time.
_FORMAT_AGNOSTIC_USER_PREFIXES: tuple[str, ...] = (
    "⚡ User interrupted.",
    # v9.18.0 이후로는 주입하지 않는다(꼬리의 `## Open Requests` + Task
    # Guidelines 와 같은 말을 세 번째로 하던 사본). 접두는 남긴다 — 9.17 이하
    # 세션을 resume 하면 history 에 이 문구가 들어 있고, 프리뷰에서 걸러져야
    # 한다.
    "⚡ Another user request arrived",
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
    "DialectBase",
    "DialectUnbound",
    "Op",
    "ParsedAction",
    "ParsedTurn",
    "all_system_user_prefixes",
    "dialect_for_model",
    "get",
    "list_names",
    "register",
    "resolve_dialect",
    "set_dialect_override",
    "try_foreign_parse",
]


# ── Builtin plugin registration ──────────────────────────────
# Plugins shipped with agent-cli register at package-import time so
# ``get("json_fc")`` works out of the box. The import is at the bottom
# (not the top) so the ``register`` symbol it depends on is already
# defined when the plugin module is loaded.
def _register_builtin_plugins() -> None:
    from agent_cli.dialects.engine import Dialect
    from agent_cli.dialects.specs.glm_argkey import GLM_ARGKEY
    from agent_cli.dialects.specs.hermes_json import HERMES_JSON
    from agent_cli.dialects.specs.json_fc import JSON_FC
    from agent_cli.dialects.specs.native_fc import NATIVE_FC
    from agent_cli.dialects.specs.xml_fc import XML_FC

    # 방언은 스펙 하나 — 넷 다 같은 엔진 (v10.0.1 에서 json_fc/xml_fc 껍데기 클래스 삭제)
    register(Dialect(JSON_FC))  # default — md_array 후계 (PHASE4, bakeoff 게이트 통과)
    register(Dialect(XML_FC))  # 태그-파라미터 (dialects PHASE2)
    # Phase 5 S4 — 스펙만으로 추가된 방언 (실모델 미검증, PHASE5 D3)
    register(
        Dialect(HERMES_JSON)
    )  # 가족 ① Hermes JSON (Qwen2.5/3/Next, Hermes, Granite 4.0/4.1)
    register(Dialect(GLM_ARGKEY))  # 가족 ⑤ GLM-4.5 ~ 5.3
    register(Dialect(NATIVE_FC))  # 서버 네이티브 함수 호출 (NATIVE.md, v10.2.0)


_register_builtin_plugins()
