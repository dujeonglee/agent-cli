"""스냅샷 빈틈 (v10.139.1, docs/snapshot/DESIGN.md §9) — 실제 git 으로.

고정하는 계약:
- 커밋 없는 하위 저장소가 있어도 세션 시작·체크포인트·restore·Changes 가 된다 — ``add`` 가 그
  경로를 거부하면 그 호출에서만 빼고 다시(여럿이면 여럿). Changes 에 안내 줄, 첫 커밋 뒤엔
  "first seen" 흐름.
- ``.git`` 을 지운 폴더: 스냅샷 색인의 낡은 gitlink 를 빼고 안 파일을 담는다(체크포인트·restore).
- 일반 파일로 담던 폴더가 저장소가 됨: 커밋 전(``git init`` 만)은 계속 담고, 첫 커밋 뒤엔
  스냅샷 색인에서만 뺀다(디스크·그 저장소 불변) — 예전 판 restore 는 되고, Changes 는 기준점
  이후 실제로 바뀐 파일로 이어진다.
- 체크포인트 실패는 "변경 없음" 과 구별해 기록하고(첫 실패 시각·이유), 성공하면 지운다 —
  Changes 에 빨간 안내 줄과 가능한 목록.

모든 저장소 메서드는 :class:`Guarded` 를 거친다 — 앞뒤로 하위 ``.git`` 지문이 같아야 한다.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from agent_cli.snapshot import GitSnapshot, git_available
from agent_cli.snapshot_nested import Verdict, judge
from tests.test_snapshot_nested import Guarded, by_path, files, git, look, make_repo

pytestmark = pytest.mark.skipif(not git_available()[0], reason="git not installed")


def open_store(w: Path, gitdirs: list[Path], sid: str = "s-1") -> Guarded:
    s = Guarded(GitSnapshot(w, min_interval_s=0), gitdirs)
    assert s.init(sid), s.store.reason
    return s


def tree_of(s, rev: str = "HEAD") -> dict[str, str]:
    """스냅샷 커밋의 경로 → mode."""
    out = s._git("ls-tree", "-r", rev).stdout
    return {ln.split("\t", 1)[1]: ln.split(" ", 1)[0] for ln in out.splitlines()}


def blob_text(s, rev: str, path: str) -> str:
    return s._git("show", f"{rev}:{path}").stdout


def unborn(path: Path, files_: dict[str, str]) -> None:
    """``git init`` 만 한 저장소(커밋 없음)."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    for rel, text in files_.items():
        (path / rel).write_text(text)


# ── 판정 — 순수 함수 (늘어난 칸) ──────────────────────────────


@pytest.mark.parametrize(
    "kind, obs, want",
    [
        (None, {"exists": True, "is_repo": True, "has_commits": False}, Verdict.UNBORN),
        (None, {"exists": True, "is_repo": True, "has_commits": True}, Verdict.NEW),
        (
            "repo",
            {"exists": True, "is_repo": True, "has_commits": False},
            Verdict.UNBORN,
        ),
        ("repo", {"exists": True, "is_repo": True, "has_commits": True}, Verdict.NEW),
        # 기록한 HEAD 가 있으면 커밋 유무는 보지 않는다(기존 판정)
        (
            "repo",
            {"exists": True, "is_repo": True, "head_found": True, "has_commits": True},
            Verdict.SAME,
        ),
        # 일반 폴더 기록 + `git init` 만 — 우리가 계속 담는다
        (
            "plain",
            {"exists": True, "is_repo": True, "has_commits": False},
            Verdict.PLAIN,
        ),
        ("plain", {"exists": True, "is_repo": True, "has_commits": True}, Verdict.NEW),
    ],
)
def test_judge_table_unborn(kind, obs, want):
    assert judge(kind, **obs) is want


# ── 1. 커밋 없는 하위 저장소 (시안 탭 1) ─────────────────────────


@pytest.fixture
def ws_unborn(tmp_path):
    w = tmp_path / "ws"
    w.mkdir()
    (w / "a.txt").write_text("1\n")
    unborn(w / "scratch", {"x.txt": "x\n"})
    return w


