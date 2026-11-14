"""웹 Changes 패널의 기준점 (v10.137.0, docs/snapshot/DESIGN.md §7) — 실제 git 으로.

고정하는 계약: 목록은 "기준점 커밋 ↔ 지금 디스크" — 셸처럼 밖에서 바꾼 것도 오른다 ·
Clear 는 목록만 비우고 파일·HEAD·실제 색인·체크포인트 간격을 건드리지 않는다 · Undo 한
단계 · resume(같은 세션 id 로 init 두 번)에도 기준은 첫 시작 커밋 · diff 는 워크스페이스
밖 경로를 거절 · 스냅샷이 꺼지면 아무것도 없다.
"""

from __future__ import annotations

import time

import pytest

from agent_cli.snapshot import (
    BASELINE_PREV_REF,
    BASELINE_REF,
    GitSnapshot,
    git_available,
)

pytestmark = pytest.mark.skipif(not git_available()[0], reason="git not installed")


@pytest.fixture
def ws(tmp_path):
    w = tmp_path / "ws"
    w.mkdir()
    (w / "inventory.py").write_text("a\nb\nc\n")
    (w / "old.csv").write_text("x\ny\n")
    (w / "data").mkdir()
    (w / "data" / "schema.sql").write_text("create table t;\n")
    return w


def _store(ws, sid="s-1", **kw) -> GitSnapshot:
    s = GitSnapshot(ws, min_interval_s=0, **kw)
    assert s.init(sid), s.reason
    return s


def _by_path(changes):
    return {c["path"]: c for c in changes}


def _files(ws):
    """워크스페이스의 파일(스냅샷 저장소 ``.agent-cli`` 밖) — 경로 → 바이트."""
    return {
        str(p.relative_to(ws)): p.read_bytes()
        for p in ws.rglob("*")
        if p.is_file() and ".agent-cli" not in p.parts
    }


def _git_state(s: GitSnapshot):
    index = (s.git_dir / "index").read_bytes()
    return s.head(), index


def test_outside_edits_add_and_delete_show_up(ws):
    s = _store(ws)
    assert s.changes() == []
    # 셸처럼 밖에서 — 체크포인트 없이 디스크만 바뀐다
    (ws / "inventory.py").write_text("a\nB\nc\nd\n")
    (ws / "tests").mkdir()
    (ws / "tests" / "test_stock.py").write_text("1\n2\n3\n")
    (ws / "old.csv").unlink()
    (ws / "logo.bin").write_bytes(b"\x00\x01\x02")
    got = _by_path(s.changes())
    assert set(got) == {"inventory.py", "tests/test_stock.py", "old.csv", "logo.bin"}
    assert got["inventory.py"] == {
        "path": "inventory.py",
        "status": "M",
        "added": 2,
        "deleted": 1,
    }
    assert got["tests/test_stock.py"]["status"] == "A"
    assert got["tests/test_stock.py"]["added"] == 3
    assert got["old.csv"]["status"] == "D" and got["old.csv"]["deleted"] == 2
    assert got["logo.bin"]["added"] is None  # 바이너리
    # 목록을 세는 것은 디스크를 커밋하지 않는다
    assert s.log(5)[0][1] == "session start (s-1)"


def test_rename_is_delete_plus_add_and_excludes_hold(ws):
    s = _store(ws)
    (ws / "old.csv").rename(ws / "new.csv")
    (ws / "node_modules").mkdir()
    (ws / "node_modules" / "x.js").write_text("x")
    (ws / ".agent-cli" / "tmp").mkdir(parents=True, exist_ok=True)
    (ws / ".agent-cli" / "tmp" / "scratch.txt").write_text("x")
    got = _by_path(s.changes())
    assert {p: c["status"] for p, c in got.items()} == {"new.csv": "A", "old.csv": "D"}


def test_non_ascii_paths_are_not_quoted(ws):
    s = _store(ws)
    (ws / "보고서 초안.md").write_text("안녕\n")
    assert [c["path"] for c in s.changes()] == ["보고서 초안.md"]
    diff, err = s.change_diff("보고서 초안.md")
    assert not err and "+안녕" in diff


def test_clear_empties_the_list_and_touches_nothing_else(ws):
    s = _store(ws)
    (ws / "inventory.py").write_text("changed\n")
    s.checkpoint("before edit_file (turn 1)")
    (ws / "data" / "schema.sql").write_text("changed\n")
    s._last_checkpoint = time.monotonic() - 60
    stamp = s._last_checkpoint
    before = _git_state(s)
    files = _files(ws)
    assert len(s.changes()) == 2

    new = s.clear()
    assert new is not None and new[1] == "cleared"
    assert s.changes() == []
    assert s.baseline() == new
    # 파일·HEAD·실제 색인·체크포인트 간격 불변, 모델의 `list` 에 사람의 커밋이 없다
    assert _files(ws) == files
    assert _git_state(s) == before
    assert s._last_checkpoint == stamp
    assert all("baseline" not in label for _, label in s.log(20))
    # 직후 체크포인트는 건너뛰지 않는다(디스크가 HEAD 와 다르다)
    assert s.checkpoint("before shell (turn 2)") is not None
    # 다시 쌓인다 — Clear 뒤에 바뀐 것만
    (ws / "inventory.py").write_text("again\n")
    assert [c["path"] for c in s.changes()] == ["inventory.py"]


