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
from agent_cli.tools import TOOLS
from agent_cli.tools.base import (
    Tool,
    default_oversized_nudge,
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
            cap=4_000,
            tools_available=frozenset(TOOLS.keys()),
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
        return TOOLS["read_file"].render_oversized(
            ToolResult(True, output="RAW_SECRET_BODY"),
            args or {"path": "big_module.py"},
            body="RAW_SECRET_BODY",
            tokens=63_488,
            cap=4_096,
            tools_available=frozenset(tools),
        )

    def test_names_path_size_and_narrowing(self):
        n = self._nudge(["read_file", "delegate"])
        assert "big_module.py" in n  # the path
        assert "63,488" in n and "4,096" in n  # size + cap
        assert "range" in n and "read_symbols" in n  # cheap narrowing
        assert "too large" in n and "read_file" in n
        assert "RAW_SECRET_BODY" not in n  # raw body never echoed

    def test_delegate_fanout_guidance_when_available(self):
        n = self._nudge(["read_file", "delegate"])
        assert "delegate" in n
        assert "section" in n  # fan-out over sections
        assert "distilled" in n

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
        assert "Fan out with delegate" in self._n(["toolx", "delegate"])
        assert "Fan out with delegate" not in self._n(["toolx"])

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
            cap=4_096,
            tools_available=frozenset({"shell", "read_file", "delegate"}),
            session_dir=tmp_path,
        )
        saved = list(tmp_path.glob("shell-output-*.txt"))
        assert len(saved) == 1, "output must be persisted exactly once"
        assert saved[0].read_text() == self._big()  # full body on disk
        assert str(saved[0]) in n  # nudge points at the file
        assert "row of output" not in n  # raw body not echoed
        assert "Fan out with delegate" in n  # fan-out offered

    def test_no_delegate_no_fanout_bullet(self, tmp_path):
        n = TOOLS["shell"].render_oversized(
            ToolResult(True, output=self._big()),
            {"command": "ls -R /"},
            body=self._big(),
            tokens=9_000,
            cap=4_096,
            tools_available=frozenset({"shell", "read_file"}),
            session_dir=tmp_path,
        )
        assert "Fan out with delegate" not in n
        assert "read_file" in n  # still steers to a ranged read of the file

    def test_headless_falls_back_to_tee_nudge(self):
        # No session dir → cannot save a file → generic tee-to-file fallback.
        n = TOOLS["shell"].render_oversized(
            ToolResult(True, output=self._big()),
            {"command": "ls"},
            body=self._big(),
            tokens=9_000,
            cap=4_096,
            tools_available=frozenset({"shell", "delegate"}),
            session_dir=None,
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
                cap=4_096,
                tools_available=frozenset({"shell"}),
                session_dir=tmp_path,
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
            cap=4_096,
            tools_available=frozenset({"delegate", "read_file"}),
            session_dir=tmp_path,
        )
        assert "task-abc/result.md" in n.replace(str(tmp_path) + "/", "")
        assert "result.md" in n
        assert "Fan out with delegate" in n  # split result.md across subagents
        assert "re-delegate a NARROWER task" in n.lower() or "narrower" in n.lower()

    def test_falls_back_without_artifact(self, tmp_path):
        n = TOOLS["delegate"].render_oversized(
            ToolResult(True, output="…", artifact=""),
            {"task": "t"},
            body="x" * 50_000,
            tokens=25_000,
            cap=4_096,
            tools_available=frozenset({"delegate"}),
            session_dir=tmp_path,
        )
        assert "too large" in n  # generic fallback still caps


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