def test_unborn_repo_does_not_stop_start_checkpoint_restore_or_changes(ws_unborn):
    w = ws_unborn
    s = open_store(w, [w / "scratch/.git"])
    start = s.head()
    assert start, "session start commit"
    assert tree_of(s) == {"a.txt": "100644"}
    (w / "a.txt").write_text("2\n")
    c = s.checkpoint("before shell (turn 4)", force=True)
    assert c and c != start
    assert s.stats.failing_since == 0 and s.stats.failure == ""
    (w / "a.txt").write_text("broken\n")
    ok, msg = s.restore("a.txt")
    assert ok, msg
    assert (w / "a.txt").read_text() == "2\n"
    rep = look(s)
    assert [(f["path"], f["status"]) for f in rep.files] == [("a.txt", "M")]
    (n,) = rep.notices
    assert n["cause"] == "unborn" and n["repo"] == "scratch"
    assert n["text"] == (
        "scratch is a git repository with no commits yet — its files are not part "
        "of the list until its first commit."
    )
    assert n["dismissable"] is False
    assert rep.repos == []  # 그룹 없음


def test_several_unborn_repos_are_each_left_out(tmp_path):
    w = tmp_path / "ws"
    w.mkdir()
    (w / "a.txt").write_text("1\n")
    for name in ("e1", "e2", "deep/e3"):
        unborn(w / name, {"f": name})
    s = open_store(w, [w / n / ".git" for n in ("e1", "e2", "deep/e3")])
    assert tree_of(s) == {"a.txt": "100644"}
    assert sorted(n["repo"] for n in look(s).notices) == ["deep/e3", "e1", "e2"]


def test_unborn_repo_after_its_first_commit_is_first_seen(ws_unborn):
    w = ws_unborn
    sub = w / "scratch"
    s = open_store(w, [sub / ".git"])
    git(sub, "add", "-A")
    git(sub, "commit", "-qm", "first")
    rep = look(s)
    assert rep.notices == []
    assert [(f["path"], f["status"], f.get("summary")) for f in rep.files] == [
        ("scratch", "A", True)
    ]
    assert [r["state"] for r in rep.repos] == ["new"]
    (sub / "x.txt").write_text("x2\n")
    got = by_path(files(s))
    assert got["scratch/x.txt"]["status"] == "M" and got["scratch/x.txt"]["repo"]
    assert "scratch" in tree_of(s, s.checkpoint("after", force=True))


def test_unborn_repo_that_appears_mid_session(tmp_path):
    w = tmp_path / "ws"
    w.mkdir()
    (w / "a.txt").write_text("1\n")
    s = open_store(w, [w / "later/.git"])
    unborn(w / "later", {"y": "y\n"})
    (w / "a.txt").write_text("2\n")
    assert s.checkpoint("x", force=True)
    rep = look(s)
    assert [n["repo"] for n in rep.notices] == ["later"]
    assert [f["path"] for f in rep.files] == ["a.txt"]


# ── 2. .git 을 지운 폴더 (시안 탭 2) ─────────────────────────────


def test_dotgit_removed_folder_is_snapshotted_and_restorable(tmp_path):
    w = tmp_path / "ws"
    w.mkdir()
    (w / "a.txt").write_text("1\n")
    sub = w / "sub"
    make_repo(sub, {"s.txt": "s\n"})
    s = open_store(w, [sub / ".git"])
    history = s._git("rev-list", "HEAD").stdout
    assert tree_of(s) == {"a.txt": "100644", "sub": "160000"}
    shutil.rmtree(sub / ".git")
    (sub / "s.txt").write_text("s2\n")
    c = s.checkpoint("before edit_file (turn 6)", force=True)
    assert c
    assert tree_of(s) == {"a.txt": "100644", "sub/s.txt": "100644"}
    assert blob_text(s, "HEAD", "sub/s.txt") == "s2\n"
    assert s._git("rev-list", "HEAD").stdout.endswith(history)  # 히스토리 불변
    (sub / "s.txt").write_text("damaged\n")
    ok, msg = s.restore("sub/s.txt")
    assert ok, msg
    assert (sub / "s.txt").read_text() == "s2\n"
    # Changes 쪽도 같은 길 — 발견한 순간부터(탭 6), 그 뒤 바뀐 것
    rep = look(s)
    assert [n["cause"] for n in rep.notices] == ["degit"] and rep.files == []
    (sub / "s.txt").write_text("s3\n")
    assert [(f["path"], f["status"]) for f in files(s)] == [("sub/s.txt", "M")]


# ── 3. 저장소가 된 폴더 ────────────────────────────────────────


