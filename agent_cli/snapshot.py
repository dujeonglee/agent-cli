"""워크스페이스 스냅샷 — git 그림자 저장소 (docs/snapshot/DESIGN.md, v10.36.0).

"손대기 전에 백업하라" 는 지시는 로컬 35B 급이 무시한다(0/5). 그래서 **구조**로
푼다: 하니스가 런 시작과 변경 도구 호출 직전마다 작업 디렉터리를 커밋해 두고,
모델에겐 "망쳤으면 `snapshot` 도구(mode restore)" 만 알린다(어댑터 흉내 실험:
db-wal 0/5 → 2/3, 세 trial 모두 안내대로 복구). 복구는 네이티브 도구
(``tools/snapshot_tool.py``)다 — 쉘로 우리 CLI 를 치게 하는 안은 PATH 에 기대고
도구와 중복이라 출시 전에 뺐다.

**그림자 저장소**: 저장소(`GIT_DIR`)는 `.agent-cli/snapshot.git`(bare), 작업 트리
(`GIT_WORK_TREE`)는 워크스페이스. 워크스페이스에 `.git` 이 생기지 않고, 이미
저장소여도 그 index·HEAD·히스토리를 건드리지 않는다.

git 의 두 가지 성질이 설계를 정한다(실측):
- ``add --force`` 는 ``info/exclude`` 까지 무시한다 — 워크스페이스의 ``.gitignore``
  를 무시하려고 ``--force`` 를 쓰므로, 우리 제외 목록은 **pathspec** ``:(exclude)``
  로 건다.
- 작업 트리 안의 다른 git 저장소는 gitlink(커밋 sha 하나)로만 기록되고 그 안의
  파일은 ``add`` 가 거부한다(``Pathspec … is in submodule``). 하위 저장소는
  HEAD·dirty 목록만 기록하고, 복구 안내에서 "그 저장소의 git 으로" 라고 가른다.

모든 git 호출은 사용자 설정을 보지 않는다(``GIT_CONFIG_GLOBAL=/dev/null``,
``GIT_CONFIG_NOSYSTEM=1``) — gpgsign·hooksPath·autocrlf·safe.directory 가
한꺼번에 무력화되고, 작성자는 고정(하니스 내부 커밋이다).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

#: 제외 디렉터리 — 어느 깊이든. ``.git`` 은 git 이 알아서 안 담는다(하위 저장소는 gitlink).
EXCLUDE_DIRS: tuple[str, ...] = (".agent-cli", "node_modules", "__pycache__", ".venv")
SNAPSHOT_DIR_NAME = "snapshot.git"
_AUTHOR = ("agent-cli snapshot", "snapshot@agent-cli")


@dataclass
class NestedRepo:
    path: str  # 워크스페이스 기준 상대 경로
    head: str
    dirty: list[str] = field(default_factory=list)  # 수정·untracked (상대 경로)


@dataclass
class SnapshotStats:
    files: int = 0
    seconds: float = 0.0
    commits: int = 0


def git_available() -> tuple[bool, str]:
    exe = shutil.which("git")
    if not exe:
        return False, "git not found"
    try:
        out = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=10, check=False
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"git --version failed: {exc}"
    return True, out


class GitSnapshot:
    """세션당 하나 — 모든 루프(main·상주·스킬·일회성)의 훅이 같은 인스턴스를 부른다."""

    def __init__(
        self,
        workspace: Path,
        *,
        git_dir: Path | None = None,
        min_interval_s: float = 10.0,
        file_max_bytes: int = -1,
        file_count_max: int = -1,
        exclude_dirs: tuple[str, ...] = EXCLUDE_DIRS,
    ):
        self.workspace = Path(workspace).resolve()
        self.git_dir = (
            Path(git_dir).resolve()
            if git_dir
            else self.workspace / ".agent-cli" / SNAPSHOT_DIR_NAME
        )
        self.min_interval_s = min_interval_s
        self.file_max_bytes = file_max_bytes
        self.file_count_max = file_count_max
        self.exclude_dirs = tuple(exclude_dirs)
        self.enabled = False
        self.reason = "not initialised"
        self.nested: list[NestedRepo] = []
        self.stats = SnapshotStats()
        self.per_turn = True  # 파일 수 상한을 넘으면 시작 스냅샷만
        self._lock = threading.Lock()
        self._last_checkpoint = 0.0
        self._big_files: list[str] = []

    # ── git plumbing ─────────────────────────────────────────

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "GIT_DIR": str(self.git_dir),
                "GIT_WORK_TREE": str(self.workspace),
                # 사용자 전역/시스템 설정 차단 — 어느 머신에서나 같은 동작
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_AUTHOR_NAME": _AUTHOR[0],
                "GIT_AUTHOR_EMAIL": _AUTHOR[1],
                "GIT_COMMITTER_NAME": _AUTHOR[0],
                "GIT_COMMITTER_EMAIL": _AUTHOR[1],
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        return env

    def _git(self, *args: str, timeout: float = 300) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-c", "safe.directory=*", "-c", "core.hooksPath=/dev/null", *args],
            cwd=str(self.workspace),
            env=self._env(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def _pathspec(self) -> list[str]:
        spec = ["--", "."]
        for d in self.exclude_dirs:
            spec.append(f":(glob,exclude)**/{d}/**")
            spec.append(f":(glob,exclude){d}/**")
        for f in self._big_files:
            spec.append(f":(exclude){f}")
        return spec

    # ── lifecycle ────────────────────────────────────────────

    def init(self) -> bool:
        """저장소 준비 + 하위 저장소 스캔 + 시작 커밋. 실패는 ``reason`` 에."""
        ok, why = git_available()
        if not ok:
            self.enabled, self.reason = False, why
            return False
        t0 = time.monotonic()
        try:
            if not (self.git_dir / "HEAD").exists():
                self.git_dir.parent.mkdir(parents=True, exist_ok=True)
                # init 은 GIT_DIR/GIT_WORK_TREE 없이 — 둘이 env 에 있으면 git 이
                # "GIT_WORK_TREE not allowed without GIT_DIR" 로 거부한다(실측).
                env = {
                    k: v
                    for k, v in self._env().items()
                    if k not in ("GIT_DIR", "GIT_WORK_TREE")
                }
                r = subprocess.run(
                    ["git", "init", "--bare", "-q", str(self.git_dir)],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
                if r.returncode != 0:
                    self.enabled, self.reason = (
                        False,
                        f"git init failed: {r.stderr.strip()}",
                    )
                    return False
            self.nested = self._scan_nested()
            self.enabled = True
            self._apply_caps()
            sha = self.checkpoint("session start", force=True)
            self.stats.seconds = time.monotonic() - t0
            if sha is None and self.head() is None:
                # 빈 워크스페이스 — 담을 게 없다. 저장소는 준비됐다.
                pass
            self.reason = "ok"
            return True
        except (OSError, subprocess.SubprocessError) as exc:
            self.enabled, self.reason = False, f"{type(exc).__name__}: {exc}"
            return False

    def _apply_caps(self) -> None:
        """상한 장치 — 기본 무제한. 파일 수를 넘으면 턴 단위 체크포인트를 끄고,
        크기를 넘는 파일은 pathspec 으로 뺀다(한 번 걷는 비용은 상한이 켜졌을 때만)."""
        if self.file_max_bytes < 0 and self.file_count_max < 0:
            return
        count, big = 0, []
        for root, dirs, files in os.walk(self.workspace):
            dirs[:] = [d for d in dirs if d not in self.exclude_dirs and d != ".git"]
            for name in files:
                count += 1
                if self.file_max_bytes >= 0:
                    p = Path(root) / name
                    try:
                        if p.stat().st_size > self.file_max_bytes:
                            big.append(str(p.relative_to(self.workspace)))
                    except OSError:
                        pass
        self._big_files = big
        self.stats.files = count
        if self.file_count_max >= 0 and count > self.file_count_max:
            self.per_turn = False

    def _scan_nested(self) -> list[NestedRepo]:
        found: list[NestedRepo] = []
        for root, dirs, files in os.walk(self.workspace):
            dirs[:] = [d for d in dirs if d not in self.exclude_dirs]
            if Path(root) != self.workspace and (".git" in dirs or ".git" in files):
                rel = str(Path(root).relative_to(self.workspace))
                found.append(self._describe_nested(root, rel))
                dirs[:] = []  # 저장소 안은 더 안 들어간다
                continue
            if ".git" in dirs:
                dirs.remove(".git")
        return found

    def _describe_nested(self, abs_dir: str, rel: str) -> NestedRepo:
        env = {
            k: v
            for k, v in self._env().items()
            if k not in ("GIT_DIR", "GIT_WORK_TREE")
        }

        def run(*a):
            return subprocess.run(
                ["git", "-c", "safe.directory=*", "-C", abs_dir, *a],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )

        head = run("rev-parse", "HEAD").stdout.strip() or "(no commits)"
        st = run("status", "--porcelain", "--untracked-files=all").stdout
        dirty = [ln[3:].strip() for ln in st.splitlines() if ln.strip()]
        return NestedRepo(path=rel, head=head, dirty=dirty)

    def checkpoint(self, label: str, *, force: bool = False) -> str | None:
        """변경 도구 직전 커밋. 변경이 없으면 None(커밋 없음). 최소 간격 안이면 건너뜀."""
        if not self.enabled:
            return None
        if not force and not self.per_turn:
            return None
        with self._lock:
            now = time.monotonic()
            if not force and now - self._last_checkpoint < self.min_interval_s:
                return None
            self._last_checkpoint = now
            r = self._git("add", "-A", "--force", *self._pathspec())
            if r.returncode != 0:
                return None
            if self.head() is None:
                staged = self._git("ls-files", "--cached").stdout.strip()
                if not staged:
                    return None
            else:
                q = self._git("diff", "--cached", "--quiet")
                if q.returncode == 0:
                    return None  # 변화 없음
            c = self._git("commit", "-q", "-m", label)
            if c.returncode != 0:
                return None
            self.stats.commits += 1
            return self.head()

    # ── queries ──────────────────────────────────────────────

    def head(self) -> str | None:
        r = self._git("rev-parse", "--verify", "-q", "HEAD")
        return r.stdout.strip() or None

    def log(self, limit: int = 20) -> list[tuple[str, str]]:
        r = self._git("log", f"-{limit}", "--format=%h%x09%s")
        return [
            tuple(ln.split("\t", 1))  # type: ignore[misc]
            for ln in r.stdout.splitlines()
            if "\t" in ln
        ]

    def changed_files(
        self, since: str = "HEAD~1", to: str = "HEAD"
    ) -> list[tuple[str, str]]:
        r = self._git("diff", "--name-status", since, to)
        out = []
        for ln in r.stdout.splitlines():
            parts = ln.split("\t")
            if len(parts) >= 2:
                out.append((parts[0], parts[-1]))
        return out

    def commits_touching(self, path: str) -> list[str]:
        r = self._git("log", "--format=%H", "--", path)
        return [ln for ln in r.stdout.splitlines() if ln]

    def _has(self, commit: str, path: str) -> bool:
        return self._git("cat-file", "-e", f"{commit}:{path}").returncode == 0

    def _blob(self, commit: str, path: str) -> str | None:
        r = self._git("rev-parse", "--verify", "-q", f"{commit}:{path}")
        return r.stdout.strip() or None

    def _disk_blob(self, path: str) -> str | None:
        p = self.workspace / path
        if not p.is_file():
            return None
        r = self._git("hash-object", str(p))
        return r.stdout.strip() or None

    def restore(self, path: str, *, at: str | None = None) -> tuple[bool, str]:
        """``path`` 를 되돌린다 (``snapshot`` 도구가 부른다). ``at`` 이 없으면 **지금
        디스크와 다른 가장 최근 스냅샷**(파일이 없으면 가장 최근에 있던 스냅샷) —
        망가진 상태가 다음 체크포인트에서 HEAD 로 커밋돼 있어도 그 전 판을 고른다."""
        if not self.enabled:
            return False, self.reason
        path = path.rstrip("/")
        if at is None:
            current = self._disk_blob(path)
            candidates = self.commits_touching(path) or (
                [self.head()] if self.head() else []
            )
            at = None
            for c in candidates:
                if not c or not self._has(c, path):
                    continue
                if current is not None and self._blob(c, path) == current:
                    continue  # 지금과 같은 내용 — 더 전으로
                at = c
                break
            if at is None:
                return False, f"no snapshot holds a different version of {path!r}"
        r = self._git("checkout", at, "--", path)
        if r.returncode != 0:
            return False, r.stderr.strip() or "checkout failed"
        return True, f"restored {path} from {at[:8]}"

    # ── model-facing ─────────────────────────────────────────

    def notice(self) -> str:
        """시스템 섹션 본문(영어 — 모델에게 가는 문구). 시작 커밋 성공 뒤에만 쓴다."""
        intro = (
            "The harness keeps git snapshots of this working directory in "
            "`.agent-cli/snapshot.git` (a bare repository; this directory is its work "
            "tree — nothing is added to the directory itself). A snapshot was taken "
            "when this session started and one is taken again just before each "
            "shell / edit_file / write_file call, so the state before your last "
            "command is always on record."
        )
        howto = (
            "If an input file gets damaged or disappears, call the `snapshot` tool "
            "with mode `restore` and the file's path — it brings back the most "
            "recent snapshot whose content differs from what is on disk now. "
            "Mode `list` shows the snapshots; `at` picks a specific one."
        )
        lines = [intro, howto]
        if self.nested:
            names = ", ".join(f"`{n.path}`" for n in self.nested)
            lines.append(
                f"Nested git repositories ({names}) are recorded as references only: "
                f"restore their tracked files with `git -C <repo> checkout -- <path>`; "
                f"uncommitted changes inside them are NOT snapshotted."
            )
        return "\n".join(lines)


# ── 세션 싱글턴 (훅과 CLI 가 같은 인스턴스를 본다) ──────────────

_STORE: GitSnapshot | None = None
_STORE_LOCK = threading.Lock()


def get_snapshot(
    workspace: Path | None = None, *, create: bool = False
) -> GitSnapshot | None:
    """워크스페이스당 하나. ``create`` 면 없을 때 만들어 ``init`` 까지 한다."""
    global _STORE
    ws = Path(workspace or Path.cwd()).resolve()
    with _STORE_LOCK:
        if _STORE is not None and _STORE.workspace == ws:
            return _STORE
        if not create:
            return None
        from agent_cli.constants import snapshot_knobs

        k = snapshot_knobs()
        store = GitSnapshot(
            ws,
            min_interval_s=k["min_interval_s"],
            file_max_bytes=k["file_max_bytes"],
            file_count_max=k["file_count_max"],
        )
        store.init()
        _STORE = store
        return store


def reset_snapshot() -> None:
    global _STORE
    with _STORE_LOCK:
        _STORE = None
