"""Session-state block — the volatile state appended to the LAST message.

Covers the three things the design rests on:

1. the block RENDERS the right thing (pure function),
2. it is DELIVERED at the tail and never persisted (ContextManager seam),
3. it costs NOTHING in provider KV-prefix terms — the property that made the
   tail the right home for content that used to sit in the system prompt.
"""

from __future__ import annotations

import json

from agent_cli.context.manager import _OBS_COMPLETE_NUDGE, ContextManager
from agent_cli.prompts.session_state import (
    COMPACTION_WARN_RATIO,
    SESSION_STATE_HEADER,
    build_session_state,
)

# ── 1. rendering ─────────────────────────────────────────────────────


class TestBuildSessionState:
    def test_empty_when_there_is_nothing_to_say(self):
        assert build_session_state() == ""

    def test_context_line_with_percentage(self):
        out = build_session_state(used_tokens=48_200, budget_tokens=140_000)
        assert "~48,200 / 140,000 tokens (34%)" in out
        assert out.startswith(SESSION_STATE_HEADER)

    def test_percentage_is_clamped_over_budget(self):
        out = build_session_state(used_tokens=200_000, budget_tokens=140_000)
        assert "(100%)" in out

    def test_turn_without_max_turns(self):
        out = build_session_state(used_tokens=1, budget_tokens=10, turn=7)
        assert "turn 7" in out and "turn 7/" not in out

    def test_turn_with_max_turns(self):
        out = build_session_state(used_tokens=1, budget_tokens=10, turn=7, max_turns=40)
        assert "turn 7/40" in out

    def test_no_budget_still_reports_usage(self):
        out = build_session_state(used_tokens=1_234)
        assert "~1,234 tokens" in out
        assert "%" not in out

    def test_agents_and_memory_keep_their_original_headings(self):
        """The sections moved verbatim out of the system prompt — the model
        must not have to re-learn what it is looking at."""
        out = build_session_state(
            used_tokens=1,
            budget_tokens=10,
            agents="## Live Agents\n- `agt-1`",
            memory="## Session Memory (1)\n✗ #1 [failure] boom",
        )
        assert "## Live Agents" in out
        assert "## Session Memory (1)" in out
        assert "agt-1" in out and "boom" in out

    def test_blocks_alone_are_enough_to_render(self):
        out = build_session_state(agents="## Live Agents\n- `a`")
        assert out.startswith(SESSION_STATE_HEADER)
        assert "## Live Agents" in out

    def test_blank_blocks_are_dropped(self):
        out = build_session_state(used_tokens=1, budget_tokens=10, agents="   ")
        assert "Live Agents" not in out


class TestCompactionWarning:
    def _at(self, ratio):
        budget = 100_000
        return build_session_state(
            used_tokens=int(budget * ratio), budget_tokens=budget
        )

    def test_silent_below_the_threshold(self):
        assert "nearly full" not in self._at(COMPACTION_WARN_RATIO - 0.05)

    def test_fires_at_the_threshold(self):
        assert "nearly full" in self._at(COMPACTION_WARN_RATIO)

    def test_fires_above_the_threshold(self):
        assert "nearly full" in self._at(0.95)

    def test_tells_the_model_to_save_not_to_stop(self):
        """Telling a model it is low on room invites premature ``complete``
        (the failure ``_OBS_COMPLETE_NUDGE`` had to be measured against), so the
        warning must read as an action, not a shortage."""
        out = self._at(0.9)
        assert "memory(mode=add)" in out
        assert "not a reason to finish early" in out

    def test_never_fires_without_a_budget(self):
        assert "nearly full" not in build_session_state(used_tokens=10**9)


# ── 2. delivery ──────────────────────────────────────────────────────


def _ctx(tmp_path, **kw):
    return ContextManager(session_dir=tmp_path / "s", max_context_tokens=100_000, **kw)


def _obs(content="RESULT"):
    return {"role": "user", "tool": "shell", "success": True, "content": content}


