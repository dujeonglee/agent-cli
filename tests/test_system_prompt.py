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

from pathlib import Path

from agent_cli.prompts.system_prompt import (
    _build_context_recovery,
    _build_delegate_inline,
    _build_environment_section,
    _build_tools_section,
    _load_directives,
    build_system_prompt,
    build_system_prompt_sections,
)
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.wire_formats import get as _get_wire_format

_SNAPSHOT_DIR = Path(__file__).parent / "snapshots"
# Fixed tool set for the snapshot — deterministic (no CWD / directives / env).
_SNAPSHOT_TOOLS = ["read_file", "shell", "code_index", "edit_file", "delegate", "ask"]


class TestToolsSectionSnapshot:
    """Regression guard for the format-aware-prompt refactor (DESIGN §5): the
    single-action formats (react / prefix_md) must render the Available Tools
    section BYTE-IDENTICALLY through the change. ``_build_tools_section`` is
    deterministic (unlike the full prompt, which carries CWD / directives), so
    it snapshots cleanly. Regenerate with::

        python -c "from agent_cli import wire_formats as w; \
from agent_cli.prompts.system_prompt import _build_tools_section as b; \
[open(f'tests/snapshots/tools_section_{n}.txt','w').write( \
b(['read_file','shell','code_index','edit_file','delegate','ask'], w.get(n))) \
for n in ('react','prefix_md')]"
    """

    @pytest.mark.parametrize("name", ["react", "prefix_md"])
    def test_tools_section_matches_snapshot(self, name):
        expected = (_SNAPSHOT_DIR / f"tools_section_{name}.txt").read_text(
            encoding="utf-8"
        )
        actual = _build_tools_section(_SNAPSHOT_TOOLS, _get_wire_format(name))
        assert actual == expected, (
            f"{name} Available Tools section changed — if intentional, "
            "regenerate the snapshot (see class docstring)."
        )


class _MultiOpFormat:
    """Minimal stand-in for a multi-op wire format (md_array-style): flat
    ``{action, plain params}`` ops, no per-tool batch, no `complete` tool.
    Duck-typed — the prompt layer only reads ``multi_op`` /
    ``exposes_complete`` and calls ``render_action_input``."""

    multi_op = True
    exposes_complete = False

    def render_action_input(self, action_input: dict) -> str:
        import json as _json

        from agent_cli.tools.registry import TOOLS

        for n in sorted(TOOLS, key=len, reverse=True):
            pfx = n + "_"
            if action_input and all(k.startswith(pfx) for k in action_input):
                flat = {k[len(pfx) :]: v for k, v in action_input.items()}
                return _json.dumps({"action": n, **flat}, ensure_ascii=False)
        return _json.dumps(action_input, ensure_ascii=False)


