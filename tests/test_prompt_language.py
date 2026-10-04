"""v10.11.1 — everything the harness sends to the MODEL is English, and no
prompt still speaks of a ``thought`` field.

Found by the prompt inventory (2026-10-03): four Korean sentences reached the
model — two ``parse_duration`` errors (monitor ``deadline``/``every``), the
spawn refusal for a profile whose model is not on the server, and an example
task in the built-in create-agent skill. They were human-facing strings
reused on a model-facing path. Human-only surfaces (confirm dialogs, console
notices, the web UI) stay Korean and are not covered here.
"""

from __future__ import annotations

import glob
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import agent_cli
from agent_cli import dialects
from agent_cli.constants import parse_duration
from agent_cli.prompts import system_prompt as sp
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.tools import TOOLS

HANGUL = re.compile(r"[\uac00-\ud7a3]")
PKG = Path(agent_cli.__file__).parent


def _system_prompt(name: str, **kw) -> str:
    caps = ModelCapabilities(
        context_window=32768, max_output_tokens=4096, supports_thinking=False
    )
    return "\n\n".join(
        text
        for _n, text in sp.build_system_prompt_sections(
            caps, sorted(TOOLS), dialect=dialects.get(name), **kw
        )
    )


class TestNoKoreanReachesTheModel:
    @pytest.mark.parametrize("name", dialects.list_names())
    def test_system_prompt_is_english(self, name):
        for kw in ({}, {"nonblocking_ask": True}, {"depth": 1, "max_depth": 1}):
            hits = [
                ln
                for ln in _system_prompt(name, **kw).splitlines()
                if HANGUL.search(ln)
            ]
            assert not hits, hits[:3]

    def test_builtin_profiles_and_skills_are_english(self):
        files = glob.glob(str(PKG / "agents/builtin/*.md")) + glob.glob(
            str(PKG / "skills/builtin/**/*.md"), recursive=True
        )
        assert len(files) >= 10
        for f in files:
            hits = [ln for ln in Path(f).read_text().splitlines() if HANGUL.search(ln)]
            assert not hits, (f, hits[:3])

    @pytest.mark.parametrize("raw", ["abc", "10x", "", "1.5h"])
    def test_duration_errors_are_english(self, raw):
        with pytest.raises(ValueError) as e:
            parse_duration(raw)
        assert not HANGUL.search(str(e.value))
        assert "seconds (600), minutes (10m) or hours (2h)" in str(e.value)

    def test_negative_duration_error_is_english(self):
        with pytest.raises(ValueError) as e:
            parse_duration("-5")
        assert str(e.value) == "a duration cannot be negative"

    def test_model_unavailable_refusal_is_english(self):
        from agent_cli.model_check import ModelNotFound
        from agent_cli.subagent.agents_live import AgentRegistry

        listing = MagicMock()
        listing.suggest.return_value = "qwen-x"
        listing.models = ["qwen-x", "qwen-y"]
        err = ModelNotFound.__new__(ModelNotFound)
        err.listing = listing
        reg = AgentRegistry.__new__(AgentRegistry)
        reg.runtime = {"base_url": "http://x/v1"}
        with patch("agent_cli.model_check.verify_model", side_effect=err):
            msg = reg._model_unavailable("qwen-z", "code-writer")
        assert not HANGUL.search(msg)
        assert "the model 'qwen-z' required by profile 'code-writer'" in msg
        assert "(closest name: qwen-x)" in msg and "Available: qwen-x, qwen-y" in msg
        assert "Spawn with a different profile" in msg
        with patch("agent_cli.model_check.verify_model", side_effect=err):
            assert "the role settings" in reg._model_unavailable("qwen-z", "")


