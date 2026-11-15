"""하위 git 저장소 안의 변경 (v10.139.0, docs/snapshot/DESIGN.md §8) — 실제 git 으로.

고정하는 계약: 기준점에 하위 저장소마다 기록(HEAD·첫 커밋·추적 파일 수·dirty 내용)을 남기고
셀 때 그 저장소의 git 으로 센다 — 시안 탭 1~7(평소·원래 dirty·기준 잃음·다시 clone·폴더 삭제·
.git 만 삭제·새 저장소) · resume 은 기록을 덮어쓰지 않는다 · Clear/Undo 가 기록을 함께 옮긴다 ·
안내 닫기 · 기록 없는 옛 기준은 지금 동작(gitlink 한 줄) · 하위 안의 또 다른 저장소는 한 줄 ·
``.git`` 파일(worktree) · 하위 ``.gitignore`` · 상한.

**하위 ``.git`` 불변**: 저장소 메서드 호출은 전부 :class:`Guarded` 를 거친다 — 호출 앞뒤로
하위 ``.git`` 아래 모든 파일의 (경로, 크기, mtime, sha256) 이 같아야 한다.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_cli.snapshot import (
    NESTED_BASELINE_PREV_REF,
    NESTED_BASELINE_REF,
    NESTED_START_PREFIX,
    GitSnapshot,
    git_available,
)
from agent_cli.snapshot_nested import Manifest, Notice, RepoRecord, Verdict, judge

pytestmark = pytest.mark.skipif(not git_available()[0], reason="git not installed")

_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def git(cwd: Path, *args: str) -> str:
    """테스트가 사람처럼 하위 저장소를 만지는 git (저장소 메서드 밖)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    r = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**env, **_ENV},
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout


def make_repo(path: Path, files: dict[str, str]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    for rel, text in files.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text)
    git(path, "add", "-A")
    git(path, "commit", "-qm", "first")


def fingerprint(gitdirs: list[Path]) -> dict:
    out = {}
    for d in gitdirs:
        if not d.exists():
            continue
        items = [d] if d.is_file() else sorted(p for p in d.rglob("*") if p.is_file())
        for p in items:
            st = p.stat()
            out[str(p)] = (
                st.st_size,
                st.st_mtime_ns,
                hashlib.sha256(p.read_bytes()).hexdigest(),
            )
    return out


class Guarded:
    """저장소 메서드를 감싼다 — 앞뒤로 하위 ``.git`` 지문이 같아야 한다."""

    def __init__(self, store: GitSnapshot, gitdirs: list[Path]):
        self.store = store
        self.gitdirs = gitdirs
        self.calls = 0

    def __getattr__(self, name):
        attr = getattr(self.store, name)
        if not callable(attr):
            return attr

        def wrapped(*a, **kw):
            before = fingerprint(self.gitdirs)
            out = attr(*a, **kw)
            assert fingerprint(self.gitdirs) == before, (
                f"{name} wrote into a nested .git"
            )
            self.calls += 1
            return out

        return wrapped


@pytest.fixture
def ws(tmp_path):
    w = tmp_path / "ws"
    w.mkdir()
    (w / "README.md").write_text("Run make.\n")
    make_repo(
        w / "git-proj",
        {
            "src/parser.py": "def parse(t):\n    return t\n",
            "src/legacy.py": "old = 1\n",
            "src/config.py": "TIMEOUT = 30\n",
            "util.py": "u = 1\n",
            ".gitignore": "*.log\n",
        },
    )
    return w


def store(ws: Path, sid="s-1", *, extra_git: list[Path] = (), **kw) -> Guarded:
    s = GitSnapshot(ws, min_interval_s=0, **kw)
    g = Guarded(s, [ws / "git-proj" / ".git", *extra_git])
    assert g.init(sid), s.reason
    return g


def by_path(files):
    return {f["path"]: f for f in files}


def look(s):
    """웹과 같은 순서 — 기록을 지금과 맞추고(``sync_nested_records``) 센다."""
    s.sync_nested_records()
    return s.report()


def files(s):
    return look(s).files


# ── 판정 — 순수 함수 ───────────────────────────────────────────


