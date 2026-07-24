"""Tests for built-in agent (worker profile) loading and discovery.

The built-in set is five general-purpose workers (main drives orchestration):
code-writer, code-reviewer, code-analyst, unittest-writer, log-analyst.
"""

from agent_cli.subagent.profiles import load_profile, _BUILTIN_PROFILES_DIR

WORKERS = (
    "code-writer",
    "code-reviewer",
    "code-analyst",
    "unittest-writer",
    "log-analyst",
)
READ_ONLY = ("code-reviewer", "code-analyst", "log-analyst")
WRITING = ("code-writer", "unittest-writer")


def _set_agent_paths(paths):
    """C2: prod 의 테스트 전용 mutator(_reset_agent_loader) 삭제 대체 —
    agents 모듈의 로더 전역을 직접 교체한다."""
    import agent_cli.subagent.profiles as _profiles_mod

    from agent_cli.resource_loader import ResourceLoader

    _profiles_mod._profile_loader = ResourceLoader(list(paths))


class TestBuiltinAgentsDirectory:
    def test_builtin_dir_exists(self):
        assert _BUILTIN_PROFILES_DIR.is_dir()

    def test_builtin_dir_has_the_five_workers(self):
        names = {p.stem for p in _BUILTIN_PROFILES_DIR.glob("*.md")}
        assert set(WORKERS) <= names, f"missing: {set(WORKERS) - names}"


class TestWorkerProfilesCommon:
    def test_all_load_with_description(self):
        for name in WORKERS:
            role, config, error = load_profile(name)
            assert error is None, f"{name}: {error}"
            assert role and len(role) > 200, name
            assert config.get("description"), name

    def test_read_only_workers_cannot_write(self):
        for name in READ_ONLY:
            _, config, _ = load_profile(name)
            tools = config["allowed-tools"]
            assert "write_file" not in tools, name
            assert "edit_file" not in tools, name
            assert "read_file" in tools, name

    def test_writing_workers_have_edit_tools(self):
        for name in WRITING:
            _, config, _ = load_profile(name)
            tools = config["allowed-tools"]
            assert "write_file" in tools and "edit_file" in tools, name
            assert "shell" in tools, name

    def test_all_workers_have_memory(self):
        """모든 워커는 격리된 private memory 로 세션을 넘어 지식 축적."""
        for name in WORKERS:
            _, config, _ = load_profile(name)
            assert "memory" in config["allowed-tools"], name

    def test_all_workers_mention_private_memory(self):
        """격리 인지 — 각 워커 본문이 memory 가 자기 것임을 명시."""
        for name in WORKERS:
            role, _, _ = load_profile(name)
            assert "private to you" in role.lower(), name


