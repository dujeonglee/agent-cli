"""Oversized-observation policy — ONE policy for every tool.

A tool observation over the cap (``context_window / 10``) never reaches the
model. It is replaced at the result→observation seam
(``ToolBridge._tool_observation``) by a nudge that is identical in SHAPE for
every tool: the full body goes to a file, a bounded head+tail excerpt is shown,
and the same recovery routes are offered (regex match / line range / agent
fan-out), plus one tool-specific line on avoiding the bulk at the source.

Per-tool surfaces: ``render_observation`` (how a result is formatted),
``apply_oversized_cap`` (whether the cap applies), ``oversized_retry_hint``
(the root-cause line) and ``oversized_source_path`` (a file the tool already
wrote). ``render_oversized`` itself is NOT overridden any more.

The cap is floored at ``MIN_CONTEXT_WINDOW / 10`` so it can never be 0, and a
per-TURN budget (``TURN_OBS_BUDGET_MULT`` × cap) bounds a multi-op turn whose
ops are each individually under the cap. ``ctx.add`` stays pure storage.
"""

import json
from pathlib import Path

from agent_cli.context.token_estimator import estimate_tokens
from agent_cli.loop import AgentLoop, LoopConfig, LoopState, ToolBridge
from agent_cli.loop.tool_bridge import TURN_OBS_BUDGET_MULT
from agent_cli.tools import TOOLS, RunContext
from agent_cli.tools.base import (
    GENERIC_RETRY_HINT,
    OVERSIZED_DIRNAME,
    Tool,
    display_path,
    oversized_excerpt,
    persist_oversized,
)
from agent_cli.tools.result import ToolResult


def _loop(cap: int, tools: list[str] | None = None) -> AgentLoop:
    """A bare AgentLoop carrying the cap + tool list — enough for
    _tool_observation (which now consults ``tools_list`` for per-tool
    over-cap guidance, e.g. delegate fan-out only when delegate is callable).
    C1 PR-1: ``tools_list`` 는 LoopConfig 소유(재할당 불가 property)라 바로
    config/state 를 조립해 단다."""
    loop = AgentLoop.__new__(AgentLoop)
    loop._config = LoopConfig(
        tools_list=list(TOOLS.keys()) if tools is None else list(tools)
    )
    loop._state = LoopState()
    loop.ctx = None  # seam reads self.ctx.session_dir if self.ctx else None
    # C1 PR-2: 도구 seam 은 ToolBridge 소유 — cap 은 브리지에 직접 단다.
    loop._tools = ToolBridge(loop._config, loop._state, ctx=None, provider=None)
    loop._oversized_cap = cap  # property → bridge 로 관통
    return loop


# ── Tool surfaces (defaults reproduce historical behaviour) ──────────


class TestToolSurfaces:
    def test_apply_oversized_cap_defaults_true(self):
        assert Tool.apply_oversized_cap is True
        # every builtin inherits the default
        assert all(t.apply_oversized_cap for t in TOOLS.values())

    def test_render_observation_default_is_output_on_success(self):
        tool = TOOLS["read_file"]
        r = ToolResult(True, output="THE OUTPUT")
        assert tool.render_observation(r, {}) == "THE OUTPUT"

    def test_render_observation_default_is_error_on_failure(self):
        tool = TOOLS["read_file"]
        r = ToolResult(False, error="boom")
        assert tool.render_observation(r, {}) == "boom"

    def test_no_tool_overrides_render_oversized(self):
        """The whole point of the unification: over-cap behaviour is decided in
        ONE place. A tool customises via oversized_retry_hint /
        oversized_source_path, never by reimplementing the policy."""
        for name, tool in TOOLS.items():
            assert type(tool).render_oversized is Tool.render_oversized, name

    def test_every_tool_has_a_retry_hint(self):
        for name, tool in TOOLS.items():
            assert tool.oversized_retry_hint.strip(), name

    def test_unknown_tool_falls_back_to_generic_hint(self):
        # An MCP tool (or any future dynamically registered one) declares no
        # hint and inherits the one piece of advice true for everything.
        from agent_cli.mcp.adapter import McpTool

        assert McpTool.oversized_retry_hint == GENERIC_RETRY_HINT


# ── the unified nudge ────────────────────────────────────────────────


