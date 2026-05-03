"""Tests for prompts/system_prompt.

Two complementary axes are exercised here:

- The "tool surface" axis (`TestBuildSystemPrompt`,
  `TestEnvironmentSection`, `TestLoadDirectives`,
  `TestDelegateInlineAgent`) — what tools/inline guides/format rules
  reach the prompt, and how directives/environment/delegate hints are
  composed.
- The "Role + Recovery" axis (`TestRoleInheritance`,
  `TestGitContextRemoved`, `TestSessionIdRemoved`,
  `TestContextRecoveryGuide`, `TestThoughtGuidelines`,
  `TestDirectiveBeforeEnvironment`) — how main/delegate/skill inherit
  Role, and how the Context Recovery Guide is composed.

The two axes were previously split into `test_system_prompt.py` and
`test_system_prompt_v2.py`; the `_v2` file was a Phase-3 redesign
artifact and has been folded back here so a single module is tested
by a single file. They keep two distinct fixture styles
(`_make_caps()` helper for the tool-surface axis, `caps` pytest
fixture for the Role/Recovery axis) — both work and unifying them
would be churn for no behavior change.
"""

import pytest

from agent_cli.prompts.system_prompt import (
    _build_context_recovery,
    _build_environment_section,
    _DELEGATE_INLINE,
    _load_directives,
    build_system_prompt,
)
from agent_cli.providers.compat import ModelCapabilities


def _make_caps(ctx_window: int = 32768) -> ModelCapabilities:
    return ModelCapabilities(
        context_window=ctx_window,
        max_output_tokens=4096,
        supports_structured_output=True,
        supports_thinking=False,
        thinking_budget=0,
        supports_strict_schema=False,
    )


