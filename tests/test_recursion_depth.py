"""Recursion + depth-ceiling contract for skill / delegate nesting.

Background. The loop has four defensive layers preventing runaway
nested calls:

  1. ``skill_stack``  — cycle (``A → B → A``) detection for
     ``run_skill`` invocations
  2. ``agent_stack``  — same shape for named ``delegate`` invocations
  3. ``depth`` / ``max_depth`` — combined ceiling that counts BOTH
     skill and delegate hops, applied symmetrically:
       - ``AgentLoop.__init__`` strips ``run_skill`` and ``delegate``
         from ``tools_list`` once ``depth >= max_depth`` (proactive)
       - ``_handle_run_skill`` / ``_run_single`` re-check at dispatch
         (belt-and-suspenders for direct callers)
  4. System prompt's ``## Execution Context`` surfaces both the
     current stack AND ``depth N/M`` so the model has the
     constraint as part of its working context

This test module covers all four. The user-asked feature: when
either kind of recursion is blocked, the LLM-facing observation
must include actionable recovery options (the recovery-from-failure
contract used by other tool-error paths).
"""

from __future__ import annotations

import re

import pytest

from agent_cli.loop import AgentLoop
from agent_cli.prompts.system_prompt import _build_execution_context
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.recovery.recursion import (
    format_depth_limit_error,
    format_recursion_error,
)


# ─── Error-message vocabulary (A) ─────────────────────────────


class TestRecursionErrorMessage:
    """The cycle-detection branch needs to give the LLM more than
    "blocked" — without an explicit recovery menu the model often
    retries the same call. Three options are encoded into the
    message so the model can pick one without further guidance.
    """

    def test_skill_message_names_the_target_and_stack(self):
        msg = format_recursion_error("skill", "summarize", ["plan", "summarize"])
        assert "skill" in msg
        assert "summarize" in msg
        # Stack is shown as a left-to-right chain so the model can
        # see the cycle at a glance.
        assert "plan → summarize" in msg

    def test_agent_message_uses_agent_kind(self):
        msg = format_recursion_error("agent", "reviewer", ["reviewer"])
        assert "agent" in msg
        assert "reviewer" in msg

    def test_message_offers_three_recovery_options(self):
        msg = format_recursion_error("skill", "x", ["x"])
        # The three recovery vocabulary anchors the LLM-facing prompt
        # teaches the model to look for. If a future refactor drops
        # one of these the model loses a recovery path.
        assert "different approach" in msg
        assert "complete" in msg
        assert "ask" in msg

    def test_empty_stack_renders_gracefully(self):
        # Defensive — caller shouldn't pass empty stack (the cycle
        # check requires a name to be IN the stack) but the helper
        # mustn't crash if it does.
        msg = format_recursion_error("skill", "x", [])
        assert "skill" in msg


class TestDepthLimitErrorMessage:
    """Depth-ceiling errors are structural: the model can't retry
    around them at the same level. Recovery is "finish or
    restructure", and the message has to point at the ``--max-depth``
    knob so the user understands what to change."""

    def test_reports_attempted_depth(self):
        # current_depth=2 means the would-be child runs at depth 3.
        # The model needs to see the *attempted* depth in the message
        # so it can reason about whether 'one less level' would
        # actually clear the limit.
        msg = format_depth_limit_error("skill", "summarize", 2, 2)
        # ``2 + 1 = 3`` appears.
        assert "3" in msg
        assert "2" in msg  # limit
        assert "summarize" in msg

    def test_mentions_max_depth_flag_for_user_escalation(self):
        msg = format_depth_limit_error("skill", "x", 2, 2)
        assert "--max-depth" in msg

    def test_makes_the_combined_counter_visible(self):
        # The semantic change here ("skill and delegate share the
        # counter") needs to be discoverable from the message itself;
        # otherwise a user who only thinks of delegate hops will be
        # surprised when a skill chain triggers the limit.
        msg = format_depth_limit_error("agent", "reviewer", 2, 2)
        assert "share" in msg.lower() or "skill and delegate" in msg.lower()


# ─── AgentLoop tools_list at depth ceiling (B) ────────────────


def _caps():
    return ModelCapabilities(
        context_window=32768,
        max_output_tokens=4096,
        supports_structured_output=True,
        supports_thinking=False,
        thinking_budget=0,
        supports_strict_schema=False,
    )


