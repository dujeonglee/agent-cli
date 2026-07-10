"""Oversized-observation cap + per-tool render/cap surfaces.

Replaces the chunked-spill subsystem. A tool observation larger than the cap
(``context_window / 10``) is replaced at the result→observation seam
(``AgentLoop._tool_observation``) with a narrow-it nudge — the full output is
never added to context (large outputs crowd out reasoning and lower quality).
Two per-tool surfaces govern this: ``Tool.render_observation`` (how the result
is formatted, default output/error) and ``Tool.apply_oversized_cap`` (whether
the cap applies, default True). ``ctx.add`` is therefore pure storage.
"""

import json

from agent_cli.context.token_estimator import estimate_tokens
from agent_cli.loop import AgentLoop
from agent_cli.tools import TOOLS, RunContext
from agent_cli.tools.base import (
    Tool,
    default_oversized_nudge,
    narrow_oversized_nudge,
    on_disk_oversized_nudge,
)
from agent_cli.tools.result import ToolResult


def _loop(cap: int, tools: list[str] | None = None) -> AgentLoop:
    """A bare AgentLoop carrying the cap + tool list — enough for
    _tool_observation (which now consults ``tools_list`` for per-tool
    over-cap guidance, e.g. delegate fan-out only when delegate is callable)."""
    loop = AgentLoop.__new__(AgentLoop)
    loop._oversized_cap = cap
    loop.tools_list = list(TOOLS.keys()) if tools is None else list(tools)
    loop.ctx = None  # seam reads self.ctx.session_dir if self.ctx else None
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

    def test_render_oversized_default_is_generic_nudge(self):
        # A tool that does NOT override the seam falls back to the shared
        # default byte-for-byte (shell has no override).
        tool = TOOLS["shell"]
        out = tool.render_oversized(
            ToolResult(True, output="x"),
            {},
            body="x",
            tokens=9_000,
            ctx=RunContext(
                oversized_cap=4_000, tools_available=frozenset(TOOLS.keys())
            ),
        )
        assert out == default_oversized_nudge("shell", 9_000, 4_000)


# ── _tool_observation: render + cap meet here ────────────────────────


class TestToolObservationCap:
    def test_under_cap_passes_through_verbatim(self):
        loop = _loop(cap=1000)
        body = "line one\n  line two\n\tline three"  # newlines/indent preserved
        out = loop._tool_observation("read_file", ToolResult(True, output=body), {})
        assert out == body

    def test_over_cap_is_nudged_not_raw(self):
        loop = _loop(cap=50)
        big = "/p/secret_marker.py\n" * 500
        out = loop._tool_observation("read_file", ToolResult(True, output=big), {})
        assert "too large" in out
        assert "secret_marker" not in out  # raw content absent
        assert "read_file" in out  # nudge names the tool

    def test_opt_out_tool_never_nudged(self, monkeypatch):
        loop = _loop(cap=50)
        monkeypatch.setattr(TOOLS["read_file"], "apply_oversized_cap", False)
        big = "x" * 100_000
        out = loop._tool_observation("read_file", ToolResult(True, output=big), {})
        assert out == big  # opted out → verbatim even when huge

    def test_cap_zero_disables_capping(self):
        loop = _loop(cap=0)  # headless/no-capabilities path
        big = "x" * 100_000
        out = loop._tool_observation("read_file", ToolResult(True, output=big), {})
        assert out == big

    def test_unknown_tool_falls_back_to_default_render_and_cap(self):
        loop = _loop(cap=50)
        big = "y" * 100_000
        out = loop._tool_observation("not_a_tool", ToolResult(True, output=big), {})
        assert "too large" in out  # default cap_on=True still applies

    def test_render_observation_override_is_honored(self, monkeypatch):
        loop = _loop(cap=1000)

        def fake_render(result, args):
            return "CUSTOM RENDER"

        monkeypatch.setattr(TOOLS["read_file"], "render_observation", fake_render)
        out = loop._tool_observation("read_file", ToolResult(True, output="raw"), {})
        assert out == "CUSTOM RENDER"


# ── cap = context_window / 10 ────────────────────────────────────────


class TestCapComputation:
    def _caps(self, window):
        class C:
            context_window = window
            max_output_tokens = 1000

        return C()

    def test_cap_is_one_tenth_of_window(self):
        # the formula __init__ uses to set self._oversized_cap
        caps = self._caps(250_000)
        cap = (
            caps.context_window // 10
            if caps and getattr(caps, "context_window", 0)
            else 0
        )
        assert cap == 25_000

    def test_no_capabilities_means_no_cap(self):
        caps = None
        cap = (
            caps.context_window // 10
            if caps and getattr(caps, "context_window", 0)
            else 0
        )
        assert cap == 0


# ── nudge text ───────────────────────────────────────────────────────


