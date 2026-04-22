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


class TestExplorerPromptIntent:
    """Tripwires for the guidance the explorer prompt must carry.

    We check intent-level phrases, not literal sentences — these tests
    should fail only when a reword actually drops a concept, not on
    cosmetic edits. Keep the substrings short and unambiguous.
    """

    def _body(self) -> str:
        role, _config, _error = _load_agent("explorer")
        return (role or "").lower()

    def _description(self) -> str:
        _role, config, _error = _load_agent("explorer")
        return (config.get("description") or "").lower()

    def test_description_signals_analysis_not_edits(self):
        """Description drives parent-agent dispatch selection, so it must
        steer callers away from using explorer for edits."""
        desc = self._description()
        # Analysis signal
        assert "analysis" in desc or "analyze" in desc or "question" in desc
        # Edit warn-off
        assert "not" in desc and ("edit" in desc or "modify" in desc)

    def test_body_warns_about_stat_trap(self):
        """The concrete failure mode: agent reads stat and treats it as
        a full read. Prompt must explicitly reject this."""
        body = self._body()
        assert "stat" in body
        # Mentions that stat alone is insufficient, in some phrasing.
        assert "size" in body or "not an answer" in body or "still need to read" in body

    def test_body_names_line_range_as_conscious_full_read(self):
        """For large files the agent must know the line_start=1,line_end=<total>
        form — this is the contract the read_file guard expects."""
        body = self._body()
        assert "line_start" in body
        assert "line_end" in body

    def test_body_requires_citations(self):
        """Every non-trivial claim should cite file:line or a named symbol."""
        body = self._body()
        assert "cite" in body or "citation" in body or "file:line" in body

    def test_body_flags_docs_vs_code_discrepancy(self):
        """Agent should trust code over docs when they diverge — the
        symptom it addresses is over-reliance on ARCHITECTURE.md."""
        body = self._body()
        assert "doc" in body and "code" in body

    def test_body_warns_about_partial_read_trap(self):
        """Symptom observed after the first rewrite: agent stopped using
        stat but started sampling the first 100 lines of a 1200-line
        file instead. Prompt must reject arbitrary-range partial reads
        explicitly."""
        body = self._body()
        assert (
            "arbitrary" in body
            or "sample" in body
            or "sampling" in body
            or "false sense" in body
        )

    def test_body_forbids_fabricated_citations(self):
        """Symptom observed after the first rewrite: agent added
        `file:1` citations for files it never opened. Prompt must rule
        this out directly."""
        body = self._body()
        assert (
            "actually read" in body
            or "fabricat" in body
            or "did not read" in body
            or "never opened" in body
        )


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

        delegate_mod._reset_agent_loader([project_dir, _BUILTIN_AGENTS_DIR])

        role, config, error = _load_agent("explorer")
        assert error is None
        assert "custom" in role.lower()
        assert "write_file" in config["allowed-tools"]

    def test_builtin_used_when_no_override(self, tmp_path, monkeypatch):
        """Built-in is used when no project/user override exists."""
        import agent_cli.tools.delegate as delegate_mod

        empty_dir = tmp_path / "agents"
        empty_dir.mkdir()

        delegate_mod._reset_agent_loader([empty_dir, _BUILTIN_AGENTS_DIR])

        role, config, error = _load_agent("explorer")
        assert error is None
        assert "write_file" not in config.get("allowed-tools", [])