@pytest.fixture
def caps():
    return ModelCapabilities(
        context_window=8000,
        max_output_tokens=2000,
        supports_structured_output=False,
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
        assert "- edit_file:" not in prompt  # Not in active_tools list

    def test_hashline_guide_inlined_with_edit(self):
        prompt = build_system_prompt(_make_caps(), ["edit_file"])
        assert "Hashline" in prompt
        # Should be inline, not a separate section
        assert "## Hashline" not in prompt

    def test_format_rules_enforce_single_action_per_turn(self):
        """Rule 9: explicit single-action enforcement. Prior to this
        rule the single-action shape was only implied by the example
        JSON. Nothing told the model that an `actions` array or a
        list in `action` was off-limits."""
        import re

        prompt = build_system_prompt(_make_caps(), ["shell"])
        flat = re.sub(r"\s+", " ", prompt)
        # "Exactly ONE action" or similar phrasing
        assert "ONE action" in flat or "exactly one" in flat.lower()
        # Explicitly rejects actions array / list-valued action
        assert "actions" in flat.lower()  # names the wrong shape
        assert "array" in flat.lower() or "list" in flat.lower()

    def test_format_rules_nudge_efficient_action(self):
        """Rule 10: within a single action, favor turn-efficient
        choices — batch input fields, shell batching (pipelines +
        multi-file surveys + listings), narrow reads, and no
        peek-then-redo. Intent-level checks so rewording doesn't
        break the test."""
        import re

        prompt = build_system_prompt(_make_caps(), ["shell", "edit_file"])
        flat = re.sub(r"\s+", " ", prompt)
        # Batch input fields named (at least one of edit_file.edits /
        # delegate.tasks appears in the guidance).
        assert "edits" in flat or "tasks" in flat
        # Shell batching concept. Three flavors should be representable:
        # pipelines, multi-file surveys, batch listings — we accept any
        # of those keywords as evidence the concept is present.
        assert (
            "pipeline" in flat.lower()
            or "multi-file" in flat.lower()
            or ("survey" in flat.lower() and "shell" in flat.lower())
        )
        # When shell survey suffices, don't redo with read_file — the
        # boundary between shell batching and read_file must be named.
        assert "read_file" in flat
        # Narrow read guidance (search / targeted / narrow).
        assert "narrow" in flat.lower() or "targeted" in flat.lower()
        # No peek-then-redo anti-pattern.
        assert "peek" in flat.lower() or "commit to" in flat.lower()

    def test_ask_description_bars_conversational_use(self):
        """Tool description for `ask` must explicitly forbid
        conversational closures. Repro: model emitted goodbyes
        ("see you next time!", "was that helpful?") via `ask` instead
        of `complete`, keeping the loop alive waiting for replies the
        user had no reason to give. Intent-level checks — at least one
        of "goodbye", "pleasantr", "satisfact", "closure" must show
        up alongside a "DO NOT" / "do not" type prohibition."""
        import re

        prompt = build_system_prompt(_make_caps(), ["ask"])
        flat = re.sub(r"\s+", " ", prompt).lower()
        # Description names what `ask` is for and what it isn't.
        assert "ask" in flat
        # A negative — the description tells the model NOT to use ask
        # for certain things.
        assert "do not use" in flat or "not for" in flat or "not use" in flat
        # And at least one of the conversational categories is named.
        assert (
            "goodbye" in flat
            or "pleasantr" in flat
            or "satisfact" in flat
            or "closure" in flat
            or "closer" in flat
        )

    def test_ask_inline_guide_contrasts_with_complete(self):
        """Inline guide for `ask` must explicitly contrast it with
        `complete` so the model has both halves of the decision in
        one place. Without this, the description alone (one paragraph
        in the tool listing) was getting drowned out and the model
        kept defaulting to `ask` for any non-task-oriented turn."""
        import re

        prompt = build_system_prompt(_make_caps(), ["ask"])
        flat = re.sub(r"\s+", " ", prompt).lower()
        # The guide places ask and complete side-by-side.
        assert "complete" in flat
        # And gives the model a concrete heuristic for picking.
        assert (
            "rule of thumb" in flat
            or "if your" in flat
            or "could be a statement" in flat
        )

    def test_hashline_guide_has_multi_edit_notes(self):
        """Multi-edit notes in _HASHLINE_INLINE prevent the three
        recurring drift patterns observed in S25FE-kernel session
        1776946589:
          1. Model assumes edits apply sequentially and uses
             post-edit-1 hashes in edit 2.
          2. Model submits overlapping edits (same ref / same region).
          3. Model tries to modify lines that an earlier edit in the
             same batch created.
        All three are intent-level tripwires, not literal-string
        checks. Whitespace is collapsed because the inline guide wraps
        across lines."""
        import re

        prompt = build_system_prompt(_make_caps(), ["edit_file"])
        flat = re.sub(r"\s+", " ", prompt)
        # (1) ORIGINAL state, not sequential pipeline
        assert "ORIGINAL file state" in flat
        # (2) overlap rejection with the fix instruction
        assert "overlap" in flat.lower()
        # (3) separate calls for dependent changes
        assert "separate edit_file calls" in flat

    def test_delegate_included(self):
        prompt = build_system_prompt(_make_caps(), ["shell", "delegate"])
        assert "delegate" in prompt.lower()
        assert "tasks" in prompt

    def test_delegate_excluded(self):
        prompt = build_system_prompt(_make_caps(), ["shell"])
        assert "delegate" not in prompt.split("## Available Tools")[1]

    def test_delegate_guide_mentions_context_modes(self):
        prompt = build_system_prompt(_make_caps(), ["shell", "delegate"])
        assert "none" in prompt
        assert "fork" in prompt

    def test_delegate_guide_mentions_parallel(self):
        prompt = build_system_prompt(_make_caps(), ["shell", "delegate"])
        assert "parallel" in prompt.lower()

    def test_delegate_guide_mentions_tasks_array(self):
        prompt = build_system_prompt(_make_caps(), ["shell", "delegate"])
        assert '"tasks"' in prompt

    def test_available_agents_shown_with_delegate(self):
        """When delegate is included, available agents are listed."""
        prompt = build_system_prompt(_make_caps(), ["shell", "delegate"])
        assert "Available Agents" in prompt
        assert "explorer" in prompt  # built-in agent

    def test_no_agents_without_delegate(self):
        prompt = build_system_prompt(_make_caps(), ["shell"])
        assert "Available Agents" not in prompt

    def test_json_format_present(self):
        prompt = build_system_prompt(_make_caps(), ["shell"])
        assert "JSON" in prompt
        assert "thought" in prompt

    def test_session_id_no_longer_creates_section(self):
        """session_id param accepted but no longer creates a ## Session section."""
        prompt = build_system_prompt(_make_caps(), ["shell"], session_id="1774882777")
        assert "## Session" not in prompt

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

    def test_workflow_review_before_complete(self):
        """The prompt's workflow example shows ready_for_review preceding
        complete. The redundant explicit rule was folded into the example
        itself ("first verify with ready_for_review, then call complete")."""
        prompt = build_system_prompt(_make_caps(), ["shell"])
        assert "first verify with `ready_for_review`" in prompt
        assert "then call `complete`" in prompt

    def test_environment_section_present(self):
        prompt = build_system_prompt(_make_caps(), ["shell"])
        assert "## Environment" in prompt
        assert "Working directory:" in prompt
        assert "Platform:" in prompt

    def test_environment_section_omits_date(self):
        # Date is intentionally excluded — see _build_environment_section
        # docstring for rationale (KV prefix cache stability).
        prompt = build_system_prompt(_make_caps(), ["shell"])
        assert "Date:" not in prompt

    def test_directives_loaded_when_present(self, tmp_path, monkeypatch):
        directive_dir = tmp_path / ".agent-cli"
        directive_dir.mkdir()
        (directive_dir / "DIRECTIVE.md").write_text("Always write tests.")
        monkeypatch.setattr(
            "agent_cli.prompts.system_prompt._DIRECTIVE_PATHS",
            [directive_dir],
        )
        prompt = build_system_prompt(_make_caps(), ["shell"])
        assert "## Directives" in prompt
        assert "Always write tests." in prompt

    def test_directives_absent_when_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "agent_cli.prompts.system_prompt._DIRECTIVE_PATHS",
            [tmp_path / "nonexistent"],
        )
        prompt = build_system_prompt(_make_caps(), ["shell"])
        assert "## Directives" not in prompt

    def test_task_guidelines_present(self):
        prompt = build_system_prompt(_make_caps(), ["shell"])
        assert "## Task Guidelines" in prompt
        # "Read before edit" applies to ALL file kinds, not only code —
        # config / docs / lockfiles all qualify.
        assert "Read a file before changing it" in prompt
        assert "code, config, docs" in prompt

    def test_no_recursive_invocation_in_guidelines(self):
        """Recursive-self-invocation guard moved from Response Format
        (where it was an outlier — a behavior rule, not a format rule)
        into Task Guidelines alongside the other safety guidance."""
        prompt = build_system_prompt(_make_caps(), ["shell"])
        guidelines = prompt.split("## Task Guidelines")[1].split("##")[0]
        assert "recursiv" in guidelines.lower()
        assert "agent-cli" in guidelines

    def test_context_discipline_present(self):
        """Primacy section teaching the LLM that the context window is a
        finite, shared resource and that each observation costs budget."""
        prompt = build_system_prompt(_make_caps(), ["shell"])
        assert "## Context Window Discipline" in prompt
        assert "single most important resource" in prompt
        assert "Read only what you need" in prompt

    def test_format_rules_present(self):
        prompt = build_system_prompt(_make_caps(), ["shell"])
        assert "## Response Format" in prompt
        # Header must enforce "JSON object only" — the wording was tightened
        # from "only valid JSON" but the contract is unchanged.
        assert "single JSON object only" in prompt

    def test_section_order_primacy_before_tools(self):
        """Context Discipline → Task Guidelines → Response Format, all in
        the primacy zone ahead of Available Tools."""
        prompt = build_system_prompt(_make_caps(), ["shell"])
        ctx_pos = prompt.index("## Context Window Discipline")
        guidelines_pos = prompt.index("## Task Guidelines")
        format_pos = prompt.index("## Response Format")
        tools_pos = prompt.index("## Available Tools")
        assert ctx_pos < guidelines_pos < format_pos < tools_pos

    def test_read_symbols_inline_guide_present(self):
        """When read_symbols is in active_tools, the inline guide should
        appear in the prompt — covering both modes, the language list,
        and the naming convention so the model can use the tool without
        a separate doc lookup."""
        prompt = build_system_prompt(_make_caps(), ["read_symbols"])
        assert "- read_symbols:" in prompt
        # Both modes documented.
        assert "mode='list'" in prompt
        assert "mode='fetch'" in prompt
        # Naming convention covers code (.) and C++ (::) and markdown headings.
        assert "Class.method" in prompt or "Foo.bar" in prompt
        assert "::" in prompt  # ns::Foo::bar
        assert "## Setup" in prompt

    def test_read_symbols_lists_supported_languages(self):
        """Every extension in _EXT_TO_LANG must appear in the inline
        guide. This is a single-source-of-truth regression guard: the
        guide is built from get_supported_extensions(), so adding a
        grammar to _EXT_TO_LANG should automatically propagate. If
        anyone reverts to a hardcoded list, this test catches it."""
        from agent_cli.tools.symbols import get_supported_extensions

        prompt = build_system_prompt(_make_caps(), ["read_symbols"])
        for ext in get_supported_extensions():
            assert ext in prompt, f"{ext} missing from read_symbols inline guide"

    def test_read_symbols_guide_advertises_hashline_output(self):
        """The fetch mode returns hashline-formatted bodies so the model
        can pipe straight into edit_file. The guide must surface that
        invariant — otherwise the model may waste a turn re-reading."""
        prompt = build_system_prompt(_make_caps(), ["read_symbols"])
        assert "hashline" in prompt.lower()
        assert "edit_file" in prompt

    def test_partial_read_recommends_substantial_range(self):
        """Models tend to over-narrow partial reads (line_start=100,
        line_end=150 to peek at one function), then come back two more
        turns later for surrounding context. The Partial-mode guidance
        names a target size and an explicit anti-pattern so the model
        reads enough on the first pass."""
        prompt = build_system_prompt(_make_caps(), ["read_file"])
        # Names a substantial target size for partial reads (5xx lines).
        assert "500 lines" in prompt or "~500" in prompt
        # Explicit anti-pattern callout — peeking at one function in a
        # 30-50 line slice usually wastes turns.
        assert "30-50 lines" in prompt or "more turns" in prompt

    def test_read_file_header_warns_both_sides(self):
        """The mode-selection header must warn against BOTH over-reading
        (full reads burn budget) AND under-reading (small reads cost
        turns). Earlier wording — 'Pick the smallest mode' — only warned
        one side and reinforced the over-narrow tendency."""
        prompt = build_system_prompt(_make_caps(), ["read_file"])
        assert "burn context budget" in prompt
        assert "costs turns" in prompt or "more turns" in prompt

    def test_read_file_steers_to_read_symbols_when_active(self):
        """When both tools are active, the read_file Flow paragraph must
        steer supported-language files at read_symbols mode='list' as
        the entry point — that's how we counteract read_file:stat
        being the cheaper-feeling default and getting read_symbols out
        of its low-baseline trap."""
        from agent_cli.tools.symbols import get_supported_extensions

        prompt = build_system_prompt(_make_caps(), ["read_file", "read_symbols"])
        # The Flow line names read_symbols as the entry point.
        assert "read_symbols mode='list' first" in prompt
        # Every supported extension must appear in the Flow paragraph
        # itself (the read_symbols guide already lists them — this
        # checks the read_file→read_symbols steering also stays in sync).
        flow_start = prompt.index(
            "Flow: for an unknown file, if its extension is supported by"
        )
        flow_end = prompt.index("instructions; follow them.", flow_start)
        flow_text = prompt[flow_start:flow_end]
        for ext in get_supported_extensions():
            assert ext in flow_text, f"{ext} missing from read_file Flow steering"

    def test_read_file_omits_steering_when_read_symbols_inactive(self):
        """If read_symbols is not in active_tools (e.g., subagent with a
        restricted tool list), the read_file guide must NOT mention it
        — pointing the model at a tool it cannot call wastes a retry on
        UNKNOWN_TOOL."""
        prompt = build_system_prompt(_make_caps(), ["read_file"])
        assert "read_symbols" not in prompt
        # Original Flow wording survives.
        assert "Flow: for an unknown file, stat first" in prompt

    def test_no_redundant_read_file_preview_rule(self):
        """The stat=true reminder moved into Context Discipline, so the
        old Task Guidelines bullet that duplicated it must be gone. Also
        guard against the legacy names ('preview', then 'peek') creeping
        back in — both were renamed because the LLM treated them as
        'I already looked at the file' and stopped after the first 20
        lines. 'stat' was chosen for its Unix-metadata connotation."""
        prompt = build_system_prompt(_make_caps(), ["read_file"])
        assert "call with preview=true first" not in prompt
        assert "preview=true" not in prompt
        assert "peek=true" not in prompt
        assert "stat" in prompt

    def test_section_order_tools_before_environment(self):
        """Available Tools should appear before Environment (recency section)."""
        prompt = build_system_prompt(_make_caps(), ["shell"])
        tools_pos = prompt.index("## Available Tools")
        env_pos = prompt.index("## Environment")
        assert tools_pos < env_pos

    def test_section_order_no_session_section(self):
        """Session ID no longer creates a section."""
        prompt = build_system_prompt(_make_caps(), ["shell"], session_id="12345")
        assert "## Session" not in prompt

    def test_static_tools_before_conditional(self):
        """Static tools (shell, read_file) should appear before conditional (edit_file)."""
        prompt = build_system_prompt(_make_caps(), ["read_file", "shell", "edit_file"])
        shell_pos = prompt.index("- shell:")
        edit_pos = prompt.index("- edit_file:")
        assert shell_pos < edit_pos

    def test_read_artifact_removed(self):
        """read_artifact tool removed from system prompt."""
        prompt = build_system_prompt(_make_caps(), ["shell"])
        assert "read_artifact" not in prompt

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
            supports_thinking=True,
            thinking_budget=1024,
            supports_strict_schema=False,
        )
        prompt = build_system_prompt(caps, ["shell"])
        assert "Thinking Budget" not in prompt

    def test_agent_role_replaces_default_role(self):
        """Agent role replaces default ROLE_PROMPT in Primacy zone."""
        prompt = build_system_prompt(
            _make_caps(), ["shell"], agent_role="You are a reviewer"
        )
        assert "## Role" in prompt
        assert "You are a reviewer" in prompt
        assert "AI assistant that solves tasks" not in prompt

    def test_agent_role_excluded_when_empty(self):
        """Empty agent_role uses default ROLE_PROMPT."""
        prompt = build_system_prompt(_make_caps(), ["shell"], agent_role="")
        assert "AI assistant" in prompt

    def test_agent_role_in_primacy_before_tools(self):
        """Agent Role is in Primacy zone, before Available Tools."""
        prompt = build_system_prompt(
            _make_caps(), ["shell"], agent_role="You are a reviewer"
        )
        role_pos = prompt.index("You are a reviewer")
        tools_pos = prompt.index("## Available Tools")
        assert role_pos < tools_pos