class TestNudge:
    def test_nudge_mentions_tool_size_and_recovery(self):
        n = default_oversized_nudge("read_context", 99_999, 5_000)
        assert "read_context" in n
        assert "99,999" in n and "5,000" in n
        assert "too large" in n
        assert "read_file" in n  # points at a narrower path


# ── read_file over-cap: pure nudge + delegate fan-out guidance ───────


class TestReadFileOversized:
    def test_read_file_overrides_render_oversized(self):
        # The surface is actually wired for read_file (not the inherited default).
        assert type(TOOLS["read_file"]).render_oversized is not Tool.render_oversized

    def _nudge(self, tools, args=None):
        # A multi-line body so the fan-out bullet proposes concrete line ranges.
        body = "\n".join(f"RAW_SECRET_BODY line {i}" for i in range(900))
        return TOOLS["read_file"].render_oversized(
            ToolResult(True, output=body),
            args or {"path": "big_module.py"},
            body=body,
            tokens=63_488,
            ctx=RunContext(oversized_cap=4_096, tools_available=frozenset(tools)),
        )

    def test_names_path_size_and_narrowing(self):
        n = self._nudge(["read_file", "delegate"])
        assert "big_module.py" in n  # the path
        assert "63,488" in n and "4,096" in n  # size + cap
        assert "range" in n and "read_symbols" in n  # cheap narrowing
        assert "too large" in n and "read_file" in n
        assert "RAW_SECRET_BODY" not in n  # raw body never echoed

    def test_delegate_fanout_is_n_way_parallel(self):
        n = self._nudge(["read_file", "delegate"])
        assert "Fan out IN PARALLEL" in n
        # N-way: several delegate ops in ONE turn, with concrete line ranges
        assert "delegate ops in ONE turn" in n
        assert "concurrently" in n
        assert n.count("delegate(task=") >= 2  # a copyable multi-op pattern
        assert "lines 1-" in n  # concrete section boundary

    def test_delegate_omitted_when_unavailable(self):
        # Depth-limited subagent: delegate is stripped from the toolset — the
        # nudge must NOT point at a tool the model cannot call.
        n = self._nudge(["read_file"])  # no delegate
        assert "delegate" not in n
        assert "range" in n  # cheap narrowing still offered

    def test_missing_path_degrades_gracefully(self):
        n = self._nudge(["read_file"], args={})
        assert "the file" in n  # placeholder, no crash


# ── through the loop seam (render_oversized + tools_available) ────────


class TestReadFileOversizedThroughSeam:
    def test_seam_emits_delegate_guidance_when_delegate_present(self):
        loop = _loop(cap=50, tools=["read_file", "delegate"])
        big = "line of source\n" * 500
        out = loop._tool_observation(
            "read_file", ToolResult(True, output=big), {"path": "m.py"}
        )
        assert "m.py" in out and "delegate" in out
        assert "line of source" not in out  # raw dropped

    def test_seam_omits_delegate_when_depth_stripped(self):
        loop = _loop(cap=50, tools=["read_file"])  # delegate depth-stripped
        big = "line of source\n" * 500
        out = loop._tool_observation(
            "read_file", ToolResult(True, output=big), {"path": "m.py"}
        )
        assert "delegate" not in out
        assert "read_symbols" in out  # still steers to a narrower read


# ── shared on-disk nudge (read_file / shell / delegate invariant) ────


class TestOnDiskNudgeHelper:
    def _n(self, tools, **kw):
        return on_disk_oversized_nudge(
            "toolx",
            "the thing",
            "saved to '/s/out.txt'",
            "/s/out.txt",
            12_345,
            4_096,
            frozenset(tools),
            **kw,
        )

    def test_specific_part_bullet_always_present(self):
        n = self._n(["toolx"])
        assert "read_file '/s/out.txt' with a line range" in n
        assert "12,345" in n and "4,096" in n
        assert "saved to '/s/out.txt'" in n

    def test_fanout_bullet_only_when_delegate_available(self):
        assert "Fan out IN PARALLEL" in self._n(["toolx", "delegate"])
        assert "Fan out IN PARALLEL" not in self._n(["toolx"])

    def test_fanout_concrete_ranges_with_nlines(self):
        # With a line count, the fan-out bullet proposes concrete parallel ops.
        n = self._n(["toolx", "delegate"], nlines=2_483)
        assert "2,483 lines" in n
        assert "delegate ops in ONE turn" in n and "concurrently" in n
        assert n.count("delegate(task=") >= 2
        assert "lines 1-" in n

    def test_fanout_generic_without_nlines(self):
        # No line count → still parallel, but generic (no concrete ranges).
        n = self._n(["toolx", "delegate"])  # nlines defaults to 0
        assert "Fan out IN PARALLEL" in n
        assert "one delegate op per section" in n
        assert "delegate(task=" not in n  # no concrete op examples

    def test_part_extra_and_tail_bullets(self):
        n = self._n(["toolx"], part_extra="read_symbols", tail_bullets=("do X.",))
        assert "read_symbols" in n
        assert "· do X." in n


