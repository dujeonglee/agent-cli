"""Unit tests for the wire-format plugin base layer.

Covers ``agent_cli/wire_formats/base.py`` (``ParsedAction`` + the
``WireFormat`` ABC and its concrete defaults) and the registry in
``agent_cli/wire_formats/__init__.py``. Concrete plugins (``JsonFcFormat``
etc.) are tested in their own files.

A small mock subclass implements every abstract method but inherits the
defaults (history pipeline round-trip, identity hooks, shared format-
rules builder). The mock verifies registry behavior and exercises the
default implementations so changes to the base land here too.
"""

from __future__ import annotations

import pytest

from agent_cli.wire_formats import (
    Op,
    ParsedAction,
    ParsedTurn,
    WireFormat,
    get,
    list_names,
    register,
)
from agent_cli.wire_formats.base import WireFormat as WireFormatProtocol

# ─── ParsedAction ──────────────────────────────────


class TestParsedAction:
    """ParsedAction is the boundary type between plugin and loop.

    Field defaults must allow constructing a "parse failed" instance
    with no arguments — the recovery path needs that early-return shape.
    """

    def test_default_construction_yields_failed_parse(self):
        p = ParsedAction()
        assert p.thought is None
        assert p.action is None
        assert p.action_input is None
        assert p.raw == ""
        assert p.parse_stage == 0  # 0 == failed
        assert p.thinking is None
        assert p.truncated is False

    def test_full_construction(self):
        p = ParsedAction(
            thought="t",
            action="read_file",
            action_input={"path": "x"},
            raw="raw",
            parse_stage=1,
            thinking="leading think",
            truncated=False,
        )
        assert p.action == "read_file"
        assert p.action_input == {"path": "x"}
        assert p.parse_stage == 1
        assert p.thinking == "leading think"


# ─── Mock plugin used by registry tests ─────────────


class _MockFormat(WireFormatProtocol):
    """Minimal WireFormat implementation for registry / ABC tests.

    Implements every abstract method (v8.41.0 ABC: ``parse_turn`` 이 1차
    추상, ``parse`` 는 첫-op 투영 기본 상속); inherits the concrete
    defaults (history pipeline, identity hooks) so the mock stays minimal."""

    name = "_mock_for_tests"
    thought_required = False

    def format_rules(self) -> str:
        return "Mock rules."

    def render_full_example(self, *, thought, action, action_input) -> str:
        if thought is None:
            return f'{{"action": "{action}", "action_input": {action_input}}}'
        return (
            f'{{"thought": "{thought}", '
            f'"action": "{action}", '
            f'"action_input": {action_input}}}'
        )

    def parse_turn(self, llm_text: str) -> ParsedTurn:
        return ParsedTurn(raw=llm_text)

    def constraint_reminder_call(self) -> str:
        return "mock call reminder"

    def constraint_reminder_action_required(self) -> str:
        return "mock action reminder"

    def failure_framing_parse_fail(self) -> str:
        return "Mock parse fail."

    def failure_framing_no_action(self) -> str:
        return "Mock no action."

    def static_retry_hint_no_json(self) -> str:
        return "Mock static no json."

    def static_retry_hint_no_action(self) -> str:
        return "Mock static no action."

    def system_user_prefixes(self) -> tuple[str, ...]:
        return ("Mock parse fail.", "Mock no action.")


class _ConfigurableFormat(_MockFormat):
    """Mock whose ``parse_turn()`` returns a preset ParsedTurn, to exercise
    the default ``parse()`` first-op projection in isolation (v8.41.0 역전:
    parse_turn 이 1차 추상, parse 가 파생 기본)."""

    name = "_configurable_for_tests"

    def __init__(self, turn: ParsedTurn):
        self._turn = turn

    def parse_turn(self, llm_text: str) -> ParsedTurn:
        return self._turn


