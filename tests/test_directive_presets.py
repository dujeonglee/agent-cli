"""Per-axis DIRECTIVE preset library (agent_cli/directive_presets.py).

User-saved directive fragments stored under
~/.agent-cli/directive-presets/<axis>/ so any room can load them. Tests point
the store at a tmp dir (never the real home).
"""

from __future__ import annotations

import pytest

from agent_cli import directive_presets as dp


@pytest.fixture(autouse=True)
def _tmp_presets(tmp_path, monkeypatch):
    root = tmp_path / "directive-presets"
    monkeypatch.setattr(dp, "_presets_root", lambda: root)
    return root


class TestSaveLoadList:
    def test_save_then_load_roundtrip(self):
        pid = dp.save("persona", "라이브러리 작성", "## 페르소나\n- 도도")
        assert dp.load("persona", pid) == "## 페르소나\n- 도도"

    def test_korean_name_preserved(self):
        pid = dp.save("task", "커널 드라이버", "body")
        labels = {p["id"]: p["label"] for p in dp.list_presets("task")}
        assert pid in labels and "커널 드라이버" in (pid + labels[pid])

    def test_list_empty_when_none(self):
        assert dp.list_presets("learned") == []

    def test_list_sorted_and_user_source(self):
        dp.save("learned", "b-preset", "x")
        dp.save("learned", "a-preset", "y")
        got = dp.list_presets("learned")
        assert [p["id"] for p in got] == sorted(p["id"] for p in got)
        assert all(p["source"] == "user" for p in got)

    def test_save_overwrites_same_name(self):
        dp.save("persona", "dup", "v1")
        dp.save("persona", "dup", "v2")
        assert dp.load("persona", "dup") == "v2"
        assert len(dp.list_presets("persona")) == 1

    def test_load_absent_is_none(self):
        assert dp.load("task", "nope") is None


class TestAxisIsolation:
    def test_same_name_different_axes_are_independent(self):
        dp.save("persona", "shared", "PERSONA-BODY")
        dp.save("task", "shared", "TASK-BODY")
        dp.save("learned", "shared", "LEARNED-BODY")
        assert dp.load("persona", "shared") == "PERSONA-BODY"
        assert dp.load("task", "shared") == "TASK-BODY"
        assert dp.load("learned", "shared") == "LEARNED-BODY"
        # a per-axis list only shows its own axis
        assert [p["id"] for p in dp.list_presets("persona")] == ["shared"]

    def test_delete_one_axis_leaves_others(self):
        dp.save("persona", "x", "p")
        dp.save("task", "x", "t")
        assert dp.delete("persona", "x") is True
        assert dp.load("persona", "x") is None
        assert dp.load("task", "x") == "t"  # other axis untouched

    def test_unknown_axis_rejected(self):
        for op in (
            lambda: dp.save("bogus", "n", "c"),
            lambda: dp.load("bogus", "n"),
            lambda: dp.list_presets("bogus"),
            lambda: dp.delete("bogus", "n"),
        ):
            with pytest.raises(ValueError):
                op()


class TestDelete:
    def test_delete_removes(self):
        dp.save("learned", "gone", "x")
        assert dp.delete("learned", "gone") is True
        assert dp.load("learned", "gone") is None

    def test_delete_absent_false(self):
        assert dp.delete("learned", "nope") is False


class TestSafety:
    def test_traversal_name_rejected(self):
        for bad in ("../evil", "a/b", "..", ".", "", "  ", ".hidden"):
            with pytest.raises(ValueError):
                dp.save("persona", bad, "x")

    def test_load_traversal_rejected(self):
        with pytest.raises(ValueError):
            dp.load("persona", "../../etc/passwd")

    def test_backslash_rejected(self):
        with pytest.raises(ValueError):
            dp.save("task", "a\\b", "x")
