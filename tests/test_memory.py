"""Session memory store + tool (agent_cli/memory.py, tools/memory_tool.py).

An LLM-curated, compaction-immune store: failures/discoveries/decisions/notes
persist in <session_dir>/memory.jsonl (surviving --resume), surface as a compact
always-on `## Session Memory` index, with full detail pulled on demand.
"""

from __future__ import annotations

import pytest

from agent_cli import memory
from agent_cli.tools import RunContext
from agent_cli.tools.memory_tool import MemoryTool


@pytest.fixture(autouse=True)
def _reset_dirty():
    memory.consume_memory_reload()  # clear any leaked dirty flag
    yield
    memory.consume_memory_reload()


class TestStore:
    def test_add_assigns_monotonic_ids(self, tmp_path):
        a = memory.add(tmp_path, type="failure", summary="빌드 깨짐")
        b = memory.add(tmp_path, type="discovery", summary="probe 는 A 경로")
        assert a["id"] == 1 and b["id"] == 2
        assert a["type"] == "failure" and a["summary"] == "빌드 깨짐"
        assert a["ts"]  # timestamp stamped

    def test_add_persists_and_reloads(self, tmp_path):
        memory.add(tmp_path, type="note", summary="s", detail="d", tags=["x", "y"])
        # fresh load (simulates --resume: no in-memory cache)
        loaded = memory.load(tmp_path)
        assert len(loaded) == 1
        assert loaded[0]["detail"] == "d" and loaded[0]["tags"] == ["x", "y"]

    def test_add_rejects_unknown_type(self, tmp_path):
        with pytest.raises(memory.MemoryError):
            memory.add(tmp_path, type="bogus", summary="s")

    def test_add_requires_summary(self, tmp_path):
        with pytest.raises(memory.MemoryError):
            memory.add(tmp_path, type="note", summary="  ")

    def test_get(self, tmp_path):
        memory.add(tmp_path, type="note", summary="s", detail="body")
        assert memory.get(tmp_path, 1)["detail"] == "body"
        assert memory.get(tmp_path, 99) is None

    def test_update_replaces_only_given_fields(self, tmp_path):
        memory.add(tmp_path, type="failure", summary="old", detail="keep")
        memory.update(tmp_path, 1, summary="new")
        e = memory.get(tmp_path, 1)
        assert e["summary"] == "new" and e["detail"] == "keep"  # detail preserved
        assert e["type"] == "failure"

    def test_update_can_change_type_and_tags(self, tmp_path):
        memory.add(tmp_path, type="note", summary="s")
        memory.update(tmp_path, 1, type="decision", tags=["a"])
        e = memory.get(tmp_path, 1)
        assert e["type"] == "decision" and e["tags"] == ["a"]

    def test_update_bad_type_and_missing_id(self, tmp_path):
        memory.add(tmp_path, type="note", summary="s")
        with pytest.raises(memory.MemoryError):
            memory.update(tmp_path, 1, type="bogus")
        with pytest.raises(memory.MemoryError):
            memory.update(tmp_path, 99, summary="x")

    def test_delete_and_ids_not_reused(self, tmp_path):
        memory.add(tmp_path, type="note", summary="a")
        memory.add(tmp_path, type="note", summary="b")
        memory.delete(tmp_path, 1)
        assert [e["id"] for e in memory.load(tmp_path)] == [2]
        # next add is max+1 = 3, NOT reusing the freed id 1
        assert memory.add(tmp_path, type="note", summary="c")["id"] == 3

    def test_delete_missing_id(self, tmp_path):
        with pytest.raises(memory.MemoryError):
            memory.delete(tmp_path, 5)

    def test_add_records_the_turn_when_given(self, tmp_path):
        """v10.81.0: 항목의 `turn` 은 `history` 의 `turn = N` 으로 원문에 닿는 손잡이."""
        memory.add(tmp_path, type="failure", summary="f", turn=12)
        memory.add(tmp_path, type="note", summary="n")  # 루프 밖 — turn 없음
        memory.add(tmp_path, type="note", summary="b", turn=True)  # bool 은 turn 아님
        a, b, c = memory.load(tmp_path)
        assert a["turn"] == 12 and "turn" not in b and "turn" not in c
        assert not hasattr(memory, "list_entries"), "`list` 는 v10.81.0 에 삭제됐다"

    def test_load_skips_corrupt_line(self, tmp_path):
        memory.add(tmp_path, type="note", summary="ok")
        p = tmp_path / "memory.jsonl"
        p.write_text(p.read_text() + "{ this is not json\n", encoding="utf-8")
        assert len(memory.load(tmp_path)) == 1  # corrupt line skipped, not fatal

    def test_load_none_session_is_empty(self):
        assert memory.load(None) == []


