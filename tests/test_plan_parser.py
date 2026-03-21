"""Tests for parsing/plan_parser."""
import pytest

from agent_cli.parsing.plan_parser import parse_plan_steps


class TestParsePlanSteps:
    def test_with_marker(self):
        text = "Here is the plan:\n>>>PLAN\n1. Read the file\n2. Analyze it\n3. Write summary"
        steps = parse_plan_steps(text)
        assert len(steps) == 3
        assert steps[0].description == "Read the file"
        assert steps[2].description == "Write summary"

    def test_without_marker(self):
        text = "1. Read the file\n2. Analyze it\n3. Write summary"
        steps = parse_plan_steps(text)
        assert len(steps) == 3

    def test_parenthesis_format(self):
        text = ">>>PLAN\n1) Read the file\n2) Analyze it"
        steps = parse_plan_steps(text)
        assert len(steps) == 2

    def test_dash_format(self):
        text = ">>>PLAN\n- Read the file\n- Analyze it"
        steps = parse_plan_steps(text)
        assert len(steps) == 2

    def test_mixed_format(self):
        text = ">>>PLAN\n1. Read the file\n2) Analyze it\n- Write summary"
        steps = parse_plan_steps(text)
        assert len(steps) == 3

    def test_renumbered(self):
        text = ">>>PLAN\n5. First step\n10. Second step"
        steps = parse_plan_steps(text)
        assert steps[0].id == 1
        assert steps[1].id == 2

    def test_empty_text(self):
        assert parse_plan_steps("") == []

    def test_no_steps(self):
        assert parse_plan_steps("No numbered list here.") == []

    def test_status_defaults_pending(self):
        steps = parse_plan_steps("1. Do something")
        assert steps[0].status == "pending"
        assert steps[0].result is None
