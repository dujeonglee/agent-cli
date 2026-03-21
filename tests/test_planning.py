"""Tests for planning module (integration with mocked provider)."""

import json
from unittest.mock import MagicMock

import pytest

from agent_cli.planning.generator import generate_plan
from agent_cli.planning.executor import (
    execute_plan,
    _build_step_context,
    _infer_tools_for_step,
)
from agent_cli.planning.models import Plan, PlanStep
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.compat import ModelCapabilities


@pytest.fixture
def caps():
    return ModelCapabilities(
        context_window=32768,
        max_output_tokens=4096,
        supports_structured_output=True,
        supports_tool_calling=False,
        supports_thinking=False,
        thinking_budget=0,
        supports_strict_schema=False,
    )


class TestGeneratePlan:
    def test_generates_plan(self, caps):
        provider = MagicMock()
        provider.call.return_value = LLMResponse(
            content=">>>PLAN\n1. Read the file\n2. Analyze it\n3. Write summary"
        )
        plan = generate_plan(
            goal="Summarize README",
            provider=provider,
            capabilities=caps,
            model="test-model",
            quiet=True,
        )
        assert plan is not None
        assert len(plan.steps) == 3
        assert plan.goal == "Summarize README"

    def test_returns_none_on_empty(self, caps):
        provider = MagicMock()
        provider.call.return_value = LLMResponse(content="No plan here.")
        plan = generate_plan(
            goal="Do something",
            provider=provider,
            capabilities=caps,
            model="test-model",
            quiet=True,
        )
        assert plan is None

    def test_returns_none_on_error(self, caps):
        provider = MagicMock()
        provider.call.side_effect = Exception("Connection refused")
        plan = generate_plan(
            goal="Do something",
            provider=provider,
            capabilities=caps,
            model="test-model",
            quiet=True,
        )
        assert plan is None


class TestBuildStepContext:
    def test_first_step(self):
        plan = Plan(
            goal="Test goal",
            steps=[
                PlanStep(id=1, description="Step 1"),
                PlanStep(id=2, description="Step 2"),
            ],
        )
        ctx = _build_step_context(plan, 0)
        assert "Test goal" in ctx
        assert "Current step (1 of 2)" in ctx
        assert "Previous steps" not in ctx

    def test_with_completed_steps(self):
        plan = Plan(
            goal="Test goal",
            steps=[
                PlanStep(
                    id=1, description="Step 1", status="done", result="Done step 1"
                ),
                PlanStep(id=2, description="Step 2"),
            ],
        )
        ctx = _build_step_context(plan, 1)
        assert "Previous steps" in ctx
        assert "Done step 1" in ctx
        assert "Current step (2 of 2)" in ctx


class TestExecutePlan:
    def test_executes_all_steps(self, caps):
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(
                content=json.dumps({"thought": "t", "final_answer": "Step 1 done"})
            ),
            LLMResponse(
                content=json.dumps({"thought": "t", "final_answer": "Step 2 done"})
            ),
        ]
        plan = Plan(
            goal="Test",
            steps=[
                PlanStep(id=1, description="Do 1"),
                PlanStep(id=2, description="Do 2"),
            ],
        )
        result = execute_plan(
            plan=plan,
            provider=provider,
            capabilities=caps,
            model="test-model",
            quiet=True,
        )
        assert result is not None
        assert plan.steps[0].status == "done"
        assert plan.steps[1].status == "done"

    def test_skips_done_steps(self, caps):
        """Resume: done steps are skipped."""
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(
                content=json.dumps({"thought": "t", "final_answer": "Step 2 done"})
            ),
        ]
        plan = Plan(
            goal="Test",
            steps=[
                PlanStep(
                    id=1, description="Do 1", status="done", result="Already done"
                ),
                PlanStep(id=2, description="Do 2"),
            ],
        )
        result = execute_plan(
            plan=plan,
            provider=provider,
            capabilities=caps,
            model="test-model",
            quiet=True,
        )
        assert result is not None
        # Provider should only be called once (for step 2)
        assert provider.call.call_count == 1

    def test_save_path(self, caps, tmp_path):
        """Plan is saved after each step when save_path is set."""
        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(content=json.dumps({"thought": "t", "final_answer": "done"})),
        ]
        plan = Plan(goal="Test", steps=[PlanStep(id=1, description="Do 1")])
        save_file = tmp_path / "plan.json"

        execute_plan(
            plan=plan,
            provider=provider,
            capabilities=caps,
            model="test-model",
            quiet=True,
            save_path=str(save_file),
        )

        assert save_file.exists()
        loaded = Plan.load(save_file)
        assert loaded.steps[0].status == "done"


class TestPlanSerialization:
    def test_roundtrip(self):
        plan = Plan(
            goal="Test goal",
            steps=[
                PlanStep(id=1, description="Step 1", status="done", result="Result 1"),
                PlanStep(id=2, description="Step 2", status="pending"),
            ],
            current_step=1,
        )
        data = plan.to_dict()
        restored = Plan.from_dict(data)
        assert restored.goal == plan.goal
        assert len(restored.steps) == 2
        assert restored.steps[0].status == "done"
        assert restored.steps[0].result == "Result 1"
        assert restored.steps[1].status == "pending"
        assert restored.current_step == 1

    def test_save_and_load(self, tmp_path):
        plan = Plan(
            goal="Saved goal",
            steps=[PlanStep(id=1, description="Step 1", status="done", result="OK")],
        )
        path = tmp_path / "plan.json"
        plan.save(path)

        loaded = Plan.load(path)
        assert loaded.goal == "Saved goal"
        assert loaded.steps[0].result == "OK"

    def test_step_roundtrip(self):
        step = PlanStep(
            id=3, description="Do something", status="failed", result="Error"
        )
        restored = PlanStep.from_dict(step.to_dict())
        assert restored.id == 3
        assert restored.status == "failed"
        assert restored.result == "Error"


class TestInferToolsForStep:
    def test_read_step(self):
        tools = _infer_tools_for_step("Read the configuration file")
        assert "read_file" in tools

    def test_edit_step(self):
        tools = _infer_tools_for_step("Edit the auth module")
        assert "edit_file" in tools
        assert "read_file" in tools  # edit requires read

    def test_shell_step(self):
        tools = _infer_tools_for_step("Run the test suite")
        assert "shell" in tools

    def test_write_step(self):
        tools = _infer_tools_for_step("Create a new test file")
        assert "write_file" in tools

    def test_install_step(self):
        tools = _infer_tools_for_step("Install dependencies with pip")
        assert "shell" in tools

    def test_ambiguous_falls_back_to_all(self):
        tools = _infer_tools_for_step("Analyze the results")
        # "Analyze" doesn't match any specific tool keyword
        assert len(tools) == 4  # all tools

    def test_multiple_tools(self):
        tools = _infer_tools_for_step("Read the file and run tests")
        assert "read_file" in tools
        assert "shell" in tools