class TestIndexRender:
    def test_empty_is_blank(self, tmp_path):
        assert memory.render_index(tmp_path) == ""

    def test_index_has_heading_icons_and_summaries_only(self, tmp_path):
        memory.add(tmp_path, type="failure", summary="빌드 깨짐", detail="LONG DETAIL")
        memory.add(tmp_path, type="discovery", summary="probe 는 A")
        idx = memory.render_index(tmp_path)
        assert "## Session Memory (2)" in idx  # English scaffolding
        assert "⚠ #1 [failure] 빌드 깨짐" in idx
        assert "💡 #2 [discovery] probe 는 A" in idx
        assert "LONG DETAIL" not in idx  # detail excluded from the index

    def test_index_caps_and_notes_hidden(self, tmp_path):
        for i in range(memory._INDEX_CAP + 5):
            memory.add(tmp_path, type="note", summary=f"s{i}")
        idx = memory.render_index(tmp_path)
        assert (
            '(5 older entries not shown — ids below 6; `memory` with `mode` "get" '
            "still reaches them)" in idx
        )
        assert "list" not in idx
        assert idx.count("[note]") == memory._INDEX_CAP  # only the cap shown

    def test_index_line_shows_the_turn(self, tmp_path):
        memory.add(tmp_path, type="failure", summary="빌드 깨짐", turn=7)
        memory.add(tmp_path, type="note", summary="no turn")
        idx = memory.render_index(tmp_path)
        assert "⚠ #1 [failure] 빌드 깨짐 (turn 7)" in idx
        assert "📝 #2 [note] no turn\n" in idx + "\n"
        assert (
            '"(turn N)" is the turn it was noted on, which `history` can look up' in idx
        )
        assert "(turn 7)" in memory.format_entry(memory.get(tmp_path, 1))

    def test_format_entry_includes_detail(self, tmp_path):
        memory.add(tmp_path, type="decision", summary="http 사설만", detail="근거 …")
        out = memory.format_entry(memory.get(tmp_path, 1))
        assert "🔀 #1 [decision] http 사설만" in out and "근거 …" in out


class TestDirtyFlag:
    def test_add_marks_dirty(self, tmp_path):
        memory.consume_memory_reload()  # clear
        memory.add(tmp_path, type="note", summary="s")
        assert memory.consume_memory_reload() is True
        assert memory.consume_memory_reload() is False  # consumed once

    def test_update_and_delete_mark_dirty(self, tmp_path):
        memory.add(tmp_path, type="note", summary="s")
        memory.consume_memory_reload()
        memory.update(tmp_path, 1, summary="s2")
        assert memory.consume_memory_reload() is True
        memory.delete(tmp_path, 1)
        assert memory.consume_memory_reload() is True


