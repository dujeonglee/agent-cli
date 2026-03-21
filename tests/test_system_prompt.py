"""Tests for prompts/system_prompt."""
import pytest

from agent_cli.prompts.system_prompt import build_system_prompt, SMALL_MODEL_HINTS
from agent_cli.providers.compat import ModelCapabilities


def _make_caps(ctx_window: int = 32768) -> ModelCapabilities:
    return ModelCapabilities(
        context_window=ctx_window, max_output_tokens=4096,
        supports_structured_output=True, supports_tool_calling=False,
        supports_thinking=False, thinking_budget=0, supports_strict_schema=False,
    )


class TestBuildSystemPrompt:
    def test_includes_all_tools(self):
        prompt = build_system_prompt(
            _make_caps(), ["read_file", "write_file", "edit_file", "shell"]
        )
        assert "read_file" in prompt
        assert "write_file" in prompt
        assert "edit_file" in prompt
        assert "shell" in prompt

    def test_active_tools_only(self):
        prompt = build_system_prompt(_make_caps(), ["shell"])
        assert "shell" in prompt
        assert "read_file" not in prompt
        assert "Hashline" not in prompt  # No edit_file → no hashline guide

    def test_hashline_guide_with_edit(self):
        prompt = build_system_prompt(_make_caps(), ["edit_file"])
        assert "Hashline" in prompt

    def test_delegate_included(self):
        prompt = build_system_prompt(
            _make_caps(), ["shell"], include_delegate=True
        )
        assert "delegate" in prompt.lower()
        assert "Delegation Rules" in prompt

    def test_delegate_excluded(self):
        prompt = build_system_prompt(
            _make_caps(), ["shell"], include_delegate=False
        )
        assert "Delegation Rules" not in prompt

    def test_small_model_hints(self):
        prompt = build_system_prompt(
            _make_caps(ctx_window=4096), ["shell"]
        )
        assert "concise" in prompt.lower()

    def test_large_model_no_hints(self):
        prompt = build_system_prompt(
            _make_caps(ctx_window=128000), ["shell"]
        )
        assert SMALL_MODEL_HINTS not in prompt

    def test_plan_context(self):
        prompt = build_system_prompt(
            _make_caps(), ["shell"],
            plan_context="Step 3 of 5: Run tests"
        )
        assert "Step 3 of 5" in prompt

    def test_json_format_present(self):
        prompt = build_system_prompt(_make_caps(), ["shell"])
        assert "JSON" in prompt
        assert "thought" in prompt