class TestToolsListDepthCeiling:
    """The proactive layer: when an ``AgentLoop`` is constructed at
    the depth ceiling, the model never sees ``delegate`` or
    ``run_skill`` in its tool list. This both prevents wasted turns
    on a guaranteed-fail call AND keeps the system prompt's tool
    section honest.

    Previously only ``delegate`` was stripped — ``run_skill`` was
    still advertised even at the ceiling, so a model that tried to
    skill-chain past the limit would only hit the dispatch-time
    refusal. Now they're stripped symmetrically.
    """

    def _make_loop(self, depth: int, max_depth: int) -> AgentLoop:
        # Provider is unused — AgentLoop.__init__ doesn't call it.
        # We only care about the constructor's tools_list derivation.
        return AgentLoop(
            query="x",
            provider=object(),
            capabilities=_caps(),
            model="m",
            depth=depth,
            max_depth=max_depth,
        )

    def test_below_ceiling_keeps_both(self):
        loop = self._make_loop(depth=0, max_depth=2)
        assert "delegate" in loop.tools_list
        assert "run_skill" in loop.tools_list

    def test_at_ceiling_strips_both_symmetrically(self):
        loop = self._make_loop(depth=2, max_depth=2)
        # Previously only ``delegate`` was stripped here; now the
        # invariant is "both, or neither". A regression would let a
        # model emit ``run_skill`` at the ceiling and bounce off the
        # dispatch-time refusal instead.
        assert "delegate" not in loop.tools_list
        assert "run_skill" not in loop.tools_list

    def test_above_ceiling_still_strips_both(self):
        # Defensive — somehow ``depth`` ran past ``max_depth``.
        # The strip is ``>=`` not ``==``, so anything beyond also
        # has them removed.
        loop = self._make_loop(depth=5, max_depth=2)
        assert "delegate" not in loop.tools_list
        assert "run_skill" not in loop.tools_list

    def test_one_below_ceiling_keeps_both(self):
        # The interesting boundary case: depth=1, max_depth=2 means
        # this loop CAN still descend once more. Both tools must
        # remain.
        loop = self._make_loop(depth=1, max_depth=2)
        assert "delegate" in loop.tools_list
        assert "run_skill" in loop.tools_list


# ─── Dispatch-time depth check in _handle_run_skill (B) ───────


class TestRunSkillDispatchDepthCheck:
    """Even though ``AgentLoop.__init__`` strips ``run_skill`` from
    the tool list at the ceiling, the dispatch helper checks depth
    too — direct callers (tests, future integrations, a custom
    ``active_tools`` that re-adds ``run_skill``) might bypass the
    init strip. This layer ensures the same actionable message
    surfaces regardless of how dispatch is reached.
    """

    def test_depth_at_limit_returns_depth_limit_error(self, tmp_path):
        from agent_cli.loop import _handle_run_skill

        result = _handle_run_skill(
            {"name": "summarize"},
            provider_name="openai",
            base_url="",
            api_key="",
            capabilities=_caps(),
            model="m",
            ctx=None,
            session=None,
            skill_stack=[],
            parent_depth=2,
            max_depth=2,
        )
        assert result.success is False
        # Depth-limit message vocabulary.
        assert "depth" in result.error.lower()
        assert "--max-depth" in result.error
        # Recursion-message vocabulary MUST NOT appear — wrong
        # diagnosis at this level confuses the model.
        assert "Recursive" not in result.error

    def test_depth_below_limit_proceeds_to_skill_check(self, tmp_path):
        from agent_cli.loop import _handle_run_skill

        # Within limit but the skill doesn't exist → "Skill 'x' not
        # found", proving we got past the depth check.
        result = _handle_run_skill(
            {"name": "does_not_exist"},
            provider_name="openai",
            base_url="",
            api_key="",
            capabilities=_caps(),
            model="m",
            ctx=None,
            session=None,
            skill_stack=[],
            parent_depth=0,
            max_depth=2,
        )
        assert result.success is False
        # Not a depth message — the call got past the depth check.
        assert "depth" not in result.error.lower() or "not found" in result.error
        assert "not found" in result.error

    def test_cycle_blocked_with_actionable_options(self):
        from agent_cli.loop import _handle_run_skill

        result = _handle_run_skill(
            {"name": "summarize"},
            provider_name="openai",
            base_url="",
            api_key="",
            capabilities=_caps(),
            model="m",
            ctx=None,
            session=None,
            skill_stack=["plan", "summarize"],
            parent_depth=2,
            max_depth=4,
        )
        assert result.success is False
        # Cycle, not depth — different recovery path.
        assert "Recursive" in result.error
        assert "different approach" in result.error
        assert "complete" in result.error
        assert "ask" in result.error


# ─── _run_single (delegate) depth check ───────────────────────


