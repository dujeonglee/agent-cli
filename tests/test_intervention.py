"""Tests for the Intervention dataclass.

Intervention is the carrier for a recovery primitive composition's output.
It bundles the user-role message to inject with the names of primitives
that produced it, so the observability layer can record what was tried.

See docs/robust-harness/DESIGN.md §3.2.
"""

import pytest

from agent_cli.recovery.intervention import Intervention


class TestIntervention:
    def test_message_required(self):
        intv = Intervention(message="hello")
        assert intv.message == "hello"

    def test_default_primitives_empty(self):
        intv = Intervention(message="x")
        assert intv.primitives == []

    def test_explicit_primitives(self):
        intv = Intervention(
            message="x", primitives=["echo_prior_output", "constrain_format_json"]
        )
        assert intv.primitives == ["echo_prior_output", "constrain_format_json"]

    def test_frozen_immutable(self):
        intv = Intervention(message="x")
        with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
            intv.message = "y"  # type: ignore[misc]

    def test_each_instance_has_own_primitives_list(self):
        # Default factory must produce a fresh list per instance —
        # otherwise mutation bleeds across calls.
        a = Intervention(message="a")
        b = Intervention(message="b")
        # frozen=True prevents reassignment but the list itself is mutable
        # via internal append; check identity to confirm fresh defaults.
        assert a.primitives is not b.primitives
