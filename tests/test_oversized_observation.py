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
from agent_cli.loop import AgentLoop, _render_oversized_nudge
from agent_cli.tools import TOOLS
from agent_cli.tools.base import Tool
from agent_cli.tools.result import ToolResult


def _loop(cap: int) -> AgentLoop:
    """A bare AgentLoop carrying only the cap — enough for _tool_observation."""
    loop = AgentLoop.__new__(AgentLoop)
    loop._oversized_cap = cap
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
        n = _render_oversized_nudge("read_context", 99_999, 5_000)
        assert "read_context" in n
        assert "99,999" in n and "5,000" in n
        assert "too large" in n
        assert "read_file" in n  # points at a narrower path


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