def _nudge(tool_name, body, *, cap=1000, tools=("agent",), tmp_path=None, args=None):
    """Render one tool's over-cap observation through the real seam."""
    tool = TOOLS[tool_name]
    ctx = RunContext(
        session_dir=tmp_path,
        oversized_cap=cap,
        tools_available=frozenset(tools),
    )
    return tool.render_oversized(
        ToolResult(True, output=body),
        args or {},
        body=body,
        tokens=estimate_tokens(body),
        ctx=ctx,
    )


class TestUnifiedNudge:
    def _body(self, n=4000):
        return "\n".join(f"line {i}" for i in range(n))

    def test_shape_is_identical_across_tools(self, tmp_path):
        """Same four elements for every tool — that is the unification."""
        for name in ("shell", "fetch", "code_index", "read_context"):
            n = _nudge(name, self._body(), tmp_path=tmp_path)
            assert n.startswith(f"[{name}:")
            assert "NOT added to context" in n
            assert "Full output saved to" in n
            assert "--- first" in n and "--- last" in n  # excerpt
            assert "search='<regex>'" in n  # regex route
            assert "line_start=<N>" in n  # range route
            assert 'agent(mode="run"' in n  # fan-out route
            assert "Root cause —" in n  # per-tool hint
            assert n.endswith("]")

    def test_root_cause_line_is_the_tools_own_hint(self, tmp_path):
        n = _nudge("shell", self._body(), tmp_path=tmp_path)
        assert TOOLS["shell"].oversized_retry_hint in n
        n = _nudge("read_context", self._body(), tmp_path=tmp_path)
        assert "LIMIT" in n and "substr(text,1,200)" in n

    def test_reports_size_and_cap(self, tmp_path):
        body = self._body()
        n = _nudge("shell", body, cap=1000, tmp_path=tmp_path)
        assert f"{estimate_tokens(body):,} tokens" in n
        assert "cap 1,000" in n

    def test_fanout_route_omitted_when_agent_not_callable(self, tmp_path):
        n = _nudge("shell", self._body(), tools=("read_file",), tmp_path=tmp_path)
        assert 'agent(mode="run"' not in n
        # the other routes survive
        assert "search='<regex>'" in n and "Root cause —" in n

    def _sections(self, nudge):
        import re as _re

        m = _re.search(r"split it into (\d+) sections", nudge)
        assert m, nudge
        return int(m.group(1))

    def test_fanout_sections_are_sized_to_fit_the_cap(self, tmp_path):
        """Old sizing clamped at 8 sections regardless of size, so a body a few
        caps large handed every sub-agent a still-over-cap slice and the fan-out
        just re-hit the same wall one level down."""
        body = self._body(3_000)  # ~7k tokens = under the section ceiling
        n = _nudge("shell", body, cap=1000, tmp_path=tmp_path)
        k = self._sections(n)
        assert estimate_tokens(body) / k <= 1000, "each section must fit the cap"
        assert "still large" not in n

    def test_fanout_says_so_when_it_cannot_fit_in_one_level(self, tmp_path):
        """Past the section ceiling the sections cannot all fit — the model is
        told to make each sub-agent narrow further instead of being handed a
        silently over-cap slice."""
        body = self._body(40_000)  # ~ 100 caps
        n = _nudge("shell", body, cap=1000, tmp_path=tmp_path)
        assert self._sections(n) == 16
        assert "still large" in n and "read_file search=" in n

    def test_persist_failure_degrades_without_claiming_a_file(self, monkeypatch):
        monkeypatch.setattr(
            "agent_cli.tools.base.persist_oversized", lambda *a, **k: ""
        )
        n = _nudge("shell", self._body(), tmp_path=None)
        assert "could NOT be saved" in n
        assert "Full output saved to" not in n
        assert "Root cause —" in n  # still tells it how to avoid the bulk


# ── where the full body lands ────────────────────────────────────────


