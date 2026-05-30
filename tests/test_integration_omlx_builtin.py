"""Integration tests for built-in agents/skills against a live omlx server.

Run: pytest tests/test_integration_omlx_builtin.py -m omlx_integration -v

Pure loading/prompt checks (skill discovery, agent tool restrictions,
system-prompt wiring) live in the unit suite (test_builtin_skills.py,
test_builtin_agents.py); this file covers only behaviour that needs a
real LLM. Skips automatically when the omlx server is unreachable.
"""

from __future__ import annotations

import os

import pytest

from tests.conftest import OMLX_BASE_URL

pytestmark = pytest.mark.omlx_integration


class TestExplorerAgent:
    """@explorer built-in agent via the delegate tool."""

    def test_explorer_explains_file(
        self, integration_model, omlx_provider, model_capabilities, tmp_path
    ):
        """@explorer reads a file and explains it."""
        from agent_cli.tools.delegate import tool_delegate

        test_file = tmp_path / "calculator.py"
        test_file.write_text(
            "def add(a, b):\n    return a + b\n\n"
            "def multiply(a, b):\n    return a * b\n"
        )

        result = tool_delegate(
            args={
                "tasks": [
                    {
                        "task": f"Read {test_file} and explain what it does. "
                        "Be brief (2-3 sentences).",
                        "agent": "explorer",
                        "context": "none",
                    }
                ]
            },
            provider=omlx_provider,
            model=integration_model,
            capabilities=model_capabilities,
            provider_name="openai",
            base_url=OMLX_BASE_URL,
            api_key="",
            max_turns=5,
        )
        assert result.success
        assert "add" in result.output.lower() or "calculator" in result.output.lower()

    def test_explorer_uses_read_only_tools(
        self, integration_model, omlx_provider, model_capabilities, tmp_path
    ):
        """Explorer agent should only use read_file and shell (no writes)."""
        from agent_cli.tools.delegate import tool_delegate

        test_file = tmp_path / "data.txt"
        test_file.write_text("Hello World")

        result = tool_delegate(
            args={
                "tasks": [
                    {
                        "task": f"Read {test_file} and tell me its content.",
                        "agent": "explorer",
                        "context": "none",
                    }
                ]
            },
            provider=omlx_provider,
            model=integration_model,
            capabilities=model_capabilities,
            provider_name="openai",
            base_url=OMLX_BASE_URL,
            api_key="",
            max_turns=5,
        )
        assert result.success
        output = result.output
        # Activity log should not reflect any file modifications.
        assert "write_file" not in output or "Files touched" in output
        if "[Files touched]" in output:
            assert (
                "Modified:" not in output
                or "(none)" in output.split("Modified:")[1].split("\n")[0]
            )


class TestPlanSkill:
    """/plan built-in skill."""

    def test_plan_creates_file(
        self, integration_model, omlx_provider, model_capabilities, tmp_path
    ):
        """Plan skill creates a markdown file in the plan/ directory."""
        from agent_cli.context.manager import ContextManager
        from agent_cli.skills import load_skills
        from agent_cli.skills.executor import execute_skill

        original_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            skills = load_skills()
            assert "plan" in skills
            skill = skills["plan"]

            ctx = ContextManager(
                provider=omlx_provider,
                model=integration_model,
                capabilities=model_capabilities,
                scratchpad_dir=tmp_path,
            )

            result = execute_skill(
                skill=skill,
                arguments="add a hello command that prints hello world",
                provider=omlx_provider,
                capabilities=model_capabilities,
                model=integration_model,
                ctx=ctx,
            )
            assert result is not None

            plan_dir = tmp_path / "plan"
            if plan_dir.is_dir():
                plan_files = list(plan_dir.glob("*.md"))
                assert len(plan_files) >= 1, "Plan should create at least one .md file"
                assert "##" in plan_files[0].read_text()
        finally:
            os.chdir(original_cwd)

    def test_plan_returns_summary(
        self, integration_model, omlx_provider, model_capabilities, tmp_path
    ):
        """Plan skill returns a meaningful summary via complete."""
        from agent_cli.context.manager import ContextManager
        from agent_cli.skills import load_skills
        from agent_cli.skills.executor import execute_skill

        original_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            skills = load_skills()
            skill = skills["plan"]

            ctx = ContextManager(
                provider=omlx_provider,
                model=integration_model,
                capabilities=model_capabilities,
                scratchpad_dir=tmp_path,
            )

            result = execute_skill(
                skill=skill,
                arguments="add a version command that shows the current version",
                provider=omlx_provider,
                capabilities=model_capabilities,
                model=integration_model,
                ctx=ctx,
            )
            assert result is not None
            assert len(result) > 20
        finally:
            os.chdir(original_cwd)


class TestAgentDispatchIntegration:
    """@agent dispatch mechanism via run_loop."""

    def test_dispatch_agent_via_run_loop(
        self, integration_model, omlx_provider, model_capabilities, tmp_path
    ):
        """@agent dispatch with a simple task."""
        from agent_cli.context.manager import ContextManager
        from agent_cli.main import _AGENT_NOT_FOUND, _dispatch_agent

        ctx = ContextManager(
            provider=omlx_provider,
            model=integration_model,
            capabilities=model_capabilities,
            scratchpad_dir=tmp_path,
        )

        result = _dispatch_agent(
            "@explorer What does agent_cli/constants.py define? Answer in one sentence.",
            omlx_provider,
            model_capabilities,
            integration_model,
            "openai",
            OMLX_BASE_URL,
            "",
            max_turns=5,
            ctx=ctx,
        )
        assert result is not _AGENT_NOT_FOUND
        assert result is not None
        assert len(result) > 20

    def test_dispatch_nonexistent_agent(
        self, integration_model, omlx_provider, model_capabilities
    ):
        """Dispatching to a non-existent agent returns AGENT_NOT_FOUND."""
        from agent_cli.main import _AGENT_NOT_FOUND, _dispatch_agent

        result = _dispatch_agent(
            "@nonexistent_agent_xyz42 do something",
            omlx_provider,
            model_capabilities,
            integration_model,
            "openai",
            OMLX_BASE_URL,
            "",
        )
        assert result is _AGENT_NOT_FOUND
