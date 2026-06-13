"""Dropped-field recovery: ``thought_required`` / ``action_required`` flags.

Two symmetric wire-format flags govern what happens when an emission is
missing a structured field:

  - ``action_required=False`` → a dropped/empty action is recovered by the
    loop via ``infer_action`` on the *preserved* action_input (wire-key
    prefix → tool). ``True`` → straight to NO_ACTION recovery.
  - ``thought_required=False`` → a missing thought is tolerated. ``True`` →
    NO_THOUGHT recovery.

The parser-side invariant (``WireFormat.parse`` contract) is that
action_input is preserved even when the action slot is empty/invalid, so
both flag branches have something to work with. This file pins:

  1. Both parsers preserve action_input across every dropped-action shape.
  2. prefix_md and react reach the SAME recovery outcome for the same
     semantic input (cross-wire parity — the gap that regressed when
     prefix_md became the default and dropped Input JSON on empty actions).
  3. The loop honors each flag: False → infer/tolerate, True → recover.
     The shipped plugins both set False, so the True branches are pinned
     against a synthetic strict plugin.
  4. The exact real-world shape (session 1780718751: '## Action' header +
     empty body + prefixed Input JSON, 18/188 turns) recovers.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from agent_cli.loop import run_loop
from agent_cli.providers.base import LLMResponse
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.tools.registry import infer_action
from agent_cli.wire_formats import get
from agent_cli.wire_formats.react import ReActFormat


# ── Fixtures / helpers ───────────────────────────────


@pytest.fixture
def caps():
    return ModelCapabilities(
        context_window=32768,
        max_output_tokens=4096,
        supports_structured_output=True,
        supports_thinking=False,
        thinking_budget=0,
        supports_strict_schema=False,
    )


def _make_provider(*responses):
    provider = MagicMock()
    provider.call.side_effect = [LLMResponse(content=r) for r in responses]
    return provider


def _complete(result: str) -> str:
    return json.dumps(
        {"thought": "done", "action": "complete", "action_input": {"result": result}}
    )


def _msgs(call_obj) -> list:
    args, kwargs = call_obj
    return (args[0] if args else kwargs.get("messages")) or []


def _joined(call_obj) -> str:
    return " ".join(m.get("content", "") or "" for m in _msgs(call_obj))


class _StrictReact(ReActFormat):
    """Synthetic plugin pinning the True branches of both flags. The two
    shipped plugins are both False, so without this the recovery paths for
    a *required* field would be untested. parse() is inherited — only the
    loop's flag-gated branches differ."""

    thought_required = True
    action_required = True


# ── 1. Parser preserves action_input across dropped-action shapes ──

# NOTE: prefix_md cases removed with the plugin (wire-format consolidation
# Step 1, 2026-06-13). The md_array side of the cross-wire dropped-action
# parity is rebuilt in Step 2, once react becomes multi-op and its shape
# settles — see project-wire-format-consolidation-roadmap memory.

# react JSON shapes — action dropped under several drift forms.
_REACT_CASES = [
    (
        "empty_action_string",
        '{"thought":"x","action":"","action_input":{"shell_command":"make"}}',
        {"shell_command": "make"},
    ),
    (
        "no_action_key",
        '{"action_input":{"shell_command":"make"}}',
        {"shell_command": "make"},
    ),
    (
        "siblings_only_no_action",
        '{"shell_command":"make"}',
        {"shell_command": "make"},
    ),
    (
        "siblings_with_thought",
        '{"thought":"x","shell_command":"make"}',
        {"shell_command": "make"},
    ),
]


class TestReactPreservation:
    @pytest.mark.parametrize(
        "name,raw,exp_input",
        _REACT_CASES,
        ids=[c[0] for c in _REACT_CASES],
    )
    def test_parse_preserves_input(self, name, raw, exp_input):
        parsed = get("react").parse(raw)
        assert parsed.action_input == exp_input
        # action is falsy in every dropped case → loop will infer
        assert not parsed.action
        assert infer_action(parsed.action_input) == "shell"

    def test_thought_only_is_unrecoverable(self):
        parsed = get("react").parse('{"thought":"just thinking"}')
        assert not parsed.action
        assert parsed.action_input is None


# ── 2. Cross-wire parity ─────────────────────────────


class TestCrossWireParity:
    def test_dropped_action_same_outcome(self):
        # The two shipped multi-op formats reach the SAME dropped-action
        # recovery from the same semantic emission (an op with no `action`,
        # only a tool-prefixed param). react (JSON) and md_array (markdown)
        # differ only in envelope; the op shape + infer_action are identical.
        rt = get("react").parse_turn(
            '{"thought": "x", "actions": [{"shell_command": "ls"}]}'
        )
        mt = get("md_array").parse_turn(
            '## Thought\nx\n\n## Action\n[{"shell_command": "ls"}]'
        )
        assert len(rt.ops) == len(mt.ops) == 1
        assert rt.ops[0].action is None and mt.ops[0].action is None
        assert (
            rt.ops[0].action_input
            == mt.ops[0].action_input
            == {"shell_command": "ls"}
        )
        assert (
            infer_action(rt.ops[0].action_input)
            == infer_action(mt.ops[0].action_input)
            == "shell"
        )

    def test_shipped_plugins_optional_by_default(self):
        for name in ("react", "md_array"):
            plugin = get(name)
            assert plugin.thought_required is False, name
            assert plugin.action_required is False, name