class TestNoThoughtFieldInPrompts:
    """The response format is "brief reasoning as plain prose" — there is no
    ``thought`` field or ``## Thought`` header for the model to fill, and the
    stream detector treats a repeated ``## Thought`` as a runaway."""

    @pytest.mark.parametrize("name", dialects.list_names())
    def test_system_prompt_never_says_thought(self, name):
        for kw in ({}, {"nonblocking_ask": True}):
            text = _system_prompt(name, **kw)
            hits = [
                ln
                for ln in text.splitlines()
                if re.search(r"\bthoughts?\b|thought-only", ln, re.IGNORECASE)
            ]
            assert not hits, hits[:3]

    def test_context_discipline_uses_the_format_rules_word(self):
        assert "Keep your reasoning brief" in sp.CONTEXT_DISCIPLINE
        assert (
            "Every line of\nreasoning, tool call, and observation"
            in sp.CONTEXT_DISCIPLINE
        )

    def test_tail_guidelines_never_say_thought(self):
        assert not re.search(r"\bthoughts?\b", sp.TASK_GUIDELINES, re.IGNORECASE)

    def test_no_complete_variant_is_gone(self):
        assert not hasattr(sp, "_ASK_INLINE_NO_COMPLETE")
        for name in dialects.list_names():
            assert not hasattr(dialects.get(name), "exposes_complete")
            assert "- complete:" in _system_prompt(name) or name == "native_fc"


# ── v10.11.2 — wording that pointed at things that do not exist ─────────────


class TestRunSkillNamesNoPhantomSkills:
    """The ``run_skill`` description used to offer 'optimize', 'review-code',
    'summarize' and 'test' as example names. None is a built-in skill, so a
    model following the example called a skill that is not there."""

    def test_description_points_at_the_listing(self):
        tool = TOOLS["run_skill"]
        text = tool.description + tool.parameters["properties"]["name"]["description"]
        assert "Available Skills" in text
        for phantom in ("optimize", "review-code", "summarize", "test generation"):
            assert phantom not in text, phantom


class TestBuiltinSkillsUseTheFlatCallShape:
    """create-agent.md showed ``{"action": "agent", "action_input": {...}}`` —
    the nested shape no dialect has emitted since v10. Arguments sit flat on
    the op."""

    @pytest.mark.parametrize(
        "path",
        sorted(
            glob.glob(str(PKG / "skills" / "builtin" / "**" / "*.md"), recursive=True)
        ),
    )
    def test_no_nested_action_input(self, path):
        assert "action_input" not in Path(path).read_text(encoding="utf-8")


class TestNoStaleTermsReachTheModel:
    """``action_input`` (the pre-v10 nested argument key) and ``delegate`` (the
    tool renamed ``agent`` in v5) lingered in error messages and in the
    Execution Context. A model cannot act on a word that names nothing it can
    emit or call."""

    def test_execution_context_names_the_agent_tool(self):
        out = sp._build_execution_context(["plan"], ["reviewer"], depth=2, max_depth=2)
        assert "delegate" not in out
        assert "'run_skill' or 'agent' calls" in out

    def test_schema_errors_say_arguments(self):
        from agent_cli.tools.registry import validate_tool_input

        ok, err, _ = validate_tool_input("read_file", 42)
        assert not ok
        assert "arguments for 'read_file'" in err
        assert "action_input" not in err

    @pytest.mark.parametrize("rel", ["loop/dispatch.py", "loop/tool_bridge.py"])
    def test_recovery_messages_say_arguments(self, rel):
        src = (PKG / rel).read_text(encoding="utf-8")
        assert "Fix action_input" not in src
        assert "action_input shape and" not in src


class TestSkillsSectionNeedsTheTool:
    """``## Available Skills`` tells the model to "use the run_skill tool". It
    was emitted even to a loop without ``run_skill`` — a narrowed profile, or
    a loop at the depth limit where the tool is removed (v10.13.0)."""

    def _names(self, tools):
        caps = ModelCapabilities(
            context_window=32768, max_output_tokens=4096, supports_thinking=False
        )
        return [
            n
            for n, _ in sp.build_system_prompt_sections(
                caps, tools, dialect=dialects.get("json_fc")
            )
        ]

    def test_present_with_run_skill(self):
        assert "Skills" in self._names(["shell", "run_skill"])

    def test_absent_without_run_skill(self):
        assert "Skills" not in self._names(["shell", "read_file"])