class TestCodeAnalystPromptIntent:
    """code-analyst carries the read-strategy tripwires (formerly explorer):
    intent-level phrases, not literal sentences — fail only when a reword
    actually drops a concept."""

    def _body(self) -> str:
        role, _c, _e = load_profile("code-analyst")
        return (role or "").lower()

    def _description(self) -> str:
        _r, config, _e = load_profile("code-analyst")
        return (config.get("description") or "").lower()

    def test_description_signals_analysis_not_edits(self):
        desc = self._description()
        assert "analy" in desc or "explains" in desc or "how" in desc
        assert "not" in desc and (
            "edit" in desc or "modify" in desc or "defect" in desc
        )

    def test_body_warns_about_stat_trap(self):
        body = self._body()
        assert "stat" in body
        assert "size" in body or "not an answer" in body or "still have to read" in body

    def test_body_names_line_range_as_conscious_full_read(self):
        body = self._body()
        assert "line_start" in body and "line_end" in body

    def test_body_requires_citations(self):
        body = self._body()
        assert "cite" in body or "citation" in body or "file:line" in body

    def test_body_flags_docs_vs_code_discrepancy(self):
        body = self._body()
        assert "doc" in body and "code" in body

    def test_body_warns_about_partial_read_trap(self):
        body = self._body()
        assert "arbitrary" in body or "false sense" in body or "sampl" in body

    def test_body_forbids_fabricated_citations(self):
        body = self._body()
        assert (
            "actually read" in body
            or "fabricat" in body
            or "did not read" in body
            or "never opened" in body
        )

    def test_body_expands_source_scope_beyond_one_language(self):
        body = self._body()
        assert "config" in body or "schema" in body or "frontmatter" in body

    def test_body_has_broad_survey_stop_criterion(self):
        body = self._body()
        assert "broad" in body
        assert (
            "subsystems where you actually read" in body
            or "read fewer" in body
            or "only the subsystems" in body
        )

    def test_body_cross_reference_rule(self):
        body = self._body()
        assert (
            "cross-reference" in body or "authoritative" in body or "manifest" in body
        )

    def test_body_traces_indirection(self):
        """kernel-analyzer 계승 — 등록 간접(callback/registry) 추적 규율."""
        body = self._body()
        assert "indirection" in body or "callback" in body or "registr" in body

    def test_boundary_with_reviewer(self):
        """analyst=이해, reviewer=결함판정 — 경계 명시."""
        body = self._body()
        assert "reviewer" in body and ("not" in body or "does not" in body)


class TestWorkerContracts:
    def test_code_writer_contract(self):
        role, _, _ = load_profile("code-writer")
        flat = role.lower()
        assert "files touched:" in flat  # 병렬 협업 보고 계약
        assert "scope" in flat  # 파일 스코프 규율
        assert "cleanup" in flat or "release" in flat  # 에러/정리 경로
        assert "narrowest" in flat or "verify" in flat  # 검증 전 보고

    def test_code_reviewer_contract(self):
        role, _, _ = load_profile("code-reviewer")
        flat = role.lower()
        assert "severity" in flat
        assert "failure scenario" in flat or "failure" in flat
        assert "false positive" in flat  # 억지 지적 경계
        assert "confirmed" in flat and "plausible" in flat  # confidence

    def test_unittest_writer_contract(self):
        role, _, _ = load_profile("unittest-writer")
        flat = role.lower()
        assert "mutation" in flat or "must bite" in flat or "must fail when" in flat
        assert "observable" in flat  # 로직 재구현 금지
        assert "fake" in flat or "stub" in flat  # 의존성 격리
        assert "files touched:" in flat

    def test_log_analyst_contract(self):
        role, _, _ = load_profile("log-analyst")
        flat = role.lower()
        assert "root cause" in flat
        assert "stack trace" in flat or "traceback" in flat
        assert "symptom" in flat  # 증상 vs 원인
        assert "confirmed" in flat and "plausible" in flat


class TestBuiltinAgentPriority:
    def test_project_overrides_builtin(self, tmp_path, monkeypatch):
        """Project agent with same name overrides built-in."""
        project_dir = tmp_path / "agents"
        project_dir.mkdir()
        (project_dir / "code-analyst.md").write_text(
            "---\nname: code-analyst\ndescription: Custom analyst\n"
            "allowed-tools: [read_file, write_file, shell]\n---\n\n"
            "# Custom Analyst\nYou are a custom analyst that can also write."
        )

        _set_agent_paths([project_dir, _BUILTIN_PROFILES_DIR])

        role, config, error = load_profile("code-analyst")
        assert error is None
        assert "custom" in role.lower()
        assert "write_file" in config["allowed-tools"]

    def test_builtin_used_when_no_override(self, tmp_path, monkeypatch):
        """Built-in is used when no project/user override exists."""
        empty_dir = tmp_path / "agents"
        empty_dir.mkdir()

        _set_agent_paths([empty_dir, _BUILTIN_PROFILES_DIR])

        role, config, error = load_profile("code-analyst")
        assert error is None
        assert "write_file" not in config.get("allowed-tools", [])