class TestPersistence:
    def test_written_under_the_session_dir(self, tmp_path):
        path = persist_oversized("shell", "BODY", "ls -R /", tmp_path)
        assert path
        p = Path(path)
        assert p.parent == tmp_path / OVERSIZED_DIRNAME
        assert p.read_text() == "BODY"
        assert p.name.startswith("shell-")

    def test_headless_falls_back_to_a_temp_dir(self):
        """ "Over the cap always lands in a file" must hold with no session."""
        path = persist_oversized("shell", "HEADLESS BODY", "cmd", None)
        assert path
        p = Path(path)
        try:
            assert p.read_text() == "HEADLESS BODY"
            assert "agent-cli-oversized" in str(p.parent)
        finally:
            p.unlink(missing_ok=True)

    def test_same_call_and_body_reuse_one_file(self, tmp_path):
        a = persist_oversized("shell", "SAME", "cmd", tmp_path)
        b = persist_oversized("shell", "SAME", "cmd", tmp_path)
        assert a == b
        assert len(list((tmp_path / OVERSIZED_DIRNAME).iterdir())) == 1

    def test_different_body_gets_its_own_file(self, tmp_path):
        a = persist_oversized("shell", "ONE", "cmd", tmp_path)
        b = persist_oversized("shell", "TWO", "cmd", tmp_path)
        assert a != b

    def test_seam_persists_and_points_at_the_file(self, tmp_path):
        body = "\n".join(f"out {i}" for i in range(4000))
        n = _nudge("shell", body, tmp_path=tmp_path, args={"command": "ls -R /"})
        saved = tmp_path / OVERSIZED_DIRNAME
        files = list(saved.iterdir())
        assert len(files) == 1
        assert str(files[0]) in n
        assert files[0].read_text() == body


class TestSourcePathTools:
    """Two tools already wrote the bulk to disk — they point at it instead of
    persisting a byte-identical copy."""

    def test_read_file_points_at_the_file_it_read(self, tmp_path):
        f = tmp_path / "big.py"
        f.write_text("\n".join(f"x = {i}" for i in range(4000)))
        body = "\n".join(f"{i}#AB:x = {i}" for i in range(4000))
        n = _nudge("read_file", body, tmp_path=tmp_path, args={"path": str(f)})
        assert str(f) in n
        assert not (tmp_path / OVERSIZED_DIRNAME).exists(), "no copy"

    def test_read_file_persists_when_the_path_is_gone(self, tmp_path):
        body = "\n".join(f"line {i}" for i in range(4000))
        n = _nudge("read_file", body, tmp_path=tmp_path, args={"path": "/nope/gone.py"})
        assert "/nope/gone.py" not in n
        assert (tmp_path / OVERSIZED_DIRNAME).exists()

    def test_agent_points_at_result_md(self, tmp_path):
        run = tmp_path / "run-1"
        run.mkdir()
        (run / "result.md").write_text("FULL ANSWER")
        body = "\n".join(f"line {i}" for i in range(4000))
        tool = TOOLS["agent"]
        ctx = RunContext(
            session_dir=tmp_path,
            oversized_cap=1000,
            tools_available=frozenset({"agent"}),
        )
        n = tool.render_oversized(
            ToolResult(True, output=body, artifact="run-1"),
            {"mode": "run"},
            body=body,
            tokens=estimate_tokens(body),
            ctx=ctx,
        )
        assert str(run / "result.md") in n
        assert not (tmp_path / OVERSIZED_DIRNAME).exists()

    def test_agent_without_artifact_persists(self, tmp_path):
        body = "\n".join(f"line {i}" for i in range(4000))
        n = _nudge("agent", body, tmp_path=tmp_path, args={"mode": "run"})
        assert (tmp_path / OVERSIZED_DIRNAME).exists()
        assert "Root cause —" in n


# ── excerpt ──────────────────────────────────────────────────────────


