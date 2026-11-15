"""워크스페이스 안 하위 git 저장소 — 기준점 기록과 비교 (docs/snapshot/DESIGN.md §8).

그림자 저장소는 하위 저장소를 gitlink(커밋 sha 한 줄)로만 담는다(§3.3). 그래서 웹 Changes
패널은 하위 저장소 안의 수정을 **그 저장소의 git 으로** 센다: 기준점 순간에 저장소마다
*기록*(상대 경로·HEAD·첫 커밋들·추적 파일 수·그때 dirty 였던 파일의 내용)을 남기고, 셀 때
"기록한 HEAD ↔ 지금 작업 트리"(사이 커밋 + 커밋 안 한 수정이 한 번에)와 untracked 목록을 그
저장소에 묻는다. 기록 때 dirty 였던 경로는 커밋이 아니라 담아 둔 내용과 비교한다. 하위
저장소 전체를 복사하지 않는다 — 담는 것은 dirty 파일 내용뿐이다.

**하위 저장소는 읽기만 한다**: 모든 하위 git 호출은 ``GIT_OPTIONAL_LOCKS=0``·
``core.fsmonitor=false``(설정된 감시 프로그램을 띄우지 않게)·``--no-ext-diff``·``--no-textconv``
이고, 사용자 전역 설정은 주입받은 env 가 이미 막는다. 색인을 읽는 호출(``diff``·``status``·
``ls-files``)은 하위 색인의 **우리 쪽 사본**(``GIT_INDEX_FILE``, 스냅샷 저장소 안)을 읽는다 —
porcelain ``git diff <commit>`` 은 ``GIT_OPTIONAL_LOCKS=0`` 이어도 stat 이 바뀐 파일을 보면
색인을 고쳐 쓴다(실측: 40회 중 8회). 사본이면 그 갱신은 사본에 떨어지고, 사본이 셈 사이에
남아 stat 캐시도 이어진다. 원본 색인이 바뀌면(stat 이 다르면) 다시 복사한다.
내용을 담는 쓰기(``hash-object -w``·임시 색인·``write-tree``)는 전부 **우리** 스냅샷 저장소에.

이 모듈은 0층이고 ``snapshot.py`` 만 import 한다(한 방향). 스냅샷 저장소의 git 은 인자로
받는다(``ours``) — 기준점 ref 를 어느 것으로 짝지을지는 ``snapshot.py`` 의 일이고, 여기는
기록을 만들고·저장하고·읽고·비교한다.

판정(:func:`judge`)은 관찰값 → :class:`Verdict` 의 순수 함수다. 판정별 처리는
:meth:`NestedRepos.reconcile` 의 작은 분기 — 판정은 여덟 가지로 닫혀 있어 상태 클래스 계층은
과하다.

스냅샷 색인의 저장소 경계(§9)도 여기서 정한다(:func:`index_paths_to_untrack` — 무엇을 뺄지만;
색인을 고치는 것은 ``snapshot.py``): ``.git`` 이 사라진 gitlink, 일반 파일로 담던 폴더가 커밋 있는
저장소가 된 자리. 넘긴 폴더의 Changes 연속성은 :meth:`NestedRepos.record_adopted`.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path

#: git 이 늘 아는 빈 트리
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
MANIFEST_NAME = "manifest.json"
RECORD_LABEL = "nested record"
MANIFEST_VERSION = 1
#: 하위 git 이 저장소 위치를 다른 데서 찾게 만드는 변수 — 상속받았으면 지운다
_REPO_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
)
_SUB_DIFF = ("diff", "--no-ext-diff", "--no-textconv", "--no-renames")

#: ``ours(*args, env=?, input=?)`` — 스냅샷 저장소의 git (``GitSnapshot._git``)
GitRunner = Callable[..., subprocess.CompletedProcess]


class Verdict(str, Enum):
    """셀 때 기록 하나(또는 기록에 없는 저장소 하나)의 판정 — 시안 탭 1~7."""

    SAME = "same"  # 1·2 기록한 HEAD 가 아직 있다
    LOST = "lost"  # 3 기록한 HEAD 가 없고 첫 커밋은 같다(히스토리 재작성 + gc)
    RECLONE = "reclone"  # 4 기록한 HEAD 가 없고 첫 커밋도 다르다(다른 저장소)
    REMOVED = "removed"  # 5 폴더가 없다
    DEGIT = "degit"  # 6 폴더는 있는데 저장소가 아니다(.git 만 지움)
    NEW = "new"  # 7 기록에 없는 저장소(또는 일반 폴더였던 것이 저장소가 됨)
    PLAIN = "plain"  # 6 뒤 — 일반 폴더 기록, 워크스페이스와 같은 비교
    UNBORN = "unborn"  # 커밋이 하나도 없는 저장소 — 안내만, 첫 커밋 뒤 NEW


def judge(
    kind: str | None,
    *,
    exists: bool,
    is_repo: bool,
    head_found: bool | None = None,
    roots_shared: bool | None = None,
    has_commits: bool | None = None,
) -> Verdict | None:
    """관찰값 → 판정. ``kind`` 는 기록의 종류(``"repo"``·``"plain"``, 기록에 없으면 None).
    ``head_found`` 는 기록한 HEAD 가 지금 저장소에 있는지(기록 때 커밋이 없었으면 None),
    ``roots_shared`` 는 기록한 첫 커밋과 지금 첫 커밋이 겹치는지(HEAD 가 없을 때만 본다),
    ``has_commits`` 는 지금 저장소에 커밋이 있는지(모르면 None — 판정을 바꾸지 않는다).
    커밋 없는 저장소는 기록에 없거나 기록 때도 커밋이 없었으면 UNBORN(첫 커밋 뒤 NEW), 일반
    폴더 기록이면 PLAIN(``git init`` 만 한 폴더는 우리가 계속 담는다).
    판정할 것이 없으면(기록에 없고 저장소도 아님) None."""
    if kind is None:
        if not is_repo:
            return None
        return Verdict.UNBORN if has_commits is False else Verdict.NEW
    if kind == "plain":
        return Verdict.NEW if is_repo and has_commits is not False else Verdict.PLAIN
    if not exists:
        return Verdict.REMOVED
    if not is_repo:
        return Verdict.DEGIT
    if head_found is None and has_commits is not None:
        return Verdict.NEW if has_commits else Verdict.UNBORN
    if head_found is None or head_found:
        return Verdict.SAME
    return Verdict.LOST if roots_shared else Verdict.RECLONE


@dataclass
class RepoRecord:
    """기준점 순간의 하위 저장소 하나. ``tree`` 는 우리 저장소의 트리 — ``repo`` 면 그때
    dirty 였던 파일의 내용(``dirty`` 에 있는데 트리에 없으면 "그때 없었음"), ``plain`` 이면
    그 폴더 전체(.git 이 지워진 순간의 것). 경로는 저장소 기준."""

    path: str  # 워크스페이스 기준
    kind: str = "repo"  # "repo" | "plain"
    head: str | None = None
    roots: list[str] = field(default_factory=list)
    tracked: int = 0
    dirty: list[str] = field(default_factory=list)
    tree: str | None = None
    since: int = 0  # 이 저장소의 기준 시각(epoch 초)
    origin: str = "baseline"  # "baseline" | "new" | "reset" | "adopted"(일반 폴더였다)


@dataclass
class Notice:
    """패널 안내 줄 — 기준을 잃은 저장소(기록에 남는다)·커밋 없는 저장소(셀 때마다 — 기록에
    남지 않는다). 문장은 :func:`notice_text` 가 만든다."""

    repo: str
    cause: str  # Verdict.LOST·RECLONE·DEGIT·UNBORN 의 값
    when: int  # 다시 잡은 시각
    head: str | None = None  # 잃은 HEAD
    recorded: int = 0  # 잃은 기록의 시각


@dataclass
class Manifest:
    when: int
    repos: list[RepoRecord] = field(default_factory=list)
    notices: list[Notice] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": MANIFEST_VERSION,
                "when": self.when,
                "repos": [asdict(r) for r in self.repos],
                "notices": [asdict(n) for n in self.notices],
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, text: str) -> Manifest | None:
        try:
            d = json.loads(text)
            if d.get("version") != MANIFEST_VERSION:
                return None
            return cls(
                when=int(d["when"]),
                repos=[RepoRecord(**r) for r in d.get("repos", [])],
                notices=[Notice(**n) for n in d.get("notices", [])],
            )
        except (ValueError, TypeError, KeyError):
            return None

    def find(self, rel: str) -> RepoRecord | None:
        """``rel`` 이 그 기록의 경로거나 그 아래면 그 기록."""
        for r in self.repos:
            if rel == r.path or rel.startswith(r.path + "/"):
                return r
        return None


# ── 문구 (사용자 표면 — 영어) ─────────────────────────────────────


def _stamp(epoch: int) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(epoch))


def _short(sha: str | None) -> str:
    return sha[:7] if sha else "(none)"


def notice_text(n: Notice) -> str:
    p, now = n.repo, _stamp(n.when)
    if n.cause == Verdict.LOST.value:
        return (
            f"{p}: baseline commit {_short(n.head)} (recorded {_stamp(n.recorded)}) "
            "no longer exists in this repository — its history was rewritten and "
            f"cleaned up (rebase or reset, then gc). Changes in {p} are counted from "
            f"{now}."
        )
    if n.cause == Verdict.RECLONE.value:
        return (
            f"{p} is now a different repository (its first commit changed — it was "
            "probably deleted and cloned again). Its baseline from "
            f"{_stamp(n.recorded)} no longer applies; changes in {p} are counted "
            f"from {now}."
        )
    if n.cause == Verdict.UNBORN.value:
        return (
            f"{p} is a git repository with no commits yet — its files are not part of "
            "the list until its first commit."
        )
    return (
        f"{p} is no longer a git repository (.git was removed). Its files are now "
        f"tracked with the rest of the workspace, counted from {now}."
    )


def summary_text(r: RepoRecord, *, removed: bool) -> str:
    """요약 줄(``D P/``·``A P/``)의 diff 자리에 서는 문장."""
    files = f"{r.tracked:,} tracked file" + ("" if r.tracked == 1 else "s")
    at = f"at commit {_short(r.head)}" if r.head else "with no commits"
    if removed:
        return (
            f"{r.path}/ was removed from disk.\n"
            f"It had {files} {at} (recorded {_stamp(r.since)})."
        )
    return (
        f"{r.path}/ appeared after the baseline as a git repository\n"
        f"({files} {at}, first seen {_stamp(r.since)}).\n"
        f"Changes inside {r.path} are listed from {_stamp(r.since)}."
    )


# ── git 출력 해석 ──────────────────────────────────────────────


def parse_raw(out: str) -> dict[str, tuple[str, str]]:
    """``diff --raw -z`` → 경로 → (상태 글자, 새 mode)."""
    parts = out.split("\0")
    got: dict[str, tuple[str, str]] = {}
    i = 0
    while i + 1 < len(parts):
        meta = parts[i]
        if not meta.startswith(":"):
            i += 1
            continue
        f = meta[1:].split(" ")
        if len(f) >= 5:
            got[parts[i + 1]] = (f[4][:1], f[1])
        i += 2
    return got


def parse_numstat(out: str) -> dict[str, tuple[int | None, int | None]]:
    """``diff --numstat -z`` → 경로 → (추가, 삭제). 바이너리는 None."""
    counts: dict[str, tuple[int | None, int | None]] = {}
    for rec in out.split("\0"):
        f = rec.split("\t", 2)
        if len(f) == 3:
            counts[f[2]] = (
                int(f[0]) if f[0].isdigit() else None,
                int(f[1]) if f[1].isdigit() else None,
            )
    return counts


def parse_ls_tree(out: str) -> dict[str, tuple[str, str]]:
    """``ls-tree -r -z`` → 경로 → (mode, sha)."""
    got: dict[str, tuple[str, str]] = {}
    for rec in out.split("\0"):
        meta, _, path = rec.partition("\t")
        f = meta.split(" ")
        if path and len(f) == 3:
            got[path] = (f[0], f[2])
    return got


def entries(
    status: dict[str, tuple[str, str]],
    counts: dict[str, tuple[int | None, int | None]],
    *,
    prefix: str = "",
) -> list[dict]:
    """목록 항목 ``{path, status, added, deleted}`` — 경로 앞에 ``prefix``."""
    return [
        {
            "path": prefix + path,
            "status": st[0],
            "added": counts.get(path, (None, None))[0],
            "deleted": counts.get(path, (None, None))[1],
        }
        for path, st in sorted(status.items())
    ]


def exclude_pathspec(exclude_dirs: tuple[str, ...]) -> list[str]:
    """워크스페이스 체크포인트와 같은 제외 — 어느 깊이든."""
    spec = ["."]
    for d in exclude_dirs:
        spec.append(f":(glob,exclude)**/{d}/**")
        spec.append(f":(glob,exclude){d}/**")
    return spec


def _excluded(path: str, exclude_dirs: tuple[str, ...]) -> bool:
    return any(part in exclude_dirs for part in path.split("/")[:-1])


# ── 스냅샷 색인의 저장소 경계 (§9) ────────────────────────────────


def parse_index(out: str) -> list[tuple[str, str]]:
    """``ls-files --stage -z`` → (경로, mode)."""
    got = []
    for rec in out.split("\0"):
        meta, _, path = rec.partition("\t")
        if path:
            got.append((path, meta.split(" ", 1)[0]))
    return got


def index_paths_to_untrack(
    index: list[tuple[str, str]],
    is_repo: Callable[[str], bool],
    has_commits: Callable[[str], bool],
) -> list[str]:
    """스냅샷 색인에서 뺄 경로 — 디스크의 저장소 경계와 어긋난 항목:

    - gitlink(160000)인데 그 자리에 저장소가 없다(``.git`` 을 지웠다) → 그 gitlink. 빼면
      ``add -A`` 가 안 파일을 일반 파일로 담는다(gitlink 가 남아 있으면 그 아래를 안 담는다).
      그 자리가 커밋 없는 저장소여도(지우고 ``git init``) 뺀다 — 남은 gitlink 는 옛 커밋을
      가리킨 채 그대로 담기고(실측), 빼면 ``add`` 가 커밋 없는 저장소로 거부해 그 호출에서
      빠진다(Changes 안내).
    - 일반 파일로 담던 폴더에 커밋 있는 저장소가 생겼다 → 그 아래 항목 전부(가장 바깥 저장소만
      보고 그 안은 묻지 않는다). 빼면 ``add -A`` 가 gitlink 하나로 담는다 — 그 저장소 몫.
      ``git init`` 만 한 폴더(커밋 없음)는 그대로 우리가 담는다.

    ``is_repo``·``has_commits`` 는 디스크·하위 git 을 읽는 함수(주입) — 묻는 횟수는 색인의
    폴더 수(stat)와 새로 생긴 저장소 수(하위 git)."""
    gitlinks = [p for p, mode in index if mode == "160000"]
    out = [p for p in gitlinks if not (is_repo(p) and has_commits(p))]
    dirs: set[str] = set()
    for path, _ in index:
        parts = path.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:i]))
    adopted: list[str] = []
    for d in sorted(dirs, key=lambda p: (p.count("/"), p)):  # 바깥부터
        if any(d.startswith(a + "/") for a in adopted):
            continue
        if is_repo(d) and has_commits(d):
            adopted.append(d)
    if adopted:
        out += [p for p, _ in index if any(p.startswith(a + "/") for a in adopted)]
    return out


# ── 기록·비교 ─────────────────────────────────────────────────


class NestedRepos:
    """하위 저장소 기록을 만들고·저장하고·비교한다. ``ours`` 는 스냅샷 저장소의 git,
    ``base_env`` 는 사용자 설정을 막은 env 를 돌려주는 함수, ``tmp_dir`` 은 임시 색인 자리
    (스냅샷 저장소 안)."""

    def __init__(
        self,
        workspace: Path,
        *,
        ours: GitRunner,
        base_env: Callable[[], dict[str, str]],
        tmp_dir: Path,
        exclude_dirs: tuple[str, ...],
        file_max_bytes: int = -1,
    ):
        self.workspace = workspace
        self.ours = ours
        self.base_env = base_env
        self.tmp_dir = tmp_dir
        self.exclude_dirs = exclude_dirs
        self.file_max_bytes = file_max_bytes
        self._cache: dict[str, Manifest] = {}  # 기록 커밋 → 기록 (커밋은 불변)
        self._index_src: dict[str, str] = {}  # 저장소 경로 → 원본 색인 경로
        self._index_sig: dict[
            str, tuple | None
        ] = {}  # 저장소 경로 → 복사한 원본의 stat

    # ── 하위 git (읽기만) ──

    def _sub_env(self) -> dict[str, str]:
        env = {k: v for k, v in self.base_env().items() if k not in _REPO_ENV}
        env["GIT_OPTIONAL_LOCKS"] = "0"
        return env

    def _index_copy(self, rel: str) -> Path:
        """하위 저장소 색인의 우리 쪽 사본 — 원본이 바뀌었으면(크기·mtime·inode) 다시
        복사한다(mtime 까지 — racy-git 판단을 원본과 같게, ``_disk_tree`` 와 같은 이유).
        원본이 없으면(아직 add 한 적 없음) 사본도 없다 — git 은 빈 색인으로 읽는다."""
        src = self._index_src.get(rel)
        if src is None:
            src = self.sub(
                rel, "rev-parse", "--path-format=absolute", "--git-path", "index"
            ).stdout.strip()
            self._index_src[rel] = src
        key = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16]
        dst = self.tmp_dir / f"index.sub-{key}"
        try:
            st = os.stat(src) if src else None
            sig = (st.st_size, st.st_mtime_ns, st.st_ino) if st else None
        except OSError:
            sig = None
            self._index_src.pop(
                rel, None
            )  # 저장소가 바뀌었을 수 있다 — 다음엔 다시 찾는다
        if self._index_sig.get(rel, ()) == sig and (dst.exists() or sig is None):
            return dst
        tmp = dst.with_name(f"{dst.name}.{os.getpid()}-{threading.get_ident()}")
        try:
            if sig is not None:
                shutil.copy2(src, tmp)
                os.replace(tmp, dst)
            else:
                dst.unlink(missing_ok=True)
        except OSError:
            tmp.unlink(missing_ok=True)
        self._index_sig[rel] = sig
        return dst

    def refresh_index_copy(self, rel: str) -> None:
        """우리 쪽 색인 사본의 stat 정보를 지금 디스크로 — ``update-index --refresh`` 는
        ``GIT_INDEX_FILE``(사본)에만 쓴다. 하위 git 의 읽기 명령은 잠금 없는 읽기라 사본을
        갱신하지 않으므로, 이것 없이는 stat 이 낡은 파일을 셈마다 다시 해시한다(실측: 2만
        파일 1.2 s → 0.4 s). 내용(blob sha)은 바뀌지 않는다 — 사본은 여전히 원본의 판이다."""
        self.sub(rel, "update-index", "-q", "--refresh", index=True)

    def sub(
        self, rel: str, *args: str, index: bool = False
    ) -> subprocess.CompletedProcess:
        """하위 저장소의 git — 읽기만. ``index`` 면 색인을 읽는 명령이라 우리 쪽 사본으로."""
        env = self._sub_env()
        if index:
            env["GIT_INDEX_FILE"] = str(self._index_copy(rel))
        return subprocess.run(
            [
                "git",
                "-c",
                "safe.directory=*",
                "-c",
                "core.fsmonitor=false",
                "-C",
                str(self.workspace / rel),
                *args,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )

    def _spec(self) -> list[str]:
        return ["--", *exclude_pathspec(self.exclude_dirs)]

    def head(self, rel: str) -> str | None:
        return (
            self.sub(rel, "rev-parse", "-q", "--verify", "HEAD").stdout.strip() or None
        )

    def dirty(self, rel: str) -> list[str]:
        """수정·삭제·untracked 경로(저장소 기준, ``--untracked-files=all``). untracked
        하위 저장소는 git 처럼 ``inner/``."""
        out = self.sub(
            rel,
            "status",
            "--porcelain",
            "-z",
            "--untracked-files=all",
            "--no-renames",
            *self._spec(),
            index=True,
        ).stdout
        return [rec[3:] for rec in out.split("\0") if len(rec) > 3]

    def is_repo(self, rel: str) -> bool:
        p = self.workspace / rel
        return p.is_dir() and not p.is_symlink() and (p / ".git").exists()

    # ── 디스크 내용 → 우리 저장소 ──

    def _too_big(self, abs_path: Path) -> bool:
        if self.file_max_bytes < 0:
            return False
        try:
            return abs_path.lstat().st_size > self.file_max_bytes
        except OSError:
            return False

    def _disk_entries(self, rels: list[str]) -> dict[str, tuple[str, str]]:
        """워크스페이스 기준 경로들의 지금 내용 → (mode, sha). 내용은 우리 저장소에 쓴다
        (``hash-object -w --no-filters`` — 기록 때와 셀 때 같은 해시). 없는 것·상한을 넘는
        것은 빠진다. 폴더는 저장소면 gitlink(그 HEAD), 아니면 빠진다."""
        got: dict[str, tuple[str, str]] = {}
        files: list[tuple[str, str]] = []
        for rel in rels:
            p = self.workspace / rel
            try:
                st = p.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                r = self.ours("hash-object", "-w", "--stdin", input=os.readlink(p))
                if r.returncode == 0 and r.stdout.strip():
                    got[rel] = ("120000", r.stdout.strip())
            elif stat.S_ISDIR(st.st_mode):
                if (p / ".git").exists():
                    h = self.head(rel)
                    if h:
                        got[rel] = ("160000", h)
            elif stat.S_ISREG(st.st_mode) and "\n" not in rel:
                if self._too_big(p):
                    continue
                mode = "100755" if st.st_mode & stat.S_IXUSR else "100644"
                files.append((rel, mode))
        if files:
            r = self.ours(
                "hash-object",
                "-w",
                "--no-filters",
                "--stdin-paths",
                input="".join(str(self.workspace / rel) + "\n" for rel, _ in files),
            )
            shas = r.stdout.split()
            if r.returncode == 0 and len(shas) == len(files):
                for (rel, mode), sha in zip(files, shas):
                    got[rel] = (mode, sha)
        return got

    def _tree(self, items: dict[str, tuple[str, str]]) -> str | None:
        """경로 → (mode, sha) 로 트리를 만든다 — 임시 색인, 우리 저장소 안."""
        if not items:
            return EMPTY_TREE
        tmp = self.tmp_dir / f"index.nested-{os.getpid()}-{threading.get_ident()}"
        env = {"GIT_INDEX_FILE": str(tmp)}
        kind = {"160000": "commit", "040000": "tree"}
        info = "".join(
            f"{mode} {kind.get(mode, 'blob')} {sha}\t{path}\0"
            for path, (mode, sha) in sorted(items.items())
        )
        try:
            r = self.ours(
                "update-index", "-z", "--add", "--index-info", input=info, env=env
            )
            if r.returncode != 0:
                return None
            return self.ours("write-tree", env=env).stdout.strip() or None
        finally:
            for p in (tmp, tmp.with_name(tmp.name + ".lock")):
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass

    def _tree_items(self, tree: str | None) -> dict[str, tuple[str, str]]:
        if not tree or tree == EMPTY_TREE:
            return {}
        return parse_ls_tree(self.ours("ls-tree", "-r", "-z", tree).stdout)

    def _import_head_blob(self, rel: str, head: str, q: str) -> tuple[str, str] | None:
        """하위 저장소 커밋의 파일 하나를 우리 저장소로 — ``git rm --cached`` 처럼 추적에서
        빠졌는데 디스크엔 남은 경로의 비교 기준. 바이트 그대로(임시 파일 경유)."""
        ls = self.sub(rel, "ls-tree", "-z", head, "--", q).stdout.split("\0")[0]
        meta, _, _ = ls.partition("\t")
        f = meta.split(" ")
        if len(f) != 3 or f[1] != "blob":
            return None
        tmp = self.tmp_dir / f"blob.nested-{os.getpid()}-{threading.get_ident()}"
        try:
            with open(tmp, "wb") as fh:
                r = subprocess.run(
                    ["git", "-c", "safe.directory=*", "-C", str(self.workspace / rel)]
                    + ["cat-file", "blob", f[2]],
                    env=self._sub_env(),
                    stdout=fh,
                    stderr=subprocess.DEVNULL,
                    timeout=300,
                    check=False,
                )
            if r.returncode != 0:
                return None
            h = self.ours("hash-object", "-w", "--no-filters", str(tmp))
            sha = h.stdout.strip()
            return (f[0], sha) if h.returncode == 0 and sha else None
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    # ── 기록 ──

    def record_repo(
        self,
        rel: str,
        now: int,
        *,
        origin: str = "baseline",
        dirty: list[str] | None = None,
    ) -> RepoRecord:
        """지금 상태로 저장소 하나를 기록 — dirty 파일 내용은 우리 저장소에 담는다."""
        head = self.head(rel)
        roots: list[str] = []
        tracked = 0
        if head:
            roots = sorted(
                self.sub(rel, "rev-list", "--max-parents=0", "HEAD").stdout.split()
            )
            tracked = self.sub(rel, "ls-files", "-z", index=True).stdout.count("\0")
        paths = []
        for d in self.dirty(rel) if dirty is None else dirty:
            q = d.rstrip("/")
            if (
                q
                and not _excluded(q, self.exclude_dirs)
                and not self._too_big(self.workspace / rel / q)
            ):
                paths.append(q)
        items = self._disk_entries([f"{rel}/{q}" for q in paths])
        tree = self._tree({k[len(rel) + 1 :]: v for k, v in items.items()})
        return RepoRecord(
            path=rel,
            kind="repo",
            head=head,
            roots=roots,
            tracked=tracked,
            dirty=sorted(paths),
            tree=tree,
            since=now,
            origin=origin,
        )

    def record_plain(self, rel: str, now: int, disk_tree: str) -> RepoRecord:
        """저장소가 아니게 된 폴더 — 지금 디스크 트리의 그 폴더가 그 아래의 기준."""
        return RepoRecord(
            path=rel, kind="plain", tree=self._subtree(disk_tree, rel), since=now
        )

    def record_adopted(self, rel: str, now: int, folder_tree: str) -> RepoRecord:
        """일반 파일로 담던 폴더가 커밋 있는 저장소가 된 자리 — 기준은 그 폴더의 기준 트리
        (``folder_tree``, 우리 저장소). 기록 HEAD 는 지금 HEAD, dirty 덮어쓰기는 기준 트리의
        blob 중 HEAD 와 다른 것(이미 우리 저장소에 있는 blob 을 가리킨다 — 복사 없음)과 HEAD
        에만 있는 경로("그때 없었음"). 그래서 셈은 "기준점 이후 실제로 바뀐 파일" 로 이어진다."""
        r = self.record_repo(rel, now, origin="adopted", dirty=[])
        out = self.sub(rel, "ls-tree", "-r", "-z", r.head).stdout if r.head else ""
        base, head = (
            {p: v for p, v in items.items() if not _excluded(p, self.exclude_dirs)}
            for items in (self._tree_items(folder_tree), parse_ls_tree(out))
        )
        dirty = sorted(
            {p for p, v in base.items() if head.get(p) != v}
            | {p for p in head if p not in base}
        )
        tree = self._tree({p: base[p] for p in dirty if p in base})
        r.dirty, r.tree = dirty, tree
        return r

    def _subtree(self, tree: str, rel: str) -> str:
        """트리 안 폴더의 트리 — 없거나 폴더가 아니면(gitlink·파일) 빈 트리."""
        # `<tree>:<path>^{tree}` 는 안 된다 — 콜론 뒤는 전부 경로다
        out = self.ours("ls-tree", "-z", tree, "--", rel).stdout.split("\0")[0]
        meta, _, path = out.partition("\t")
        f = meta.split(" ")
        return f[2] if path == rel and len(f) == 3 and f[1] == "tree" else EMPTY_TREE

    def snapshot(self, rels: list[tuple[str, list[str] | None]], now: int) -> Manifest:
        """기준점 순간의 기록 — ``rels`` 는 (경로, 이미 읽은 dirty 또는 None)."""
        return Manifest(
            when=now,
            repos=[self.record_repo(rel, now, dirty=d) for rel, d in rels],
        )

    # ── 저장 (우리 저장소 — 커밋 하나, ref 는 snapshot.py 가) ──

    def save(self, m: Manifest) -> str | None:
        """기록을 커밋으로 — 트리 ``{manifest.json, t/<i>: 담아 둔 트리…}``. 담아 둔 트리를
        트리가 가리키므로 ref 하나가 전부를 gc 에서 붙잡는다."""
        blob = self.ours(
            "hash-object", "-w", "--stdin", input=m.to_json()
        ).stdout.strip()
        if not blob:
            return None
        lines = [f"100644 blob {blob}\t{MANIFEST_NAME}"]
        held = [r.tree for r in m.repos if r.tree and r.tree != EMPTY_TREE]
        if held:
            sub = "".join(f"040000 tree {t}\t{i}\n" for i, t in enumerate(held))
            t = self.ours("mktree", input=sub).stdout.strip()
            if not t:
                return None
            lines.append(f"040000 tree {t}\tt")
        root = self.ours("mktree", input="\n".join(lines) + "\n").stdout.strip()
        if not root:
            return None
        c = self.ours("commit-tree", root, "-m", RECORD_LABEL).stdout.strip() or None
        if c:
            self._cache[c] = m
        return c

    def load(self, commit: str | None) -> Manifest | None:
        if not commit:
            return None
        if commit in self._cache:
            return self._cache[commit]
        r = self.ours("cat-file", "blob", f"{commit}:{MANIFEST_NAME}")
        m = Manifest.from_json(r.stdout) if r.returncode == 0 else None
        if m is not None:
            self._cache[commit] = m
        return m

    # ── 판정과 처리 ──

    def observe(self, r: RepoRecord | None, rel: str) -> Verdict | None:
        exists = (self.workspace / rel).is_dir() and not (
            self.workspace / rel
        ).is_symlink()
        repo = self.is_repo(rel)
        head_found = roots_shared = has_commits = None
        if repo and (r is None or r.kind != "repo" or not r.head):
            has_commits = self.head(rel) is not None
        if r is not None and r.kind == "repo" and repo and r.head:
            head_found = (
                self.sub(rel, "cat-file", "-e", f"{r.head}^{{commit}}").returncode == 0
            )
            if not head_found:
                now = self.sub(
                    rel, "rev-list", "--max-parents=0", "HEAD"
                ).stdout.split()
                roots_shared = bool(set(now) & set(r.roots))
        return judge(
            r.kind if r else None,
            exists=exists,
            is_repo=repo,
            head_found=head_found,
            roots_shared=roots_shared,
            has_commits=has_commits,
        )

    def reconcile(
        self,
        m: Manifest,
        disk_tree: str,
        found: list[str],
        now: int,
        verdicts: dict[str, Verdict | None],
        base: str | None = None,
    ) -> tuple[Manifest, bool]:
        """기록을 지금과 맞춘다 — 기준을 잃은 저장소만 지금으로(안내 줄과 함께), 저장소가
        아니게 된 폴더는 일반 폴더 기록으로(``disk_tree`` 는 그 자리 gitlink 를 뺀 트리),
        ``found``(기록에 없는 새 저장소)는 그 순간으로 — 기준점(``base`` 커밋)에 그 자리가
        일반 폴더로 있었으면 그 폴더 트리를 기준으로 넘겨받는다(:meth:`record_adopted`).
        일반 폴더 기록이 저장소가 된 것도 그 기록의 트리로 넘겨받는다. 커밋 없는 저장소
        (UNBORN)는 그대로 — 안내는 셀 때마다. 다른 기록은 그대로. ``verdicts`` 는 기록
        경로마다 :meth:`observe` 한 판정. 바뀌었으면 (새 기록, True)."""
        repos: list[RepoRecord] = []
        notices = list(m.notices)
        changed = False
        for r in m.repos:
            v = verdicts.get(r.path)
            if v in (Verdict.LOST, Verdict.RECLONE):
                repos.append(self.record_repo(r.path, now, origin="reset"))
                notices = [n for n in notices if n.repo != r.path]
                notices.append(Notice(r.path, v.value, now, r.head, r.since))
                changed = True
            elif v is Verdict.DEGIT:
                repos.append(self.record_plain(r.path, now, disk_tree))
                notices = [n for n in notices if n.repo != r.path]
                notices.append(Notice(r.path, v.value, now, r.head, r.since))
                changed = True
            elif v is Verdict.NEW and r.kind == "plain":
                # 일반 폴더가 다시 저장소가 됐다 — "no longer a git repository" 안내는 낡았다
                repos.append(self.record_adopted(r.path, now, r.tree or EMPTY_TREE))
                notices = [n for n in notices if n.repo != r.path]
                changed = True
            elif v is Verdict.NEW:  # 기록 때 커밋이 없던 저장소의 첫 커밋
                repos.append(self.record_repo(r.path, now, origin="new"))
                changed = True
            else:
                repos.append(r)
        for rel in found:
            if m.find(rel) is None and self.observe(None, rel) is Verdict.NEW:
                folder = self._subtree(base, rel) if base else EMPTY_TREE
                if folder != EMPTY_TREE:
                    repos.append(self.record_adopted(rel, now, folder))
                else:
                    repos.append(self.record_repo(rel, now, origin="new"))
                changed = True
        if not changed:
            return m, False
        repos.sort(key=lambda r: r.path)
        return Manifest(when=m.when, repos=repos, notices=notices), True

    # ── 비교 ──

    def group(self, r: RepoRecord) -> dict:
        """그룹 머리 — ``{path, state, head_from, head_to, commits, since}``. ``state`` 는
        ``normal``·``new``(기준점 뒤에 생김)·``reset``(기준을 잃어 다시 잡음)·``removed``."""
        if not self.is_repo(r.path):
            return {
                "path": r.path,
                "state": "removed",
                "head_from": _short(r.head) if r.head else None,
                "head_to": None,
                "commits": 0,
                "since": r.since,
            }
        now = self.head(r.path)
        commits = 0
        if r.head and now and now != r.head:
            out = self.sub(r.path, "rev-list", "--count", f"{r.head}..{now}").stdout
            commits = int(out.strip()) if out.strip().isdigit() else 0
        state = {"new": "new", "reset": "reset"}.get(r.origin, "normal")
        return {
            "path": r.path,
            "state": state,
            "head_from": _short(r.head) if r.head else None,
            "head_to": _short(now) if now else None,
            "commits": commits,
            "since": r.since,
        }

    def listing(self, r: RepoRecord, disk_tree: str) -> list[dict]:
        """그 기록 ↔ 지금 — 목록 항목(경로는 워크스페이스 기준). ``repo`` 기록이면 항목에
        ``repo``, 요약 줄이면 ``summary`` 도."""
        if r.kind == "plain":
            now = self._plain_now(r, disk_tree)
            st, ns = self._ours_diff(r.tree or EMPTY_TREE, now)
            return entries(st, ns, prefix=r.path + "/")
        tag = {"repo": r.path}
        if not self.is_repo(r.path):  # 지워졌다(판정 REMOVED) — 한 줄
            return [
                {"path": r.path, "status": "D", "added": None, "deleted": None}
                | tag
                | {"summary": True}
            ]
        out: list[dict] = []
        if r.origin == "new":
            out.append(
                {"path": r.path, "status": "A", "added": None, "deleted": None}
                | tag
                | {"summary": True}
            )
        plan = self._plan(r)
        if plan is None:
            return out
        st, ns, base, now = plan
        got = entries(st, ns, prefix=r.path + "/")
        if base is not None:
            ost, ons = self._ours_diff(base, now)
            got += entries(ost, ons, prefix=r.path + "/")
        got = [e for e in got if not self._too_big(self.workspace / e["path"])]
        out += [e | tag for e in sorted(got, key=lambda e: e["path"])]
        return out

    def diff(
        self, r: RepoRecord, rel: str, disk_tree: str | None
    ) -> tuple[str | None, str]:
        """그 기록 아래 경로 하나의 unified diff(경로는 워크스페이스 기준 ``a/P/…``). 요약
        줄의 경로(기록 경로 자체)면 요약 문장."""
        pre = ("--src-prefix=a/" + r.path + "/", "--dst-prefix=b/" + r.path + "/")
        if rel == r.path:
            if r.kind == "repo" and not self.is_repo(r.path):
                return summary_text(r, removed=True), ""
            if r.kind == "repo" and r.origin == "new":
                return summary_text(r, removed=False), ""
            return "", ""
        q = rel[len(r.path) + 1 :]
        if r.kind == "plain":
            if disk_tree is None:
                return None, "nothing to compare"
            now = self._plain_now(r, disk_tree)
            out = self.ours(
                "diff", "--no-renames", *pre, r.tree or EMPTY_TREE, now, "--", q
            )
            return (
                (out.stdout, "") if out.returncode == 0 else (None, out.stderr.strip())
            )
        if not self.is_repo(r.path):
            return "", ""
        plan = self._plan(r, only=q)
        if plan is None:
            return None, f"cannot read the repository {r.path}"
        st, _, base, now = plan
        if base is not None and q not in st:
            out = self.ours("diff", "--no-renames", *pre, base, now, "--", q)
        else:
            out = self.sub(
                r.path, *_SUB_DIFF, *pre, r.head or EMPTY_TREE, "--", q, index=True
            )
        return (out.stdout, "") if out.returncode == 0 else (None, out.stderr.strip())

    def _plain_now(self, r: RepoRecord, disk_tree: str) -> str:
        return self._subtree(disk_tree, r.path)

    def _ours_diff(self, a: str, b: str) -> tuple[dict, dict]:
        common = ("diff", "--no-renames", "-z")
        st = self.ours(*common, "--raw", a, b)
        ns = self.ours(*common, "--numstat", a, b)
        if st.returncode != 0 or ns.returncode != 0:
            return {}, {}
        return parse_raw(st.stdout), parse_numstat(ns.stdout)

    def _plan(
        self, r: RepoRecord, only: str | None = None
    ) -> tuple[dict, dict, str | None, str | None] | None:
        """하위 git 의 "기록 HEAD ↔ 작업 트리"(``st``·``ns``) + 따로 비교할 경로의 트리 둘
        (``base``·``now`` — 기록 때 dirty 였던 경로·untracked·추적에서 빠졌는데 남은 경로).
        ``only`` 면 그 경로 하나로 좁힌다."""
        spec = self._spec() if only is None else ["--", only]
        base_rev = r.head or EMPTY_TREE
        self.refresh_index_copy(r.path)
        st_r = self.sub(r.path, *_SUB_DIFF, "-z", "--raw", base_rev, *spec, index=True)
        ns_r = self.sub(
            r.path, *_SUB_DIFF, "-z", "--numstat", base_rev, *spec, index=True
        )
        un_r = self.sub(
            r.path,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            *spec,
            index=True,
        )
        if st_r.returncode != 0 or ns_r.returncode != 0 or un_r.returncode != 0:
            return None
        st, ns = parse_raw(st_r.stdout), parse_numstat(ns_r.stdout)
        untracked = {u.rstrip("/") for u in un_r.stdout.split("\0") if u}
        untracked = {u for u in untracked if u and not _excluded(u, self.exclude_dirs)}
        dirty = set(r.dirty) if only is None else set(r.dirty) & {only}
        untracked_kept = {u for u in untracked if st.get(u, ("",))[0] == "D"}
        override = dirty | untracked | untracked_kept
        for p in override:
            st.pop(p, None)
            ns.pop(p, None)
        if not override:
            return st, ns, None, None
        base_items = {k: v for k, v in self._tree_items(r.tree).items() if k in dirty}
        for q in untracked_kept - dirty:
            got = self._import_head_blob(r.path, base_rev, q) if r.head else None
            if got:
                base_items[q] = got
        now_items = self._disk_entries([f"{r.path}/{q}" for q in sorted(override)])
        now_items = {k[len(r.path) + 1 :]: v for k, v in now_items.items()}
        base = self._tree(base_items)
        now = self._tree(now_items)
        if base is None or now is None:
            return None
        return st, ns, base, now
