"""Tests for prompts/system_prompt."""

from agent_cli.prompts.system_prompt import (
    build_system_prompt,
    _build_environment_section,
    _load_directives,
    MAX_DIRECTIVE_FILE_CHARS,
)
from agent_cli.providers.compat import ModelCapabilities


def _make_caps(ctx_window: int = 32768) -> ModelCapabilities:
    return ModelCapabilities(
        context_window=ctx_window,
        max_output_tokens=4096,
        supports_structured_output=True,
        supports_tool_calling=False,
        supports_thinking=False,
        thinking_budget=0,
        supports_strict_schema=False,
    )


class TestBuildSystemPrompt:
    def test_includes_all_tools(self):
        prompt = build_system_prompt(
            _make_caps(), ["read_file", "write_file", "edit_file", "shell"]
        )
        assert "read_file" in prompt
        assert "write_file" in prompt
        assert "edit_file" in prompt
        assert "shell" in prompt

    def test_active_tools_only(self):
        prompt = build_system_prompt(_make_caps(), ["shell"])
        assert "shell" in prompt
        assert "Hashline" not in prompt  # No edit_file → no hashline guide
        assert "edit_file" not in prompt  # Not in active_tools

    def test_hashline_guide_inlined_with_edit(self):
        prompt = build_system_prompt(_make_caps(), ["edit_file"])
        assert "Hashline" in prompt
        # Should be inline, not a separate section
        assert "## Hashline" not in prompt

    def test_delegate_included(self):
        prompt = build_system_prompt(_make_caps(), ["shell"], include_delegate=True)
        assert "delegate" in prompt.lower()
        assert "tasks" in prompt

    def test_delegate_excluded(self):
        prompt = build_system_prompt(_make_caps(), ["shell"], include_delegate=False)
        assert "delegate" not in prompt.split("## Available Tools")[1]

    def test_delegate_guide_mentions_context_modes(self):
        prompt = build_system_prompt(_make_caps(), ["shell"], include_delegate=True)
        assert "none" in prompt
        assert "fork" in prompt
        assert "inherit" in prompt

    def test_delegate_guide_mentions_parallel(self):
        prompt = build_system_prompt(_make_caps(), ["shell"], include_delegate=True)
        assert "parallel" in prompt.lower()

    def test_delegate_guide_mentions_tasks_array(self):
        prompt = build_system_prompt(_make_caps(), ["shell"], include_delegate=True)
        assert '"tasks"' in prompt

    def test_json_format_present(self):
        prompt = build_system_prompt(_make_caps(), ["shell"])
        assert "JSON" in prompt
        assert "thought" in prompt

    def test_session_id_included(self):
        prompt = build_system_prompt(_make_caps(), ["shell"], session_id="1774882777")
        assert "1774882777" in prompt
        assert "Session" in prompt

    def test_session_id_omitted_when_empty(self):
        prompt = build_system_prompt(_make_caps(), ["shell"], session_id="")
        assert "Current session ID" not in prompt

    def test_ready_for_review_in_prompt(self):
        prompt = build_system_prompt(_make_caps(), ["shell"])
        assert "ready_for_review" in prompt
        assert "complete" in prompt

    def test_ready_for_review_before_complete_workflow(self):
        """The prompt should instruct to call ready_for_review before complete."""
        prompt = build_system_prompt(_make_caps(), ["shell"])
        rfr_pos = prompt.index("ready_for_review")
        complete_pos = prompt.index('"complete"')
        assert rfr_pos < complete_pos

    def test_rule_8_review_before_complete(self):
        prompt = build_system_prompt(_make_caps(), ["shell"])
        assert "ALWAYS call ready_for_review first" in prompt

    def test_environment_section_present(self):
        prompt = build_system_prompt(_make_caps(), ["shell"])
        assert "## Environment" in prompt
        assert "Working directory:" in prompt
        assert "Date:" in prompt
        assert "Platform:" in prompt

    def test_directives_loaded_when_present(self, tmp_path, monkeypatch):
        directive_dir = tmp_path / ".agent-cli"
        directive_dir.mkdir()
        (directive_dir / "DIRECTIVE.md").write_text("Always write tests.")
        monkeypatch.setattr(
            "agent_cli.prompts.system_prompt._DIRECTIVE_PATHS",
            [directive_dir / "DIRECTIVE.md"],
        )
        prompt = build_system_prompt(_make_caps(), ["shell"])
        assert "## Directives" in prompt
        assert "Always write tests." in prompt

    def test_directives_absent_when_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "agent_cli.prompts.system_prompt._DIRECTIVE_PATHS",
            [tmp_path / "nonexistent" / "DIRECTIVE.md"],
        )
        prompt = build_system_prompt(_make_caps(), ["shell"])
        assert "## Directives" not in prompt

    def test_task_guidelines_present(self):
        prompt = build_system_prompt(_make_caps(), ["shell"])
        assert "## Task Guidelines" in prompt
        assert "Read relevant code" in prompt

    def test_format_rules_present(self):
        prompt = build_system_prompt(_make_caps(), ["shell"])
        assert "## Response Format" in prompt
        assert "ONLY valid JSON" in prompt

    def test_section_order_primacy_before_tools(self):
        """Task Guidelines and Format Rules should appear before Available Tools."""
        prompt = build_system_prompt(_make_caps(), ["shell"])
        guidelines_pos = prompt.index("## Task Guidelines")
        format_pos = prompt.index("## Response Format")
        tools_pos = prompt.index("## Available Tools")
        assert guidelines_pos < format_pos < tools_pos

    def test_section_order_tools_before_environment(self):
        """Available Tools should appear before Environment (recency section)."""
        prompt = build_system_prompt(_make_caps(), ["shell"])
        tools_pos = prompt.index("## Available Tools")
        env_pos = prompt.index("## Environment")
        assert tools_pos < env_pos

    def test_section_order_session_in_recency(self):
        """Session ID should appear in the recency section, after tools."""
        prompt = build_system_prompt(_make_caps(), ["shell"], session_id="12345")
        tools_pos = prompt.index("## Available Tools")
        session_pos = prompt.index("## Session")
        env_pos = prompt.index("## Environment")
        assert tools_pos < session_pos < env_pos

    def test_static_tools_before_conditional(self):
        """Static tools (shell, read_file) should appear before conditional (edit_file)."""
        prompt = build_system_prompt(_make_caps(), ["read_file", "shell", "edit_file"])
        shell_pos = prompt.index("- shell:")
        edit_pos = prompt.index("- edit_file:")
        assert shell_pos < edit_pos

    def test_artifact_guide_inlined(self):
        prompt = build_system_prompt(_make_caps(), ["shell", "read_artifact"])
        assert "read_artifact" in prompt
        assert "scratchpad" in prompt.lower()
        # Should be inline, not a separate section
        assert "## Scratchpad & Artifacts" not in prompt

    def test_no_small_model_hints(self):
        """Small model hints should no longer be included."""
        prompt = build_system_prompt(_make_caps(ctx_window=4096), ["shell"])
        assert "Keep responses concise" not in prompt

    def test_no_thinking_hints(self):
        """Thinking model hints should no longer be included."""
        caps = ModelCapabilities(
            context_window=4096,
            max_output_tokens=4096,
            supports_structured_output=True,
            supports_tool_calling=False,
            supports_thinking=True,
            thinking_budget=1024,
            supports_strict_schema=False,
        )
        prompt = build_system_prompt(caps, ["shell"])
        assert "Thinking Budget" not in prompt


