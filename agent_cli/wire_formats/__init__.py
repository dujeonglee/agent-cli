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
# md_array (2026-06-11): promoted from experimental after Phase-2 (95.2% =
# react) + real-world validation (DOOM web, 150 turns, 0.7% format-failure).
# It is a functional superset of the retired prefix_md (single-op plus
# multi-op). prefix_md was removed (2026-06-13, wire-format consolidation
# roadmap Step 1) — md_array subsumes its markdown shape. The two remaining
# formats are md_array (markdown, multi-op) and react (JSON).
DEFAULT_WIRE_FORMAT = "md_array"


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
    from agent_cli.wire_formats.md_array import MdArrayFormat
    from agent_cli.wire_formats.react import ReActFormat
    from agent_cli.wire_formats.xml_fc import XmlFcFormat

    register(ReActFormat())
    register(MdArrayFormat())  # default — multi-op (DESIGN §7)
    register(XmlFcFormat())  # 태그-파라미터 (multi-wire-format PHASE2)


_register_builtin_plugins()
