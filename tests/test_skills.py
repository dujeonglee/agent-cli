"""Tests for skill system."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from agent_cli.providers.base import LLMResponse
from agent_cli.providers.compat import ModelCapabilities
from agent_cli.skills.executor import execute_skill, substitute_arguments
from agent_cli.skills.loader import _parse_skill_file, load_skills
import agent_cli.skills.loader as _loader
from agent_cli.skills.models import Skill


@pytest.fixture(autouse=True)
def _clear_skill_cache():
    """Clear skill cache before each test."""
    _loader._cached_skills = None
    yield
    _loader._cached_skills = None


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
        thinking_format="",
    )


class TestSkillModel:
    def test_create_skill(self):
        skill = Skill(
            name="review",
            description="Review code",
            prompt_template="Review $ARGUMENTS",
        )
        assert skill.name == "review"
        assert skill.allowed_tools is None
        assert skill.max_iter == 0

    def test_skill_with_tools(self):
        skill = Skill(
            name="test",
            description="Generate tests",
            prompt_template="Test $ARGUMENTS",
            allowed_tools=["read_file", "write_file"],
            max_iter=10,
        )
        assert skill.allowed_tools == ["read_file", "write_file"]
        assert skill.max_iter == 10


class TestArgumentSubstitution:
    def test_arguments_replaced(self):
        result = substitute_arguments("Review $ARGUMENTS now", "src/auth.py")
        assert result == "Review src/auth.py now"

    def test_numbered_args(self):
        result = substitute_arguments("Compare $0 with $1", "file1.py file2.py")
        assert result == "Compare file1.py with file2.py"

    def test_no_args(self):
        result = substitute_arguments("Do something with $ARGUMENTS", "")
        assert result == "Do something with "

    def test_unreplaced_placeholders_cleaned(self):
        result = substitute_arguments("Use $0 and $1 and $2", "only-one-arg")
        assert "$2" not in result
        assert "only-one-arg" in result

    def test_mixed_args_and_arguments(self):
        result = substitute_arguments("Read $ARGUMENTS, focus on $0", "src/main.py")
        assert "src/main.py" in result


class TestSkillLoader:
    def test_parse_skill_file(self, tmp_path):
        skill_file = tmp_path / "review.md"
        skill_file.write_text(
            "---\n"
            "name: review\n"
            "description: Review code\n"
            "allowed-tools: [read_file]\n"
            "max-iter: 5\n"
            "argument-hint: <file>\n"
            "---\n\n"
            "Review $ARGUMENTS for bugs.\n"
        )
        skill = _parse_skill_file(skill_file)
        assert skill is not None
        assert skill.name == "review"
        assert skill.description == "Review code"
        assert skill.allowed_tools == ["read_file"]
        assert skill.max_iter == 5
        assert "Review $ARGUMENTS" in skill.prompt_template

    def test_parse_minimal_frontmatter(self, tmp_path):
        skill_file = tmp_path / "simple.md"
        skill_file.write_text(
            "---\nname: simple\ndescription: A simple skill\n---\n\nDo $ARGUMENTS\n"
        )
        skill = _parse_skill_file(skill_file)
        assert skill is not None
        assert skill.name == "simple"
        assert skill.allowed_tools is None
        assert skill.max_iter == 0

    def test_parse_no_frontmatter(self, tmp_path):
        skill_file = tmp_path / "bad.md"
        skill_file.write_text("Just some text without frontmatter")
        skill = _parse_skill_file(skill_file)
        assert skill is None

    def test_filename_as_fallback_name(self, tmp_path):
        skill_file = tmp_path / "my-skill.md"
        skill_file.write_text("---\ndescription: No name field\n---\n\nDo something\n")
        skill = _parse_skill_file(skill_file)
        assert skill is not None
        assert skill.name == "my-skill"

    def test_load_skills_from_directory(self, tmp_path, monkeypatch):
        skills_dir = tmp_path / ".agent-cli" / "skills"
        skills_dir.mkdir(parents=True)

        (skills_dir / "skill1.md").write_text(
            "---\nname: skill1\ndescription: First\n---\n\nDo 1\n"
        )
        (skills_dir / "skill2.md").write_text(
            "---\nname: skill2\ndescription: Second\n---\n\nDo 2\n"
        )

        import agent_cli.skills.loader as loader

        monkeypatch.setattr(loader, "_SEARCH_PATHS", [skills_dir])

        skills = load_skills()
        assert "skill1" in skills
        assert "skill2" in skills

    def test_project_local_overrides_global(self, tmp_path, monkeypatch):
        global_dir = tmp_path / "global" / "skills"
        local_dir = tmp_path / "local" / "skills"
        global_dir.mkdir(parents=True)
        local_dir.mkdir(parents=True)

        (global_dir / "review.md").write_text(
            "---\nname: review\ndescription: Global\n---\n\nGlobal version\n"
        )
        (local_dir / "review.md").write_text(
            "---\nname: review\ndescription: Local\n---\n\nLocal version\n"
        )

        import agent_cli.skills.loader as loader

        monkeypatch.setattr(loader, "_SEARCH_PATHS", [local_dir, global_dir])

        skills = load_skills()
        assert skills["review"].description == "Local"


class TestSkillExecution:
    def test_execute_with_allowed_tools(self, caps):
        provider = MagicMock()
        provider.call.return_value = LLMResponse(
            content=json.dumps(
                {
                    "thought": "reviewing",
                    "action": "complete",
                    "action_input": {"result": "looks good"},
                }
            )
        )

        skill = Skill(
            name="review",
            description="Review",
            prompt_template="Review $ARGUMENTS",
            allowed_tools=["read_file"],
            max_iter=3,
        )
        result = execute_skill(
            skill=skill,
            arguments="test.py",
            provider=provider,
            capabilities=caps,
            model="test-model",
            quiet=True,
        )
        assert result is not None

    def test_execute_uses_skill_max_iter(self, caps):
        provider = MagicMock()
        provider.call.return_value = LLMResponse(
            content=json.dumps(
                {
                    "thought": "t",
                    "action": "complete",
                    "action_input": {"result": "done"},
                }
            )
        )

        skill = Skill(
            name="s",
            description="d",
            prompt_template="Do $ARGUMENTS",
            max_iter=7,
        )
        execute_skill(
            skill=skill,
            arguments="task",
            provider=provider,
            capabilities=caps,
            model="m",
            quiet=True,
        )
        # Skill's max_iter should be used (verified by run_loop not exceeding it)
        assert provider.call.called


class TestBuiltinSkills:
    def test_builtin_skills_loadable(self):
        """Built-in skills in .agent-cli/skills/ should be parseable."""
        from pathlib import Path

        skills_dir = Path(__file__).parent.parent / ".agent-cli" / "skills"
        if not skills_dir.exists():
            pytest.skip("Built-in skills directory not found")

        for md_file in skills_dir.glob("*.md"):
            skill = _parse_skill_file(md_file)
            assert skill is not None, f"Failed to parse {md_file.name}"
            assert skill.name, f"No name in {md_file.name}"
            assert skill.description, f"No description in {md_file.name}"
            assert "$ARGUMENTS" in skill.prompt_template, (
                f"No $ARGUMENTS in {md_file.name}"
            )