@pytest.mark.parametrize(
    "kind, obs, want",
    [
        ("repo", {"exists": True, "is_repo": True, "head_found": True}, Verdict.SAME),
        ("repo", {"exists": True, "is_repo": True, "head_found": None}, Verdict.SAME),
        (
            "repo",
            {
                "exists": True,
                "is_repo": True,
                "head_found": False,
                "roots_shared": True,
            },
            Verdict.LOST,
        ),
        (
            "repo",
            {
                "exists": True,
                "is_repo": True,
                "head_found": False,
                "roots_shared": False,
            },
            Verdict.RECLONE,
        ),
        ("repo", {"exists": False, "is_repo": False}, Verdict.REMOVED),
        ("repo", {"exists": True, "is_repo": False}, Verdict.DEGIT),
        (None, {"exists": True, "is_repo": True}, Verdict.NEW),
        (None, {"exists": True, "is_repo": False}, None),
        ("plain", {"exists": True, "is_repo": False}, Verdict.PLAIN),
        ("plain", {"exists": False, "is_repo": False}, Verdict.PLAIN),
        ("plain", {"exists": True, "is_repo": True}, Verdict.NEW),
    ],
)
def test_judge_table(kind, obs, want):
    assert judge(kind, **obs) is want


def test_manifest_round_trip():
    m = Manifest(
        when=5,
        repos=[
            RepoRecord(
                path="a", head="h", roots=["r"], tracked=3, dirty=["x"], tree="t"
            )
        ],
        notices=[Notice("a", "lost", 9, "h", 5)],
    )
    assert Manifest.from_json(m.to_json()) == m
    assert Manifest.from_json('{"version": 99}') is None
    assert Manifest.from_json("not json") is None


# ── 탭 1~7 ────────────────────────────────────────────────────


def test_tab1_commits_and_uncommitted_in_one_list(ws):
    s = store(ws)
    sub = ws / "git-proj"
    assert files(s) == []
    (sub / "src/parser.py").write_text(
        "def parse(t):\n    tree = []\n    return tree\n"
    )
    git(sub, "commit", "-qam", "parser")
    git(sub, "rm", "-q", "src/legacy.py")
    git(sub, "commit", "-qm", "drop legacy")
    (sub / "tests").mkdir()
    (sub / "tests/test_parser.py").write_text("import pytest\n\n\ndef test(): ...\n")
    (sub / "debug.log").write_text("ignored by git-proj/.gitignore\n")
    (ws / "README.md").write_text("Run `make build`.\n")
    rep = look(s)
    got = by_path(rep.files)
    assert set(got) == {
        "README.md",
        "git-proj/src/parser.py",
        "git-proj/src/legacy.py",
        "git-proj/tests/test_parser.py",
    }
    assert "repo" not in got["README.md"]
    assert got["git-proj/src/parser.py"] == {
        "path": "git-proj/src/parser.py",
        "status": "M",
        "added": 2,
        "deleted": 1,
        "repo": "git-proj",
    }
    assert got["git-proj/src/legacy.py"]["status"] == "D"
    assert got["git-proj/tests/test_parser.py"]["status"] == "A"
    assert got["git-proj/tests/test_parser.py"]["added"] == 4
    (grp,) = rep.repos
    assert grp["path"] == "git-proj" and grp["state"] == "normal"
    assert grp["commits"] == 2 and grp["head_from"] != grp["head_to"]
    assert rep.notices == []
    diff, err = s.change_diff("git-proj/src/parser.py")
    assert not err
    assert "--- a/git-proj/src/parser.py" in diff and "+    tree = []" in diff
    diff, _ = s.change_diff("git-proj/tests/test_parser.py")
    assert "+++ b/git-proj/tests/test_parser.py" in diff and "+import pytest" in diff