class TestEnvironmentSection:
    def test_contains_required_fields(self):
        section = _build_environment_section()
        assert "Working directory:" in section
        assert "Platform:" in section

    def test_excludes_date(self):
        # Date removed for KV prefix-cache stability across midnight.
        section = _build_environment_section()
        assert "Date:" not in section


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
            [d],
        )
        result = _load_directives()
        assert "Rule one." in result
        assert "## Directives" in result

    def test_large_file_not_truncated(self, tmp_path, monkeypatch):
        """Large directives are loaded fully — no truncation."""
        d = tmp_path / ".agent-cli"
        d.mkdir()
        long_content = "x" * 10000
        (d / "DIRECTIVE.md").write_text(long_content)
        monkeypatch.setattr(
            "agent_cli.prompts.system_prompt._DIRECTIVE_PATHS",
            [d],
        )
        result = _load_directives()
        assert "[truncated]" not in result
        assert "x" * 100 in result

    def test_dedup_identical_content(self, tmp_path, monkeypatch):
        d1 = tmp_path / "proj" / ".agent-cli"
        d2 = tmp_path / "home" / ".agent-cli"
        d1.mkdir(parents=True)
        d2.mkdir(parents=True)
        (d1 / "DIRECTIVE.md").write_text("Same rule.")
        (d2 / "DIRECTIVE.md").write_text("Same rule.")
        monkeypatch.setattr(
            "agent_cli.prompts.system_prompt._DIRECTIVE_PATHS",
            [d1, d2],
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
            [d1, d2],
        )
        result = _load_directives()
        assert "Project rule." in result
        assert "User rule." in result


