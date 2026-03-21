"""Tests for fuzzy edit matching in tools/edit_file."""

import pytest

from agent_cli.tools.read_file import compute_line_hash
from agent_cli.tools.edit_file import fuzzy_verify_ref, _normalize_for_fuzzy


class TestNormalize:
    def test_tabs_to_spaces(self):
        assert _normalize_for_fuzzy("\thello") == "hello"

    def test_collapse_spaces(self):
        assert _normalize_for_fuzzy("a   b") == "a b"

    def test_smart_quotes(self):
        assert _normalize_for_fuzzy("\u201chello\u201d") == '"hello"'

    def test_em_dash(self):
        assert _normalize_for_fuzzy("a\u2014b") == "a-b"


class TestFuzzyVerifyRef:
    def test_exact_match(self):
        lines = ["def hello():", "    pass"]
        h = compute_line_hash(1, lines[0])
        idx, was_fuzzy = fuzzy_verify_ref(lines, f"1#{h}")
        assert idx == 0
        assert was_fuzzy is False

    def test_fuzzy_match_accepted(self):
        """When hash doesn't match but line exists, fuzzy accepts."""
        lines = ["def hello():"]
        # Use a wrong hash — fuzzy should accept since line number is valid
        idx, was_fuzzy = fuzzy_verify_ref(lines, "1#ZZ")
        assert idx == 0
        assert was_fuzzy is True

    def test_out_of_range_raises(self):
        lines = ["only one line"]
        with pytest.raises(RuntimeError):
            fuzzy_verify_ref(lines, "5#ZZ")
