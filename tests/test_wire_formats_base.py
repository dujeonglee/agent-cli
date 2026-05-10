"""Unit tests for the wire-format plugin base layer.

Covers ``agent_cli/wire_formats/base.py`` (data + Protocol) and the
registry in ``agent_cli/wire_formats/__init__.py``. Concrete plugins
(``ReActFormat`` etc.) are tested in their own files.

The Protocol itself isn't directly testable — instead we use a small
mock implementation to verify the registry's behavior and that a
typical plugin shape satisfies the Protocol's runtime check.
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


class _MockFormat:
    """Minimal WireFormat implementation for registry/Protocol tests."""

    name = "_mock_for_tests"
    thought_required = False

    def format_rules(self) -> str:
        return "## Mock Rules"

    def wrap_action_input_example(self, action: str, args_json: str, idval: str) -> str:
        return args_json

    def wrap_full_call_example(self, action: str, args_json: str, idval: str) -> str:
        return f'{{"action": "{action}", "action_input": {args_json}}}'

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

    def prefill(self) -> str:
        return ""

    def normalize_assistant_text(self, raw: str) -> str:
        return raw

    def provider_call_kwargs(self) -> dict:
        return {}


class TestProtocolConformance:
    """A typical plugin shape should satisfy the Protocol at runtime.

    ``runtime_checkable`` only validates attribute *presence*, not type
    signatures — the test still catches the most common mistake
    (missing method) which is the realistic risk for Protocol-based
    plugin systems.
    """

    def test_mock_satisfies_protocol(self):
        plugin = _MockFormat()
        assert isinstance(plugin, WireFormatProtocol)

    def test_missing_method_fails_check(self):
        class Incomplete:
            name = "incomplete"

        assert not isinstance(Incomplete(), WireFormatProtocol)


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
        class _MockB:
            name = "bbb"
            thought_required = False
            # Minimal Protocol satisfaction — only ``name`` is read by
            # list_names, but isinstance check would fail without the
            # rest. We register without going through the Protocol check.

            def format_rules(self) -> str:
                return ""

            def wrap_action_input_example(self, a, b, c) -> str:
                return ""

            def wrap_full_call_example(self, a, b, c) -> str:
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

            def prefill(self) -> str:
                return ""

            def normalize_assistant_text(self, raw: str) -> str:
                return raw

            def provider_call_kwargs(self) -> dict:
                return {}

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