class TestDelegateInlineAgent:
    """AG-27 ~ AG-28: _DELEGATE_INLINE agent field tests."""

    def test_delegate_inline_mentions_agent(self):
        """AG-27: _DELEGATE_INLINE contains agent field description."""
        assert '"agent"' in _DELEGATE_INLINE
        assert ".agent-cli/agents/" in _DELEGATE_INLINE

    def test_delegate_inline_agent_example(self):
        """AG-28: _DELEGATE_INLINE contains agent usage example."""
        assert '"agent": "security-reviewer"' in _DELEGATE_INLINE


# ── Role + Recovery axis (formerly test_system_prompt_v2.py) ────────


class TestRoleInheritance:
    def test_main_uses_default_role(self, caps):
        prompt = build_system_prompt(caps, ["read_file", "shell"])
        assert "AI assistant" in prompt

    def test_delegate_replaces_role(self, caps):
        prompt = build_system_prompt(
            caps, ["read_file"], agent_role="You are an explorer agent."
        )
        assert "explorer agent" in prompt
        assert "AI assistant that solves tasks" not in prompt

    def test_skill_inherits_parent_role(self, caps):
        prompt = build_system_prompt(
            caps, ["read_file"], parent_role="You are a code reviewer."
        )
        assert "code reviewer" in prompt
        assert "AI assistant that solves tasks" not in prompt

    def test_agent_role_takes_precedence_over_parent_role(self, caps):
        """If both agent_role and parent_role given, agent_role wins."""
        prompt = build_system_prompt(
            caps,
            ["read_file"],
            agent_role="You are an explorer.",
            parent_role="You are a reviewer.",
        )
        assert "explorer" in prompt
        assert "reviewer" not in prompt


