"""Tests for plan generation prompt."""
import pytest

from agent_cli.prompts.system_prompt import build_plan_generation_prompt
from agent_cli.providers.compat import ModelCapabilities


def _make_caps(ctx_window: int = 32768) -> ModelCapabilities:
    return ModelCapabilities(
        context_window=ctx_window, max_output_tokens=4096,
        supports_structured_output=True, supports_tool_calling=False,
        supports_thinking=False, thinking_budget=0, supports_strict_schema=False,
    )


class TestBuildPlanGenerationPrompt:
    def test_contains_plan_marker(self):
        prompt = build_plan_generation_prompt(_make_caps(), ["shell"])
        assert ">>>PLAN" in prompt

    def test_contains_max_steps(self):
        prompt = build_plan_generation_prompt(_make_caps(), ["shell"], max_steps=10)
        assert "10" in prompt

    def test_contains_tools(self):
        prompt = build_plan_generation_prompt(
            _make_caps(), ["read_file", "shell"]
        )
        assert "read_file" in prompt
        assert "shell" in prompt

    def test_small_model_hints(self):
        prompt = build_plan_generation_prompt(_make_caps(4096), ["shell"])
        assert "concise" in prompt.lower()
