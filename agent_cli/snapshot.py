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

**v10.80.0 수리 (docs/snapshot/DESIGN.md §6, 벤치 716 trial 실측)**: 모델은 과제문의
절대 경로(``/app/main.db-wal``)를 그대로 넣는데 ``<commit>:<path>`` 문법은 트리 기준
상대 경로만 받아 후보가 전부 걸러졌다(restore 실패 59건 중 52건). 경로는 ``_relative``
가 워크스페이스 기준으로 정규화한다. 저장소는 세션을 넘어 이어지므로(의도된 성질 —
사용자 결정) 세션 시작 커밋은 비어도 찍어 경계로 삼고 라벨에 세션 id 를 둔다. 복구
결과에는 "어느 스냅샷까지 멀쩡했고 어느 스냅샷에서 달라졌나" 를 붙여, 같은 명령을
다시 돌려 다시 망치는 반복(financial 33회 호출)을 끊는다.
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
#: 직전에 체크포인트를 찍는 도구 — 내장 훅(``hooks/builtin/10_snapshot.py``)이 보고,
#: 모델에게 가는 문구(시스템 섹션·도구 설명)도 여기서 나온다(한 곳 — v10.66.0 에
#: ``python`` 이 훅에만 더해져 문구가 셋만 말하던 어긋남을 닫는다).
MUTATING_TOOLS: tuple[str, ...] = ("shell", "python", "edit_file", "write_file")
MUTATING_TOOLS_PHRASE = " / ".join(f"`{t}`" for t in MUTATING_TOOLS)
#: 체크포인트 라벨에 붙는 호출 요약의 상한(문자) — "before shell (turn 7): sqlite3 main.db"
LABEL_SUMMARY_CHARS = 60