class TestMultiOpPromptBranches:
    """The multi-op / no-complete prompt rendering (DESIGN §5): per-tool batch
    prose is dropped (the op array IS the batch — nesting a batch array inside
    the op array is what broke 27B), param keys lose their wire prefix (flat
    op convention), and `complete` is withheld. Single-action formats are
    byte-guarded by the snapshot test above."""

    @pytest.fixture
    def section(self):
        return _build_tools_section(_SNAPSHOT_TOOLS, _MultiOpFormat())

    def test_complete_not_listed(self, section):
        assert "- complete:" not in section
        # ready_for_review stays, with its `complete` reference rephrased
        assert "- ready_for_review:" in section
        assert "BEFORE complete" not in section

    def test_param_keys_unprefixed(self, section):
        # No prefixed batch key appears at all under multi-op — not as a param
        # and not in prose. (An earlier B-guard line that NAMED read_file_reads
        # to forbid it was removed: on the 27B the negative mention primed the
        # very token it forbade — "don't think of an elephant" — Exp 8 live.)
        assert "read_file_reads" not in section
        assert "shell_command" not in section
        assert "code_index_queries" not in section
        assert "delegate_tasks" not in section

    def test_read_file_single_target_no_batch(self, section):
        assert "Each read_file op targets ONE file" in section
        assert "5. Batch" not in section
        assert "takes a LIST of reads" not in section
        assert '{"action": "read_file", "path": "app.py", "stat": true}' in section

    def test_read_file_guide_nudges_same_turn_batch(self, section):
        # B (DESIGN §6): read_file is the dominant missed-batch tool, so its
        # multi-op guide must steer several files into ONE turn — with the
        # no-nesting guardrail right where batching is suggested.
        assert "SAME turn" in section
        assert "never\n  a list inside one op" in section or (
            "never a list inside one op" in " ".join(section.split())
        )

    def test_read_file_guide_avoids_reads_plural_seed(self, section):
        # DESIGN Exp 8: the 27B composed `read_file` + the plural noun "reads"
        # into the invented key `read_file_reads`. The md_array read_file guide
        # must not put "reads" as a plural noun next to the tool name — guard
        # the specific phrasings that were the seed.
        assert "independent reads" not in section
        assert "full reads" not in section
        assert "read_file op reads" not in section

    def test_edit_file_one_edit_per_op(self, section):
        assert "one edit per op" in section
        assert "batch in one call" not in section
        # the batch-array constraint bullet is dropped
        assert "the array is NOT a" not in section

    def test_code_index_one_query_per_op(self, section):
        assert "Each code_index op runs ONE query" in section
        assert "takes a LIST of queries" not in section

    def test_code_index_guide_nudges_same_turn_batch(self, section):
        # Regression guard: code_index's multi-op guide already nudges
        # same-turn batching — keep it (B leaves it as-is).
        assert "in\n  the same turn" in section or (
            "in the same turn" in " ".join(section.split())
        )

    def test_delegate_one_task_per_op(self, section):
        assert "Each delegate op runs ONE subagent task" in section
        assert 'Always use the "tasks" array format' not in section

    def test_ask_guide_uses_no_complete_variant(self, section):
        assert "`ask` vs finishing" in section
        assert "`ask` vs `complete`" not in section

    def test_default_formats_keep_batch_and_complete(self):
        # Sanity inverse: a singular format still renders batch + complete.
        section = _build_tools_section(_SNAPSHOT_TOOLS, _get_wire_format("react"))
        assert "- complete:" in section
        assert "read_file_reads" in section
        assert "5. Batch" in section

    # ── Root fix (DESIGN Exp 8): the tool Input-JSON schema, not just the
    # inline guide, must advertise the FLAT single-op shape under multi-op.
    # The batch array param is unwrapped to its item fields; batch prose is
    # neutralized. (This is the schema the 27B copied into `read_file_reads`.)

    def test_read_file_input_json_is_flat_not_batch(self, section):
        flat = " ".join(section.split())
        # item fields surfaced at top level …
        assert '"path": "string, required' in flat
        assert '"stat":' in flat and '"search":' in flat
        # … and the batch array wrapper param is gone (both prefixed + bare)
        assert '"reads":' not in flat
        assert '"read_file_reads":' not in flat
        # batch-framing prose neutralized
        for phrase in ("one or more files", "as a list", "one-element list"):
            assert phrase not in flat

    def test_code_index_input_json_is_flat_not_batch(self, section):
        flat = " ".join(section.split())
        assert '"mode": "string, required' in flat
        assert '"queries":' not in flat
        assert '"code_index_queries":' not in flat
        assert "Each op runs one query." in flat
        assert "Provide queries as a LIST" not in flat

    def test_edit_file_input_json_flat_merges_scalar_and_item(self, section):
        flat = " ".join(section.split())
        # scalar param kept + edit-item fields surfaced (matches wrap_single_op)
        assert '"path": "string, required' in flat
        assert '"op": "string, required' in flat and '"pos":' in flat
        assert '"edits":' not in flat and '"edit_file_edits":' not in flat

    def test_delegate_input_json_is_flat_not_batch(self, section):
        flat = " ".join(section.split())
        assert '"task": "string, required' in flat
        assert '"tasks":' not in flat and '"delegate_tasks":' not in flat


