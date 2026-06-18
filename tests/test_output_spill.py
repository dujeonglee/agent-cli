"""Oversized tool-output spill: a tool observation larger than
``_SPILL_THRESHOLD_TOKENS`` is stored as a chunked spill record
(``content = {"spill": True, "output": [guide, chunk1, ...]}``); only the
guide (``output[0]``) enters the context/cache/summary, the full content is
preserved in history.jsonl, and chunks are retrievable via read_context's
``json_extract``.

Regression target: a single >window tool output (a ``find`` / ``code_index``
dump) used to (a) inflate the cache token count to >800K, (b) make
compaction evict the entire dynamic set → ``tokens_after=0`` (cache emptied,
newest message lost), and (c) push the summariser's own call over the
window. Spilling at ``ctx.add`` removes all three.
"""

import json

import pytest

from agent_cli.context import manager as M
from agent_cli.context.manager import (
    ContextManager,
    _classify_record,
    _estimate_message_tokens,
    _maybe_spill,
    _spill_view,
    _to_summary_text,
    estimate_tokens,
)


@pytest.fixture
def small_spill(monkeypatch):
    """Shrink the thresholds so modest strings trigger spill (fast tests)."""
    monkeypatch.setattr(M, "_SPILL_THRESHOLD_TOKENS", 50)
    monkeypatch.setattr(M, "_SPILL_CHUNK_TOKENS", 50)


def _big_output(lines: int = 600) -> str:
    return "\n".join(
        f"/Users/x/workspace/agent-cli/pkg/file_{i:04}.py" for i in range(lines)
    )


def _obs(content, tool="shell"):
    return {"role": "user", "tool": tool, "success": True, "content": content}


# ── _spill_view ────────────────────────────────────


class TestSpillView:
    def test_spill_dict_returns_guide(self):
        c = {"spill": True, "output": ["GUIDE", "c1", "c2"]}
        assert _spill_view(c) == "GUIDE"

    def test_plain_string_unchanged(self):
        assert _spill_view("hello") == "hello"

    def test_non_spill_dict_unchanged(self):
        d = {"foo": "bar"}
        assert _spill_view(d) == d

    def test_empty_output_safe(self):
        assert _spill_view({"spill": True, "output": []}) == ""


# ── _maybe_spill ───────────────────────────────────


class TestMaybeSpill:
    def test_small_observation_unchanged(self, small_spill):
        msg = _obs("tiny")
        assert _maybe_spill(msg, 1) is msg or _maybe_spill(msg, 1)["content"] == "tiny"

    def test_large_observation_becomes_spill(self, small_spill):
        full = _big_output()
        out = _maybe_spill(_obs(full), 7)
        c = out["content"]
        assert isinstance(c, dict) and c["spill"] is True
        assert len(c["output"]) >= 2  # guide + >=1 chunk
        # other fields preserved
        assert out["tool"] == "shell" and out["success"] is True

    def test_chunks_rejoin_to_original_no_loss(self, small_spill):
        full = _big_output()
        out = _maybe_spill(_obs(full), 7)
        chunks = out["content"]["output"][1:]
        assert "".join(chunks) == full

    def test_guide_references_turn_and_retrieval(self, small_spill):
        out = _maybe_spill(_obs(_big_output()), 14)
        guide = out["content"]["output"][0]
        assert "read_context" in guide
        assert "turn=14" in guide
        assert "json_extract" in guide

    def test_non_tool_message_never_spills(self, small_spill):
        # assistant / plain-user (no "tool" key) stay untouched even if large
        big = _big_output()
        assistant = {"role": "assistant", "content": big}
        user_chat = {"role": "user", "content": big}
        assert _maybe_spill(assistant, 1) is assistant
        assert _maybe_spill(user_chat, 1) is user_chat

    def test_non_string_content_unchanged(self, small_spill):
        already = _obs({"spill": True, "output": ["g", "c"]})
        assert _maybe_spill(already, 1) is already


# ── token accounting ───────────────────────────────


class TestEstimate:
    def test_spilled_record_counts_guide_only(self, small_spill):
        full = _big_output()
        full_tokens = estimate_tokens(full)
        spilled = _maybe_spill(_obs(full), 3)
        est = _estimate_message_tokens(spilled)
        guide = spilled["content"]["output"][0]
        # estimate ≈ guide size, NOT the full multi-chunk content
        assert est < full_tokens // 2
        assert est >= estimate_tokens(guide)


# ── classify (read_context text surface) ───────────


class TestClassify:
    def test_spill_record_classifies_as_observation_with_guide_text(self, small_spill):
        spilled = _maybe_spill(_obs(_big_output(), tool="code_index"), 5)
        kind, tools, text = _classify_record(spilled)
        assert kind == "observation"
        assert tools == ["code_index"]
        assert text == spilled["content"]["output"][0]  # guide, not chunks


# ── summary transcript ─────────────────────────────


class TestSummaryText:
    def test_summary_uses_guide_not_chunks(self, small_spill):
        spilled = _maybe_spill(_obs(_big_output()), 2)
        line = _to_summary_text(spilled)
        # the excerpt comes from the guide (bounded), never the raw dump
        assert "[shell]" in line
        full = "\n".join(spilled["content"]["output"][1:])
        assert full[:300] not in line


# ── ctx.add integration ────────────────────────────


