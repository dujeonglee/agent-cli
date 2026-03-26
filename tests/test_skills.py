"""Tests for skill system."""

from __future__ import annotations

import json
import unittest.mock
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

    def test_model_default_none(self):
        """Skill.model defaults to None (no override)."""
        skill = Skill(name="s", description="d", prompt_template="Do $ARGUMENTS")
        assert skill.model is None

    def test_model_field(self):
        """Skill.model stores the override model string."""
        skill = Skill(
            name="s",
            description="d",
            prompt_template="Do $ARGUMENTS",
            model="qwen3:8b",
        )
        assert skill.model == "qwen3:8b"

    def test_context_default_none(self):
        """Skill.context defaults to None (no fork)."""
        skill = Skill(name="s", description="d", prompt_template="Do $ARGUMENTS")
        assert skill.context is None

    def test_context_field(self):
        """Skill.context stores the context mode string."""
        skill = Skill(
            name="s",
            description="d",
            prompt_template="Do $ARGUMENTS",
            context="fork",
        )
        assert skill.context == "fork"


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

    def test_skill_dir_substitution(self):
        """${CLAUDE_SKILL_DIR} replaced with skill directory path."""
        result = substitute_arguments(
            "python ${CLAUDE_SKILL_DIR}/scripts/run.py",
            "",
            skill_dir="/home/user/.agent-cli/skills/my-skill",
        )
        assert result == "python /home/user/.agent-cli/skills/my-skill/scripts/run.py"

    def test_session_id_substitution(self):
        """${SESSION_ID} replaced with current session ID."""
        result = substitute_arguments(
            "Log to ${SESSION_ID}.log",
            "",
            session_id="1774272070",
        )
        assert result == "Log to 1774272070.log"

    def test_arguments_bracket_notation(self):
        """$ARGUMENTS[N] replaced with nth argument."""
        result = substitute_arguments(
            "Migrate $ARGUMENTS[0] from $ARGUMENTS[1] to $ARGUMENTS[2]",
            "SearchBar React Vue",
        )
        assert result == "Migrate SearchBar from React to Vue"

    def test_arguments_bracket_out_of_range(self):
        """$ARGUMENTS[N] out of range → cleaned up."""
        result = substitute_arguments(
            "Use $ARGUMENTS[0] and $ARGUMENTS[5]",
            "only-one",
        )
        assert "only-one" in result
        assert "$ARGUMENTS[5]" not in result

    def test_no_skill_dir_or_session(self):
        """Missing skill_dir/session_id → variables replaced with empty string."""
        result = substitute_arguments(
            "dir=${CLAUDE_SKILL_DIR} sid=${SESSION_ID}",
            "",
        )
        assert result == "dir= sid="


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

    def test_parse_model_from_frontmatter(self, tmp_path):
        """Frontmatter with model field → skill.model populated."""
        skill_file = tmp_path / "with-model.md"
        skill_file.write_text(
            "---\n"
            "name: with-model\n"
            "description: Skill with model override\n"
            "model: qwen3:8b\n"
            "---\n\n"
            "Do $ARGUMENTS\n"
        )
        skill = _parse_skill_file(skill_file)
        assert skill is not None
        assert skill.model == "qwen3:8b"

    def test_parse_no_model_in_frontmatter(self, tmp_path):
        """Frontmatter without model field → skill.model is None."""
        skill_file = tmp_path / "no-model.md"
        skill_file.write_text(
            "---\nname: no-model\ndescription: No model\n---\n\nDo $ARGUMENTS\n"
        )
        skill = _parse_skill_file(skill_file)
        assert skill is not None
        assert skill.model is None

    def test_parse_context_from_frontmatter(self, tmp_path):
        """Frontmatter with context field → skill.context populated."""
        skill_file = tmp_path / "forked.md"
        skill_file.write_text(
            "---\n"
            "name: forked\n"
            "description: Forked skill\n"
            "context: fork\n"
            "---\n\n"
            "Do $ARGUMENTS\n"
        )
        skill = _parse_skill_file(skill_file)
        assert skill is not None
        assert skill.context == "fork"

    def test_parse_no_context_in_frontmatter(self, tmp_path):
        """Frontmatter without context field → skill.context is None."""
        skill_file = tmp_path / "no-ctx.md"
        skill_file.write_text(
            "---\nname: no-ctx\ndescription: No context\n---\n\nDo $ARGUMENTS\n"
        )
        skill = _parse_skill_file(skill_file)
        assert skill is not None
        assert skill.context is None

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

    def test_parse_directory_skill(self, tmp_path):
        """skills/<name>/SKILL.md directory structure loads correctly."""
        skill_dir = tmp_path / "review"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: review\ndescription: Review code\n---\n\nReview $ARGUMENTS\n"
        )
        skill = _parse_skill_file(skill_dir / "SKILL.md")
        assert skill is not None
        assert skill.name == "review"
        assert skill.description == "Review code"

    def test_directory_name_as_fallback(self, tmp_path):
        """SKILL.md without name → uses parent directory name."""
        skill_dir = tmp_path / "my-checker"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\ndescription: Check stuff\n---\n\nCheck $ARGUMENTS\n"
        )
        skill = _parse_skill_file(skill_dir / "SKILL.md")
        assert skill is not None
        assert skill.name == "my-checker"

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

    def test_load_flat_and_directory_mixed(self, tmp_path, monkeypatch):
        """Flat *.md and <name>/SKILL.md coexist with different names."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        # Flat skill
        (skills_dir / "flat-skill.md").write_text(
            "---\nname: flat-skill\ndescription: Flat\n---\n\nFlat $ARGUMENTS\n"
        )
        # Directory skill
        dir_skill = skills_dir / "dir-skill"
        dir_skill.mkdir()
        (dir_skill / "SKILL.md").write_text(
            "---\nname: dir-skill\ndescription: Dir\n---\n\nDir $ARGUMENTS\n"
        )

        import agent_cli.skills.loader as loader

        monkeypatch.setattr(loader, "_SEARCH_PATHS", [skills_dir])

        skills = load_skills()
        assert "flat-skill" in skills
        assert "dir-skill" in skills
        assert skills["flat-skill"].description == "Flat"
        assert skills["dir-skill"].description == "Dir"

    def test_duplicate_name_flat_and_directory_raises(self, tmp_path, monkeypatch):
        """Same skill name in flat and directory → raises error."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()

        # Flat skill
        (skills_dir / "review.md").write_text(
            "---\nname: review\ndescription: Flat\n---\n\nFlat $ARGUMENTS\n"
        )
        # Directory skill with same name
        dir_skill = skills_dir / "review"
        dir_skill.mkdir()
        (dir_skill / "SKILL.md").write_text(
            "---\nname: review\ndescription: Dir\n---\n\nDir $ARGUMENTS\n"
        )

        import agent_cli.skills.loader as loader

        monkeypatch.setattr(loader, "_SEARCH_PATHS", [skills_dir])

        with pytest.raises(ValueError, match="review"):
            load_skills()


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

    def test_execute_no_model_override(self, caps):
        """skill.model=None → run_loop called with the original model."""
        provider = MagicMock()
        provider.call.return_value = LLMResponse(
            content=json.dumps(
                {
                    "thought": "t",
                    "action": "complete",
                    "action_input": {"result": "ok"},
                }
            )
        )

        skill = Skill(
            name="s", description="d", prompt_template="Do $ARGUMENTS", model=None
        )
        with unittest.mock.patch("agent_cli.skills.executor.run_loop") as mock_run_loop:
            mock_run_loop.return_value = "ok"
            execute_skill(
                skill=skill,
                arguments="task",
                provider=provider,
                capabilities=caps,
                model="original-model",
                quiet=True,
            )
            _, kwargs = mock_run_loop.call_args
            assert kwargs["model"] == "original-model"

    def test_execute_with_model_override(self, caps):
        """skill.model set → run_loop called with the overridden model."""
        provider = MagicMock()
        provider.call.return_value = LLMResponse(
            content=json.dumps(
                {
                    "thought": "t",
                    "action": "complete",
                    "action_input": {"result": "ok"},
                }
            )
        )

        skill = Skill(
            name="s",
            description="d",
            prompt_template="Do $ARGUMENTS",
            model="qwen3:8b",
        )
        with unittest.mock.patch("agent_cli.skills.executor.run_loop") as mock_run_loop:
            mock_run_loop.return_value = "ok"
            execute_skill(
                skill=skill,
                arguments="task",
                provider=provider,
                capabilities=caps,
                model="original-model",
                quiet=True,
            )
            _, kwargs = mock_run_loop.call_args
            assert kwargs["model"] == "qwen3:8b"

    def test_execute_no_context_fork(self, caps):
        """skill.context=None → run_loop called with the original ctx."""
        provider = MagicMock()
        fake_ctx = MagicMock()

        skill = Skill(
            name="s", description="d", prompt_template="Do $ARGUMENTS", context=None
        )
        with unittest.mock.patch("agent_cli.skills.executor.run_loop") as mock_run_loop:
            mock_run_loop.return_value = "ok"
            execute_skill(
                skill=skill,
                arguments="task",
                provider=provider,
                capabilities=caps,
                model="m",
                quiet=True,
                ctx=fake_ctx,
            )
            _, kwargs = mock_run_loop.call_args
            assert kwargs["ctx"] is fake_ctx

    def test_execute_context_fork(self, caps):
        """skill.context='fork' → run_loop called with ctx=None (independent)."""
        provider = MagicMock()
        fake_ctx = MagicMock()

        skill = Skill(
            name="s", description="d", prompt_template="Do $ARGUMENTS", context="fork"
        )
        with unittest.mock.patch("agent_cli.skills.executor.run_loop") as mock_run_loop:
            mock_run_loop.return_value = "ok"
            execute_skill(
                skill=skill,
                arguments="task",
                provider=provider,
                capabilities=caps,
                model="m",
                quiet=True,
                ctx=fake_ctx,
            )
            _, kwargs = mock_run_loop.call_args
            assert kwargs["ctx"] is None


class TestYamlRequired:
    def test_import_error_without_yaml(self):
        """Loader should raise ImportError when PyYAML is not installed."""
        import importlib
        import sys

        # Temporarily remove yaml from sys.modules and block re-import
        saved = sys.modules.pop("yaml", None)
        with unittest.mock.patch.dict(sys.modules, {"yaml": None}):
            with pytest.raises(ImportError):
                # Force reimport of loader to trigger the import
                if "agent_cli.skills.loader" in sys.modules:
                    del sys.modules["agent_cli.skills.loader"]
                importlib.import_module("agent_cli.skills.loader")

        # Restore
        if saved is not None:
            sys.modules["yaml"] = saved
        # Re-import to restore normal state
        if "agent_cli.skills.loader" in sys.modules:
            del sys.modules["agent_cli.skills.loader"]
        importlib.import_module("agent_cli.skills.loader")


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
