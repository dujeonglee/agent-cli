"""Unit tests for the wire-format plugin base layer.

Covers ``agent_cli/wire_formats/base.py`` (``ParsedAction`` + the
``WireFormat`` ABC and its concrete defaults) and the registry in
``agent_cli/wire_formats/__init__.py``. Concrete plugins (``ReActFormat``
etc.) are tested in their own files.

A small mock subclass implements every abstract method but inherits the
defaults (history pipeline round-trip, identity hooks, shared format-
rules builder). The mock verifies registry behavior and exercises the
default implementations so changes to the base land here too.
"""

from __future__ import annotations

import pytest

from agent_cli.wire_formats import (
    ParsedAction,
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

    Implements every abstract method; inherits the concrete defaults
    (history pipeline, identity hooks, shared format-rules builder)
    from :class:`WireFormat` so the mock stays minimal."""

    name = "_mock_for_tests"
    thought_required = False

    def format_rules_anchor(self) -> str:
        return "Mock anchor."

    def format_rules_field_specific(self) -> str:
        return "1. Mock rule 1.\n2. Mock rule 2."

    def render_full_example(self, *, thought, action, action_input) -> str:
        if thought is None:
            return f'{{"action": "{action}", "action_input": {action_input}}}'
        return (
            f'{{"thought": "{thought}", '
            f'"action": "{action}", '
            f'"action_input": {action_input}}}'
        )

    def parse(self, llm_text: str) -> ParsedAction:
        return ParsedAction(raw=llm_text)

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

            def format_rules_anchor(self) -> str:
                return ""

            def format_rules_field_specific(self) -> str:
                return ""

            def render_full_example(self, *, thought, action, action_input) -> str:
                return ""

            def parse(self, t) -> ParsedAction:
                return ParsedAction()

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
        from agent_cli.wire_formats import ParsedAction as PA, WireFormat as WF

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
        # ReAct is registered at import time; its three framings must
        # show up in the union without any extra wiring.
        from agent_cli.wire_formats import all_system_user_prefixes

        prefixes = all_system_user_prefixes()
        assert "Your response was not valid JSON." in prefixes
        assert "Your JSON was parsed but has no action." in prefixes
        assert "Your JSON was missing the 'thought' field." in prefixes

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