# ── 3. Loop honors the flags ─────────────────────────


# A dropped/missing field is recovered or not — measured by whether the
# tool actually ran (its file side-effect), NOT by scanning message text:
# NO_ACTION / NO_THOUGHT interventions echo the raw emission back, so the
# inferred tool's args would appear in the transcript even when it never
# executed. The file side-effect is unambiguous.


class TestActionRequiredGate:
    def test_false_infers_and_dispatches(self, caps, tmp_path):
        # action dropped but inferable; action_required=False (react) → the
        # loop infers write_file and runs it (the file gets created).
        target = tmp_path / "made.txt"
        provider = _make_provider(
            json.dumps(
                {
                    "thought": "x",
                    "action_input": {
                        "write_file_path": str(target),
                        "write_file_content": "data",
                    },
                }
            ),
            _complete("done"),
        )
        result = run_loop(
            query="go",
            provider=provider,
            capabilities=caps,
            model="m",
            wire_format=ReActFormat(),
        )
        assert result.success
        assert target.exists()  # inferred write_file ran

    def test_true_skips_infer_and_recovers(self, caps, tmp_path):
        # Same input, action_required=True → inference skipped, NO_ACTION
        # recovery fires (write_file never runs), then the model completes.
        target = tmp_path / "made.txt"
        provider = _make_provider(
            json.dumps(
                {
                    "thought": "x",
                    "action_input": {
                        "write_file_path": str(target),
                        "write_file_content": "data",
                    },
                }
            ),
            _complete("done"),
        )
        result = run_loop(
            query="go",
            provider=provider,
            capabilities=caps,
            model="m",
            wire_format=_StrictReact(),
        )
        assert result.success
        assert provider.call.call_count == 2  # NO_ACTION retry happened
        assert not target.exists()  # infer skipped → file NOT created


class TestThoughtRequiredGate:
    def test_false_tolerates_missing_thought(self, caps, tmp_path):
        # action present, no thought; thought_required=False (react) →
        # dispatched without a NO_THOUGHT retry.
        target = tmp_path / "made.txt"
        provider = _make_provider(
            json.dumps(
                {
                    "action": "write_file",
                    "action_input": {
                        "write_file_path": str(target),
                        "write_file_content": "data",
                    },
                }
            ),
            _complete("done"),
        )
        result = run_loop(
            query="go",
            provider=provider,
            capabilities=caps,
            model="m",
            wire_format=ReActFormat(),
        )
        assert result.success
        assert target.exists()  # ran despite missing thought

    def test_true_fires_no_thought_recovery(self, caps, tmp_path):
        # Same input, thought_required=True → NO_THOUGHT recovery fires
        # before the action runs (write deferred to after the retry).
        target = tmp_path / "made.txt"
        provider = _make_provider(
            json.dumps(
                {
                    "action": "write_file",
                    "action_input": {
                        "write_file_path": str(target),
                        "write_file_content": "data",
                    },
                }
            ),
            _complete("done"),
        )
        result = run_loop(
            query="go",
            provider=provider,
            capabilities=caps,
            model="m",
            wire_format=_StrictReact(),
        )
        assert result.success
        assert provider.call.call_count == 2  # NO_THOUGHT retry happened
        assert not target.exists()  # recovery before write → file NOT created


# ── 4. Real-world failure shape (session 1780718751) ──
# NOTE: this guarded prefix_md's specific '## Action'+empty+'## Input' bug
# (18/188 turns). Removed with prefix_md (Step 1); the md_array-equivalent
# real-shape guard is added in Step 2. See the consolidation-roadmap memory.


# ── 5. Prompt flag hook (output unchanged, gate wired) ──


class TestPromptFlagHook:
    """``_gated_rule`` lets the flags weaken/drop a Format-Rules clause
    later. Today no plugin supplies a ``soft`` variant, so the prompt is
    unchanged — the hook is wired but inert."""

    def test_gated_rule_selects_by_flag(self):
        from agent_cli.wire_formats.base import WireFormat

        assert WireFormat._gated_rule(True, "S", "soft") == "S"
        assert WireFormat._gated_rule(False, "S", "soft") == "soft"
        # Inert without a soft variant — strong wording regardless of flag.
        assert WireFormat._gated_rule(False, "S") == "S"
        assert WireFormat._gated_rule(True, "S") == "S"

    def test_prompts_keep_strong_wording(self):
        # Flags are False but no soft variant is wired, so the strong
        # obligation still shows in react's Format Rules (unchanged).
        fr = get("react").format_rules()
        assert "Do not leave it empty" in fr

    def test_field_specific_composes_numbered_rules(self):
        for name in ("react", "md_array"):
            fs = get(name).format_rules_field_specific()
            assert fs.startswith("1. "), name
            assert "\n2. " in fs, name

    def test_softening_takes_effect_via_synthetic_plugin(self):
        # Prove the gate actually drives the section: a plugin that both
        # sets thought_required=False AND supplies a soft variant drops the
        # strong thought wording. (Shipped plugins don't do this yet.)
        class _SoftThought(ReActFormat):
            def format_rules_field_specific(self) -> str:
                return f"1. {self._gated_rule(self.thought_required, 'STRONG', 'thought optional')}"

        out = _SoftThought().format_rules_field_specific()
        assert "thought optional" in out
        assert "STRONG" not in out