class TestDelivery:
    def test_appended_to_the_last_message(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.add({"role": "user", "content": "do the thing"})
        ctx.set_session_state("STATE")
        msgs = ctx.get_messages()
        assert msgs[-1]["content"].endswith("STATE")
        assert msgs[-1]["content"].startswith("do the thing")

    def test_message_count_is_unchanged(self, tmp_path):
        """Delivered by appending, not by adding a trailing message: providers
        forward ``messages`` verbatim, so an extra ``role=user`` turn would
        create consecutive same-role messages that providers treat differently."""
        ctx = _ctx(tmp_path)
        ctx.add({"role": "user", "content": "q"})
        before = len(ctx.get_messages())
        ctx.set_session_state("STATE")
        assert len(ctx.get_messages()) == before

    def test_present_on_a_fresh_user_request_too(self, tmp_path):
        """Not gated on the last message being an observation — a new request
        is exactly when the memory index and roster matter most."""
        ctx = _ctx(tmp_path)
        ctx.add({"role": "user", "content": "brand new request"})
        ctx.set_session_state("STATE")
        assert "STATE" in ctx.get_messages()[-1]["content"]

    def test_comes_after_the_complete_nudge(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.add({"role": "user", "content": "q"})
        ctx.add({"role": "assistant", "thought": "t", "action": "shell"})
        ctx.add(_obs())
        ctx.set_session_state("STATE")
        tail = ctx.get_messages()[-1]["content"]
        assert "`complete`" in tail  # the nudge is still there
        assert tail.index("`complete`") < tail.index("STATE")
        assert tail.endswith("STATE")

    def test_never_persisted_to_history(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.add({"role": "user", "content": "q"})
        ctx.set_session_state("STATE-DO-NOT-PERSIST")
        ctx.get_messages()
        raw = ctx.history_path.read_text()
        assert "STATE-DO-NOT-PERSIST" not in raw
        for line in raw.splitlines():
            assert "STATE-DO-NOT-PERSIST" not in json.dumps(json.loads(line))

    def test_never_mutates_the_stored_record(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.add({"role": "user", "content": "q"})
        ctx.set_session_state("STATE")
        ctx.get_messages()
        assert ctx.get_raw_messages()[-1]["content"] == "q"

    def test_does_not_accumulate_across_calls(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.add({"role": "user", "content": "q"})
        ctx.set_session_state("STATE")
        assert ctx.get_messages()[-1]["content"].count("STATE") == 1
        assert ctx.get_messages()[-1]["content"].count("STATE") == 1

    def test_replaced_not_appended_on_reset(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.add({"role": "user", "content": "q"})
        ctx.set_session_state("OLD")
        ctx.get_messages()
        ctx.set_session_state("NEW")
        tail = ctx.get_messages()[-1]["content"]
        assert "NEW" in tail and "OLD" not in tail

    def test_empty_state_disables_it(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.add({"role": "user", "content": "q"})
        ctx.set_session_state("STATE")
        ctx.set_session_state("")
        assert ctx.get_messages()[-1]["content"] == "q"

    def test_no_messages_is_safe(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.set_session_state("STATE")
        assert ctx.get_messages() == []


# ── 3. the KV-prefix property ────────────────────────────────────────


def _flat(msgs) -> str:
    """The message list as one token-ish stream — what a provider hashes a
    prefix of."""
    return "\n".join(f"{m['role']}:{m.get('content', '')}" for m in msgs)


def _common_prefix(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


class TestKvPrefixCost:
    """The claim the design rests on: a per-turn block at the TAIL invalidates
    nothing that would not have been re-processed anyway.

    Note the block is not the only feed-time annotation at that position —
    ``_OBS_COMPLETE_NUDGE`` is attached to whichever observation is currently
    last, so it too disappears from that message next turn. Both are bounded
    constants; what matters is that no STORED content falls out of the prefix.
    """

    def _turn(self, ctx, state):
        ctx.set_session_state(state)
        return _flat(ctx.get_messages())

    def _annotation_budget(self, state: str) -> int:
        return len(_OBS_COMPLETE_NUDGE) + len("\n\n" + state)

    def test_no_stored_content_falls_out_of_the_prefix(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.add({"role": "user", "content": "the original request"})
        ctx.add({"role": "assistant", "thought": "t1", "action": "shell"})
        ctx.add(_obs("first result"))
        turn_n = self._turn(ctx, "AAAA")

        # next turn: one assistant + one observation appended
        ctx.add({"role": "assistant", "thought": "t2", "action": "shell"})
        ctx.add(_obs("second result"))
        turn_n1 = self._turn(ctx, "BBBB")

        shared = _common_prefix(turn_n, turn_n1)
        reused, lost = turn_n[:shared], turn_n[shared:]
        # the whole earlier conversation is still cached
        assert "the original request" in reused
        assert "first result" in reused
        # and what is NOT reused is only the feed-time annotations
        assert "the original request" not in lost
        assert "first result" not in lost
        assert "AAAA" in lost
        assert len(lost) <= self._annotation_budget("AAAA")

    def test_a_head_placement_would_destroy_the_prefix(self, tmp_path):
        """Contrast — the same block placed right after the user's request (the
        intuitive "put it where attention is" spot) sits near the FRONT in an
        agent loop, so every later turn is invalidated."""
        ctx = _ctx(tmp_path)
        ctx.add({"role": "user", "content": "the original request"})
        ctx.add({"role": "assistant", "thought": "t1", "action": "shell"})
        ctx.add(_obs("first result"))

        def head_placed(msgs, state):
            out = [dict(m) for m in msgs]
            out[0]["content"] = out[0].get("content", "") + "\n\n" + state
            return _flat(out)

        base = ctx.get_messages()
        a = head_placed(base, "AAAA")
        ctx.add({"role": "assistant", "thought": "t2", "action": "shell"})
        ctx.add(_obs("second result"))
        b = head_placed(ctx.get_messages(), "BBBB")

        shared_head = _common_prefix(a, b)
        # divergence lands inside the FIRST message — nothing after the user's
        # request survives, including conversation that never changed
        assert shared_head < len(_flat(base[:1]) + "\n\nAAAA")
        assert "first result" not in a[:shared_head]

    def test_reuse_loss_is_bounded_by_the_annotations_not_the_history(self, tmp_path):
        """The cost must not grow with conversation length — that is the whole
        difference from putting volatile text in the system prompt."""
        ctx = _ctx(tmp_path)
        ctx.add({"role": "user", "content": "q"})
        for i in range(8):
            ctx.add({"role": "assistant", "thought": f"t{i}", "action": "shell"})
            ctx.add(_obs(f"result number {i} " * 20))
        tail_n = self._turn(ctx, "AAAA")
        ctx.add({"role": "assistant", "thought": "t8", "action": "shell"})
        ctx.add(_obs("result number 8"))
        tail_n1 = self._turn(ctx, "BBBB")

        lost = len(tail_n) - _common_prefix(tail_n, tail_n1)
        assert lost <= self._annotation_budget("AAAA")


# ── 4. loop wiring ───────────────────────────────────────────────────


def _caps(window=32768):
    from agent_cli.providers.capabilities import ModelCapabilities

    return ModelCapabilities(
        context_window=window, max_output_tokens=4096, supports_thinking=False
    )


def _caller(tmp_path, **cfg_kw):
    from agent_cli.loop.llm import LLMCaller
    from agent_cli.loop.state import LoopConfig, LoopState

    ctx = _ctx(tmp_path)
    config = LoopConfig(capabilities=_caps(), **cfg_kw)
    state = LoopState()
    return LLMCaller(config, state, ctx, provider=None, prompt=None), ctx


class TestLoopAssembly:
    def test_memory_index_is_pulled_from_the_session_dir(self, tmp_path):
        from agent_cli import memory

        caller, ctx = _caller(tmp_path)
        memory.add(ctx.session_dir, type="failure", summary="보드 부팅 실패")
        assert "보드 부팅 실패" in caller._build_session_state(10_000)

    def test_roster_needs_both_a_registry_and_the_agent_tool(self, tmp_path):
        class _Reg:
            def roster_snapshot(self):
                return [{"key": "agt-1", "state": "idle", "pending_requests": 0}]

            def get(self, key):
                return None

        # registry but no `agent` tool in this loop → nothing to advertise
        caller, _ = _caller(tmp_path, agent_registry=_Reg(), tools_list=["shell"])
        assert "agt-1" not in caller._build_session_state(10_000)
        # both present → advertised, with live state (tail-only capability)
        caller, _ = _caller(tmp_path, agent_registry=_Reg(), tools_list=["agent"])
        out = caller._build_session_state(10_000)
        assert "agt-1" in out and "[idle]" in out

    def test_no_registry_is_safe(self, tmp_path):
        caller, _ = _caller(tmp_path, tools_list=["agent"])
        assert "Live Agents" not in caller._build_session_state(10_000)

    def test_block_size_is_reserved_from_the_compaction_budget(self, tmp_path):
        """The block is in the request but not in ``system``, so the budget
        must account for it or the turn silently overshoots the window."""
        caller, _ = _caller(tmp_path)
        assert caller._state_tokens == 0
        caller._state_tokens = 500
        # the reservation is subtracted alongside the system prompt
        import inspect

        src = inspect.getsource(type(caller)._call_llm)
        assert "- self._state_tokens" in src


class TestKvWinEndToEnd:
    """The payoff: a ``memory add`` mid-run no longer rewrites the system
    prompt, so the provider-side prefix survives — while the note still
    reaches the model on the next turn."""

    def _run(self, tmp_path, ops):
        import json
        from unittest.mock import MagicMock

        from agent_cli.loop import run_loop
        from agent_cli.providers.base import LLMResponse

        calls = iter(ops)
        provider = MagicMock()
        provider.call = MagicMock(
            side_effect=lambda *a, **k: LLMResponse(content=json.dumps(next(calls)))
        )
        ctx = _ctx(tmp_path)
        result = run_loop(
            query="do it",
            provider=provider,
            capabilities=_caps(),
            model="m",
            ctx=ctx,
        )
        assert result.success
        return provider

    def test_memory_add_does_not_touch_the_system_prompt(self, tmp_path):
        provider = self._run(
            tmp_path,
            [
                {
                    "action": "memory",
                    "mode": "add",
                    "type": "failure",
                    "summary": "보드 부팅 실패",
                },
                {"action": "complete", "result": "ok"},
            ],
        )
        sys1 = provider.call.call_args_list[0][1]["system"]
        sys2 = provider.call.call_args_list[1][1]["system"]
        # byte-identical → the whole cached prefix is still valid
        assert sys1 == sys2
        # and the index itself is not a system section (the `memory` tool
        # DESCRIPTION mentions the heading, which is static and fine)
        assert "## Session Memory (" not in sys2

    def test_the_note_still_reaches_the_model_next_turn(self, tmp_path):
        provider = self._run(
            tmp_path,
            [
                {
                    "action": "memory",
                    "mode": "add",
                    "type": "failure",
                    "summary": "보드 부팅 실패",
                },
                {"action": "complete", "result": "ok"},
            ],
        )
        tail = provider.call.call_args_list[1][1]["messages"][-1]["content"]
        assert SESSION_STATE_HEADER in tail
        # inside the state block, not merely somewhere in the observation text
        block = tail.split(SESSION_STATE_HEADER, 1)[1]
        assert "## Session Memory (1)" in block
        assert "보드 부팅 실패" in block

    def test_context_figures_are_present_every_turn(self, tmp_path):
        provider = self._run(
            tmp_path,
            [
                {"action": "shell", "command": "echo hi"},
                {"action": "complete", "result": "ok"},
            ],
        )
        for call in provider.call.call_args_list:
            tail = call[1]["messages"][-1]["content"]
            assert SESSION_STATE_HEADER in tail
            assert "context: ~" in tail


# ── 4. Task Guidelines in the tail (v8.52.0) ─────────────────────────


class TestGuidelinesInTail:
    """TASK_GUIDELINES 전체가 시스템 프롬프트 Primacy 에서 꼬리 블록으로 이동
    — 단일 상수는 system_prompt.py 에 유지, 렌더 위치만 바뀐다."""

    def test_guidelines_render_under_the_header(self):
        out = build_session_state(guidelines="## Task Guidelines\n- rule one")
        assert out.startswith(SESSION_STATE_HEADER)
        assert "## Task Guidelines" in out and "- rule one" in out

    def test_guidelines_alone_are_enough_to_render(self):
        assert build_session_state(guidelines="x") != ""

    def test_compaction_warning_stays_last(self):
        out = build_session_state(
            used_tokens=90, budget_tokens=100, guidelines="## Task Guidelines\n- r"
        )
        assert out.index("## Task Guidelines") < out.index("nearly full")