# ── shell over-cap: persist output to file + on-disk nudge ───────────


class TestShellOversized:
    def _big(self):
        return "row of output\n" * 800

    def test_persists_output_and_points_at_file(self, tmp_path):
        n = TOOLS["shell"].render_oversized(
            ToolResult(True, output=self._big()),
            {"command": "grep -r foo ."},
            body=self._big(),
            tokens=9_000,
            ctx=RunContext(
                session_dir=tmp_path,
                oversized_cap=4_096,
                tools_available=frozenset({"shell", "read_file", "delegate"}),
            ),
        )
        saved = list(tmp_path.glob("shell-output-*.txt"))
        assert len(saved) == 1, "output must be persisted exactly once"
        assert saved[0].read_text() == self._big()  # full body on disk
        assert str(saved[0]) in n  # nudge points at the file
        assert "row of output" not in n  # raw body not echoed
        assert "Fan out IN PARALLEL" in n  # fan-out offered

    def test_no_delegate_no_fanout_bullet(self, tmp_path):
        n = TOOLS["shell"].render_oversized(
            ToolResult(True, output=self._big()),
            {"command": "ls -R /"},
            body=self._big(),
            tokens=9_000,
            ctx=RunContext(
                session_dir=tmp_path,
                oversized_cap=4_096,
                tools_available=frozenset({"shell", "read_file"}),
            ),
        )
        assert "Fan out IN PARALLEL" not in n
        assert "read_file" in n  # still steers to a ranged read of the file

    def test_headless_falls_back_to_tee_nudge(self):
        # No session dir → cannot save a file → generic tee-to-file fallback.
        n = TOOLS["shell"].render_oversized(
            ToolResult(True, output=self._big()),
            {"command": "ls"},
            body=self._big(),
            tokens=9_000,
            ctx=RunContext(
                session_dir=None,
                oversized_cap=4_096,
                tools_available=frozenset({"shell", "delegate"}),
            ),
        )
        assert "tee" in n  # generic default nudge advises tee→read_file
        assert "shell" in n

    def test_identical_output_dedups_filename(self, tmp_path):
        # Same command+output → same hash → same file (no proliferation).
        for _ in range(3):
            TOOLS["shell"].render_oversized(
                ToolResult(True, output=self._big()),
                {"command": "dup"},
                body=self._big(),
                tokens=9_000,
                ctx=RunContext(
                    session_dir=tmp_path,
                    oversized_cap=4_096,
                    tools_available=frozenset({"shell"}),
                ),
            )
        assert len(list(tmp_path.glob("shell-output-*.txt"))) == 1


# ── delegate over-cap: point at result.md + fan-out + re-delegate ────


class TestDelegateOversized:
    def test_points_at_result_md_with_fanout_and_redelegate(self, tmp_path):
        # tool_delegate persisted the answer at <session>/<artifact>result.md.
        (tmp_path / "task-abc").mkdir()
        (tmp_path / "task-abc" / "result.md").write_text("BIG ANSWER")
        n = TOOLS["delegate"].render_oversized(
            ToolResult(True, output="STATUS: success…", artifact="task-abc/"),
            {"task": "analyse everything"},
            body="x" * 50_000,
            tokens=25_000,
            ctx=RunContext(
                session_dir=tmp_path,
                oversized_cap=4_096,
                tools_available=frozenset({"delegate", "read_file"}),
            ),
        )
        assert "task-abc/result.md" in n.replace(str(tmp_path) + "/", "")
        assert "result.md" in n
        assert "Fan out IN PARALLEL" in n  # split result.md across subagents
        assert "re-delegate a NARROWER task" in n.lower() or "narrower" in n.lower()

    def test_falls_back_without_artifact(self, tmp_path):
        n = TOOLS["delegate"].render_oversized(
            ToolResult(True, output="…", artifact=""),
            {"task": "t"},
            body="x" * 50_000,
            tokens=25_000,
            ctx=RunContext(
                session_dir=tmp_path,
                oversized_cap=4_096,
                tools_available=frozenset({"delegate"}),
            ),
        )
        assert "too large" in n  # generic fallback still caps


# ── fetch over-cap: persist content to file + on-disk nudge ──────────