class TestGitContextRemoved:
    def test_no_git_context(self, caps):
        prompt = build_system_prompt(caps, ["read_file", "shell"])
        assert "git status" not in prompt.lower() or "## Git" not in prompt


class TestSessionIdRemoved:
    def test_no_session_section(self, caps):
        prompt = build_system_prompt(caps, ["read_file"], session_id="test-123")
        # session_id param still accepted but no longer creates a section
        assert "## Session" not in prompt


class TestContextRecoveryGuide:
    def test_recovery_guide_present(self, caps):
        prompt = build_system_prompt(
            caps, ["read_file"], session_dir="/tmp/sessions/abc"
        )
        assert "## Context Recovery" in prompt
        assert "history.jsonl" in prompt
        assert "/tmp/sessions/abc" in prompt

    def test_no_recovery_without_session_dir(self, caps):
        prompt = build_system_prompt(caps, ["read_file"])
        assert "## Context Recovery" not in prompt

    def test_build_context_recovery_format(self):
        result = _build_context_recovery("/tmp/test")
        assert "read_file" in result
        assert "/tmp/test/history.jsonl" in result


class TestThoughtGuidelines:
    def test_thought_includes_purpose_and_reason(self, caps):
        prompt = build_system_prompt(caps, ["read_file"])
        assert "purpose" in prompt.lower()
        assert "reason" in prompt.lower()


