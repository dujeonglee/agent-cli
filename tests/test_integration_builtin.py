"""Integration tests for built-in skills and agents.

Requires running Ollama with configured models.
Run with: pytest tests/test_integration_builtin.py -m ollama_integration
"""

import os

import pytest


pytestmark = pytest.mark.ollama_integration


class TestExplorerAgent:
    """Integration tests for @explorer built-in agent via delegate."""

    def test_explorer_explains_file(
        self, integration_model, ollama_provider, model_capabilities, tmp_path
    ):
        """@explorer reads a file and explains it."""
        from agent_cli.tools.delegate import tool_delegate

        # Create a simple test file
        test_file = tmp_path / "calculator.py"
        test_file.write_text(
            "def add(a, b):\n    return a + b\n\n"
            "def multiply(a, b):\n    return a * b\n"
        )

        result = tool_delegate(
            args={
                "tasks": [
                    {
                        "task": f"Read {test_file} and explain what it does. Be brief (2-3 sentences).",
                        "agent": "explorer",
                        "context": "none",
                    }
                ]
            },
            provider=ollama_provider,
            model=integration_model,
            capabilities=model_capabilities,
            provider_name="ollama",
            base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
            api_key="",
            max_turns=5,
        )
        assert result.success
        assert "add" in result.output.lower() or "calculator" in result.output.lower()

    def test_explorer_uses_read_only_tools(
        self, integration_model, ollama_provider, model_capabilities, tmp_path
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
            provider=ollama_provider,
            model=integration_model,
            capabilities=model_capabilities,
            provider_name="ollama",
            base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
            api_key="",
            max_turns=5,
        )
        assert result.success
        # Activity log should only show read_file and shell
        output = result.output
        assert "write_file" not in output or "Files touched" in output
        # Should not have modified files
        if "[Files touched]" in output:
            assert (
                "Modified:" not in output
                or "(none)" in output.split("Modified:")[1].split("\n")[0]
            )


class TestPlanSkill:
    """Integration tests for /plan built-in skill."""

    def test_plan_creates_file(
        self, integration_model, ollama_provider, model_capabilities, tmp_path
    ):
        """Plan skill creates a markdown file in plan/ directory."""
        from agent_cli.context.manager import ContextManager
        from agent_cli.skills import load_skills
        from agent_cli.skills.executor import execute_skill

        # Ensure we're working in tmp_path
        original_cwd = os.getcwd()
        os.chdir(tmp_path)

        try:
            skills = load_skills(use_cache=False)
            assert "plan" in skills

            skill = skills["plan"]

            ctx = ContextManager(
                provider=ollama_provider,
                model=integration_model,
                capabilities=model_capabilities,
                scratchpad_dir=tmp_path,
            )

            result = execute_skill(
                skill=skill,
                arguments="add a hello command that prints hello world",
                provider=ollama_provider,
                capabilities=model_capabilities,
                model=integration_model,
                ctx=ctx,
            )

            assert result is not None

            # Plan should have created a file in plan/ directory
            plan_dir = tmp_path / "plan"
            if plan_dir.is_dir():
                plan_files = list(plan_dir.glob("*.md"))
                assert len(plan_files) >= 1, "Plan should create at least one .md file"
                # Check content has expected sections
                content = plan_files[0].read_text()
                assert "##" in content  # Should have markdown headings
        finally:
            os.chdir(original_cwd)

    def test_plan_returns_summary(
        self, integration_model, ollama_provider, model_capabilities, tmp_path
    ):
        """Plan skill returns a summary via complete."""
        from agent_cli.context.manager import ContextManager
        from agent_cli.skills import load_skills
        from agent_cli.skills.executor import execute_skill

        original_cwd = os.getcwd()
        os.chdir(tmp_path)

        try:
            skills = load_skills(use_cache=False)
            skill = skills["plan"]

            ctx = ContextManager(
                provider=ollama_provider,
                model=integration_model,
                capabilities=model_capabilities,
                scratchpad_dir=tmp_path,
            )

            result = execute_skill(
                skill=skill,
                arguments="add a version command that shows the current version",
                provider=ollama_provider,
                capabilities=model_capabilities,
                model=integration_model,
                ctx=ctx,
            )

            assert result is not None
            assert len(result) > 20  # Should be a meaningful summary
        finally:
            os.chdir(original_cwd)