def test_tab2_files_dirty_at_baseline_compare_with_their_recorded_content(ws):
    sub = ws / "git-proj"
    (sub / "src/parser.py").write_text("def parse(t):\n" + "    x = 1\n" * 40)
    (sub / "src/config.py").write_text("TIMEOUT = 60\n")
    (sub / "draft.txt").write_text("untracked at baseline\n")
    (sub / "notes.txt").write_text("untracked, removed later\n")
    git(sub, "rm", "-q", "src/legacy.py")  # 기준점 때 이미 지운 파일
    s = store(ws)
    assert files(s) == []
    (sub / "src/config.py").write_text("TIMEOUT = 60\nRETRIES = 3\n")
    (sub / "notes.txt").unlink()
    (sub / "src/legacy.py").write_text("old = 1\n")  # 그때 없었는데 지금 있다
    got = by_path(files(s))
    assert {p: f["status"] for p, f in got.items()} == {
        "git-proj/src/config.py": "M",
        "git-proj/notes.txt": "D",
        "git-proj/src/legacy.py": "A",
    }
    assert (
        got["git-proj/src/config.py"]["added"],
        got["git-proj/src/config.py"]["deleted"],
    ) == (1, 0)
    diff, _ = s.change_diff("git-proj/src/config.py")
    # 커밋(TIMEOUT = 30) 대비가 아니라 기준점 때 내용 대비
    assert "TIMEOUT = 30" not in diff and "+RETRIES = 3" in diff
    assert "a/git-proj/src/config.py" in diff
    # 기준점 때 고쳐 둔 것을 커밋해도 내용이 같으면 목록에 없다
    (sub / "src/config.py").write_text("TIMEOUT = 60\n")
    git(sub, "commit", "-qam", "commit what was dirty")
    assert "git-proj/src/config.py" not in by_path(files(s))


def _rewrite_and_gc(sub: Path) -> None:
    git(sub, "commit", "-q", "--amend", "-m", "rewritten")
    git(sub, "reflog", "expire", "--expire=now", "--all")
    git(sub, "gc", "-q", "--prune=now")


def test_tab3_lost_baseline_resets_only_that_repo_with_a_notice(ws):
    sub = ws / "git-proj"
    (sub / "util.py").write_text("u = 2\n")
    git(sub, "commit", "-qam", "second")
    s = store(ws)
    old = git(sub, "rev-parse", "HEAD").strip()
    (ws / "README.md").write_text("changed\n")
    (sub / "util.py").write_text("u = 3\n")
    git(sub, "add", "-A")
    _rewrite_and_gc(sub)
    assert subprocess.run(
        ["git", "-C", str(sub), "cat-file", "-e", old + "^{commit}"],
        capture_output=True,
        check=False,
    ).returncode
    rep = look(s)
    assert [f["path"] for f in rep.files] == ["README.md"]  # 워크스페이스는 그대로
    (n,) = rep.notices
    assert n["repo"] == "git-proj" and n["cause"] == "lost"
    assert n["text"].startswith(f"git-proj: baseline commit {old[:7]} (recorded ")
    assert "rebase or reset, then gc" in n["text"]
    assert rep.repos[0]["state"] == "reset"
    # 안내는 기록에 남는다 — 다시 세도(재접속) 그대로, 다시 잡지 않는다
    again = look(s)
    assert (
        again.notices == rep.notices
        and again.repos[0]["since"] == rep.repos[0]["since"]
    )
    # 그 뒤 고친 것은 그 그룹 아래에
    (sub / "src/parser.py").write_text("changed\n")
    assert "git-proj/src/parser.py" in by_path(files(s))


def test_tab4_reclone_is_a_different_repository(ws):
    s = store(ws)
    shutil.rmtree(ws / "git-proj")
    make_repo(ws / "git-proj", {"other.txt": "another history\n"})
    rep = look(s)
    (n,) = rep.notices
    assert n["cause"] == "reclone"
    assert n["text"].startswith("git-proj is now a different repository")
    assert rep.files == []  # "삭제 후 추가" 로 쏟지 않는다


def test_tab5_removed_folder_is_one_summary_line(ws):
    s = store(ws)
    shutil.rmtree(ws / "git-proj")
    rep = look(s)
    assert rep.files == [
        {
            "path": "git-proj",
            "status": "D",
            "added": None,
            "deleted": None,
            "repo": "git-proj",
            "summary": True,
        }
    ]
    assert rep.repos[0]["state"] == "removed" and rep.notices == []
    text, _ = s.change_diff("git-proj")
    assert text.startswith("git-proj/ was removed from disk.")
    assert "It had 5 tracked files at commit" in text
    s.clear()
    assert files(s) == []


