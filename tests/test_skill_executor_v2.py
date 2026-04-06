"""Tests for skills/executor.py redesign (Phase 5)."""

import pytest
from unittest.mock import MagicMock, patch

from agent_cli.skills.executor import execute_skill
from agent_cli.skills.models import Skill
from agent_cli.context.manager import ContextManager
from agent_cli.providers.compat import ModelCapabilities


@pytest.fixture
def caps():
    return ModelCapabilities(
        context_window=8000,
        max_output_tokens=2000,
        supports_structured_output=False,
        supports_tool_calling=False,
        supports_thinking=False,
        thinking_budget=0,
        supports_strict_schema=False,
    )


@pytest.fixture
def ctx(tmp_path):
    return ContextManager(session_dir=tmp_path / "sessions" / "test")


def _make_skill(name="test", allowed_tools=None, prompt="Do $ARGUMENTS"):
    return Skill(
        name=name,
        description="test skill",
        prompt_template=prompt,
        allowed_tools=allowed_tools or [],
        max_turns=0,
        source_path="",
    )


class TestToolIntersection:
    def test_intersection_filters_tools(self, caps, ctx):
        """Skill tools ∩ parent tools = effective tools."""
        skill = _make_skill(allowed_tools=["read_file", "shell", "write_file"])

        with patch("agent_cli.skills.executor.run_loop") as mock_loop:
            mock_loop.return_value = "done"
            execute_skill(
                skill=skill,
                arguments="test",
                provider=MagicMock(),
                capabilities=caps,
                model="test",
                ctx=ctx,
                parent_tools=["read_file", "shell"],
            )
            call_kwargs = mock_loop.call_args
            active_tools = call_kwargs.kwargs.get("active_tools") or call_kwargs[1].get(
                "active_tools"
            )
            assert set(active_tools) == {"read_file", "shell"}

    def test_empty_intersection_rejected(self, caps, ctx):
        """Empty intersection → execution rejected with error."""
        skill = _make_skill(allowed_tools=["write_file", "fetch"])

        result = execute_skill(
            skill=skill,
            arguments="test",
            provider=MagicMock(),
            capabilities=caps,
            model="test",
            ctx=ctx,
            parent_tools=["read_file", "shell"],
        )
        assert result is not None
        assert not result.success
        assert "cannot run" in result.error
        assert result.artifact == ""

    def test_no_parent_tools_uses_skill_tools(self, caps, ctx):
        """No parent_tools → use skill's tools as-is."""
        skill = _make_skill(allowed_tools=["read_file", "write_file"])

        with patch("agent_cli.skills.executor.run_loop") as mock_loop:
            mock_loop.return_value = "done"
            execute_skill(
                skill=skill,
                arguments="test",
                provider=MagicMock(),
                capabilities=caps,
                model="test",
                ctx=ctx,
            )
            call_kwargs = mock_loop.call_args
            active_tools = call_kwargs.kwargs.get("active_tools") or call_kwargs[1].get(
                "active_tools"
            )
            assert set(active_tools) == {"read_file", "write_file"}

    def test_no_skill_tools_uses_parent_tools(self, caps, ctx):
        """Skill has no tool restriction → use parent's tools."""
        skill = _make_skill(allowed_tools=[])

        with patch("agent_cli.skills.executor.run_loop") as mock_loop:
            mock_loop.return_value = "done"
            execute_skill(
                skill=skill,
                arguments="test",
                provider=MagicMock(),
                capabilities=caps,
                model="test",
                ctx=ctx,
                parent_tools=["read_file", "shell"],
            )
            call_kwargs = mock_loop.call_args
            active_tools = call_kwargs.kwargs.get("active_tools") or call_kwargs[1].get(
                "active_tools"
            )
            assert set(active_tools) == {"read_file", "shell"}


class TestParentRoleInheritance:
    def test_parent_role_passed_to_run_loop(self, caps, ctx):
        """parent_role is forwarded as agent_role to run_loop."""
        skill = _make_skill()

        with patch("agent_cli.skills.executor.run_loop") as mock_loop:
            mock_loop.return_value = "done"
            execute_skill(
                skill=skill,
                arguments="test",
                provider=MagicMock(),
                capabilities=caps,
                model="test",
                ctx=ctx,
                parent_role="You are an explorer agent.",
            )
            call_kwargs = mock_loop.call_args
            agent_role = call_kwargs.kwargs.get("agent_role") or call_kwargs[1].get(
                "agent_role"
            )
            assert agent_role == "You are an explorer agent."

    def test_no_parent_role_empty(self, caps, ctx):
        """No parent_role → agent_role is empty."""
        skill = _make_skill()

        with patch("agent_cli.skills.executor.run_loop") as mock_loop:
            mock_loop.return_value = "done"
            execute_skill(
                skill=skill,
                arguments="test",
                provider=MagicMock(),
                capabilities=caps,
                model="test",
                ctx=ctx,
            )
            call_kwargs = mock_loop.call_args
            agent_role = call_kwargs.kwargs.get("agent_role") or call_kwargs[1].get(
                "agent_role"
            )
            assert agent_role == ""


class TestSkillSubdir:
    def test_skill_creates_subdir(self, caps, ctx):
        """Skill creates its own subdir with history.jsonl."""
        skill = _make_skill(name="summarize")

        with patch("agent_cli.skills.executor.run_loop") as mock_loop:
            mock_loop.return_value = "done"
            execute_skill(
                skill=skill,
                arguments="test",
                provider=MagicMock(),
                capabilities=caps,
                model="test",
                ctx=ctx,
            )
            call_kwargs = mock_loop.call_args
            skill_ctx = call_kwargs.kwargs.get("ctx") or call_kwargs[1].get("ctx")
            # Should be a different ContextManager than parent
            assert skill_ctx is not ctx
            assert "skill_summarize" in str(skill_ctx.session_dir)
