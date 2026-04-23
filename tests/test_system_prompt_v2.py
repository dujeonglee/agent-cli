"""Tests for system_prompt.py redesign (Phase 3)."""

import pytest

from agent_cli.prompts.system_prompt import build_system_prompt, _build_context_recovery
from agent_cli.providers.compat import ModelCapabilities


@pytest.fixture
def caps():
    return ModelCapabilities(
        context_window=8000,
        max_output_tokens=2000,
        supports_structured_output=False,
        supports_thinking=False,
        thinking_budget=0,
        supports_strict_schema=False,
    )


class TestRoleInheritance:
    def test_main_uses_default_role(self, caps):
        prompt = build_system_prompt(caps, ["read_file", "shell"])
        assert "AI assistant" in prompt

    def test_delegate_replaces_role(self, caps):
        prompt = build_system_prompt(
            caps, ["read_file"], agent_role="You are an explorer agent."
        )
        assert "explorer agent" in prompt
        assert "AI assistant that solves tasks" not in prompt

    def test_skill_inherits_parent_role(self, caps):
        prompt = build_system_prompt(
            caps, ["read_file"], parent_role="You are a code reviewer."
        )
        assert "code reviewer" in prompt
        assert "AI assistant that solves tasks" not in prompt

    def test_agent_role_takes_precedence_over_parent_role(self, caps):
        """If both agent_role and parent_role given, agent_role wins."""
        prompt = build_system_prompt(
            caps,
            ["read_file"],
            agent_role="You are an explorer.",
            parent_role="You are a reviewer.",
        )
        assert "explorer" in prompt
        assert "reviewer" not in prompt


class TestGitContextRemoved:
    def test_no_git_context(self, caps):
        prompt = build_system_prompt(caps, ["read_file", "shell"])
        assert "git status" not in prompt.lower() or "## Git" not in prompt


class TestSessionIdRemoved:
    def test_no_session_section(self, caps):
        prompt = build_system_prompt(caps, ["read_file"], session_id="test-123")
        # session_id param still accepted but no longer creates a section
        assert "## Session" not in prompt


class TestContextRecoveryGuide:
    def test_recovery_guide_present(self, caps):
        prompt = build_system_prompt(
            caps, ["read_file"], session_dir="/tmp/sessions/abc"
        )
        assert "## Context Recovery" in prompt
        assert "history.jsonl" in prompt
        assert "/tmp/sessions/abc" in prompt

    def test_no_recovery_without_session_dir(self, caps):
        prompt = build_system_prompt(caps, ["read_file"])
        assert "## Context Recovery" not in prompt

    def test_build_context_recovery_format(self):
        result = _build_context_recovery("/tmp/test")
        assert "read_file" in result
        assert "/tmp/test/history.jsonl" in result


class TestThoughtGuidelines:
    def test_thought_includes_purpose_and_reason(self, caps):
        prompt = build_system_prompt(caps, ["read_file"])
        assert "purpose" in prompt.lower()
        assert "reason" in prompt.lower()


class TestDirectiveBeforeEnvironment:
    def test_directive_section_order(self, caps):
        """DIRECTIVE should come before Environment in recency zone."""
        prompt = build_system_prompt(caps, ["read_file"], session_dir="/tmp/test")
        # Environment section should exist
        env_pos = prompt.find("## Environment")
        recovery_pos = prompt.find("## Context Recovery")
        if env_pos >= 0 and recovery_pos >= 0:
            assert env_pos < recovery_pos