def test_undo_restores_the_previous_baseline_once(ws):
    s = _store(ws)
    start = s.baseline()
    assert start[1] == "session"
    (ws / "inventory.py").write_text("one\n")
    s.clear()
    assert s.changes() == []
    assert s.undo_clear() is True
    assert s.baseline() == start
    assert [c["path"] for c in s.changes()] == ["inventory.py"]
    assert s.undo_clear() is False  # 한 단계만
    assert s.baseline() == start
    # Clear 두 번 → Undo 는 첫 Clear 로
    first = s.clear()
    (ws / "old.csv").write_text("two\n")
    s.clear()
    assert s.undo_clear() is True
    assert s.baseline() == first
    assert [c["path"] for c in s.changes()] == ["old.csv"]
    assert s.undo_clear() is False
    assert s._rev(BASELINE_PREV_REF) is None


def test_resume_keeps_the_first_session_start(ws):
    s = _store(ws, sid="s-7")
    first = s.baseline()
    time.sleep(1.1)  # 커밋 시각이 초 단위라 두 시작 커밋을 가른다
    (ws / "inventory.py").write_text("between runs\n")
    again = _store(ws, sid="s-7")  # resume — 같은 세션 id 로 새 프로세스
    assert again.log(1)[0][1] == "session start (s-7)"
    assert again.head() != first[0]
    assert again.baseline() == first
    assert [c["path"] for c in again.changes()] == ["inventory.py"]
    # 다른 세션은 자기 시작부터
    other = _store(ws, sid="s-8")
    assert other.baseline()[0] == other.head()
    assert other.changes() == []


def test_clear_survives_a_new_process(ws):
    s = _store(ws, sid="s-1")
    (ws / "inventory.py").write_text("x\n")
    cleared = s.clear()
    again = _store(ws, sid="s-1")
    assert again.baseline() == cleared
    assert again.changes() == []


def test_change_diff_against_the_baseline_and_path_guard(ws):
    s = _store(ws)
    (ws / "inventory.py").write_text("a\nB\nc\n")
    diff, err = s.change_diff("inventory.py")
    assert err == ""
    assert "--- a/inventory.py" in diff and "-b" in diff and "+B" in diff
    diff2, _ = s.change_diff(str(ws / "inventory.py"))  # 절대 경로도 안이면 받는다
    assert diff2 == diff
    for bad in ("../outside.txt", "/etc/passwd", ""):
        d, e = s.change_diff(bad)
        assert d is None and e
    # 바뀌지 않은 파일은 빈 diff
    assert s.change_diff("old.csv") == ("", "")


def test_change_diff_is_capped(ws):
    s = _store(ws)
    (ws / "inventory.py").write_text("".join(f"line {i}\n" for i in range(50)))
    diff, _ = s.change_diff("inventory.py", max_lines=10)
    lines = diff.split("\n")
    assert len(lines) == 11
    assert lines[-1].startswith("… truncated, ") and lines[-1].endswith(" more lines")


def test_concurrent_listing_leaves_no_temp_index(ws):
    s = _store(ws)
    (ws / "inventory.py").write_text("x\n")
    s.changes()
    s.change_diff("inventory.py")
    assert not list(s.git_dir.glob("index.changes-*"))


def test_snapshots_off(ws, monkeypatch):
    import agent_cli.snapshot as snap

    monkeypatch.setattr(snap, "git_available", lambda: (False, "git not found"))
    s = GitSnapshot(ws)
    assert s.init("s-1") is False
    assert s.baseline() is None
    assert s.changes() is None
    assert s.clear() is None and s.undo_clear() is False
    d, e = s.change_diff("inventory.py")
    assert d is None and e == "git not found"
    assert not (ws / ".agent-cli").exists()


def test_refs_hold_the_commits(ws):
    s = _store(ws)
    (ws / "inventory.py").write_text("x\n")
    s.clear()
    s.clear()
    r = s._git("for-each-ref", "--format=%(refname)", "refs/agent-cli/")
    assert r.stdout.split() == [BASELINE_REF, BASELINE_PREV_REF]


def test_same_size_edit_in_the_same_second_is_seen(ws):
    """임시 색인은 실제 색인을 mtime 까지 복사해 시작한다 — 새 mtime 의 복사본이면
    git 이 같은 초·같은 크기로 바뀐 파일을 stat 만 보고 "그대로" 로 믿는다(racy-git).
    목록을 여러 번 센 뒤에도 그런 편집이 빠지지 않는다."""
    s = _store(ws)
    for _ in range(3):
        assert s.changes() == []
    (ws / "inventory.py").write_text("a\nB\nc\n")  # 같은 크기
    for _ in range(3):
        assert [c["path"] for c in s.changes()] == ["inventory.py"]
    s.clear()
    (ws / "inventory.py").write_text("a\nb\nc\n")
    for _ in range(3):
        assert [c["path"] for c in s.changes()] == ["inventory.py"]