def test_tab6_dotgit_removed_moves_the_folder_to_the_workspace(ws):
    s = store(ws)
    sub = ws / "git-proj"
    shutil.rmtree(sub / ".git")
    rep = look(s)
    assert rep.files == []  # 다섯 파일 전부 A 로 쏟지 않는다
    (n,) = rep.notices
    assert n["cause"] == "degit" and "(.git was removed)" in n["text"]
    assert rep.repos == []  # 그룹 없음 — 워크스페이스 쪽
    (sub / "src/parser.py").write_text("changed\n")
    (sub / "new.txt").write_text("n\n")
    got = by_path(files(s))
    assert {p: f["status"] for p, f in got.items()} == {
        "git-proj/src/parser.py": "M",
        "git-proj/new.txt": "A",
    }
    assert "repo" not in got["git-proj/src/parser.py"]
    diff, _ = s.change_diff("git-proj/src/parser.py")
    assert "a/git-proj/src/parser.py" in diff and "+changed" in diff
    # Clear 뒤에도 그 폴더는 워크스페이스와 함께 센다
    s.clear()
    assert look(s).notices == [] and files(s) == []
    (sub / "util.py").write_text("u = 9\n")
    assert [f["path"] for f in files(s)] == ["git-proj/util.py"]


def test_tab7_new_repository_is_a_summary_line_then_its_changes(ws):
    s = store(ws, extra_git=[ws / "lib-x" / ".git"])
    make_repo(
        ws / "lib-x", {"src/core.c": "int f(void) { return 0; }\n", "README": "x\n"}
    )
    rep = look(s)
    assert rep.files == [
        {
            "path": "lib-x",
            "status": "A",
            "added": None,
            "deleted": None,
            "repo": "lib-x",
            "summary": True,
        }
    ]
    assert [r["state"] for r in rep.repos] == ["normal", "new"]
    assert rep.notices == []
    text, _ = s.change_diff("lib-x")
    assert text.startswith("lib-x/ appeared after the baseline as a git repository")
    assert "2 tracked files at commit" in text
    (ws / "lib-x/src/core.c").write_text("int f(void) { return 1; }\n")
    got = files(s)
    assert [(f["path"], f["status"]) for f in got] == [
        ("lib-x", "A"),
        ("lib-x/src/core.c", "M"),
    ]
    s.clear()
    assert files(s) == [] and [r["state"] for r in look(s).repos] == [
        "normal",
        "normal",
    ]


# ── 기록의 수명 ───────────────────────────────────────────────


def _ref(s, name):
    return s._git("rev-parse", "-q", "--verify", name).stdout.strip() or None


def test_resume_keeps_the_record(ws):
    sub = ws / "git-proj"
    s = store(ws, "s-9")
    rec = _ref(s, NESTED_START_PREFIX + "s-9")
    assert rec
    (sub / "util.py").write_text("u = 2\n")
    git(sub, "commit", "-qam", "after start")
    s2 = store(ws, "s-9")  # resume — 같은 세션 id 로 다시 init
    assert _ref(s2, NESTED_START_PREFIX + "s-9") == rec
    assert [f["path"] for f in files(s2)] == ["git-proj/util.py"]


def test_old_session_without_record_keeps_the_gitlink_line(ws):
    """기록 없는 기준(옛 세션의 resume) — 지금 동작 그대로: 커밋이 바뀌면 gitlink 한 줄."""
    sub = ws / "git-proj"
    s = store(ws, "old")
    s._git("update-ref", "-d", NESTED_START_PREFIX + "old")
    (sub / "util.py").write_text("u = 2\n")
    assert files(s) == []  # 커밋 안 한 수정은 안 보인다
    git(sub, "commit", "-qam", "c")
    assert [(f["path"], f["status"]) for f in files(s)] == [("git-proj", "M")]
    # resume 도 기록을 새로 남기지 않는다 — 그 기준점과 짝이 아니다
    s2 = store(ws, "old")
    assert _ref(s2, NESTED_START_PREFIX + "old") is None
    assert look(s2).repos == []


def test_head_baseline_has_no_record(ws):
    s = store(ws, None)
    (ws / "git-proj/util.py").write_text("u = 2\n")
    rep = look(s)
    assert rep.files == [] and rep.repos == []


