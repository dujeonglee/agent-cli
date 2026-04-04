"""Tests for built-in skills loading and discovery."""

from agent_cli.skills.loader import load_skills, _BUILTIN_DIR, _parse_skill_file


class TestBuiltinDirectory:
    def test_builtin_dir_exists(self):
        assert _BUILTIN_DIR.is_dir()

    def test_builtin_dir_has_skills(self):
        md_files = list(_BUILTIN_DIR.glob("*.md"))
        assert (
            len(md_files) >= 3
        )  # create-skill, create-agent, plan (+ create-team dir)


class TestBuiltinSkillsParsing:
    def test_create_skill_parses(self):
        path = _BUILTIN_DIR / "create-skill.md"
        skill = _parse_skill_file(path)
        assert skill is not None
        assert skill.name == "create-skill"
        assert skill.description
        assert skill.disable_model_invocation is True
        assert "ask" in skill.allowed_tools

    def test_create_agent_parses(self):
        path = _BUILTIN_DIR / "create-agent.md"
        skill = _parse_skill_file(path)
        assert skill is not None
        assert skill.name == "create-agent"
        assert skill.description
        assert skill.disable_model_invocation is True
        assert "ask" in skill.allowed_tools

    def test_plan_parses(self):
        path = _BUILTIN_DIR / "plan.md"
        skill = _parse_skill_file(path)
        assert skill is not None
        assert skill.name == "plan"
        assert skill.description
        assert "write_file" in skill.allowed_tools
        assert skill.disable_model_invocation is False  # LLM can auto-invoke


class TestBuiltinSkillsLoading:
    def setup_method(self):
        """Reset loader to default paths before each test."""
        import agent_cli.skills.loader as loader

        loader._reset_loader()

    def test_builtin_included_in_load(self):
        skills = load_skills(use_cache=False)
        assert "create-skill" in skills
        assert "create-agent" in skills
        assert "plan" in skills

    def test_builtin_has_lower_priority(self, tmp_path, monkeypatch):
        """Project-local skill with same name overrides built-in."""
        import agent_cli.skills.loader as loader

        local_dir = tmp_path / "skills"
        local_dir.mkdir()
        (local_dir / "create-skill.md").write_text(
            "---\nname: create-skill\ndescription: Custom override\n---\n\nCustom prompt"
        )

        loader._reset_loader([local_dir, _BUILTIN_DIR])
        skills = load_skills(use_cache=False)
        assert skills["create-skill"].description == "Custom override"

    def test_builtin_coexists_with_project(self, tmp_path, monkeypatch):
        """Built-in and project skills coexist when names differ."""
        import agent_cli.skills.loader as loader

        local_dir = tmp_path / "skills"
        local_dir.mkdir()
        (local_dir / "my-custom.md").write_text(
            "---\nname: my-custom\ndescription: My skill\n---\n\nDo stuff"
        )

        loader._reset_loader([local_dir, _BUILTIN_DIR])
        skills = load_skills(use_cache=False)
        assert "my-custom" in skills
        assert "create-skill" in skills
        assert "create-agent" in skills


class TestBuiltinSkillContent:
    def test_create_skill_has_format_docs(self):
        path = _BUILTIN_DIR / "create-skill.md"
        content = path.read_text()
        assert "frontmatter" in content.lower()
        assert "$ARGUMENTS" in content
        assert "${SKILL_DIR}" in content
        assert "scripts/" in content

    def test_create_agent_has_format_docs(self):
        path = _BUILTIN_DIR / "create-agent.md"
        content = path.read_text()
        assert "frontmatter" in content.lower()
        assert ".agent-cli/agents/" in content
        assert "delegate" in content.lower()

    def test_create_skill_user_invocable(self):
        skill = _parse_skill_file(_BUILTIN_DIR / "create-skill.md")
        assert skill.user_invocable is True  # users can call /create-skill

    def test_create_agent_user_invocable(self):
        skill = _parse_skill_file(_BUILTIN_DIR / "create-agent.md")
        assert skill.user_invocable is True

    def test_plan_has_output_template(self):
        path = _BUILTIN_DIR / "plan.md"
        content = path.read_text()
        assert "plan/" in content
        assert "## Tasks" in content
        assert "## Dependencies" in content
        assert "## Scope" in content
        assert "- [ ]" in content

    def test_plan_user_invocable(self):
        skill = _parse_skill_file(_BUILTIN_DIR / "plan.md")
        assert skill.user_invocable is True

    def test_create_team_parses(self):
        path = _BUILTIN_DIR / "create-team" / "SKILL.md"
        skill = _parse_skill_file(path)
        assert skill is not None
        assert skill.name == "create-team"
        assert skill.description
        assert skill.disable_model_invocation is True

    def test_create_team_has_references(self):
        ref_dir = _BUILTIN_DIR / "create-team" / "references"
        assert ref_dir.is_dir()
        assert (ref_dir / "design-patterns.md").is_file()
        assert (ref_dir / "agent-writing.md").is_file()
        assert (ref_dir / "skill-writing.md").is_file()

    def test_create_team_references_content(self):
        ref_dir = _BUILTIN_DIR / "create-team" / "references"
        for name in ("design-patterns.md", "agent-writing.md", "skill-writing.md"):
            content = (ref_dir / name).read_text()
            assert len(content) > 100  # Not empty stubs

    def test_create_team_skill_references_skill_dir(self):
        path = _BUILTIN_DIR / "create-team" / "SKILL.md"
        content = path.read_text()
        assert "${SKILL_DIR}" in content  # References are loaded via SKILL_DIR