class TestDelegateDispatchDepthCheck:
    """Same belt-and-suspenders for delegate. Even named delegates
    that aren't on the stack must still be refused once the
    combined depth ceiling is hit."""

    def _make_provider(self):
        # _run_single's pre-flight checks happen BEFORE any provider
        # call, so a bare object suffices.
        from unittest.mock import MagicMock

        return MagicMock()

    def test_depth_at_limit_returns_depth_limit_error(self):
        from agent_cli.tools.delegate import _run_single

        result = _run_single(
            task="do x",
            agent_name="reviewer",
            agent_stack=["main_agent"],  # reviewer not in stack
            depth=2,
            max_depth=2,
            provider=self._make_provider(),
            capabilities=_caps(),
            model="m",
        )
        assert result.success is False
        assert "depth" in result.error.lower()
        assert "--max-depth" in result.error

    def test_anonymous_delegate_at_limit_also_blocked(self):
        from agent_cli.tools.delegate import _run_single

        # Anonymous delegates aren't on ``agent_stack`` so the cycle
        # check can't help. The depth check is the only thing
        # standing between an anonymous delegate and a runaway nest.
        result = _run_single(
            task="do x",
            agent_name="",  # anonymous
            agent_stack=[],
            depth=2,
            max_depth=2,
            provider=self._make_provider(),
            capabilities=_caps(),
            model="m",
        )
        assert result.success is False
        assert "depth" in result.error.lower()

    def test_cycle_check_still_fires_independently(self):
        from agent_cli.tools.delegate import _run_single

        # Within depth limit but the named agent is already on the
        # stack — cycle, not depth.
        result = _run_single(
            task="do x",
            agent_name="reviewer",
            agent_stack=["main", "reviewer"],
            depth=1,
            max_depth=4,
            provider=self._make_provider(),
            capabilities=_caps(),
            model="m",
        )
        assert result.success is False
        assert "Recursive" in result.error
        assert "reviewer" in result.error


# ─── System prompt depth annotation (C) ───────────────────────


class TestExecutionContextDepth:
    """``_build_execution_context`` surfaces the depth gauge so the
    model can see how much room remains for nesting. Three flavours
    to pin:

      - No stack and depth=0 → section omitted entirely (KV cache).
      - Depth visible when ``max_depth`` is set AND depth>0,
        regardless of whether there's a stack.
      - ``Depth limit reached`` line appears when ``depth >=
        max_depth`` so the model knows *why* ``run_skill`` /
        ``delegate`` vanished from the tool list.
    """

    def test_no_stack_no_depth_section_is_empty(self):
        assert _build_execution_context(None, None, depth=0, max_depth=0) == ""
        assert _build_execution_context([], [], depth=0, max_depth=0) == ""

    def test_depth_shown_with_stack(self):
        out = _build_execution_context(["summarize"], None, depth=1, max_depth=2)
        assert "depth 1/2" in out
        assert "summarize" in out

    def test_depth_shown_without_stack(self):
        # An anonymous delegate at depth 1 has no stack entry but
        # the depth gauge is still useful. Section must render.
        out = _build_execution_context(None, None, depth=1, max_depth=2)
        assert out  # non-empty
        assert "depth 1/2" in out

    def test_limit_reached_explicitly_announced(self):
        out = _build_execution_context(
            ["plan", "summarize"], None, depth=2, max_depth=2
        )
        # The model sees this section IS at the limit — so it knows
        # to wrap up instead of attempting another nest.
        assert re.search(r"depth limit reached", out, re.IGNORECASE)
        assert "run_skill" in out
        assert "delegate" in out

    def test_max_depth_zero_keeps_old_behaviour(self):
        # max_depth=0 is the "I don't know the limit" sentinel used
        # by test helpers and ad-hoc callers that don't wire the
        # loop's depth state in. The depth line must NOT render,
        # to keep the section identical to its pre-feature output
        # and avoid baseline-test churn.
        out = _build_execution_context(["x"], None, depth=0, max_depth=0)
        assert "depth" not in out.lower()
        assert "x" in out  # stack still shown

    def test_section_remains_last_in_full_prompt(self):
        # The whole reason ``## Execution Context`` lives where it
        # does is to be the cache-friendly tail. Adding the depth
        # gauge must not change its position. Build a full prompt
        # and confirm the section appears last.
        from agent_cli.prompts.system_prompt import build_system_prompt

        prompt = build_system_prompt(
            capabilities=_caps(),
            active_tools=["read_file"],
            skill_stack=["foo"],
            depth=1,
            max_depth=2,
        )
        # Position the Execution Context after every other ``##``
        # section, so prefix-stable KV cache holds.
        idx_exec = prompt.find("## Execution Context")
        assert idx_exec != -1
        # No further ``##`` after it.
        remainder = prompt[idx_exec + len("## Execution Context") :]
        assert "## " not in remainder