class TestExcerpt:
    def test_keeps_head_and_tail_with_an_omission_marker(self):
        body = "\n".join(f"line {i}" for i in range(500))
        e = oversized_excerpt(body, cap=10_000)
        assert "line 0" in e and "line 499" in e
        assert "line 250" not in e
        assert "omitted" in e

    def test_tail_survives_a_small_budget(self):
        """The answer in a build log is at the END — the tail is what must not
        be the part that gets dropped."""
        body = "\n".join(f"line {i}" for i in range(500)) + "\n[exit code: 2]"
        e = oversized_excerpt(body, cap=120)  # tiny cap → floor budget
        assert "[exit code: 2]" in e

    def test_bounded_by_a_fraction_of_the_cap(self):
        body = "\n".join("x" * 200 for _ in range(10_000))
        cap = 20_000
        e = oversized_excerpt(body, cap=cap)
        assert estimate_tokens(e) <= cap * 0.2

    def test_single_enormous_line_is_hard_sliced(self):
        body = "y" * 1_000_000  # one line, no newlines at all
        e = oversized_excerpt(body, cap=1000)
        assert e
        assert len(e) < 5000

    def test_few_lines_are_split_not_duplicated(self):
        body = "\n".join(f"L{i}" for i in range(6))
        e = oversized_excerpt(body, cap=10_000)
        assert e.count("L3") == 1


# ── the cap itself ───────────────────────────────────────────────────


def _bridge(window):
    class _Caps:
        context_window = window
        max_output_tokens = 1000

    return ToolBridge(
        LoopConfig(
            tools_list=list(TOOLS.keys()),
            capabilities=_Caps() if window is not None else None,
        ),
        LoopState(),
        ctx=None,
        provider=None,
    )


class TestCapComputation:
    def test_cap_is_one_tenth_of_window(self):
        assert _bridge(250_000)._oversized_cap == 25_000

    def test_missing_capabilities_still_caps(self):
        """Used to be cap=0 — the cap was DISABLED, so an unbounded observation
        went into context with no file and no warning."""
        from agent_cli.providers.capabilities import MIN_CONTEXT_WINDOW

        assert _bridge(None)._oversized_cap == MIN_CONTEXT_WINDOW // 10
        assert _bridge(0)._oversized_cap == MIN_CONTEXT_WINDOW // 10

    def test_floor_is_the_smallest_supported_window(self):
        from agent_cli.providers.capabilities import MIN_CONTEXT_WINDOW

        # a window below the minimum the agent will even start on cannot make
        # the cap smaller than that minimum implies
        assert _bridge(4096)._oversized_cap == MIN_CONTEXT_WINDOW // 10

    def test_turn_budget_tracks_the_cap(self):
        b = _bridge(250_000)
        assert b._turn_obs_budget == 25_000 * TURN_OBS_BUDGET_MULT
        b._oversized_cap = 1_000  # reassignment must not leave it stale
        assert b._turn_obs_budget == 1_000 * TURN_OBS_BUDGET_MULT


# ── the seam ─────────────────────────────────────────────────────────


class TestToolObservationCap:
    def test_under_cap_passes_through_verbatim(self):
        loop = _loop(cap=1000)
        r = ToolResult(True, output="small")
        assert loop._tool_observation("shell", r, {}) == "small"

    def test_over_cap_is_replaced(self, tmp_path):
        loop = _loop(cap=100)
        loop._tools.ctx = type("C", (), {"session_dir": tmp_path})()
        big = "\n".join(f"line {i}" for i in range(4000))
        obs = loop._tool_observation("shell", ToolResult(True, output=big), {})
        assert obs != big
        assert "Full output saved to" in obs

    def test_opt_out_tool_never_replaced(self, monkeypatch):
        loop = _loop(cap=10)
        monkeypatch.setattr(type(TOOLS["shell"]), "apply_oversized_cap", False)
        big = "x" * 100_000
        assert loop._tool_observation("shell", ToolResult(True, output=big), {}) == big

    def test_unknown_tool_is_capped_too(self, tmp_path):
        loop = _loop(cap=100)
        loop._tools.ctx = type("C", (), {"session_dir": tmp_path})()
        big = "\n".join(f"line {i}" for i in range(4000))
        obs = loop._tool_observation("srv.thing", ToolResult(True, output=big), {})
        assert obs.startswith("[srv.thing:")
        assert "Full output saved to" in obs
        assert GENERIC_RETRY_HINT in obs

    def test_render_observation_override_is_honored(self, monkeypatch):
        loop = _loop(cap=1000)
        monkeypatch.setattr(
            type(TOOLS["shell"]),
            "render_observation",
            lambda self, result, args: "REWRITTEN",
        )
        r = ToolResult(True, output="original")
        assert loop._tool_observation("shell", r, {}) == "REWRITTEN"


# ── per-TURN budget: N ops each under the cap ────────────────────────