class TestEnvironmentSection:
    def test_contains_required_fields(self):
        section = _build_environment_section()
        assert "Working directory:" in section
        assert "Date:" in section
        assert "Platform:" in section

    def test_date_format(self):
        section = _build_environment_section()
        import re

        assert re.search(r"\d{4}-\d{2}-\d{2}", section)


class TestLoadDirectives:
    def test_empty_when_no_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "agent_cli.prompts.system_prompt._DIRECTIVE_PATHS",
            [tmp_path / "nope.md"],
        )
        assert _load_directives() == ""

    def test_loads_single_file(self, tmp_path, monkeypatch):
        d = tmp_path / ".agent-cli"
        d.mkdir()
        (d / "DIRECTIVE.md").write_text("Rule one.")
        monkeypatch.setattr(
            "agent_cli.prompts.system_prompt._DIRECTIVE_PATHS",
            [d / "DIRECTIVE.md"],
        )
        result = _load_directives()
        assert "Rule one." in result
        assert "## Directives" in result

    def test_truncates_large_file(self, tmp_path, monkeypatch):
        d = tmp_path / ".agent-cli"
        d.mkdir()
        (d / "DIRECTIVE.md").write_text("x" * (MAX_DIRECTIVE_FILE_CHARS + 500))
        monkeypatch.setattr(
            "agent_cli.prompts.system_prompt._DIRECTIVE_PATHS",
            [d / "DIRECTIVE.md"],
        )
        result = _load_directives()
        assert "[truncated]" in result

    def test_dedup_identical_content(self, tmp_path, monkeypatch):
        d1 = tmp_path / "proj" / ".agent-cli"
        d2 = tmp_path / "home" / ".agent-cli"
        d1.mkdir(parents=True)
        d2.mkdir(parents=True)
        (d1 / "DIRECTIVE.md").write_text("Same rule.")
        (d2 / "DIRECTIVE.md").write_text("Same rule.")
        monkeypatch.setattr(
            "agent_cli.prompts.system_prompt._DIRECTIVE_PATHS",
            [d1 / "DIRECTIVE.md", d2 / "DIRECTIVE.md"],
        )
        result = _load_directives()
        assert result.count("Same rule.") == 1

    def test_loads_both_when_different(self, tmp_path, monkeypatch):
        d1 = tmp_path / "proj" / ".agent-cli"
        d2 = tmp_path / "home" / ".agent-cli"
        d1.mkdir(parents=True)
        d2.mkdir(parents=True)
        (d1 / "DIRECTIVE.md").write_text("Project rule.")
        (d2 / "DIRECTIVE.md").write_text("User rule.")
        monkeypatch.setattr(
            "agent_cli.prompts.system_prompt._DIRECTIVE_PATHS",
            [d1 / "DIRECTIVE.md", d2 / "DIRECTIVE.md"],
        )
        result = _load_directives()
        assert "Project rule." in result
        assert "User rule." in result
