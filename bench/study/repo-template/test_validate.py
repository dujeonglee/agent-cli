"""Existing tests — deliberately boundary-blind (pair-1 P1 adds the
boundary cases these miss)."""

import pytest

from validate import ValidationError, validate_priority, validate_title


def test_normal_title_passes():
    assert validate_title("write the report") == "write the report"


def test_title_is_stripped():
    assert validate_title("  hello  ") == "hello"


def test_none_title_rejected():
    with pytest.raises(ValidationError):
        validate_title(None)


def test_normal_priority_passes():
    assert validate_priority("2") == 2


def test_non_numeric_priority_rejected():
    with pytest.raises(ValidationError):
        validate_priority("high")
