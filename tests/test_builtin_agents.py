"""Tests for built-in agent loading and discovery."""

from agent_cli.tools.delegate import _load_agent, _BUILTIN_AGENTS_DIR


class TestBuiltinAgentsDirectory:
    def test_builtin_dir_exists(self):
        assert _BUILTIN_AGENTS_DIR.is_dir()

    def test_builtin_dir_has_agents(self):
        md_files = list(_BUILTIN_AGENTS_DIR.glob("*.md"))
        assert len(md_files) >= 1  # explorer


class TestExplorerAgent:
    def test_loads_successfully(self):
        role, config, error = _load_agent("explorer")
        assert error is None
        assert role is not None
        assert "explorer" in role.lower() or "read-only" in role.lower()

    def test_has_tool_restrictions(self):
        role, config, error = _load_agent("explorer")
        assert "allowed-tools" in config
        tools = config["allowed-tools"]
        assert "read_file" in tools
        assert "shell" in tools
        assert "write_file" not in tools
        assert "edit_file" not in tools

    def test_has_description(self):
        role, config, error = _load_agent("explorer")
        assert config.get("description")

    def test_role_mentions_read_only(self):
        role, config, error = _load_agent("explorer")
        assert "read" in role.lower()


class TestBuiltinAgentPriority:
    def test_project_overrides_builtin(self, tmp_path, monkeypatch):
        """Project agent with same name overrides built-in."""
        import agent_cli.tools.delegate as delegate_mod

        project_dir = tmp_path / "agents"
        project_dir.mkdir()
        (project_dir / "explorer.md").write_text(
            "---\nname: explorer\ndescription: Custom explorer\n"
            "allowed-tools: [read_file, write_file, shell]\n---\n\n"
            "# Custom Explorer\nYou are a custom explorer that can also write."
        )

        monkeypatch.setattr(
            delegate_mod,
            "_AGENT_SEARCH_PATHS",
            [project_dir, _BUILTIN_AGENTS_DIR],
        )

        role, config, error = _load_agent("explorer")
        assert error is None
        assert "custom" in role.lower()
        assert "write_file" in config["allowed-tools"]

    def test_builtin_used_when_no_override(self, tmp_path, monkeypatch):
        """Built-in is used when no project/user override exists."""
        import agent_cli.tools.delegate as delegate_mod

        empty_dir = tmp_path / "agents"
        empty_dir.mkdir()

        monkeypatch.setattr(
            delegate_mod,
            "_AGENT_SEARCH_PATHS",
            [empty_dir, _BUILTIN_AGENTS_DIR],
        )

        role, config, error = _load_agent("explorer")
        assert error is None
        assert "write_file" not in config.get("allowed-tools", [])