class TestParseDefaultProjection:
    """``WireFormat.parse`` defaults to the first-op projection of
    ``parse_turn`` — 레거시 단수 소비자(history 직렬화 기본·직접 호출)용.
    v8.41.0 역전: 종전엔 parse 가 추상 + parse_turn 기본이 그것을 감쌌는데
    등록 포맷 전부 multi-op 라 그 기본이 사문이었다."""

    def test_first_op_projected(self):
        turn = ParsedTurn(
            thought="t",
            ops=[Op("read_file", {"path": "x"}), Op("shell", {"command": "ls"})],
            raw="raw",
            parse_stage=1,
            thinking="th",
        )
        pa = _ConfigurableFormat(turn).parse("raw")
        assert isinstance(pa, ParsedAction)
        assert pa.action == "read_file"
        assert pa.action_input == {"path": "x"}
        # turn-level metadata carried through verbatim
        assert pa.thought == "t"
        assert pa.raw == "raw"
        assert pa.parse_stage == 1
        assert pa.thinking == "th"

    def test_dropped_action_op_preserves_input(self):
        # parse-preservation invariant 승계: action 없는 op 도 input 이
        # 살아 있으면 투영에 그대로 실린다 (infer_action / echo 전제).
        turn = ParsedTurn(
            ops=[Op(None, {"code_index_queries": [{"mode": "list"}]})],
            parse_stage=3,
        )
        pa = _ConfigurableFormat(turn).parse("raw")
        assert pa.action is None
        assert pa.action_input == {"code_index_queries": [{"mode": "list"}]}
        assert pa.parse_stage == 3

    def test_total_failure_projects_empty(self):
        pa = _ConfigurableFormat(ParsedTurn(parse_stage=0)).parse("garbage")
        assert pa.action is None and pa.action_input is None
        assert pa.parse_stage == 0

    def test_truncated_flag_carried_from_op(self):
        turn = ParsedTurn(
            ops=[Op("edit_file", {"x": 1}, truncated=True)], parse_stage=1
        )
        assert _ConfigurableFormat(turn).parse("raw").truncated is True

    def test_react_render_round_trips_through_parse_turn(self):
        # react is multi-op now: render_full_example emits {thought, actions:
        # [op]} and parse_turn reads it back as a 1-op turn. `terminal` is
        # always False (completion is an explicit `complete` op). parse() is
        # the single-op projection and no longer mirrors parse_turn for a
        # multi-op format, so we verify parse_turn directly.
        wf = get("json_fc")
        text = wf.render_full_example(
            thought="reason",
            action="shell",
            action_input=wf.render_action_input({"command": "ls"}),
        )
        turn = wf.parse_turn(text)
        assert turn.terminal is False
        assert len(turn.ops) == 1
        assert turn.ops[0].action == "shell"
        assert turn.ops[0].action_input == {"command": "ls"}
        assert turn.thought == "reason"


class TestABCConformance:
    """A typical plugin shape should be a valid WireFormat subclass.

    The base is an ABC, so missing ``@abstractmethod`` implementations
    fail at instantiation rather than at the isinstance check — that
    catches the most common plugin-author mistake (forgetting a
    method) at the earliest possible moment.
    """

    def test_mock_inherits_from_base(self):
        plugin = _MockFormat()
        assert isinstance(plugin, WireFormatProtocol)

    def test_missing_abstractmethod_fails_instantiation(self):
        class Incomplete(WireFormatProtocol):
            name = "incomplete"
            thought_required = True
            # missing every abstract method

        with pytest.raises(TypeError) as exc_info:
            Incomplete()
        # Python's ABC mechanism mentions the abstract method name(s) in
        # the error so plugin authors see what's missing.
        assert "abstract" in str(exc_info.value).lower()

    def test_unrelated_class_is_not_an_instance(self):
        class Unrelated:
            name = "unrelated"

        assert not isinstance(Unrelated(), WireFormatProtocol)


# ─── Registry ──────────────────────────────────────


@pytest.fixture
def isolated_registry(monkeypatch):
    """Replace the registry with an empty dict for the duration of a test.

    Tests that mutate the registry must use this — leaking a registration
    across tests would couple test order. We monkeypatch the module-level
    dict so both ``register`` and ``get`` see the override.
    """
    from agent_cli import wire_formats as wf_pkg

    monkeypatch.setattr(wf_pkg, "_registry", {})
    yield


