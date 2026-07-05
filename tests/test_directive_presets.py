"""Per-axis DIRECTIVE preset library (agent_cli/directive_presets.py).

User-saved directive fragments stored under
~/.agent-cli/directive-presets/<axis>/ so any room can load them. Tests point
the store at a tmp dir (never the real home).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_cli import directive_presets as dp


@pytest.fixture(autouse=True)
def _tmp_presets(tmp_path, monkeypatch):
    root = tmp_path / "directive-presets"
    monkeypatch.setattr(dp, "_presets_root", lambda: root)
    # Isolate the user-store tests from the shipped built-ins (point them at an
    # empty dir); the built-in tests below re-point ``_BUILTIN_ROOT`` explicitly.
    monkeypatch.setattr(dp, "_BUILTIN_ROOT", tmp_path / "builtin-empty")
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


class TestBuiltinPresets:
    """Built-ins merge in read-only; a same-name user preset shadows them."""

    @pytest.fixture
    def builtin(self, tmp_path, monkeypatch):
        root = tmp_path / "builtin"
        (root / "persona").mkdir(parents=True)
        (root / "persona" / "간결한 전문가.md").write_text(
            "- 톤: 간결", encoding="utf-8"
        )
        monkeypatch.setattr(dp, "_BUILTIN_ROOT", root)
        return root

    def test_builtin_listed_as_source_builtin(self, builtin):
        assert dp.list_presets("persona") == [
            {"id": "간결한 전문가", "label": "간결한 전문가", "source": "builtin"}
        ]

    def test_builtin_loads(self, builtin):
        assert dp.load("persona", "간결한 전문가") == "- 톤: 간결"

    def test_user_shadows_builtin(self, builtin):
        dp.save("persona", "간결한 전문가", "USER-OVERRIDE")
        assert dp.load("persona", "간결한 전문가") == "USER-OVERRIDE"
        # one merged entry, now owned by the user
        assert {p["id"]: p["source"] for p in dp.list_presets("persona")} == {
            "간결한 전문가": "user"
        }

    def test_builtin_not_deletable(self, builtin):
        assert dp.delete("persona", "간결한 전문가") is False  # read-only
        assert dp.load("persona", "간결한 전문가") == "- 톤: 간결"  # still there

    def test_user_and_builtin_merge_sorted(self, builtin):
        dp.save("persona", "가나다", "u")
        ids = [p["id"] for p in dp.list_presets("persona")]
        assert ids == sorted(ids)
        assert set(ids) == {"가나다", "간결한 전문가"}


class TestShippedBuiltins:
    """The actual .md files bundled in the package load and are well-formed
    (guards packaging + the persona-headingless / task-heading contract)."""

    @pytest.fixture(autouse=True)
    def _real_builtin(self, monkeypatch):
        real = Path(dp.__file__).resolve().parent / "directive_presets_builtin"
        monkeypatch.setattr(dp, "_BUILTIN_ROOT", real)

    def test_persona_builtins_headingless(self):
        got = {p["id"]: p for p in dp.list_presets("persona")}
        assert {"간결한 전문가", "친근한 페어 프로그래머"} <= set(got)
        assert all(p["source"] == "builtin" for p in got.values())
        # persona body is heading-less (_zone_set prepends ## 페르소나)
        body = dp.load("persona", "간결한 전문가")
        assert body and not body.lstrip().startswith("## 페르소나")

    def test_task_builtins_have_heading_and_memory_principle(self):
        got = {p["id"] for p in dp.list_presets("task")}
        assert {"TDD 개발자", "코드 리뷰어", "로그 데이터 분석가"} <= got
        for tid in ("TDD 개발자", "코드 리뷰어", "로그 데이터 분석가"):
            body = dp.load("task", tid)
            assert body.lstrip().startswith("## 업무")
            assert "memory" in body  # 메모리 활용 원칙이 들어 있어야 한다
            # domain-neutral labels — no coding-specific shoehorning
            assert "빌드·테스트·정적분석 규율" not in body
            assert "검증·품질 규율" in body

    def test_learned_has_no_builtins(self):
        assert dp.list_presets("learned") == []
