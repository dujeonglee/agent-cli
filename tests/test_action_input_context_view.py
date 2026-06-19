"""Per-tool action_input context-view seam (`Tool.render_action_input_for_context`).

A symmetric counterpart to ``Tool.render_observation``: how a tool's ACTION
(the action_input the model emitted) is represented when RE-FED to the LLM each
turn. The default is identity (no change) — this commit only lays the seam, so
behaviour is byte-identical to before. Later, write_file/edit_file will override
to elide their bulky body args (content / lines) on re-feed, keeping the op
shape but dropping the bulk (the file is on disk; read_file to view).

The seam is consulted at the two context boundaries — render
(``_to_natural_language``) and budget (``_estimate_message_tokens``) — via the
shared ``_context_view`` helper, always on a COPY so history.jsonl + the cache
stay faithful.
"""

import json

from agent_cli.context.manager import (
    ContextManager,
    _context_view,
    _estimate_message_tokens,
    _to_natural_language,
)
from agent_cli.tools import TOOLS
from agent_cli.tools.base import Tool
from agent_cli.wire_formats import get as get_wire


def _assistant_write(content="line1\nline2\nline3"):
    return {
        "role": "assistant",
        "thought": "writing",
        "ops": [
            {
                "action": "write_file",
                "action_input": {"path": "src/x.py", "content": content},
            }
        ],
    }


# ── default seam = identity (behaviour unchanged) ────────────────────


class TestSeamDefaultIdentity:
    def test_base_default_returns_input_unchanged(self):
        ai = {"path": "x", "content": "body"}
        # default is identity — returns the SAME object
        assert TOOLS["write_file"].render_action_input_for_context(ai) is ai

    def test_all_builtins_default_identity(self):
        ai = {"path": "x", "content": "body", "lines": ["a"]}
        for t in TOOLS.values():
            assert t.render_action_input_for_context(ai) is ai

    def test_context_view_noop_for_default_tools(self):
        msg = _assistant_write()
        # identity everywhere → _context_view returns the message unchanged
        assert _context_view(msg) is msg

    def test_render_unchanged_with_default_seam(self):
        msg = _assistant_write("AAA\nBBB\nCCC")
        wf = get_wire("md_array")
        # rendering through the seam must equal rendering the raw msg directly
        assert _to_natural_language(msg, wf) == wf.render_assistant_from_history(msg)

    def test_estimate_unchanged_with_default_seam(self):
        msg = _assistant_write("x" * 4000)
        # the full content is still counted (identity → no elision)
        assert _estimate_message_tokens(msg) >= 4000 // 4


# ── mechanism: a tool that DOES override (write/edit will, later) ────


class _ElidingTool(Tool):
    name = "write_file"  # shadow registry entry for the test
    description = ""
    parameters: dict = {}

    def _run(self, args, *, session_dir=None):  # pragma: no cover - unused
        raise NotImplementedError

    def render_action_input_for_context(self, action_input: dict) -> dict:
        ai = dict(action_input)
        body = ai.get("content")
        if isinstance(body, str) and body:
            n = body.count("\n") + 1
            ai["content"] = f"<{n} lines elided — read_file {ai.get('path', '')}>"
        return ai


class TestContextViewElision:
    def _patch(self, monkeypatch):
        monkeypatch.setitem(TOOLS, "write_file", _ElidingTool())

    def test_view_elides_heavy_arg(self, monkeypatch):
        self._patch(monkeypatch)
        msg = _assistant_write("a\nb\nc\nd")
        view = _context_view(msg)
        body = view["ops"][0]["action_input"]["content"]
        assert "elided" in body and "src/x.py" in body
        assert "\n" not in body  # the real 4-line body is gone

    def test_original_message_not_mutated(self, monkeypatch):
        self._patch(monkeypatch)
        msg = _assistant_write("a\nb\nc\nd")
        _context_view(msg)
        # the source record is untouched — history.jsonl + cache stay faithful
        assert msg["ops"][0]["action_input"]["content"] == "a\nb\nc\nd"

    def test_render_uses_elided_view(self, monkeypatch):
        self._patch(monkeypatch)
        msg = _assistant_write("SECRET_BODY_LINE\nmore")
        rendered = _to_natural_language(msg, get_wire("md_array"))
        assert "SECRET_BODY_LINE" not in json.dumps(rendered, ensure_ascii=False)
        assert "elided" in json.dumps(rendered, ensure_ascii=False)

    def test_estimate_reflects_elided_size(self, monkeypatch):
        self._patch(monkeypatch)
        big = _assistant_write("x" * 40_000)
        small_est = _estimate_message_tokens(big)
        # with elision the estimate is far below the 40K-char body
        assert small_est < 40_000 // 4 // 2

    def test_legacy_single_op_shape_supported(self, monkeypatch):
        self._patch(monkeypatch)
        legacy = {
            "role": "assistant",
            "action": "write_file",
            "action_input": {"path": "src/x.py", "content": "a\nb"},
        }
        view = _context_view(legacy)
        assert "elided" in view["action_input"]["content"]
        assert legacy["action_input"]["content"] == "a\nb"  # original intact


# ── end-to-end through ContextManager (faithful storage) ─────────────


class TestThroughContextManager:
    def test_history_keeps_full_body_even_when_view_elides(self, tmp_path, monkeypatch):
        monkeypatch.setitem(TOOLS, "write_file", _ElidingTool())
        ctx = ContextManager(session_dir=tmp_path, wire_format=get_wire("md_array"))
        ctx.add(_assistant_write("FULL\nBODY\nHERE"))
        # on-disk history is faithful (full body), regardless of context view
        last = [
            json.loads(line)
            for line in ctx.history_path.read_text().splitlines()
            if line.strip()
        ][-1]
        assert last["ops"][0]["action_input"]["content"] == "FULL\nBODY\nHERE"
