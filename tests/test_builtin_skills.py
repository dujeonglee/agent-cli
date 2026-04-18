"""Tests for built-in skills loading and discovery."""

from agent_cli.skills.loader import load_skills, _BUILTIN_DIR, _parse_skill_file


class TestBuiltinDirectory:
    def test_builtin_dir_exists(self):
        assert _BUILTIN_DIR.is_dir()

    def test_builtin_dir_has_skills(self):
        flat = list(_BUILTIN_DIR.glob("*.md"))
        dir_entries = [d for d in _BUILTIN_DIR.iterdir() if (d / "SKILL.md").is_file()]
        # create-agent.md, plan.md (flat) + create-skill/, create-team/ (dirs)
        assert len(flat) + len(dir_entries) >= 4


_CREATE_SKILL_PATH = _BUILTIN_DIR / "create-skill" / "SKILL.md"
_CREATE_SKILL_FORMAT_REF = _BUILTIN_DIR / "create-skill" / "references" / "format.md"


class TestBuiltinSkillsParsing:
    def test_create_skill_parses(self):
        skill = _parse_skill_file(_CREATE_SKILL_PATH)
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
        skills = load_skills()
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
        skills = load_skills()
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
        skills = load_skills()
        assert "my-custom" in skills
        assert "create-skill" in skills
        assert "create-agent" in skills


class TestBuiltinSkillContent:
    def test_create_skill_body_delegates_format_to_reference(self):
        """The SKILL.md body must point the LLM at the format reference.

        The placeholder-heavy format docs live in references/format.md
        so that the template substitutor leaves ${SKILL_DIR} / $ARGUMENTS
        alone — tool results are not substituted, so a read_file of the
        reference delivers the literal placeholder strings the LLM needs
        to copy. If the body ever inlined the format docs again, every
        ${SKILL_DIR} example would get substituted to create-skill's own
        absolute path and the LLM would bake that into new skills.
        """
        body = _CREATE_SKILL_PATH.read_text()
        assert "${SKILL_DIR}/references/format.md" in body
        assert "read_file" in body.lower()
        # Placeholder-heavy format text must NOT live in the body.
        assert "Pattern A" not in body
        assert "Pattern B" not in body

    def test_create_skill_format_reference_has_literal_placeholders(self):
        """The reference file carries the exact strings the LLM must
        copy into new SKILL.md files."""
        content = _CREATE_SKILL_FORMAT_REF.read_text()
        assert "frontmatter" in content.lower()
        assert "$ARGUMENTS" in content
        assert "${SKILL_DIR}" in content
        assert "scripts/" in content
        assert "Pattern A" in content and "Pattern B" in content

    def test_create_skill_format_reference_documents_hooks_field(self):
        """The reference must describe the `hooks:` frontmatter field so
        LLM-generated skills can use it. Guard: if someone prunes the
        table row, skills silently lose the ability to declare overlays."""
        content = _CREATE_SKILL_FORMAT_REF.read_text()
        assert "| hooks " in content  # table row present
        assert "PreToolUse" in content  # concrete event example
        assert "matcher:" in content  # YAML shape example

    def test_create_agent_docs_hooks_field(self):
        """Symmetric guard for create-agent: the frontmatter table must
        list the `hooks:` field so /create-agent produces agents that
        can declare overlays."""
        content = (_BUILTIN_DIR / "create-agent.md").read_text()
        assert "| hooks " in content

    def test_create_skill_rendered_prompt_preserves_placeholder_teaching(self):
        """Render create-skill's SKILL.md through substitute_arguments and
        check that the LLM ends up with a prompt that (a) still points at
        the reference file, (b) contains no shell-injection fallout, and
        (c) leaks the resolved skill_dir path only in the read_file
        instruction.

        Regression guard for the bug where ${SKILL_DIR} examples inside
        the SKILL.md body were silently substituted to create-skill's
        own absolute path, so the LLM saw a concrete /Users/... path
        everywhere instead of the literal placeholder and copied the
        resolved path into every new skill it produced.
        """
        from agent_cli.skills.executor import substitute_arguments

        body = _CREATE_SKILL_PATH.read_text()
        rendered = substitute_arguments(
            body,
            arguments="new-skill describe it",
            skill_dir="/FAKE/SKILL/DIR",
            session_id="SESS",
        )

        # $ARGUMENTS must still substitute for the live task steps.
        assert "The first word of new-skill describe it" in rendered

        # The read_file instruction must resolve to the real skill dir so
        # the LLM can actually open the reference.
        assert "/FAKE/SKILL/DIR/references/format.md" in rendered

        # No !`cmd` in the body should fire at render time.
        assert "[error]" not in rendered

        # The resolved skill_dir path may appear exactly once — only in
        # the read_file instruction. Any additional mention means a
        # ${SKILL_DIR} in prose / examples got substituted and the LLM
        # will see the concrete path instead of the placeholder.
        assert rendered.count("/FAKE/SKILL/DIR") == 1

    def test_create_skill_warns_against_hardcoded_paths(self):
        """Guards against LLM baking absolute paths into new SKILL.md files.

        History: past runs of /create-skill produced SKILL.md bodies
        containing `bash /Users/.../agent_cli/skills/builtin/scripts/run.sh`
        because the docs were inline and the ${SKILL_DIR} examples were
        substituted before the LLM ever saw the literal placeholder.

        Current design puts the placeholder-heavy teaching in
        references/format.md (read via read_file at runtime, so the
        literal `${SKILL_DIR}` strings survive) and keeps the SKILL.md
        body slim — one primacy-zone reminder in Task step 5 plus the
        DO/DON'T evidence below.
        """
        # Body must still remind the LLM not to write absolute paths.
        body_lower = _CREATE_SKILL_PATH.read_text().lower()
        assert "/users/" in body_lower  # anti-example in Task step 5
        assert "never" in body_lower

        # Reference carries the full DO/DON'T evidence.
        ref = _CREATE_SKILL_FORMAT_REF.read_text()
        ref_lower = ref.lower()
        assert "DO" in ref and "DON'T" in ref
        assert "never hardcode" in ref_lower or "not portable" in ref_lower
        assert "/users/" in ref_lower  # anti-example path
        assert "agent_cli/skills/builtin" in ref  # wrong-tree anti-example

    def test_create_agent_has_format_docs(self):
        path = _BUILTIN_DIR / "create-agent.md"
        content = path.read_text()
        assert "frontmatter" in content.lower()
        assert ".agent-cli/agents/" in content
        assert "delegate" in content.lower()

    def test_create_skill_user_invocable(self):
        skill = _parse_skill_file(_CREATE_SKILL_PATH)
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