def checkpoint_label(tool: str, turn: int, summary: str = "") -> str:
    """변경 도구 직전 체크포인트의 라벨. ``summary`` 는 그 호출의 요약(명령·경로)이며
    한 줄·상한 안으로 접는다 — 모델이 `list` 만 보고 "망치기 직전" 을 고를 수 있게."""
    head = f"before {tool} (turn {turn})"
    text = " ".join(summary.split())
    if not text:
        return head
    if len(text) > LABEL_SUMMARY_CHARS:
        text = text[: LABEL_SUMMARY_CHARS - 1] + "…"
    return f"{head}: {text}"


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

    def init(self, session_id: str | None = None) -> bool:
        """저장소 준비 + 하위 저장소 스캔 + 시작 커밋. 실패는 ``reason`` 에.

        시작 커밋은 변경이 없어도(빈 워크스페이스여도) 찍는다 — 저장소가 세션을 넘어
        이어지므로 `list` 에서 세션의 경계가 되고, 라벨이 세션 id 를 말한다."""
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
            label = "session start" + (f" ({session_id})" if session_id else "")
            self.checkpoint(label, force=True, allow_empty=True)
            self.stats.seconds = time.monotonic() - t0
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

    def checkpoint(
        self, label: str, *, force: bool = False, allow_empty: bool = False
    ) -> str | None:
        """변경 도구 직전 커밋. 변경이 없으면 None(커밋 없음) — ``allow_empty`` 면 변경이
        없어도 찍는다(세션 시작 경계). 최소 간격 안이면 건너뜀."""
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
            if not allow_empty:
                if self.head() is None:
                    staged = self._git("ls-files", "--cached").stdout.strip()
                    if not staged:
                        return None
                else:
                    q = self._git("diff", "--cached", "--quiet")
                    if q.returncode == 0:
                        return None  # 변화 없음
            c = self._git("commit", "-q", "--allow-empty", "-m", label)
            if c.returncode != 0:
                return None
            self.stats.commits += 1
            return self.head()

    # ── queries ──────────────────────────────────────────────

    def head(self) -> str | None:
        r = self._git("rev-parse", "--verify", "-q", "HEAD")
        return r.stdout.strip() or None

    def log(self, limit: int = 20, path: str | None = None) -> list[tuple[str, str]]:
        """최근 스냅샷 (짧은 sha, 라벨). ``path`` 를 주면 그 경로가 바뀐 스냅샷만 —
        모델은 `list` 에 `path` 를 넣어 "이 파일의 판들" 을 기대한다(벤치 18회)."""
        args = ["log", f"-{limit}", "--format=%h%x09%s"]
        if path:
            args += ["--", path]
        r = self._git(*args)
        return [
            tuple(ln.split("\t", 1))  # type: ignore[misc]
            for ln in r.stdout.splitlines()
            if "\t" in ln
        ]

    def label(self, commit: str) -> str:
        return self._git("log", "-1", "--format=%s", commit).stdout.strip()

    def relative(self, path: str) -> tuple[str | None, str]:
        """모델이 준 경로를 워크스페이스 기준 상대 경로로 — git 의 ``<commit>:<path>``
        는 트리 기준 상대 경로만 받는다. 절대 경로는 워크스페이스 안이면 받고 밖이면
        분명한 오류, ``./``·``..`` 은 접는다. 반환 ``(경로, 오류)`` 중 하나만 채워진다."""
        raw = path.strip().rstrip("/")
        if not raw:
            return None, "`path` is empty"
        p = Path(raw)
        if p.is_absolute():
            try:
                rel = p.resolve().relative_to(self.workspace)
            except ValueError:
                return (
                    None,
                    f"{raw!r} is outside the working directory {self.workspace}",
                )
            raw = str(rel)
        norm = os.path.normpath(raw)
        if norm == "." or norm == "" or norm.startswith(".."):
            return None, f"{path.strip()!r} is not a path inside the working directory"
        return norm, ""

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
        망가진 상태가 다음 체크포인트에서 HEAD 로 커밋돼 있어도 그 전 판을 고른다.

        결과 문장에 원인의 자리를 붙인다: 그 경로가 아직 멀쩡했던 마지막 스냅샷의
        라벨("before shell (turn 7): sqlite3 main.db")과 달라진 채 찍힌 첫 스냅샷의
        라벨. 라벨이 호출 요약을 담으므로(``checkpoint_label``) 모델은 history 를 열지
        않고도 어느 명령이 망쳤는지 안다 — 복구 뒤 같은 명령을 또 돌려 또 망치는 반복을
        끊는 재료."""
        if not self.enabled:
            return False, self.reason
        rel, err = self.relative(path)
        if rel is None:
            return False, err
        if at is not None:
            r = self._git("checkout", at, "--", rel)
            if r.returncode != 0:
                return False, r.stderr.strip() or "checkout failed"
            return True, f'restored {rel} from {at[:8]} ("{self.label(at)}")'
        candidates = self.commits_touching(rel)
        if not candidates:
            return False, (
                f"no snapshot contains {rel!r} (paths are relative to the working "
                "directory)"
            )
        current = self._disk_blob(rel)
        pick = -1
        for i, c in enumerate(candidates):
            if not self._has(c, rel):
                continue
            if current is not None and self._blob(c, rel) == current:
                continue  # 지금과 같은 내용 — 더 전으로
            pick = i
            break
        if pick < 0:
            return False, (
                f"every snapshot of {rel!r} matches what is on disk now — nothing to "
                "restore"
            )
        at = candidates[pick]
        r = self._git("checkout", at, "--", rel)
        if r.returncode != 0:
            return False, r.stderr.strip() or "checkout failed"
        if pick == 0:
            # 달라진 상태가 아직 어느 스냅샷에도 없다 — 가장 최근 스냅샷까지는 멀쩡했다.
            head = self.head() or at
            return True, (
                f'restored {rel} from {at[:8]} — intact at "{self.label(head)}", '
                "changed after that snapshot."
            )
        first_bad = candidates[pick - 1]
        last_good = self._git("rev-parse", f"{first_bad}^").stdout.strip() or at
        return True, (
            f'restored {rel} from {at[:8]} — intact at "{self.label(last_good)}", '
            f'changed by "{self.label(first_bad)}".'
        )

    # ── model-facing ─────────────────────────────────────────

    def notice(self) -> str:
        """시스템 섹션 본문(영어 — 모델에게 가는 문구). 시작 커밋 성공 뒤에만 쓴다."""
        intro = (
            "The harness keeps git snapshots of this working directory in "
            "`.agent-cli/snapshot.git` (a bare repository; this directory is its work "
            "tree — nothing is added to the directory itself). A snapshot was taken "
            "when this session started and one is taken again just before each "
            f"{MUTATING_TOOLS_PHRASE} call, so the state before your last "
            "command is always on record."
        )
        howto = (
            "If an input file gets damaged or disappears, call the `snapshot` tool "
            '`mode` "restore" and the file\'s `path` — it brings back the most '
            "recent snapshot whose content differs from what is on disk now and "
            "tells you which call changed it. "
            '`mode` "list" shows the snapshots (with `path`, only those that '
            "changed that file); `at` picks a specific one."
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
    workspace: Path | None = None,
    *,
    create: bool = False,
    session_id: str | None = None,
) -> GitSnapshot | None:
    """워크스페이스당 하나. ``create`` 면 없을 때 만들어 ``init`` 까지 한다(시작 커밋
    라벨에 ``session_id``)."""
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
        store.init(session_id)
        _STORE = store
        return store


def reset_snapshot() -> None:
    global _STORE
    with _STORE_LOCK:
        _STORE = None