def test_clear_and_undo_move_the_record_with_the_baseline(ws):
    sub = ws / "git-proj"
    (sub / "util.py").write_text("u = 1.5\n")
    git(sub, "commit", "-qam", "second")  # 첫 커밋은 남기고 그 위를 고쳐 쓴다(탭 3)
    s = store(ws)
    (sub / "util.py").write_text("u = 2\n")
    git(sub, "add", "-A")
    _rewrite_and_gc(sub)
    assert [n["cause"] for n in look(s).notices] == ["lost"]
    (sub / "src/config.py").write_text("TIMEOUT = 1\n")
    before = files(s)
    assert [f["path"] for f in before] == ["git-proj/src/config.py"]
    s.clear()
    assert _ref(s, NESTED_BASELINE_REF) and not _ref(s, NESTED_BASELINE_PREV_REF)
    rep = look(s)
    assert rep.files == [] and rep.notices == []  # Clear 가 안내도 닫는다
    (sub / "util.py").write_text("u = 3\n")
    assert [f["path"] for f in files(s)] == ["git-proj/util.py"]
    assert s.undo_clear() is True
    rep = look(s)
    assert [n["cause"] for n in rep.notices] == ["lost"]  # 이전 기록(안내 포함)
    assert {f["path"] for f in rep.files} == {
        "git-proj/src/config.py",
        "git-proj/util.py",
    }
    assert not _ref(s, NESTED_BASELINE_REF)
    assert s.undo_clear() is False
    # Clear 두 번 → Undo 는 첫 Clear 의 기록으로
    s.clear()
    first = _ref(s, NESTED_BASELINE_REF)
    s.clear()
    assert _ref(s, NESTED_BASELINE_PREV_REF) == first
    s.undo_clear()
    assert _ref(s, NESTED_BASELINE_REF) == first


def test_dismiss_notice(ws):
    s = store(ws)
    shutil.rmtree(ws / "git-proj/.git")
    assert [n["repo"] for n in look(s).notices] == ["git-proj"]
    assert s.dismiss_notice("git-proj") is True
    assert look(s).notices == []
    assert s.dismiss_notice("git-proj") is False
    assert s.dismiss_notice("nope") is False


# ── 모양 ──────────────────────────────────────────────────────


def test_repo_inside_the_nested_repo_is_one_line(ws):
    inner = ws / "git-proj/vendor/inner"
    s = store(ws, extra_git=[inner / ".git"])
    make_repo(inner, {"z.txt": "z\n"})
    got = files(s)
    assert [(f["path"], f["status"]) for f in got] == [("git-proj/vendor/inner", "A")]
    (inner / "z.txt").write_text("zz\n")  # 커밋 안 한 수정은 그 저장소의 일
    assert [f["path"] for f in files(s)] == ["git-proj/vendor/inner"]
    git(inner, "commit", "-qam", "2")
    assert [f["path"] for f in files(s)] == ["git-proj/vendor/inner"]
    diff, _ = s.change_diff("git-proj/vendor/inner")
    assert "Subproject commit" in diff


def test_dotgit_file_worktree(tmp_path):
    """``.git`` 이 파일인 하위 저장소(``git worktree``) — 하위 git 이 처리한다."""
    main = tmp_path / "main"
    make_repo(main, {"a.txt": "a\n"})
    w = tmp_path / "ws"
    w.mkdir()
    git(main, "worktree", "add", "-q", str(w / "wt"), "-b", "side")
    assert (w / "wt/.git").is_file()
    s = GitSnapshot(w, min_interval_s=0)
    g = Guarded(s, [main / ".git", w / "wt/.git"])
    assert g.init("s-w"), s.reason
    assert [n.path for n in s.nested] == ["wt"]
    (w / "wt/a.txt").write_text("b\n")
    (w / "wt/b.txt").write_text("new\n")
    git(w / "wt", "add", "b.txt")
    git(w / "wt", "commit", "-qm", "side")
    got = by_path(files(g))
    assert {p: f["status"] for p, f in got.items()} == {
        "wt/a.txt": "M",
        "wt/b.txt": "A",
    }


def test_caps_and_excluded_dirs_hold_inside(ws):
    sub = ws / "git-proj"
    s = store(ws, file_max_bytes=100)
    (sub / "big.bin").write_bytes(b"x" * 500)
    (sub / "node_modules").mkdir()
    (sub / "node_modules/dep.js").write_text("x\n")
    (sub / "src/__pycache__").mkdir()
    (sub / "src/__pycache__/p.pyc").write_bytes(b"\0")
    (sub / "small.txt").write_text("ok\n")
    assert [f["path"] for f in files(s)] == ["git-proj/small.txt"]


def test_paths_outside_and_unknown_are_refused(ws):
    s = store(ws)
    assert s.change_diff("../x")[0] is None
    assert s.change_diff(str(ws.parent / "elsewhere"))[0] is None
    assert s.change_diff("git-proj/no-such-file") == ("", "")