class TestCtxAddIntegration:
    def _ctx(self, tmp_path):
        ctx = ContextManager(session_dir=tmp_path / "s", max_context_tokens=1_000_000)
        ctx.set_turn(4)
        return ctx

    def test_huge_observation_does_not_inflate_cache(self, tmp_path, small_spill):
        ctx = self._ctx(tmp_path)
        full = _big_output()
        ctx.add(_obs(full))
        # cache token count reflects the guide, not the full dump
        assert ctx.get_estimated_tokens() < estimate_tokens(full) // 2

    def test_history_preserves_full_chunks(self, tmp_path, small_spill):
        ctx = self._ctx(tmp_path)
        full = _big_output()
        ctx.add(_obs(full))
        last = [
            json.loads(line)
            for line in ctx.history_path.read_text().splitlines()
            if line.strip()
        ][-1]
        c = last["content"]
        assert c["spill"] is True
        assert "".join(c["output"][1:]) == full  # no loss on disk

    def test_get_messages_renders_guide_only(self, tmp_path, small_spill):
        ctx = self._ctx(tmp_path)
        full = _big_output()
        ctx.add(_obs(full))
        msgs = ctx.get_messages()
        blob = json.dumps(msgs, ensure_ascii=False)
        # the observation message must carry the guide, not the chunks
        assert "read_context" in blob  # guide present
        assert full[:300] not in blob  # raw dump absent

    def test_no_zero_tok_emptying_on_dominant_message(self, tmp_path, small_spill):
        """The reported bug: the token-dominant newest message made compaction
        evict everything → cache emptied to 0. With spill, the dump never
        dominates the cache, so this can't happen."""
        ctx = self._ctx(tmp_path)
        ctx.add(_obs("small one"))
        ctx.add({"role": "assistant", "content": "ok"})
        ctx.add(_obs(_big_output()))  # huge newest
        # cache stays modest; the huge content is not counted raw
        assert ctx.get_estimated_tokens() < 5_000


# ── resume ─────────────────────────────────────────


class TestResume:
    def test_resume_estimates_guide_only(self, tmp_path, small_spill):
        sd = tmp_path / "s"
        ctx = ContextManager(session_dir=sd, max_context_tokens=1_000_000)
        ctx.set_turn(2)
        full = _big_output()
        ctx.add(_obs(full))
        # fresh resume from the same history
        ctx2 = ContextManager(session_dir=sd, max_context_tokens=1_000_000, resume=True)
        assert ctx2.get_estimated_tokens() < estimate_tokens(full) // 2
        blob = json.dumps(ctx2.get_messages(), ensure_ascii=False)
        assert full[:300] not in blob


# ── read_context retrieval ─────────────────────────


class TestReadContextRetrieval:
    """read_context exposes a JSON ``content`` column so a spilled chunk is
    retrievable with ``json_extract`` — and the default ``text`` column shows
    the guide, not the raw dump."""

    def _session(self, tmp_path):
        sessions = tmp_path / ".agent-cli" / "sessions"
        sdir = sessions / "1700000000"
        sdir.mkdir(parents=True)
        spill = {
            "role": "user",
            "tool": "shell",
            "success": True,
            "content": {
                "spill": True,
                "output": ["GUIDE: read_context turn=3", "CHUNK_ONE\n", "CHUNK_TWO\n"],
            },
            "kind": "observation",
            "turn": 3,
            "tools": "shell",
            "files": "",
            "text": "GUIDE: read_context turn=3",
        }
        (sdir / "history.jsonl").write_text(
            json.dumps(spill, ensure_ascii=False) + "\n"
        )
        return sdir

    def test_json_extract_returns_single_chunk(self, tmp_path):
        from agent_cli.tools.context import tool_read_context

        sdir = self._session(tmp_path)
        res = tool_read_context(
            {
                "query": (
                    "SELECT json_extract(content, '$.output[1]') "
                    "FROM history WHERE turn=3 AND json_extract(content,'$.spill')=1"
                )
            },
            session_dir=sdir,
        )
        assert res.success, res.error
        assert "CHUNK_ONE" in res.output
        assert "CHUNK_TWO" not in res.output  # only the requested chunk

    def test_text_column_shows_guide_not_chunks(self, tmp_path):
        from agent_cli.tools.context import tool_read_context

        sdir = self._session(tmp_path)
        res = tool_read_context(
            {"query": "SELECT text FROM history WHERE turn=3"},
            session_dir=sdir,
        )
        assert res.success, res.error
        assert "GUIDE" in res.output
        assert "CHUNK_ONE" not in res.output


# ── web resume render ──────────────────────────────


class TestWebReplaySpill:
    """``replay_from_history`` (--resume) renders a spilled observation as the
    guide only — the raw chunks never hit the SSE card."""

    def test_replay_renders_guide_not_chunks(self):
        from agent_cli.render.web import WebConnection, WebRenderer

        class _Ctx:
            def get_raw_messages(self):
                return [
                    {
                        "role": "user",
                        "tool": "shell",
                        "success": True,
                        "content": {
                            "spill": True,
                            "output": [
                                "GUIDE-LINE read_context",
                                "RAWCHUNK1",
                                "RAWCHUNK2",
                            ],
                        },
                    }
                ]

        r = WebRenderer()
        conn = WebConnection(id="c1")
        r.register_connection(conn)
        r.replay_from_history(_Ctx())
        # drain queued events, find the observation
        seen = []
        while not conn.queue.empty():
            seen.append(conn.queue.get_nowait())
        obs = [d for ev, d in seen if ev == "observation"]
        assert obs, "no observation event emitted"
        blob = json.dumps(obs, ensure_ascii=False)
        assert "GUIDE-LINE" in blob
        assert "RAWCHUNK1" not in blob
