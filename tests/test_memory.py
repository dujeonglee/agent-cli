"""Session memory store + tool (agent_cli/memory.py, tools/memory_tool.py).

An LLM-curated, compaction-immune store: failures/discoveries/decisions/notes
persist in <session_dir>/memory.jsonl (surviving --resume), surface as a compact
always-on `## Session Memory` index, with full detail pulled on demand.
"""

from __future__ import annotations

import pytest

from agent_cli import memory
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

    def test_list_filters_by_type_and_tag(self, tmp_path):
        memory.add(tmp_path, type="failure", summary="f", tags=["build"])
        memory.add(tmp_path, type="discovery", summary="d", tags=["build", "probe"])
        memory.add(tmp_path, type="discovery", summary="d2", tags=["other"])
        assert len(memory.list_entries(tmp_path, type="discovery")) == 2
        assert len(memory.list_entries(tmp_path, tag="build")) == 2
        assert len(memory.list_entries(tmp_path, type="failure", tag="build")) == 1

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
        assert "5 older entries hidden" in idx
        assert idx.count("[note]") == memory._INDEX_CAP  # only the cap shown

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
        return MemoryTool()._run(args, session_dir=session_dir)

    def test_add_then_get(self, tmp_path):
        r = self._run(
            {"mode": "add", "type": "failure", "summary": "빌드 깨짐", "detail": "why"},
            tmp_path,
        )
        assert r.success and "#1" in r.output
        g = self._run({"mode": "get", "id": 1}, tmp_path)
        assert g.success and "why" in g.output

    def test_list(self, tmp_path):
        self._run({"mode": "add", "type": "note", "summary": "a"}, tmp_path)
        self._run({"mode": "add", "type": "failure", "summary": "b"}, tmp_path)
        r = self._run({"mode": "list", "type": "failure"}, tmp_path)
        assert r.success and "#2" in r.output and "#1" not in r.output

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
            supports_structured_output=True,
            supports_thinking=False,
            thinking_budget=0,
            supports_strict_schema=False,
        )

    def _sections(self, session_dir):
        from agent_cli.prompts.system_prompt import build_system_prompt_sections

        return dict(
            build_system_prompt_sections(
                self._caps(), ["read_file"], session_dir=str(session_dir)
            )
        )

    def test_section_absent_when_empty(self, tmp_path):
        assert "Session Memory" not in self._sections(tmp_path)

    def test_section_present_summaries_only(self, tmp_path):
        memory.add(
            tmp_path, type="failure", summary="빌드 깨짐", detail="SECRET DETAIL"
        )
        d = self._sections(tmp_path)
        assert "Session Memory" in d
        assert "빌드 깨짐" in d["Session Memory"]
        # detail must NOT leak into the always-on system prompt (index = summaries)
        assert "SECRET DETAIL" not in d["Session Memory"]