@pytest.fixture
def ws_lib(tmp_path):
    w = tmp_path / "ws"
    w.mkdir()
    (w / "a.txt").write_text("1\n")
    lib = w / "lib"
    lib.mkdir()
    (lib / "l.txt").write_text("l\n")
    (lib / "m.txt").write_text("m\n")
    (lib / "gone.txt").write_text("g\n")
    return w


def test_git_init_without_commit_keeps_the_folder_in_our_snapshots(ws_lib):
    w = ws_lib
    lib = w / "lib"
    s = open_store(w, [lib / ".git"])
    git(lib, "init", "-q")
    (lib / "l.txt").write_text("l2\n")
    (lib / "n.txt").write_text("n\n")
    c = s.checkpoint("x", force=True)
    assert c
    assert blob_text(s, c, "lib/l.txt") == "l2\n"
    assert "lib/n.txt" in tree_of(s, c)
    rep = look(s)
    assert rep.notices == []  # 넘기지 않으니 안내도 없다
    assert {f["path"]: f["status"] for f in rep.files} == {
        "lib/l.txt": "M",
        "lib/n.txt": "A",
    }


def test_folder_that_became_a_repository_is_handed_over(ws_lib):
    w = ws_lib
    lib = w / "lib"
    s = open_store(w, [lib / ".git"])
    git(lib, "init", "-q")
    (lib / "l.txt").write_text("l2\n")
    (lib / "n.txt").write_text("n\n")
    (lib / "gone.txt").unlink()
    git(lib, "add", "-A")
    git(lib, "commit", "-qm", "first")
    (lib / "m.txt").write_text("m2\n")  # 커밋 안 한 수정
    disk = {p.name: p.read_bytes() for p in lib.iterdir() if p.is_file()}
    c = s.checkpoint("before shell (turn 9)", force=True)
    assert c
    t = tree_of(s, c)
    assert t["lib"] == "160000" and not any(p.startswith("lib/") for p in t)
    assert {p.name: p.read_bytes() for p in lib.iterdir() if p.is_file()} == disk
    # Changes — 기준점 이후 실제로 바뀐 파일로 이어진다(요약 한 줄이 아님)
    rep = look(s)
    assert {f["path"]: (f["status"], f.get("repo")) for f in rep.files} == {
        "lib/l.txt": ("M", "lib"),
        "lib/m.txt": ("M", "lib"),
        "lib/n.txt": ("A", "lib"),
        "lib/gone.txt": ("D", "lib"),
    }
    assert rep.notices == []
    assert [r["state"] for r in rep.repos] == ["normal"]
    diff, _ = s.change_diff("lib/l.txt")
    assert "-l\n" in diff and "+l2" in diff
    # 그 뒤의 커밋·수정도 기준점 대비로
    git(lib, "commit", "-qam", "m2")
    (lib / "l.txt").write_text("l\n")  # 기준점 내용으로 되돌림 → 목록에서 빠진다
    got = {f["path"]: f["status"] for f in files(s)}
    assert got == {"lib/m.txt": "M", "lib/n.txt": "A", "lib/gone.txt": "D"}
    # 예전 체크포인트에는 넘기기 전 판이 남는다 — 모델의 restore 가 된다
    (lib / "l.txt").write_text("damaged\n")
    ok, msg = s.restore("lib/l.txt")
    assert ok, msg
    assert (lib / "l.txt").read_text() == "l\n"
    # 다음 체크포인트도 여전히 gitlink 하나
    (w / "a.txt").write_text("2\n")
    t = tree_of(s, s.checkpoint("y", force=True))
    assert t["lib"] == "160000" and not any(p.startswith("lib/") for p in t)


def test_handed_over_folder_that_was_not_at_the_baseline_is_first_seen(tmp_path):
    w = tmp_path / "ws"
    w.mkdir()
    (w / "a.txt").write_text("1\n")
    s = open_store(w, [w / "lib/.git"])
    lib = w / "lib"
    lib.mkdir()
    (lib / "l.txt").write_text("l\n")
    assert s.checkpoint("x", force=True)  # 일반 파일로 담김
    git(lib, "init", "-q")
    git(lib, "add", "-A")
    git(lib, "commit", "-qm", "first")
    rep = look(s)
    assert [(f["path"], f.get("summary")) for f in rep.files] == [("lib", True)]
    assert [r["state"] for r in rep.repos] == ["new"]