def test_record_refs_stay_off_head_and_the_model_list(ws):
    s = store(ws)
    head = s.head()
    log = s.log()
    (ws / "git-proj/util.py").write_text("u = 2\n")
    look(s)
    s.clear()
    assert s.head() == head and s.log() == log
    labels = s._git("log", "--all", "--format=%s").stdout.split("\n")
    assert "nested record" in labels  # ref 가 붙잡는다 — gc 에 안전
    assert "nested record" not in s._git("log", "--format=%s").stdout


def test_guard_sees_an_index_refresh(ws):
    """지문 검사가 실제로 잡는다 — 잠금 있는 보통 ``git status`` 는 stat 이 바뀐 파일을 보고
    하위 색인을 고쳐 쓴다. 우리 호출(``GIT_OPTIONAL_LOCKS=0``)은 같은 상황에서 안 쓴다."""
    sub = ws / "git-proj"
    s = store(ws)
    os.utime(sub / "util.py", (1, 1))
    os.utime(sub / "util.py", None)
    assert files(s) == []  # Guarded — 안 썼다

    class Plain:
        def status(self):
            git(sub, "status", "--porcelain")

    with pytest.raises(AssertionError, match="wrote into a nested .git"):
        Guarded(Plain(), [sub / ".git"]).status()


def _all_refs(s):
    return s._git("for-each-ref", "--format=%(refname) %(objectname)").stdout


@pytest.mark.parametrize("situation", ["lost", "new", "degit"])
def test_listing_reads_only_and_sync_is_its_own_call(ws, situation):
    """목록·diff 는 읽기만 — 스냅샷 저장소의 모든 ref 가 그대로. 기록을 지금과 맞추는 것은
    이름이 그 일을 말하는 ``sync_nested_records`` 뿐이다."""
    sub = ws / "git-proj"
    (sub / "util.py").write_text("u = 2\n")
    git(sub, "commit", "-qam", "second")
    s = store(ws, extra_git=[ws / "lib-x" / ".git"])
    if situation == "lost":
        _rewrite_and_gc(sub)
    elif situation == "new":
        make_repo(ws / "lib-x", {"c.txt": "c\n"})
    else:
        shutil.rmtree(sub / ".git")
    before = _all_refs(s)
    for _ in range(2):
        rep = s.report()
        s.changes()
        s.change_diff("git-proj/util.py")
        s.change_diff("lib-x")
        assert _all_refs(s) == before
    assert rep.notices == []  # 저장된 기록 그대로 센다
    assert [r["state"] for r in rep.repos if r["path"] == "lib-x"] == []
    assert s.sync_nested_records() is True
    assert _all_refs(s) != before
    after = s.report()
    if situation == "new":
        assert [r["state"] for r in after.repos] == ["normal", "new"]
    else:
        assert [n["cause"] for n in after.notices] == [situation]
    assert s.sync_nested_records() is False  # 이미 맞다


def test_index_reading_calls_use_our_copy_of_the_nested_index(ws):
    """porcelain ``git diff <commit>`` 은 ``GIT_OPTIONAL_LOCKS=0`` 이어도 하위 색인을 고쳐 쓸
    때가 있다(실측 40회 중 8회) — 색인을 읽는 하위 호출은 스냅샷 저장소 안의 사본을 읽는다.
    원본이 바뀌면 다시 복사한다."""
    sub = ws / "git-proj"
    s = store(ws)
    seen: list[str | None] = []
    real = subprocess.run

    def spy(args, *a, **kw):
        if "-C" in args and any(c in args for c in ("diff", "status", "ls-files")):
            seen.append((kw.get("env") or {}).get("GIT_INDEX_FILE"))
        return real(args, *a, **kw)

    subprocess.run = spy
    try:
        (sub / "util.py").write_text("u = 2\n")
        look(s)
        s.change_diff("git-proj/util.py")
    finally:
        subprocess.run = real
    assert seen and all(p and p.startswith(str(s.store.git_dir)) for p in seen)
    assert Path(seen[0]).exists()
    # 원본 색인이 바뀌면(사람이 add) 사본도 따라간다 — 목록이 그 색인으로 센다
    (sub / "new.txt").write_text("n\n")
    git(sub, "add", "new.txt")
    got = {f["path"]: f["status"] for f in files(s)}
    assert got == {"git-proj/util.py": "M", "git-proj/new.txt": "A"}