class TestTurnBudget:
    def _obs(self, cap_tokens):
        # a body comfortably under the per-op cap
        return "\n".join(f"line {i}" for i in range(cap_tokens // 3))

    def test_ops_under_the_cap_still_bounded_in_aggregate(self, tmp_path):
        loop = _loop(cap=1000, tools=["shell", "agent"])
        loop._tools.ctx = type("C", (), {"session_dir": tmp_path})()
        acc = []
        body = self._obs(1000)
        assert estimate_tokens(body) < 1000, "each op must be individually legal"
        for _ in range(12):
            loop._tools.accumulate_observation(
                acc, "shell", ToolResult(True, output=body), {"command": "x"}
            )
        verbatim = [r for r in acc if r["observation"] == body]
        replaced = [r for r in acc if "saved to" in r["observation"]]
        assert verbatim, "early ops keep their output"
        assert replaced, "the overflow degrades to file pointers"
        total = sum(r["tokens"] for r in acc)
        assert total < 1000 * 12, "aggregate must be smaller than N x cap"

    def test_earlier_ops_are_the_ones_kept(self, tmp_path):
        loop = _loop(cap=1000, tools=["shell", "agent"])
        loop._tools.ctx = type("C", (), {"session_dir": tmp_path})()
        acc = []
        body = self._obs(1000)
        for _ in range(12):
            loop._tools.accumulate_observation(
                acc, "shell", ToolResult(True, output=body), {"command": "x"}
            )
        kept = [i for i, r in enumerate(acc) if r["observation"] == body]
        dropped = [i for i, r in enumerate(acc) if r["observation"] != body]
        assert max(kept) < min(dropped)

    def test_budget_nudge_explains_the_turn_not_the_result(self, tmp_path):
        loop = _loop(cap=1000, tools=["shell", "agent"])
        loop._tools.ctx = type("C", (), {"session_dir": tmp_path})()
        acc = []
        body = self._obs(1000)
        for _ in range(12):
            loop._tools.accumulate_observation(
                acc, "shell", ToolResult(True, output=body), {"command": "x"}
            )
        replaced = next(r for r in acc if r["observation"] != body)["observation"]
        assert "per-turn budget" in replaced
        assert "Emit fewer ops per turn" in replaced
        # and NOT the misleading single-result reason
        assert "too large for one context" not in replaced

    def test_a_single_op_is_never_charged_a_turn_budget(self, tmp_path):
        loop = _loop(cap=1000)
        loop._tools.ctx = type("C", (), {"session_dir": tmp_path})()
        body = self._obs(1000)
        assert (
            loop._tool_observation("shell", ToolResult(True, output=body), {}) == body
        )

    def test_suffix_survives_replacement(self, tmp_path):
        loop = _loop(cap=1000, tools=["shell", "agent"])
        loop._tools.ctx = type("C", (), {"session_dir": tmp_path})()
        acc = []
        big = "\n".join(f"line {i}" for i in range(4000))
        loop._tools.accumulate_observation(
            acc, "shell", ToolResult(True, output=big), {}, suffix="[warn] truncated"
        )
        assert "[warn] truncated" in acc[0]["observation"]
        assert "Full output saved to" in acc[0]["observation"]


# ── run_skill goes through the seam like everything else ─────────────


class TestRunSkillCapped:
    def test_run_skill_is_a_capped_tool(self, tmp_path):
        loop = _loop(cap=100, tools=["run_skill", "agent"])
        loop._tools.ctx = type("C", (), {"session_dir": tmp_path})()
        big = "\n".join(f"line {i}" for i in range(4000))
        obs = loop._tool_observation("run_skill", ToolResult(True, output=big), {})
        assert "Full output saved to" in obs
        assert TOOLS["run_skill"].oversized_retry_hint in obs

    def test_dispatch_routes_run_skill_through_the_seam(self):
        import inspect

        from agent_cli.loop import dispatch

        src = inspect.getsource(dispatch.TurnDispatcher._op_run_skill)
        assert "_tool_observation" in src


# ── ctx.add is pure storage (no spill transform) ─────────────────────


class TestCtxAddPureStorage:
    def _ctx(self, tmp_path):
        from agent_cli.context.manager import ContextManager

        ctx = ContextManager(session_dir=tmp_path / "s", max_context_tokens=1_000_000)
        ctx.set_turn(4)
        return ctx

    def _obs(self, content):
        return {"role": "user", "tool": "shell", "success": True, "content": content}

    def test_add_stores_content_verbatim(self, tmp_path):
        ctx = self._ctx(tmp_path)
        body = "Observation: a\nb\nc with {curly} not json"
        stored = ctx.add(self._obs(body))
        assert stored["content"] == body  # no transform
        last = [
            json.loads(line)
            for line in ctx.history_path.read_text().splitlines()
            if line.strip()
        ][-1]
        assert last["content"] == body  # on-disk verbatim too

    def test_estimate_counts_full_content(self, tmp_path):
        ctx = self._ctx(tmp_path)
        big = "x" * 40_000
        ctx.add(self._obs(big))
        # no spill discount: the full content is counted (the loop caps BEFORE
        # this, so anything reaching add is already within budget by design)
        assert ctx.get_estimated_tokens() >= estimate_tokens(big) // 2

    def test_user_and_assistant_unaffected(self, tmp_path):
        ctx = self._ctx(tmp_path)
        user = {"role": "user", "content": "a huge pasted blob " * 100}
        asst = {"role": "assistant", "content": "ok"}
        assert ctx.add(user)["content"] == user["content"]
        assert ctx.add(asst)["content"] == "ok"


# ── read_context returns content VERBATIM (the truncation-bug fix) ────


class TestReadContextVerbatim:
    def _session(self, tmp_path, content):
        sdir = tmp_path / ".agent-cli" / "sessions" / "1700000000"
        sdir.mkdir(parents=True)
        rec = {
            "role": "user",
            "tool": "read_file",
            "success": True,
            "content": content,
            "kind": "observation",
            "turn": 3,
            "tools": "read_file",
            "files": "",
            "text": content,
        }
        (sdir / "history.jsonl").write_text(json.dumps(rec, ensure_ascii=False) + "\n")
        return sdir

    def test_full_content_not_truncated_at_200(self, tmp_path):
        from agent_cli.tools.context import tool_read_context

        long = "A" * 5000  # well over the old 200-char cell cap
        sdir = self._session(tmp_path, "Observation: " + long)
        res = tool_read_context(
            {"query": "SELECT text FROM history WHERE turn=3"}, session_dir=sdir
        )
        assert res.success, res.error
        assert long in res.output  # verbatim, no '…' truncation
        assert "…" not in res.output

    def test_newlines_and_indent_preserved(self, tmp_path):
        from agent_cli.tools.context import tool_read_context

        code = "1#def f():\n2#    if x:\n3#        return y"
        sdir = self._session(tmp_path, code)
        res = tool_read_context(
            {"query": "SELECT text FROM history WHERE turn=3"}, session_dir=sdir
        )
        assert res.success, res.error
        # newlines + indentation survive (no whitespace collapse)
        assert "2#    if x:" in res.output
        assert "3#        return y" in res.output

    def test_content_column_removed(self, tmp_path):
        from agent_cli.tools.context import tool_read_context

        sdir = self._session(tmp_path, "hi")
        res = tool_read_context(
            {"query": "SELECT content FROM history"}, session_dir=sdir
        )
        # the spill-era 'content' column is gone → querying it is an error
        assert not res.success


# ── RunContext: the per-call bundle threaded to both tool surfaces ────


class TestRunContext:
    def test_defaults_are_empty(self):
        c = RunContext()
        assert c.session_dir is None
        assert c.oversized_cap == 0
        assert c.tools_available == frozenset()

    def test_frozen(self):
        c = RunContext(oversized_cap=10)
        import dataclasses

        try:
            c.oversized_cap = 20  # type: ignore[misc]
        except dataclasses.FrozenInstanceError:
            pass
        else:  # pragma: no cover - guards the invariant
            raise AssertionError("RunContext must be frozen (shared-safe)")

    def test_loop_run_ctx_bundles_the_three_fields(self, tmp_path):
        # _run_ctx is the single construction point handed to BOTH seams.
        loop = _loop(cap=4_096, tools=["read_file", "agent"])

        class _C:
            session_dir = tmp_path

        # C1 PR-2: RunContext 조립은 ToolBridge 소유 — ctx 도 브리지에 주입
        loop.ctx = _C()
        loop._tools.ctx = _C()
        c = loop._run_ctx()
        assert isinstance(c, RunContext)
        assert c.session_dir == tmp_path
        assert c.oversized_cap == 4_096
        assert c.tools_available == frozenset({"read_file", "agent"})

    def test_loop_run_ctx_headless_session_dir_none(self):
        loop = _loop(cap=0, tools=["read_file"])  # loop.ctx is None
        c = loop._run_ctx()
        assert c.session_dir is None and c.oversized_cap == 0

    def test_loop_run_ctx_cached_single_instance(self):
        # Inputs are immutable after __init__ and RunContext is frozen, so
        # _run_ctx builds ONCE and returns the same instance thereafter
        # (tool-call seam + oversized-render seam share it).
        loop = _loop(cap=4_096, tools=["read_file", "agent"])
        assert loop._run_ctx() is loop._run_ctx()


# ── read_file stat hint is cap-aware, threaded via RunContext, and the
#    over-cap decision agrees with the loop's real capping ─────────────


class TestStatHintCapAwareThreading:
    def _bigfile(self, tmp_path):
        f = tmp_path / "huge.py"
        f.write_text(
            "\n".join(f"line number {i} with some content" for i in range(4000))
        )
        return f

    def test_run_threads_cap_into_stat_hint(self, tmp_path):
        # End-to-end through the public Tool.run → _run → _stat path: a cap in
        # the RunContext makes the stat hint cap-aware.
        f = self._bigfile(tmp_path)
        out = (
            TOOLS["read_file"]
            .run(
                {"path": str(f), "stat": True},
                ctx=RunContext(
                    oversized_cap=4_096,
                    tools_available=frozenset({"read_file", "agent"}),
                ),
            )
            .output
        )
        assert "would exceed the context cap" in out
        assert "for a full read" not in out
        assert "Fan out" in out  # delegate available

    def test_run_without_ctx_keeps_plain_hint(self, tmp_path):
        # A direct/test caller that omits ctx (ctx=None) → plain hint, no crash.
        f = self._bigfile(tmp_path)
        out = TOOLS["read_file"].run({"path": str(f), "stat": True}).output
        assert "for a full read" in out
        assert "would exceed" not in out

    def test_est_tokens_is_upper_bound_of_real_body(self, tmp_path):
        # The cheap estimate must not UNDER-count the real formatted body (that
        # would let a full read slip past the stat warning and then get capped).
        from agent_cli.tools.read_file import _full_read_est_tokens, format_hashlines

        f = self._bigfile(tmp_path)
        text = f.read_text()
        total = text.count("\n") + 1
        est = _full_read_est_tokens(text, total)
        real = estimate_tokens(format_hashlines(text))
        assert est >= real  # conservative: warns at or before the real cap

    def test_stat_over_cap_decision_matches_seam_capping(self, tmp_path):
        # The invariant the whole change rests on: stat says "would exceed the
        # cap" for exactly the caps at which a real full read is dropped by the
        # loop seam. Check one cap on each side of the boundary.
        from agent_cli.tools.read_file import _full_read_est_tokens

        f = self._bigfile(tmp_path)
        text = f.read_text()
        total = text.count("\n") + 1
        est = _full_read_est_tokens(text, total)
        over_cap = est // 2  # full read clearly over
        under_cap = est * 4  # full read clearly under

        full_body = TOOLS["read_file"].run({"path": str(f)}).output

        for cap, expect_over in ((over_cap, True), (under_cap, False)):
            stat_out = (
                TOOLS["read_file"]
                .run(
                    {"path": str(f), "stat": True},
                    ctx=RunContext(oversized_cap=cap),
                )
                .output
            )
            says_over = "would exceed the context cap" in stat_out
            # the loop's real decision for a FULL read at the same cap
            loop = _loop(cap=cap, tools=["read_file"])
            seam_out = loop._tool_observation(
                "read_file", ToolResult(True, output=full_body), {"path": str(f)}
            )
            really_capped = "too large" in seam_out
            assert says_over == expect_over
            assert says_over == really_capped  # stat prediction == reality


# ── paths the model is shown ─────────────────────────────────────────


class TestDisplayPath:
    """A path handed back in a nudge becomes an argument the model echoes into
    its next call — and an ``action_input`` is re-fed on every turn after it. So
    the loop must hand back the same SHAPE of path the system prompt asks the
    model to write (relative to the working directory)."""

    def test_under_cwd_becomes_relative(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "a" / "b").mkdir(parents=True)
        f = tmp_path / "a" / "b" / "out.txt"
        f.write_text("x")
        assert display_path(f) == "a/b/out.txt"

    def test_outside_cwd_stays_absolute(self, tmp_path, monkeypatch):
        work = tmp_path / "work"
        work.mkdir()
        other = tmp_path / "elsewhere"
        other.mkdir()
        monkeypatch.chdir(work)
        assert display_path(other / "f.txt") == str(other / "f.txt")

    def test_already_relative_is_preserved(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "x.txt").write_text("x")
        assert display_path("x.txt") == "x.txt"

    def test_cwd_itself(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert display_path(tmp_path) == "."

    def test_never_raises_on_a_nonexistent_path(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert display_path(tmp_path / "nope" / "gone.txt") == "nope/gone.txt"


class TestNudgePathShape:
    def _big(self):
        return "\n".join(f"line {i}" for i in range(4000))

    def test_saved_file_is_shown_relative(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        sess = tmp_path / ".agent-cli" / "sessions" / "s1"
        sess.mkdir(parents=True)
        body = self._big()
        n = _nudge("shell", body, tmp_path=sess, args={"command": "make"})
        assert str(tmp_path) not in n, "absolute workspace prefix leaked into the nudge"
        assert ".agent-cli/sessions/s1/oversized/" in n

    def test_every_occurrence_is_short(self, tmp_path, monkeypatch):
        """The path appears in the header AND in each recovery route, so the
        saving compounds — one absolute leak would undo the rest."""
        monkeypatch.chdir(tmp_path)
        sess = tmp_path / ".agent-cli" / "sessions" / "s1"
        sess.mkdir(parents=True)
        n = _nudge("shell", self._big(), tmp_path=sess, args={"command": "make"})
        assert n.count(".agent-cli/sessions/s1/oversized/") >= 3
        assert n.count(str(tmp_path)) == 0

    def test_read_file_keeps_the_models_own_relative_path(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "big.py").write_text("\n".join(f"x={i}" for i in range(4000)))
        n = _nudge("read_file", self._big(), tmp_path=tmp_path, args={"path": "big.py"})
        assert "'big.py'" in n
        assert str(tmp_path) not in n

    def test_absolute_source_path_is_shortened_too(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        f = tmp_path / "big.py"
        f.write_text("\n".join(f"x={i}" for i in range(4000)))
        n = _nudge("read_file", self._big(), tmp_path=tmp_path, args={"path": str(f)})
        assert "'big.py'" in n
        assert str(tmp_path) not in n

    def test_headless_tempdir_fallback_stays_absolute(self, tmp_path, monkeypatch):
        """The temp dir is genuinely outside the working directory — shortening
        it would produce a path that does not resolve."""
        import re

        monkeypatch.chdir(tmp_path)
        n = _nudge("shell", self._big(), tmp_path=None, args={"command": "make"})
        m = re.search(r"Full output saved to '([^']+)'", n)
        assert m, n
        saved = m.group(1)
        assert "agent-cli-oversized" in saved
        assert saved.startswith("/"), saved
        assert Path(saved).is_file()
        Path(saved).unlink(missing_ok=True)


class TestLoopPracticesWhatThePromptPreaches:
    def test_prompt_asks_for_relative_and_the_nudge_supplies_it(
        self, tmp_path, monkeypatch
    ):
        """If the Environment rule and the nudge disagreed, the loop would be
        teaching by counter-example on every over-cap result."""
        from agent_cli.prompts.system_prompt import _build_environment_section

        monkeypatch.chdir(tmp_path)
        assert "RELATIVE" in _build_environment_section()
        sess = tmp_path / ".agent-cli" / "sessions" / "s1"
        sess.mkdir(parents=True)
        n = _nudge("shell", "\n".join(f"l{i}" for i in range(4000)), tmp_path=sess)
        assert str(tmp_path) not in n
