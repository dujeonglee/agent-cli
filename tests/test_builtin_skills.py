"""Tests for built-in skills loading and discovery."""

from agent_cli.skills.loader import load_skills, _BUILTIN_DIR, _parse_skill_file


class TestBuiltinDirectory:
    def test_builtin_dir_exists(self):
        assert _BUILTIN_DIR.is_dir()

    def test_builtin_dir_has_skills(self):
        md_files = list(_BUILTIN_DIR.glob("*.md"))
        assert len(md_files) >= 2  # create-skill, create-agent


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


class TestBuiltinSkillsLoading:
    def test_builtin_included_in_load(self):
        skills = load_skills(use_cache=False)
        assert "create-skill" in skills
        assert "create-agent" in skills

    def test_builtin_has_lower_priority(self, tmp_path, monkeypatch):
        """Project-local skill with same name overrides built-in."""
        import agent_cli.skills.loader as loader

        # Create a project-local skill with same name as built-in
        local_dir = tmp_path / "skills"
        local_dir.mkdir()
        (local_dir / "create-skill.md").write_text(
            "---\nname: create-skill\ndescription: Custom override\n---\n\nCustom prompt"
        )

        monkeypatch.setattr(
            loader,
            "_SEARCH_PATHS",
            [local_dir, _BUILTIN_DIR],
        )
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

        monkeypatch.setattr(
            loader,
            "_SEARCH_PATHS",
            [local_dir, _BUILTIN_DIR],
        )
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