class TestBuildSystemPromptSections:
    """``build_system_prompt_sections`` is the single assembly point — the
    joined form MUST be byte-identical to ``build_system_prompt`` (the web
    Prompt Inspector renders the same sections the LLM receives, no drift),
    and the (name, text) structure is what the inspector keys on."""

    def test_join_is_byte_identical_to_build(self):
        kwargs = dict(
            capabilities=_make_caps(),
            active_tools=["read_file", "shell", "edit_file", "delegate"],
            session_dir="/tmp/sess",
            skill_stack=["s1"],
            depth=1,
            max_depth=3,
        )
        sections = build_system_prompt_sections(**kwargs)
        assert "\n\n".join(t for _, t in sections) == build_system_prompt(**kwargs)

    def test_section_names_present_and_ordered(self):
        sections = build_system_prompt_sections(
            _make_caps(), ["read_file", "shell", "delegate"], session_dir="/tmp/s"
        )
        names = [n for n, _ in sections]
        # Primacy → Middle → Recency ordering of the always-present sections
        core = [
            "Role",
            "Context Discipline",
            "Task Guidelines",
            "Response Format",
            "Available Tools",
            "Environment",
        ]
        positions = [names.index(c) for c in core]
        assert positions == sorted(positions)
        assert "Context Recovery" in names  # session_dir given
        assert "Agents" in names  # delegate active
        assert (
            names.index("Execution Context") == len(names) - 1
            if ("Execution Context" in names)
            else True
        )

    def test_names_unique(self):
        sections = build_system_prompt_sections(
            _make_caps(), ["read_file", "shell", "delegate"], session_dir="/tmp/s"
        )
        names = [n for n, _ in sections]
        assert len(names) == len(set(names))

    def test_conditional_sections_absent_when_not_applicable(self):
        sections = build_system_prompt_sections(_make_caps(), ["shell"])
        names = [n for n, _ in sections]
        assert "Context Recovery" not in names  # no session_dir
        assert "Agents" not in names  # no delegate
        assert "MCP Tools" not in names  # no mcp_manager

    def test_agent_role_replaces_role_section(self):
        sections = build_system_prompt_sections(
            _make_caps(), ["shell"], agent_role="You are a security reviewer."
        )
        role_texts = [t for n, t in sections if n == "Role"]
        assert len(role_texts) == 1
        assert "security reviewer" in role_texts[0]

    def test_every_section_text_nonempty(self):
        sections = build_system_prompt_sections(_make_caps(), ["shell"])
        assert all(t.strip() for _, t in sections)


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
        """The `ask` guidance must steer conversational closures to
        `complete` instead of `ask`. Repro: model emitted goodbyes
        ("see you next time!", "was that helpful?") via `ask` instead
        of `complete`, keeping the loop alive waiting for replies the
        user had no reason to give. The detailed prohibition lives in
        the inline guide (`_ASK_INLINE`); the one-line tool description
        is kept compact to avoid duplicating it. Intent-level checks —
        at least one conversational category is named AND it is routed
        to `complete`."""
        import re

        prompt = build_system_prompt(_make_caps(), ["ask"])
        flat = re.sub(r"\s+", " ", prompt).lower()
        assert "ask" in flat
        # A conversational category is named...
        assert (
            "goodbye" in flat
            or "pleasantr" in flat
            or "satisfact" in flat
            or "closure" in flat
            or "closer" in flat
        )
        # ...and routed to `complete` (the prohibition: don't ask for it).
        assert "use `complete`" in flat or "end with `complete`" in flat

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

    def test_hashline_guide_demands_current_turn_read(self):
        """Pre-call routine: "Always read the file first" was too lax —
        models reused hashes from earlier turns and got hash mismatches
        on edit. The guide must require the read to happen in the
        CURRENT turn, and must call out that code_index fetch counts as
        a fresh read (its output is hashline-formatted), so the model
        doesn't waste a turn doing a redundant read_file after fetch."""
        import re

        prompt = build_system_prompt(_make_caps(), ["edit_file", "code_index"])
        flat = re.sub(r"\s+", " ", prompt)
        # Current-turn / immediacy is asserted (case-insensitive: the
        # source uses CURRENT in caps for emphasis).
        assert "current turn" in flat.lower()
        # The drift mechanism is named — hashes from earlier turns are
        # not reusable because something else may have touched the file.
        assert "drift" in flat.lower()
        # code_index mode='fetch' is acknowledged as a fresh read so the
        # model doesn't double-read.
        assert "code_index mode='fetch'" in flat or "fetch counts" in flat.lower()

    def test_hashline_guide_reframes_mismatch_as_guardrail(self):
        """Post-error tone: a hash mismatch must read as a guardrail,
        not a failure. Models that take mismatch as "I made a mistake"
        spiral into apology / excessive caution; framing it as a system
        guardrail keeps the recovery action mechanical (re-read,
        retry)."""
        import re

        prompt = build_system_prompt(_make_caps(), ["edit_file"])
        flat = re.sub(r"\s+", " ", prompt)
        # The reframe word "guardrail" appears.
        assert "guardrail" in flat.lower()
        # The negation "not a failure" (or close paraphrase) appears so
        # the model doesn't take it as their own error.
        assert "not a failure" in flat.lower()
        # Recovery action is still spelled out.
        assert "re-read" in flat.lower() or "re-fetch" in flat.lower()

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

    def test_task_guidelines_block_feature_creep(self):
        """Task Guidelines must explicitly forbid feature creep, premature
        abstraction, and speculative flexibility — the LLM equivalents
        of "while I'm here..." cleanups that bloat diffs and create
        review friction. Tested at intent level: at least one of the
        anti-pattern phrasings must appear."""
        import re

        prompt = build_system_prompt(_make_caps(), ["shell"])
        guidelines = prompt.split("## Task Guidelines")[1].split("##")[0]
        flat = re.sub(r"\s+", " ", guidelines).lower()
        # Names "beyond what the task requires" or equivalent scope cap.
        assert "beyond what the task requires" in flat or "scope" in flat
        # Names "premature abstraction" / "single-use abstraction" /
        # "helper" anti-pattern.
        assert (
            "premature abstraction" in flat
            or "doesn't need a helper" in flat
            or "single-use" in flat
        )
        # Names "hypothetical" future requirements / configurability.
        assert "hypothetical" in flat or "speculative" in flat

    def test_task_guidelines_block_impossible_error_handling(self):
        """Models reflexively add try/except, fallbacks, and input
        validation for scenarios that an internal caller cannot produce
        — that's noise that obscures real error paths. The guidelines
        must name the boundary rule (validate at system boundaries
        only) so the model has a clear place to draw the line."""
        import re

        prompt = build_system_prompt(_make_caps(), ["shell"])
        guidelines = prompt.split("## Task Guidelines")[1].split("##")[0]
        flat = re.sub(r"\s+", " ", guidelines).lower()
        # Anti-pattern is named.
        assert (
            "scenarios that can't happen" in flat
            or "impossible" in flat
            or "can't happen" in flat
        )
        # System-boundary rule is given as the affirmative guidance, not
        # just a "don't" — otherwise the model has nowhere to put the
        # legitimate validation.
        assert "system boundaries" in flat or "boundaries" in flat

    def test_task_guidelines_orphan_rule_distinguishes_ownership(self):
        """The orphan rule has two halves and both must be present:
        (a) clean up imports/variables/functions YOUR change made
        unused, (b) do NOT delete pre-existing dead code unsolicited.
        Without (b), the model auto-deletes whatever looks unused and
        bloats the diff."""
        import re

        prompt = build_system_prompt(_make_caps(), ["shell"])
        guidelines = prompt.split("## Task Guidelines")[1].split("##")[0]
        flat = re.sub(r"\s+", " ", guidelines).lower()
        # (a) clean own orphans — names what gets removed.
        assert "imports" in flat
        assert "your change" in flat or "your changes" in flat
        # (b) leave pre-existing dead code alone unless asked.
        assert "pre-existing" in flat or "without asking" in flat

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

    def test_code_index_inline_guide_present(self):
        """When code_index is in active_tools, the inline guide should
        appear in the prompt — covering the principal modes, the
        language list, and the naming convention so the model can use
        the tool without a separate doc lookup."""
        prompt = build_system_prompt(_make_caps(), ["code_index"])
        assert "- code_index:" in prompt
        # The principal modes are documented (representative subset).
        assert "mode='list'" in prompt
        assert "mode='fetch'" in prompt
        assert "mode='lookup'" in prompt
        assert "mode='callers'" in prompt
        assert "mode='slice'" in prompt
        # Naming convention covers code (.) and C++ (::) and markdown headings.
        assert "Class.method" in prompt
        assert "::" in prompt  # namespace::Class::method
        assert "## Setup" in prompt

    def test_code_index_lists_supported_languages(self):
        """Every extension registered by a walker module must appear in
        the inline guide. This is a single-source-of-truth regression
        guard: the guide is built from get_supported_extensions(), so
        adding a walker should automatically propagate. If anyone
        reverts to a hardcoded list, this test catches it."""
        from agent_cli.code_index.languages import get_supported_extensions

        prompt = build_system_prompt(_make_caps(), ["code_index"])
        for ext in get_supported_extensions():
            assert ext in prompt, f"{ext} missing from code_index inline guide"

    def test_code_index_guide_distinguishes_per_file_from_index_wide(self):
        """The 10-mode surface splits into per-file (list/fetch) and
        index-wide (lookup/kind/refs/callers/callees/slice). The guide
        must make that scope distinction explicit so the model doesn't
        try to use mode='file' for an arbitrary out-of-root file."""
        import re

        prompt = build_system_prompt(_make_caps(), ["read_file", "code_index"])
        flat = re.sub(r"\s+", " ", prompt).lower()
        # Per-file out-of-root falls back to on-demand parse.
        assert "on-demand parse" in flat
        # Cross-file modes are explicitly index-scoped.
        assert "index-scoped" in flat

    def test_read_file_flow_drops_comparative_tone(self):
        """Earlier wording leaned on a comparison ("stronger entry point
        than stat's 20-line head") to justify routing supported files at
        code_index. Imperative wording is more turn-efficient: just say
        what to call. Regression guard against re-introducing the
        comparative phrasing."""
        prompt = build_system_prompt(_make_caps(), ["read_file", "code_index"])
        # Old comparative phrasing must not return.
        assert "stronger entry point" not in prompt
        # The imperative form remains (covered by the existing steering
        # test, but pinned here too for clarity).
        assert "code_index mode='list' first" in prompt

    def test_code_index_guide_advertises_hashline_output(self):
        """The fetch mode returns hashline-formatted bodies so the model
        can pipe straight into edit_file. The guide must surface that
        invariant — otherwise the model may waste a turn re-reading."""
        prompt = build_system_prompt(_make_caps(), ["code_index"])
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

    def test_read_file_steers_to_code_index_when_active(self):
        """When both tools are active, the read_file Flow paragraph must
        steer supported-language files at code_index mode='list' as the
        entry point — that's how we counteract read_file:stat being the
        cheaper-feeling default and getting code_index out of its
        low-baseline trap."""
        from agent_cli.code_index.languages import get_supported_extensions

        prompt = build_system_prompt(_make_caps(), ["read_file", "code_index"])
        # The Flow line names code_index as the entry point.
        assert "code_index mode='list' first" in prompt
        # Every supported extension must appear in the Flow paragraph
        # itself (the code_index guide already lists them — this checks
        # the read_file→code_index steering also stays in sync).
        flow_start = prompt.index(
            "Flow: for an unknown file, if its extension is supported by"
        )
        flow_end = prompt.index("seen the first 20 lines.", flow_start)
        flow_text = prompt[flow_start:flow_end]
        for ext in get_supported_extensions():
            assert ext in flow_text, f"{ext} missing from read_file Flow steering"

    def test_read_file_omits_steering_when_code_index_inactive(self):
        """If code_index is not in active_tools (e.g., subagent with a
        restricted tool list), the read_file guide must NOT mention it
        — pointing the model at a tool it cannot call wastes a retry on
        UNKNOWN_TOOL."""
        prompt = build_system_prompt(_make_caps(), ["read_file"])
        assert "code_index" not in prompt
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

    def test_scope_labels_are_positional(self, tmp_path, monkeypatch):
        """Scope comes from list position ([project, user]), not from a
        source-path substring match."""
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
        assert "(scope: project)\nProject rule." in result
        assert "(scope: user)\nUser rule." in result

    def test_cwd_is_home_collapses_to_one(self, tmp_path, monkeypatch):
        """N1: when cwd == home the project and user paths resolve to the
        same file — it must be read once and labeled 'project', not read
        twice or mislabeled 'user'."""
        d = tmp_path / ".agent-cli"
        d.mkdir()
        (d / "DIRECTIVE.md").write_text("Shared rule.")
        # Both entries point at the same dir (the cwd == home case).
        monkeypatch.setattr(
            "agent_cli.prompts.system_prompt._DIRECTIVE_PATHS",
            [d, d],
        )
        result = _load_directives()
        assert result.count("Shared rule.") == 1
        assert "(scope: project)" in result
        assert "(scope: user)" not in result