def test_only_the_outermost_new_repository_is_handed_over(ws_lib):
    w = ws_lib
    lib = w / "lib"
    inner = lib / "inner"
    inner.mkdir()
    (inner / "i.txt").write_text("i\n")
    s = open_store(w, [lib / ".git", inner / ".git"])
    git(inner, "init", "-q")  # 안쪽도 저장소 — 바깥 것만 넘긴다
    git(inner, "add", "-A")
    git(inner, "commit", "-qm", "i")
    git(lib, "init", "-q")
    git(lib, "add", "-A")
    git(lib, "commit", "-qm", "outer")
    t = tree_of(s, s.checkpoint("x", force=True))
    assert t["lib"] == "160000" and not any(p.startswith("lib/") for p in t)


@pytest.mark.parametrize("checkpoint_between", [True, False])
def test_plain_folder_that_became_a_repository_again_continues(
    tmp_path, checkpoint_between
):
    """.git 을 지워 일반 폴더 기록이 된 자리(탭 6)가 다시 커밋 있는 저장소가 되면 — 그 기록의
    트리(발견한 순간)를 기준으로 넘겨받고, "no longer a git repository" 안내는 거둔다.
    커밋 전(``git init`` 만): 그 사이 체크포인트가 있었으면 색인이 일반 파일이라 계속 담고,
    없었으면(지우고 바로 init) 남은 gitlink 를 빼 커밋 없는 저장소 안내로."""
    w = tmp_path / "ws"
    w.mkdir()
    sub = w / "sub"
    make_repo(sub, {"s.txt": "s\n", "t.txt": "t\n"})
    s = open_store(w, [sub / ".git"])
    shutil.rmtree(sub / ".git")
    assert [n["cause"] for n in look(s).notices] == ["degit"]
    if checkpoint_between:
        assert "sub/s.txt" in tree_of(s, s.checkpoint("before shell", force=True))
    (sub / "s.txt").write_text("s2\n")
    git(sub, "init", "-q")
    rep = look(s)
    if checkpoint_between:  # 계속 담는다
        assert [f["path"] for f in rep.files] == ["sub/s.txt"]
        assert blob_text(s, s.checkpoint("x", force=True), "sub/s.txt") == "s2\n"
    else:
        assert rep.files == []
        assert [n["cause"] for n in rep.notices] == ["unborn", "degit"]
        c = s.checkpoint("x", force=True)  # 낡은 gitlink 가 빠진다 — 실패 아님
        assert c and "sub" not in tree_of(s, c) and s.stats.failing_since == 0
    git(sub, "add", "-A")
    git(sub, "commit", "-qm", "again")
    rep = look(s)
    assert rep.notices == []
    assert [(f["path"], f["status"], f.get("repo")) for f in rep.files] == [
        ("sub/s.txt", "M", "sub")
    ]


# ── 4. 조용한 실패 (시안 탭 3) ─────────────────────────────────


@pytest.fixture
def ws_locked(tmp_path):
    w = tmp_path / "ws"
    w.mkdir()
    (w / "a.txt").write_text("1\n")
    yield w
    for p in w.rglob("*"):
        os.chmod(p, 0o755 if p.is_dir() else 0o644)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads unreadable files")
def test_checkpoint_failure_is_recorded_and_cleared(ws_locked):
    w = ws_locked
    s = open_store(w, [])
    st = s.stats
    assert s.checkpoint("no change", force=True) is None  # 변경 없음 — 실패 아님
    assert st.failing_since == 0
    (w / "a.txt").write_text("2\n")
    bad = w / "secret.txt"
    bad.write_text("s\n")
    os.chmod(bad, 0)
    assert s.checkpoint("before shell (turn 2)", force=True) is None
    first = st.failing_since
    assert first > 0 and st.failure.startswith("git add: ")
    assert "secret.txt" in st.failure
    assert s.checkpoint("again", force=True) is None
    assert st.failing_since == first  # 첫 실패 시각을 지킨다
    rep = s.report()  # "Snapshots are off" 가 아니다 — 안내 + 가능한 목록
    assert rep is not None
    (n,) = rep.notices
    assert n["cause"] == "snapshot_failing" and n["dismissable"] is False
    assert n["text"].startswith("Snapshots have been failing since ")
    assert "(git add: " in n["text"]
    assert n["text"].endswith(
        "Files changed after that may be missing from this list and cannot be restored."
    )
    os.chmod(bad, 0o644)
    assert s.checkpoint("ok", force=True)
    assert st.failing_since == 0 and st.failure == ""
    rep = s.report()
    assert rep.notices == []
    assert {f["path"] for f in rep.files} == {"a.txt", "secret.txt"}