class TestTool:
    def _run(self, args, session_dir):
        return MemoryTool()._run(args, ctx=RunContext(session_dir=session_dir))

    def test_add_then_get(self, tmp_path):
        r = self._run(
            {"mode": "add", "type": "failure", "summary": "빌드 깨짐", "detail": "why"},
            tmp_path,
        )
        assert r.success and "#1" in r.output
        g = self._run({"mode": "get", "id": 1}, tmp_path)
        assert g.success and "why" in g.output

    def test_list_is_gone(self, tmp_path):
        """사용자 결정(2026-10-30): 벤치 716 trial 에서 0회, 상시 색인과 중복."""
        from agent_cli.tools.memory_tool import MODES

        assert "list" not in MODES
        r = self._run({"mode": "list"}, tmp_path)
        assert not r.success and 'unknown `mode` "list"' in r.error
        assert MemoryTool().validate({"mode": "list"}) == (
            'unknown `mode` "list"; use "add" | "get" | "update" | "delete"'
        )
        assert "tag" not in MemoryTool.parameters["properties"]

    def test_mode_less_call_with_a_summary_is_an_add(self, tmp_path):
        """벤치의 add 16회 중 3회가 `type`·`summary`·`detail` 만 보내 "invalid mode: ''"
        로 거절됐다 — 모호함이 없는 모양은 구제한다(구제 원칙). 스키마의 `required` 는
        그대로라 문법은 여전히 `mode` 를 강제한다."""
        from agent_cli.tools.registry import validate_tool_input

        tool = MemoryTool()
        args = {"type": "failure", "summary": "no mode", "detail": "d"}
        assert tool.validate(args) is None
        assert tool.schema_gaps(args) == ([], [])
        ok, err, _ = validate_tool_input("memory", args)
        assert ok, err
        r = tool.run(args, ctx=RunContext(session_dir=tmp_path))
        assert r.success and r.output.startswith("Saved memory #1 [failure]: no mode")
        assert tool.validate({"type": "failure"}) == (
            '`mode` is required unless a `summary` is given; use "add" | "get" | '
            '"update" | "delete"'
        )
        assert tool.schema_gaps({"type": "failure"}) == (["mode"], [])
        assert MemoryTool.parameters["required"] == ["mode"]

    def test_add_takes_the_turn_from_the_run_context(self, tmp_path):
        tool = MemoryTool()
        ctx = RunContext(session_dir=tmp_path, turn_now=lambda: 9)
        assert tool.run(
            {"mode": "add", "type": "note", "summary": "s"}, ctx=ctx
        ).success
        assert memory.get(tmp_path, 1)["turn"] == 9
        assert "(turn 9)" in tool.run({"mode": "get", "id": 1}, ctx=ctx).output

    def test_update_and_delete(self, tmp_path):
        self._run({"mode": "add", "type": "note", "summary": "old"}, tmp_path)
        assert self._run(
            {"mode": "update", "id": 1, "summary": "new"}, tmp_path
        ).success
        assert "new" in self._run({"mode": "get", "id": 1}, tmp_path).output
        assert self._run({"mode": "delete", "id": 1}, tmp_path).success
        assert not self._run({"mode": "get", "id": 1}, tmp_path).success

    def test_bad_mode(self, tmp_path):
        r = self._run({"mode": "frobnicate"}, tmp_path)
        assert not r.success and "mode" in (r.error or "")

    def test_description_has_no_caps_emphasis_or_placement_note(self):
        d = MemoryTool.description
        for bad in ("FAILURES", "DISCOVERIES", "DECISIONS", "NOTES", "END", "list"):
            assert bad not in d, bad
        assert "session-state" not in d and "at the end of your context" not in d
        assert '"## Session Memory"' in d

    def test_bad_type_is_clean_failure(self, tmp_path):
        r = self._run({"mode": "add", "type": "x", "summary": "s"}, tmp_path)
        assert not r.success and "type" in (r.error or "")

    def test_no_session(self):
        r = self._run({"mode": "add", "type": "note", "summary": "s"}, None)
        assert not r.success and "session" in (r.error or "").lower()

    def test_registered_and_flat_native(self):
        from agent_cli.tools.registry import TOOLS

        assert "memory" in TOOLS
        t = TOOLS["memory"]
        assert t.wrap_single_op({"mode": "add"}) == {"mode": "add"}  # identity


class TestSystemPromptSection:
    def _caps(self):
        from agent_cli.providers.capabilities import ModelCapabilities

        return ModelCapabilities(
            context_window=32768,
            max_output_tokens=4096,
            supports_thinking=False,
        )

    def _sections(self, session_dir):
        from agent_cli.prompts.system_prompt import build_system_prompt_sections

        return dict(
            build_system_prompt_sections(
                self._caps(), ["read_file"], session_dir=str(session_dir)
            )
        )

    def test_never_a_system_prompt_section(self, tmp_path):
        """v8.46.0: the index moved OUT of the system prompt. It changes on
        every ``memory add``, and a system-prompt edit invalidates the provider
        KV prefix for the whole conversation after it — one note cost a full
        re-prefill. It now rides in the per-turn tail block, where a rewrite is
        free. Empty or not, it must never come back as a system section."""
        assert "Session Memory" not in self._sections(tmp_path)
        memory.add(tmp_path, type="failure", summary="빌드 깨짐", detail="D")
        assert "Session Memory" not in self._sections(tmp_path)

    def test_index_is_summaries_only(self, tmp_path):
        memory.add(
            tmp_path, type="failure", summary="빌드 깨짐", detail="SECRET DETAIL"
        )
        idx = memory.render_index(tmp_path)
        assert "빌드 깨짐" in idx
        # detail must NOT leak into the always-on index (pulled via mode=get)
        assert "SECRET DETAIL" not in idx

    def test_index_reaches_the_llm_through_the_tail_block(self, tmp_path):
        """The move must not lose the content — it has to actually arrive."""
        from agent_cli.prompts.session_state import build_session_state

        memory.add(tmp_path, type="failure", summary="빌드 깨짐", detail="D")
        block = build_session_state(memory=memory.render_index(tmp_path))
        assert "빌드 깨짐" in block
        assert "Session Memory" in block