# ─── Depth threading through execute_skill (B) ────────────────


class TestSkillBumpsDepth:
    """The whole point of unifying depth: a skill hop counts the
    same as a delegate hop. ``execute_skill`` was previously calling
    ``run_loop(depth=0)`` unconditionally, so skill chains slid
    past ``max_depth``. Now it passes ``depth=parent_depth + 1``.

    Whitebox test — check that the executor passes the right depth
    forward by patching ``run_loop`` and inspecting the call kwargs.
    """

    def test_skill_passes_parent_depth_plus_one_to_run_loop(self, monkeypatch):
        from agent_cli.skills.executor import execute_skill
        from agent_cli.skills.loader import Skill
        from agent_cli.tools.result import ToolResult

        captured = {}

        def fake_run_loop(**kwargs):
            captured.update(kwargs)
            return ToolResult(True, output="ok")

        monkeypatch.setattr("agent_cli.skills.executor.run_loop", fake_run_loop)

        skill = Skill(
            name="example",
            description="",
            allowed_tools=None,
            argument_hint="",
            disable_model_invocation=False,
            prompt_template="hello",
            source_path="",
            model="",
            max_turns=0,
            hooks={},
        )
        execute_skill(
            skill=skill,
            arguments="",
            provider=object(),
            capabilities=_caps(),
            model="m",
            parent_depth=3,
            max_depth=5,
        )
        # depth=3+1=4 (one hop above the parent), max_depth passed through.
        assert captured["depth"] == 4
        assert captured["max_depth"] == 5

    def test_skill_with_default_parent_depth_starts_at_one(self, monkeypatch):
        # Default ``parent_depth=0`` is the "called directly, no
        # parent loop" case. Sub-loop should start at depth 1 so the
        # subsequent strip-at-ceiling logic accounts for the skill
        # hop. Without this, a top-level skill behaved as depth 0
        # — equivalent to a no-op for the depth counter.
        from agent_cli.skills.executor import execute_skill
        from agent_cli.skills.loader import Skill
        from agent_cli.tools.result import ToolResult

        captured = {}

        def fake_run_loop(**kwargs):
            captured.update(kwargs)
            return ToolResult(True, output="ok")

        monkeypatch.setattr("agent_cli.skills.executor.run_loop", fake_run_loop)

        skill = Skill(
            name="example",
            description="",
            allowed_tools=None,
            argument_hint="",
            disable_model_invocation=False,
            prompt_template="hello",
            source_path="",
            model="",
            max_turns=0,
            hooks={},
        )
        execute_skill(
            skill=skill,
            arguments="",
            provider=object(),
            capabilities=_caps(),
            model="m",
            # parent_depth omitted → 0 default
        )
        assert captured["depth"] == 1


# ─── End-to-end regression: existing recursion paths still work ───


class TestExistingRecursionPathsUnchanged:
    """The two cycle-detection paths still work as they did, with
    the only delta being the richer message. Pin both so the
    refactor doesn't accidentally drop the block."""

    def test_skill_cycle_still_blocked(self):
        from agent_cli.loop import _handle_run_skill

        result = _handle_run_skill(
            {"name": "summarize"},
            provider_name="openai",
            base_url="",
            api_key="",
            capabilities=_caps(),
            model="m",
            ctx=None,
            session=None,
            skill_stack=["plan", "summarize"],
        )
        assert result.success is False
        assert "summarize" in result.error
        assert "plan" in result.error

    def test_named_agent_cycle_still_blocked(self):
        from unittest.mock import MagicMock

        from agent_cli.tools.delegate import _run_single

        result = _run_single(
            task="x",
            agent_name="reviewer",
            agent_stack=["main", "reviewer"],
            depth=0,
            max_depth=4,
            provider=MagicMock(),
            capabilities=_caps(),
            model="m",
        )
        assert result.success is False
        assert "reviewer" in result.error

    @pytest.mark.parametrize("kind", ["skill", "agent"])
    def test_message_format_consistent_across_both_kinds(self, kind):
        # The two kinds share the same helper, so their wording
        # should differ only in the ``skill`` / ``agent`` noun.
        msg_skill = format_recursion_error("skill", "x", ["x"])
        msg_agent = format_recursion_error("agent", "x", ["x"])
        # Replace the noun and the rest should match.
        normalized_skill = msg_skill.replace("skill", "KIND")
        normalized_agent = msg_agent.replace("agent", "KIND")
        assert normalized_skill == normalized_agent