class TestRecencySectionOrder:
    """Recency layout (passive → active, persistent → immediate):

    Environment → Context Recovery → Directives → Execution Context.

    Execution Context comes last because it's the only Recency section
    that mutates within a session (skill/agent boundaries) — keeping it
    last leaves the preceding three as a stable KV-cache-friendly prefix.
    """

    def test_environment_before_recovery(self, caps):
        prompt = build_system_prompt(caps, ["read_file"], session_dir="/tmp/test")
        env_pos = prompt.find("## Environment")
        recovery_pos = prompt.find("## Context Recovery")
        assert env_pos >= 0 and recovery_pos >= 0
        assert env_pos < recovery_pos

    def test_recovery_before_execution_context(self, caps, tmp_path, monkeypatch):
        directive_dir = tmp_path / ".agent-cli"
        directive_dir.mkdir()
        (directive_dir / "DIRECTIVE.md").write_text("Always be brief.")
        monkeypatch.chdir(tmp_path)

        prompt = build_system_prompt(
            caps,
            ["read_file"],
            skill_stack=["my-skill"],
            session_dir="/tmp/test",
        )
        recovery_pos = prompt.find("## Context Recovery")
        directives_pos = prompt.find("## Directives")
        exec_pos = prompt.find("## Execution Context")
        assert recovery_pos >= 0 and directives_pos >= 0 and exec_pos >= 0
        assert recovery_pos < directives_pos < exec_pos

    def test_execution_context_is_last_when_present(self, caps):
        """When Execution Context is included, no Recency section follows it."""
        prompt = build_system_prompt(
            caps,
            ["read_file"],
            skill_stack=["my-skill"],
            session_dir="/tmp/test",
        )
        exec_pos = prompt.find("## Execution Context")
        assert exec_pos >= 0
        # Nothing else should come after it.
        for section in ("## Environment", "## Context Recovery", "## Directives"):
            pos = prompt.find(section)
            if pos >= 0:
                assert pos < exec_pos