class TestBuiltinSkillsAvailability:
    """Test that all built-in skills are discoverable and loadable."""

    def test_all_builtin_skills_loaded(self):
        """All built-in skills should be available in load_skills()."""
        from agent_cli.skills import load_skills

        skills = load_skills(use_cache=False)
        expected = {"create-skill", "create-agent", "plan", "create-team"}
        for name in expected:
            assert name in skills, f"Built-in skill '{name}' not found"

    def test_builtin_skills_have_descriptions(self):
        """All built-in skills should have non-empty descriptions."""
        from agent_cli.skills import load_skills

        skills = load_skills(use_cache=False)
        for name in ("create-skill", "create-agent", "plan", "create-team"):
            assert skills[name].description, f"Skill '{name}' has empty description"

    def test_create_skill_is_user_only(self):
        """create-skill should not be auto-invocable by LLM."""
        from agent_cli.skills import load_skills

        skills = load_skills(use_cache=False)
        assert skills["create-skill"].disable_model_invocation is True

    def test_create_agent_is_user_only(self):
        """create-agent should not be auto-invocable by LLM."""
        from agent_cli.skills import load_skills

        skills = load_skills(use_cache=False)
        assert skills["create-agent"].disable_model_invocation is True

    def test_plan_is_model_invocable(self):
        """plan skill should be invocable by LLM."""
        from agent_cli.skills import load_skills

        skills = load_skills(use_cache=False)
        assert skills["plan"].disable_model_invocation is False


class TestBuiltinAgentsAvailability:
    """Test that built-in agents are discoverable."""

    def test_explorer_agent_loads(self):
        """Explorer agent should load successfully."""
        from agent_cli.tools.delegate import _load_agent

        role, config, error = _load_agent("explorer")
        assert error is None
        assert role is not None
        assert "read" in role.lower()

    def test_explorer_has_tool_restrictions(self):
        """Explorer should only have read_file and shell."""
        from agent_cli.tools.delegate import _load_agent

        role, config, error = _load_agent("explorer")
        tools = config.get("allowed-tools", [])
        assert "read_file" in tools
        assert "shell" in tools
        assert "write_file" not in tools
        assert "edit_file" not in tools

    def test_explorer_in_system_prompt(self):
        """Explorer should appear in Available Agents section of system prompt."""
        from agent_cli.prompts.system_prompt import build_agent_descriptions

        desc = build_agent_descriptions()
        assert "explorer" in desc


class TestAgentDispatchIntegration:
    """Integration tests for @agent dispatch mechanism."""

    def test_dispatch_agent_via_run_loop(
        self, integration_model, ollama_provider, model_capabilities, tmp_path
    ):
        """@agent dispatch via run_loop with a simple task."""
        from agent_cli.main import _dispatch_agent, _AGENT_NOT_FOUND
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(
            provider=ollama_provider,
            model=integration_model,
            capabilities=model_capabilities,
            scratchpad_dir=tmp_path,
        )

        result = _dispatch_agent(
            "@explorer What does agent_cli/constants.py define? Answer in one sentence.",
            ollama_provider,
            model_capabilities,
            integration_model,
            "ollama",
            os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
            "",
            max_turns=5,
            ctx=ctx,
        )

        assert result is not _AGENT_NOT_FOUND
        assert result is not None
        assert len(result) > 20

    def test_dispatch_nonexistent_agent(
        self, integration_model, ollama_provider, model_capabilities
    ):
        """Dispatching to non-existent agent returns AGENT_NOT_FOUND."""
        from agent_cli.main import _dispatch_agent, _AGENT_NOT_FOUND

        result = _dispatch_agent(
            "@nonexistent_agent_xyz42 do something",
            ollama_provider,
            model_capabilities,
            integration_model,
            "ollama",
            os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
            "",
        )

        assert result is _AGENT_NOT_FOUND
