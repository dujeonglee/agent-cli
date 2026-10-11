"""`snapshot` 네이티브 도구 (v10.36.0) — 모델이 망친 입력을 되돌리는 길.

고정하는 계약: 스키마(mode enum·path minLength) · validate 거부 문구 · 저장소 없음/꺼짐
에러 · list 출력 · restore(기본 = 지금과 다른 최근 판 / `at` 지정) · 디렉터리 복구 ·
레지스트리 등록과 자리(schedule 뒤) · 도구 왕복 계약(wrap_single_op) · 인벤토리 노출.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_cli.snapshot import GitSnapshot, get_snapshot, git_available, reset_snapshot
from agent_cli.tools.registry import _BUILTIN_ORDER, TOOLS
from agent_cli.tools.snapshot_tool import MODES, SnapshotTool

pytestmark = pytest.mark.skipif(not git_available()[0], reason="git not installed")


@pytest.fixture(autouse=True)
def _reset():
    reset_snapshot()
    yield
    reset_snapshot()


@pytest.fixture
def ws(tmp_path, monkeypatch):
    w = tmp_path / "ws"
    w.mkdir()
    (w / "main.db").write_bytes(b"base")
    (w / "main.db-wal").write_bytes(b"wal-bytes")
    (w / "docs").mkdir()
    (w / "docs" / "a.txt").write_text("a\n")
    (w / "docs" / "b.txt").write_text("b\n")
    monkeypatch.chdir(w)
    monkeypatch.setenv("AGENT_CLI_SNAPSHOT_MIN_INTERVAL_S", "0")
    return w


def _store(ws) -> GitSnapshot:
    s = get_snapshot(ws, create=True)
    assert s is not None and s.enabled, getattr(s, "reason", None)
    return s


class TestSchemaAndValidation:
    def test_registered_native_after_schedule(self):
        assert "snapshot" in TOOLS and isinstance(TOOLS["snapshot"], SnapshotTool)
        order = list(_BUILTIN_ORDER)
        assert order.index("snapshot") == order.index("schedule") + 1

    def test_schema_shape(self):
        p = TOOLS["snapshot"].parameters
        assert p["required"] == ["mode"]
        assert p["properties"]["mode"]["enum"] == list(MODES) == ["restore", "list"]
        assert p["properties"]["path"]["minLength"] == 1
        assert set(p["properties"]) == {"mode", "path", "at"}

    @pytest.mark.parametrize(
        ("args", "msg"),
        [
            ({}, "mode must be one of"),
            ({"mode": "delete"}, "mode must be one of"),
            ({"mode": "restore"}, "requires `path`"),
            ({"mode": "restore", "path": "  "}, "requires `path`"),
        ],
    )
    def test_validate_rejects_bad_shapes(self, args, msg):
        err = TOOLS["snapshot"].validate(args)
        assert err and msg in err

    def test_validate_accepts_good_shapes(self):
        t = TOOLS["snapshot"]
        assert t.validate({"mode": "list"}) is None
        assert t.validate({"mode": "restore", "path": "x"}) is None
        assert t.validate({"mode": "restore", "path": "x", "at": "abc"}) is None

    def test_summary_arg(self):
        t = TOOLS["snapshot"]
        assert (
            t.summary_arg({"mode": "restore", "path": "main.db-wal"})
            == "restore main.db-wal"
        )
        assert t.summary_arg({"mode": "list"}) == "list"

    def test_description_is_english_and_names_the_trigger(self):
        d = TOOLS["snapshot"].description
        assert "damaged" in d and "restore" in d and "list" in d
        assert not any("가" <= ch <= "힣" for ch in d), "모델 문구는 영어"


class TestWithoutStore:
    def test_no_store_is_a_clear_error(self, ws):
        r = TOOLS["snapshot"].run({"mode": "list"})
        assert not r.success and "not available" in r.error

    def test_disabled_store_reports_its_reason(self, ws, monkeypatch):
        monkeypatch.setattr("agent_cli.snapshot.shutil.which", lambda _n: None)
        s = get_snapshot(ws, create=True)
        assert s is not None and not s.enabled
        r = TOOLS["snapshot"].run({"mode": "restore", "path": "main.db-wal"})
        assert not r.success and "git not found" in r.error


class TestListAndRestore:
    def test_list_newest_first(self, ws):
        s = _store(ws)
        (ws / "docs" / "a.txt").write_text("a2\n")
        s.checkpoint("before shell (turn 3)", force=True)
        r = TOOLS["snapshot"].run({"mode": "list"})
        assert r.success
        lines = r.output.splitlines()
        assert lines[0] == "Snapshots (newest first):"
        assert "before shell (turn 3)" in lines[1] and "session start" in lines[2]

    def test_list_on_an_empty_store(self, tmp_path, monkeypatch):
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.chdir(empty)
        _store(empty)  # 담을 파일이 없어 커밋 없음
        r = TOOLS["snapshot"].run({"mode": "list"})
        assert r.success and r.output == "No snapshots yet."

    def test_restore_a_deleted_file(self, ws):
        s = _store(ws)
        (ws / "main.db-wal").unlink()
        s.checkpoint("after damage", force=True)
        r = TOOLS["snapshot"].run({"mode": "restore", "path": "main.db-wal"})
        assert r.success and r.output.startswith("restored main.db-wal from ")
        assert (ws / "main.db-wal").read_bytes() == b"wal-bytes"

    def test_restore_prefers_the_version_that_differs_from_disk(self, ws):
        """망가진 판이 HEAD 로 커밋돼 있어도 그 전 판을 돌려준다."""
        s = _store(ws)
        (ws / "main.db-wal").write_bytes(b"corrupted")
        s.checkpoint("after damage", force=True)
        r = TOOLS["snapshot"].run({"mode": "restore", "path": "main.db-wal"})
        assert r.success and (ws / "main.db-wal").read_bytes() == b"wal-bytes"

    def test_restore_at_a_specific_snapshot(self, ws):
        s = _store(ws)
        first = s.head()
        (ws / "docs" / "a.txt").write_text("v2\n")
        s.checkpoint("v2", force=True)
        (ws / "docs" / "a.txt").write_text("v3\n")
        s.checkpoint("v3", force=True)
        r = TOOLS["snapshot"].run(
            {"mode": "restore", "path": "docs/a.txt", "at": first}
        )
        assert r.success and (ws / "docs" / "a.txt").read_text() == "a\n"

    def test_restore_a_directory(self, ws):
        s = _store(ws)
        import shutil

        shutil.rmtree(ws / "docs")
        s.checkpoint("after rm -rf", force=True)
        r = TOOLS["snapshot"].run({"mode": "restore", "path": "docs"})
        assert r.success, r.error
        assert (ws / "docs" / "a.txt").read_text() == "a\n"
        assert (ws / "docs" / "b.txt").read_text() == "b\n"

    def test_unknown_path_and_unknown_commit_are_errors(self, ws):
        _store(ws)
        r = TOOLS["snapshot"].run({"mode": "restore", "path": "nope.txt"})
        assert not r.success and "no snapshot" in r.error
        r = TOOLS["snapshot"].run(
            {"mode": "restore", "path": "main.db", "at": "deadbeef"}
        )
        assert not r.success

    def test_unchanged_file_has_nothing_to_restore(self, ws):
        _store(ws)
        r = TOOLS["snapshot"].run({"mode": "restore", "path": "main.db"})
        assert not r.success and "no snapshot holds a different version" in r.error


class TestWireAndPrompt:
    def test_single_op_wrapping_keeps_the_verdict(self):
        """도구 왕복 계약(test_tool_wire_contract 와 같은 축): flat op → wrap → 검증."""
        t = TOOLS["snapshot"]
        flat = {"mode": "restore", "path": "x"}
        assert t.validate(t.wrap_single_op(dict(flat))) == t.validate(flat)

    def test_appears_in_the_prompt_inventory(self):
        from agent_cli.prompts.inventory import build_inventory

        inv = build_inventory()
        d = next(iter(inv["dialects"].values()))
        names = [t["name"] for t in d["tools"]]
        assert "snapshot" in names
        if "functions" in d["scenarios"]["main"]:
            fns = [f["name"] for f in d["scenarios"]["main"]["functions"]]
            assert "snapshot" in fns

    def test_notice_points_at_the_tool_not_a_cli(self, ws):
        s = _store(ws)
        n = s.notice()
        assert "`snapshot` tool" in n and "agent-cli snapshot" not in n
        assert Path(".agent-cli/snapshot.git").exists()
