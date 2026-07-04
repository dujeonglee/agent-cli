"""Named DIRECTIVE preset library (agent_cli/directive_presets.py).

User-saved DIRECTIVE.md bodies stored under ~/.agent-cli/directive-presets/ so
any room can load them. Tests point the store at a tmp dir (never the real home).
"""

from __future__ import annotations

import pytest

from agent_cli import directive_presets as dp


@pytest.fixture(autouse=True)
def _tmp_presets(tmp_path, monkeypatch):
    root = tmp_path / "directive-presets"
    monkeypatch.setattr(dp, "_presets_dir", lambda: root)
    return root


class TestSaveLoadList:
    def test_save_then_load_roundtrip(self):
        pid = dp.save("라이브러리 작성", "## 학습된 지침\n- 항상 테스트 먼저")
        assert dp.load(pid) == "## 학습된 지침\n- 항상 테스트 먼저"

    def test_korean_name_preserved(self):
        pid = dp.save("커널 드라이버", "body")
        labels = {p["id"]: p["label"] for p in dp.list_presets()}
        assert pid in labels and "커널 드라이버" in (pid + labels[pid])

    def test_list_empty_when_none(self):
        assert dp.list_presets() == []

    def test_list_sorted_and_user_source(self):
        dp.save("b-preset", "x")
        dp.save("a-preset", "y")
        got = dp.list_presets()
        assert [p["id"] for p in got] == sorted(p["id"] for p in got)
        assert all(p["source"] == "user" for p in got)

    def test_save_overwrites_same_name(self):
        dp.save("dup", "v1")
        dp.save("dup", "v2")
        assert dp.load("dup") == "v2"
        assert len(dp.list_presets()) == 1

    def test_load_absent_is_none(self):
        assert dp.load("nope") is None


class TestDelete:
    def test_delete_removes(self):
        dp.save("gone", "x")
        assert dp.delete("gone") is True
        assert dp.load("gone") is None

    def test_delete_absent_false(self):
        assert dp.delete("nope") is False


class TestSafety:
    def test_traversal_name_rejected(self):
        for bad in ("../evil", "a/b", "..", ".", "", "  ", ".hidden"):
            with pytest.raises(ValueError):
                dp.save(bad, "x")

    def test_load_traversal_rejected(self, _tmp_presets):
        # a load id that tries to escape the presets dir must raise, not read.
        with pytest.raises(ValueError):
            dp.load("../../etc/passwd")

    def test_backslash_rejected(self):
        with pytest.raises(ValueError):
            dp.save("a\\b", "x")