class TestRegistry:
    def test_get_unknown_name_raises_with_available_list(self, isolated_registry):
        # Empty registry case lists "(none)" so the CLI error is still useful.
        with pytest.raises(KeyError) as exc_info:
            get("unknown")
        msg = str(exc_info.value)
        assert "unknown" in msg
        assert "(none)" in msg

    def test_register_then_get_round_trip(self, isolated_registry):
        plugin = _MockFormat()
        register(plugin)
        assert get("_mock_for_tests") is plugin

    def test_register_idempotent_on_same_instance(self, isolated_registry):
        plugin = _MockFormat()
        register(plugin)
        # Second register of the SAME instance is a no-op, not an error.
        register(plugin)
        assert get("_mock_for_tests") is plugin

    def test_register_collision_with_different_instance_raises(self, isolated_registry):
        register(_MockFormat())
        with pytest.raises(ValueError) as exc_info:
            register(_MockFormat())  # different instance, same name
        assert "_mock_for_tests" in str(exc_info.value)

    def test_list_names_sorted(self, isolated_registry):
        class _MockB(WireFormatProtocol):
            name = "bbb"
            thought_required = False

            def format_rules(self) -> str:
                return ""

            def render_full_example(self, *, thought, action, action_input) -> str:
                return ""

            def parse_turn(self, t) -> ParsedTurn:
                return ParsedTurn()

            def constraint_reminder_call(self) -> str:
                return ""

            def constraint_reminder_action_required(self) -> str:
                return ""

            def failure_framing_parse_fail(self) -> str:
                return ""

            def failure_framing_no_action(self) -> str:
                return ""

            def static_retry_hint_no_json(self) -> str:
                return ""

            def static_retry_hint_no_action(self) -> str:
                return ""

            def system_user_prefixes(self) -> tuple[str, ...]:
                return ()

        a = _MockFormat()  # name "_mock_for_tests"
        b = _MockB()  # name "bbb"
        register(a)
        register(b)
        # Sorted by name — "_mock_for_tests" < "bbb" lexicographically.
        assert list_names() == ["_mock_for_tests", "bbb"]

    def test_real_world_top_level_imports_work(self):
        """The package re-exports ``ParsedAction`` and ``WireFormat`` so
        callers don't have to drill into ``base``. Caught by import-time
        symbol resolution — failing here means external callers break."""
        from agent_cli.wire_formats import ParsedAction as PA
        from agent_cli.wire_formats import WireFormat as WF

        assert PA is ParsedAction
        assert WF is WireFormat


class TestAllSystemUserPrefixes:
    """``all_system_user_prefixes`` is the single entry point for any code
    that needs to filter system-injected user messages (resume preview,
    telemetry). It must combine format-agnostic prefixes with every
    registered plugin's prefixes — adding a new plugin must extend the
    returned list automatically."""

    def test_includes_format_agnostic_prefixes(self):
        from agent_cli.wire_formats import all_system_user_prefixes

        prefixes = all_system_user_prefixes()
        # B1 (action loop) and interrupt — emitted by code paths
        # outside any single wire format.
        assert "⚡ User interrupted." in prefixes
        assert "You have called" in prefixes
        assert "You were asked to:" in prefixes

    def test_includes_registered_plugin_prefixes(self):
        # 내장 플러그인은 import 시 등록 — framing prefix 가 자동 합류.
        from agent_cli.wire_formats import all_system_user_prefixes

        prefixes = all_system_user_prefixes()
        assert "Your response did not match the expected format" in prefixes
        assert "Your JSON array had no usable tool call" in prefixes

    def test_isolated_registry_yields_only_format_agnostic(self, isolated_registry):
        # With a fresh empty registry no plugins contribute prefixes —
        # only the format-agnostic baseline remains. This confirms the
        # function actually pulls from the registry rather than caching.
        from agent_cli.wire_formats import all_system_user_prefixes

        prefixes = all_system_user_prefixes()
        assert "⚡ User interrupted." in prefixes
        assert "Your response was not valid JSON." not in prefixes

    def test_new_registration_extends_result(self, isolated_registry):
        # Registering a new plugin must extend ``all_system_user_prefixes``
        # without touching session.py or any other consumer — that is
        # the whole point of routing through this function.
        from agent_cli.wire_formats import all_system_user_prefixes, register

        before = all_system_user_prefixes()
        plugin = _MockFormat()  # name "_mock_for_tests"
        # Override system_user_prefixes so the test assertion is unique.
        plugin.system_user_prefixes = lambda: ("UNIQUE_MOCK_FRAMING_42",)
        register(plugin)
        after = all_system_user_prefixes()
        assert "UNIQUE_MOCK_FRAMING_42" not in before
        assert "UNIQUE_MOCK_FRAMING_42" in after


# NOTE (v8.4.0): TestProseCompletionParity 삭제 — prose_completion 자체가
# 제거됐다(산문-only 턴은 항상 NO_ACTION 넛지, 완료는 명시적 complete 만).
# 새 cross-format 계약은 test_multi_op.TestProseRequiresExplicitComplete 가
# 실루프로 고정한다.