class TestFetchOversized:
    def _big(self):
        return "# Section\n\nlots of fetched prose. " * 900

    def _call(self, tmp_path, tools, url="http://example.com/big"):
        # fetch uses the fetch_ wire prefix: the raw action_input key is fetch_url.
        return TOOLS["fetch"].render_oversized(
            ToolResult(True, output=self._big()),
            {"fetch_url": url},
            body=self._big(),
            tokens=30_000,
            ctx=RunContext(
                session_dir=tmp_path,
                oversized_cap=4_096,
                tools_available=frozenset(tools),
            ),
        )

    def test_persists_content_and_points_at_file(self, tmp_path):
        n = self._call(tmp_path, ["fetch", "read_file", "delegate"])
        saved = list(tmp_path.glob("fetch-output-*.txt"))
        assert len(saved) == 1
        assert saved[0].read_text() == self._big()  # full content on disk
        assert str(saved[0]) in n
        assert "fetched prose" not in n  # raw body not echoed
        assert "Fan out IN PARALLEL" in n
        assert "fetch_depth" in n  # fetch-specific narrower-URL bullet

    def test_no_delegate_no_fanout(self, tmp_path):
        n = self._call(tmp_path, ["fetch", "read_file"])
        assert "Fan out IN PARALLEL" not in n
        assert "read_file" in n

    def test_headless_falls_back(self):
        n = TOOLS["fetch"].render_oversized(
            ToolResult(True, output=self._big()),
            {"fetch_url": "http://x"},
            body=self._big(),
            tokens=30_000,
            ctx=RunContext(
                session_dir=None,
                oversized_cap=4_096,
                tools_available=frozenset({"fetch", "delegate"}),
            ),
        )
        assert "too large" in n  # generic fallback


# ── narrow (not-on-disk) nudge: context / code_index ─────────────────


class TestNarrowNudgeHelper:
    def test_preamble_and_bullets(self):
        n = narrow_oversized_nudge(
            "toolx", "the result", 30_000, 4_096, bullets=("do A.", "do B.")
        )
        assert "toolx: the result is ~30,000 tokens" in n
        assert "cap 4,096" in n
        assert "the call succeeded" in n
        assert "· do A." in n and "· do B." in n
        # narrow nudge is in-place: it must NOT drag in file/tee/read_file advice
        assert "tee" not in n and "read_file" not in n


class TestContextOversized:
    def _n(self, tools=("read_context", "read_file", "delegate")):
        return TOOLS["read_context"].render_oversized(
            ToolResult(True, output="…"),
            {"read_context_query": "SELECT * FROM history"},
            body="…" * 10,
            tokens=30_000,
            ctx=RunContext(oversized_cap=4_096, tools_available=frozenset(tools)),
        )

    def test_overrides_and_steers_to_sql_narrowing(self):
        assert type(TOOLS["read_context"]).render_oversized is not Tool.render_oversized
        n = self._n()
        assert "read_context" in n and "30,000" in n
        assert "LIMIT" in n and "substr(text,1,200)" in n  # SQL-native recovery
        # no irrelevant generic advice / no file/fan-out for a SQL result
        assert "line range" not in n
        assert "tee" not in n and "Fan out IN PARALLEL" not in n

    def test_seam_produces_sql_nudge(self):
        loop = _loop(cap=50)
        big = "x" * 100_000
        out = loop._tool_observation("read_context", ToolResult(True, output=big), {})
        assert "LIMIT" in out and "substr" in out
        assert "x" * 50 not in out  # raw dropped


class TestCodeIndexOversized:
    def _n(self):
        return TOOLS["code_index"].render_oversized(
            ToolResult(True, output="…"),
            {"mode": "kind", "symbol_kind": "function"},
            body="…" * 10,
            tokens=30_000,
            ctx=RunContext(
                oversized_cap=4_096,
                tools_available=frozenset({"code_index", "read_file"}),
            ),
        )

    def test_overrides_and_steers_to_index_params(self):
        assert type(TOOLS["code_index"]).render_oversized is not Tool.render_oversized
        n = self._n()
        assert "code_index" in n and "30,000" in n
        # real code_index params, not generic advice
        assert "mode=fetch" in n and "search=" in n and "max_bytes" in n
        assert "line range" not in n and "tee" not in n

    def test_seam_produces_index_nudge(self):
        loop = _loop(cap=50)
        big = "y" * 100_000
        out = loop._tool_observation("code_index", ToolResult(True, output=big), {})
        assert "mode=fetch" in out
        assert "y" * 50 not in out


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
        loop = _loop(cap=4_096, tools=["read_file", "delegate"])

        class _C:
            session_dir = tmp_path

        loop.ctx = _C()
        c = loop._run_ctx()
        assert isinstance(c, RunContext)
        assert c.session_dir == tmp_path
        assert c.oversized_cap == 4_096
        assert c.tools_available == frozenset({"read_file", "delegate"})

    def test_loop_run_ctx_headless_session_dir_none(self):
        loop = _loop(cap=0, tools=["read_file"])  # loop.ctx is None
        c = loop._run_ctx()
        assert c.session_dir is None and c.oversized_cap == 0


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
                    tools_available=frozenset({"read_file", "delegate"}),
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