class TestDelegateInlineAgent:
    """AG-27 ~ AG-28: delegate inline guide agent-field tests.

    Step 4 of the wire_format extraction turned the constant
    ``_DELEGATE_INLINE`` into a builder ``_build_delegate_inline(wire_format)``;
    the assertions below check the rendered guide rather than the
    pre-render literal. Behavior is unchanged for the ``"react"``
    plugin.
    """

    def _delegate_guide(self) -> str:
        from agent_cli import wire_formats

        return _build_delegate_inline(wire_formats.get("react"))

    def test_delegate_inline_mentions_agent(self):
        guide = self._delegate_guide()
        assert '"agent"' in guide
        assert ".agent-cli/agents/" in guide

    def test_delegate_inline_agent_example(self):
        guide = self._delegate_guide()
        assert '"agent": "security-reviewer"' in guide


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
        monkeypatch.setattr(
            "agent_cli.prompts.system_prompt._DIRECTIVE_PATHS",
            [directive_dir],
        )

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


class TestSystemSectionsSingleSource:
    """The loop's ``_system_sections`` is the single source of truth and
    ``self.system`` is always derived by joining it — the Prompt Inspector
    shows exactly what the LLM receives (no drift), including after hook
    sections are applied/replaced."""

    def _loop(self):
        from unittest.mock import MagicMock

        from agent_cli.loop import AgentLoop

        loop = AgentLoop(
            query="Q", provider=MagicMock(), capabilities=_make_caps(), model="m"
        )
        loop._setup()
        return loop

    def test_system_equals_joined_sections_after_setup(self):
        loop = self._loop()
        assert loop.system == "\n\n".join(t for _, t in loop._system_sections)
        assert [n for n, _ in loop._system_sections][0] == "Role"

    def test_hook_sections_keep_invariant_and_marker(self):
        loop = self._loop()

        class Ctx:
            system_sections = {"Sprint Goals": "Ship the inspector."}

        loop._apply_system_sections(Ctx())
        assert loop.system == "\n\n".join(t for _, t in loop._system_sections)
        assert "<!-- HOOK_SECTIONS -->" in loop.system
        assert "## Sprint Goals\nShip the inspector." in loop.system
        names = [n for n, _ in loop._system_sections]
        assert "Hook: Sprint Goals" in names

    def test_hook_reapply_replaces_not_accumulates(self):
        loop = self._loop()

        class A:
            system_sections = {"One": "1"}

        class B:
            system_sections = {"Two": "2"}

        loop._apply_system_sections(A())
        loop._apply_system_sections(B())
        names = [n for n, _ in loop._system_sections]
        assert "Hook: Two" in names and "Hook: One" not in names
        assert loop.system.count("<!-- HOOK_SECTIONS -->") == 1

    def test_snapshot_sent_to_renderer_each_call(self):
        from unittest.mock import MagicMock, patch

        from agent_cli.loop import AgentLoop
        from agent_cli.providers.base import LLMResponse

        provider = MagicMock()
        provider.call.side_effect = [
            LLMResponse(
                content='{"thought":"t","action":"complete","action_input":{"result":"ok"}}'
            )
        ]
        loop = AgentLoop(
            query="Q",
            provider=provider,
            capabilities=_make_caps(),
            model="m",
            wire_format=__import__("agent_cli.wire_formats", fromlist=["get"]).get(
                "react"
            ),
        )
        with patch("agent_cli.loop.render_system_prompt_snapshot") as snap:
            loop.run()
        assert snap.called
        sections, turn = snap.call_args.args
        assert sections == loop._system_sections
        assert isinstance(turn, int)
