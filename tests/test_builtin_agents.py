"""Tests for built-in agent loading and discovery."""

from agent_cli.subagent.profiles import load_profile, _BUILTIN_PROFILES_DIR


def _set_agent_paths(paths):
    """C2: prod 의 테스트 전용 mutator(_reset_agent_loader) 삭제 대체 —
    agents 모듈의 로더 전역을 직접 교체한다."""
    import agent_cli.subagent.profiles as _profiles_mod

    from agent_cli.resource_loader import ResourceLoader

    _profiles_mod._profile_loader = ResourceLoader(list(paths))


class TestBuiltinAgentsDirectory:
    def test_builtin_dir_exists(self):
        assert _BUILTIN_PROFILES_DIR.is_dir()

    def test_builtin_dir_has_agents(self):
        md_files = list(_BUILTIN_PROFILES_DIR.glob("*.md"))
        assert len(md_files) >= 1  # explorer


class TestExplorerAgent:
    def test_loads_successfully(self):
        role, config, error = load_profile("explorer")
        assert error is None
        assert role is not None
        assert "explorer" in role.lower() or "read-only" in role.lower()

    def test_has_tool_restrictions(self):
        role, config, error = load_profile("explorer")
        assert "allowed-tools" in config
        tools = config["allowed-tools"]
        assert "read_file" in tools
        assert "shell" in tools
        assert "write_file" not in tools
        assert "edit_file" not in tools

    def test_has_description(self):
        role, config, error = load_profile("explorer")
        assert config.get("description")

    def test_role_mentions_read_only(self):
        role, config, error = load_profile("explorer")
        assert "read" in role.lower()


class TestExplorerPromptIntent:
    """Tripwires for the guidance the explorer prompt must carry.

    We check intent-level phrases, not literal sentences — these tests
    should fail only when a reword actually drops a concept, not on
    cosmetic edits. Keep the substrings short and unambiguous.
    """

    def _body(self) -> str:
        role, _config, _error = load_profile("explorer")
        return (role or "").lower()

    def _description(self) -> str:
        _role, config, _error = load_profile("explorer")
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

    def test_body_expands_source_scope_beyond_python(self):
        """A1 — Source must include skill/agent markdown and config, not
        just .py. Symptom: earlier runs treated 'source' as python-only
        and ignored .md-backed subsystems (skills, agents)."""
        body = self._body()
        # One of the non-.py source types must appear in the scope guidance.
        assert (
            ".md" in body
            or "markdown" in body
            or "yaml frontmatter" in body
            or "configuration" in body
        )

    def test_body_has_broad_survey_stop_criterion(self):
        """A3 — Broad questions ("analyze the workspace") must constrain
        the answer to subsystems with an actual implementation read.
        Symptom: describing mcp/skills/providers internals from only
        reading their __init__.py."""
        body = self._body()
        assert "broad-survey" in body or "broad survey" in body or "broad" in body
        # Concrete instruction about only describing read subsystems.
        assert (
            "subsystems where you actually read" in body
            or "read fewer" in body
            or "only the subsystems" in body
        )

    def test_body_cross_reference_rule(self):
        """Cross-reference rule — doc claims that are testable against
        authoritative sources (e.g., pyproject.toml for deps) must be
        verified before repeating. Symptom: explorer claimed
        `python-json-repair` was a dependency by repeating the README
        without checking pyproject.toml."""
        body = self._body()
        assert (
            "pyproject" in body
            or "cross-reference" in body
            or "authoritative source" in body
        )


class TestBuiltinAgentPriority:
    def test_project_overrides_builtin(self, tmp_path, monkeypatch):
        """Project agent with same name overrides built-in."""

        project_dir = tmp_path / "agents"
        project_dir.mkdir()
        (project_dir / "explorer.md").write_text(
            "---\nname: explorer\ndescription: Custom explorer\n"
            "allowed-tools: [read_file, write_file, shell]\n---\n\n"
            "# Custom Explorer\nYou are a custom explorer that can also write."
        )

        _set_agent_paths([project_dir, _BUILTIN_PROFILES_DIR])

        role, config, error = load_profile("explorer")
        assert error is None
        assert "custom" in role.lower()
        assert "write_file" in config["allowed-tools"]

    def test_builtin_used_when_no_override(self, tmp_path, monkeypatch):
        """Built-in is used when no project/user override exists."""

        empty_dir = tmp_path / "agents"
        empty_dir.mkdir()

        _set_agent_paths([empty_dir, _BUILTIN_PROFILES_DIR])

        role, config, error = load_profile("explorer")
        assert error is None
        assert "write_file" not in config.get("allowed-tools", [])


class TestKernelProfiles:
    """내장 커널 4종 (5.1.0 협업 패치) — 로딩·도구 경계·정체성 핵심 계약."""

    ALL = ("kernel-coder", "kernel-kunit", "kernel-analyzer")
    READ_ONLY = ("kernel-analyzer",)
    WRITING = ("kernel-coder", "kernel-kunit")

    def test_all_load_with_description(self):
        for name in self.ALL:
            role, config, error = load_profile(name)
            assert error is None, f"{name}: {error}"
            assert role and len(role) > 200, name
            assert config.get("description"), name

    def test_read_only_profiles_cannot_write(self):
        for name in self.READ_ONLY:
            _, config, _ = load_profile(name)
            tools = config["allowed-tools"]
            assert "write_file" not in tools, name
            assert "edit_file" not in tools, name
            assert "read_file" in tools and "code_index" in tools, name

    def test_writing_profiles_have_edit_tools(self):
        for name in self.WRITING:
            _, config, _ = load_profile(name)
            tools = config["allowed-tools"]
            assert "write_file" in tools and "edit_file" in tools, name
            assert "shell" in tools, name  # build/checkpatch/kunit.py 실행

    def test_coder_contract(self):
        role, _, _ = load_profile("kernel-coder")
        flat = role.lower()
        assert "checkpatch" in flat  # 자가 검증
        assert "goto" in flat  # 에러 경로 규율
        assert "files touched:" in flat  # 병렬 협업 보고 계약
        assert "bug_on" in flat  # 드라이버 금지 규칙

    def test_kunit_contract(self):
        role, _, _ = load_profile("kernel-kunit")
        flat = role.lower()
        assert "kunit_test_suite" in flat
        assert "kunit.py" in flat  # 실행 검증 경로
        assert "kunit_expect" in flat or "kunit_assert" in flat
        assert ".kunitconfig" in flat

    def test_analyzer_contract(self):
        role, _, _ = load_profile("kernel-analyzer")
        flat = role.lower()
        assert "file:line" in flat  # 인용 규율
        assert "softirq" in flat or "hardirq" in flat  # 컨텍스트 주석
        assert "cannot modify" in flat  # 읽기 전용 정체성
