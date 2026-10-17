"""백그라운드 작업 — 하니스가 소유하는 긴 명령 (v10.47.0, docs/jobs/DESIGN.md).

`shell` 에 ``background: true`` 를 주면 명령은 **이 프로세스의 자식**으로 뜨고
(`Popen(start_new_session)`), 출력은 ``.agent-cli/tmp/jobs/<n>.log`` 로 간다.
하니스가 부모라서 종료 코드를 안다 — 종전엔 모델이 ``cmd &`` 로 띄운 자식이
재부모화돼 `waitpid` 를 못 했고, 그래서 monitor 의 `exit` 조건을 만들 수 없었다
(docs/monitor/DESIGN.md §4.2). 끝나면 로그 끝에 ``[job #n exited rc=R]`` 을 쓰고
소유자(main 이면 깨우기 런, 에이전트면 inbox)에게 모니터 보고와 같은 길로
알린다(`kind="job"`). 돌고 있는 작업은 매 턴 꼬리에 PID 와 로그 경로로 보인다.

이게 셸 안내 문구 A/B(2026-10-17)의 결론이다: "로그로 보내고 tail 로 확인" 은
폴링 턴을 늘리고 `pkill` 패턴이 agent-cli 자신을 죽였으며, monitor 로 기다리는
쪽은 빠르지만 끝 줄을 안 쓰는 로그를 영영 기다렸다. 작업을 하니스가 들면 셋 다
사라진다 — 폴링할 이유가 없고, PID 가 있으니 패턴 kill 이 필요 없고, 종료는
확실히 통지된다.

수명: 돌고 있는 작업은 세션의 "할 일"(`runtime.session_has_live_work`) — run 은
끝나길 기다리고 web 은 자기를 거두지 않는다. 소유자가 죽으면(`drop_owner`) 그
작업은 죽이고, 세션이 끝날 때 남은 작업도 죽인다(우리가 띄운 것이므로 고아를
남기지 않는다). 영속은 없다 — PID 는 프로세스와 함께 죽는다.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

POLL_SECS = 1.0
#: 종료 통지에 싣는 로그 꼬리 줄 수 — 모니터 보고(5줄)와 같은 크기 감각
TAIL_LINES = 8
TAIL_LINE_CHARS = 300
KILL_GRACE_S = 3.0
LOG_DIR = Path(".agent-cli") / "tmp" / "jobs"


@dataclass
class Job:
    id: int
    pid: int
    command: str
    log: Path  # 작업 디렉터리 기준 상대 경로 (꼬리·통지에 그대로 보인다)
    owner: str  # "main" | "agent:<key>"
    proc: subprocess.Popen = field(repr=False)
    started_at: float = field(default_factory=time.time)
    rc: int | None = None
    ended_at: float | None = None

    @property
    def running(self) -> bool:
        return self.rc is None

    def elapsed(self, now: float | None = None) -> str:
        end = self.ended_at if self.ended_at is not None else (now or time.time())
        s = max(0, int(end - self.started_at))
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        if h:
            return f"{h}h{m:02d}m"
        return f"{m}m{sec:02d}s" if m else f"{sec}s"


class JobsUnavailable(RuntimeError):
    """배달 배선이 없거나 세션이 닫혔다 — 띄워 봐야 끝났다는 통지가 갈 곳이 없다."""


def _short(cmd: str, n: int = 80) -> str:
    one = " ".join(cmd.split())
    return one if len(one) <= n else one[: n - 1] + "…"


class JobRegistry:
    """프로세스당 하나. 스레드 하나가 돌고 있는 작업을 1초마다 `poll()` 한다."""

    def __init__(self, cwd: str | Path | None = None) -> None:
        self._cwd = Path(cwd) if cwd else Path.cwd()
        self._jobs: dict[int, Job] = {}
        self._next = 1
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: 주소 배달 seam (`AgentRegistry.deliver`) — 조립기가 꽂는다
        self.deliver: Callable[..., str] | None = None
        self.closed = False
        self._inflight = 0

    # ── 띄우기 ────────────────────────────────

    def start(self, command: str, *, owner: str) -> Job:
        if self.deliver is None:
            raise JobsUnavailable(
                "background jobs: delivery is not wired (internal error)"
            )
        if self.closed:
            raise JobsUnavailable("background jobs: the session is shutting down")
        log_dir = self._cwd / LOG_DIR
        log_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            job_id = self._next
            self._next += 1
        rel = LOG_DIR / f"{job_id}.log"
        path = self._cwd / rel
        with path.open("ab") as logf:
            logf.write(f"$ {command}\n".encode())
            logf.flush()
            proc = subprocess.Popen(
                command,
                shell=True,
                cwd=str(self._cwd),
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=logf,
                stderr=subprocess.STDOUT,
            )
        job = Job(
            id=job_id, pid=proc.pid, command=command, log=rel, owner=owner, proc=proc
        )
        with self._lock:
            self._jobs[job_id] = job
        self._start_thread()
        return job

    def get(self, job_id: int) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def running(self, owner: str | None = None) -> list[Job]:
        with self._lock:
            return [
                j
                for j in self._jobs.values()
                if j.running and (owner is None or j.owner == owner)
            ]

    def started_since(self, ts: float) -> list[Job]:
        """``ts`` 이후에 띄운 작업(끝난 것 포함) — "이 런이 작업을 썼는가" 의 근거."""
        with self._lock:
            return [j for j in self._jobs.values() if j.started_at >= ts]

    def has_active_work(self) -> bool:
        """돌고 있는 작업(또는 통지 배달 중)이 있으면 세션이 끝나면 안 된다."""
        with self._lock:
            return any(j.running for j in self._jobs.values()) or self._inflight > 0

    # ── 꼬리 ──────────────────────────────────

    def render_tail(self, owner: str, now: float | None = None) -> str:
        """이 소유자의 돌고 있는 작업 — 매 턴 세션 상태 블록에 실린다."""
        jobs = self.running(owner)
        if not jobs:
            return ""
        now = now or time.time()
        head = (
            "## Background jobs (running — you are told when each one ends; do not "
            "poll. Nothing else to do? Call `complete` and say you are waiting: the "
            "notice wakes you.)"
        )
        lines = [head]
        for j in jobs:
            lines.append(
                f"- job #{j.id} · pid {j.pid} · {j.elapsed(now)} · `{_short(j.command)}` "
                f"→ {j.log.as_posix()} · stop it: `kill {j.pid}`"
            )
        return "\n".join(lines)

    # ── 종료/정리 ─────────────────────────────

    def kill(self, job: Job) -> None:
        """프로세스 그룹에 TERM, 잠시 뒤 KILL. 이미 끝났으면 no-op."""
        if not job.running:
            return
        try:
            os.killpg(job.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            job.proc.wait(timeout=KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(job.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                job.proc.wait(timeout=KILL_GRACE_S)
            except subprocess.TimeoutExpired:
                pass
        self._finish(job, notify=False)

    def drop_owner(self, addr: str) -> int:
        """소유자가 죽었다 — 그 작업들을 죽인다. 죽인 건수 반환."""
        victims = self.running(addr)
        for j in victims:
            self.kill(j)
        return len(victims)

    def stop_all(self) -> list[Job]:
        """세션 종료 — 남은 작업 전부 죽이고 목록을 돌려준다(콘솔에 적으라고)."""
        self.closed = True
        self._stop.set()
        victims = self.running()
        for j in victims:
            self.kill(j)
        return victims

    # ── 폴링 ──────────────────────────────────

    def _start_thread(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="jobs-poll", daemon=True
            )
            self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(POLL_SECS):
            try:
                self.tick()
            except Exception:  # 작업 관리가 런을 죽이면 안 된다
                pass
            if not self.running():
                return

    def tick(self) -> None:
        """한 틱 — 테스트가 직접 부를 수 있도록 스레드와 분리."""
        for job in self.running():
            if job.proc.poll() is not None:
                self._finish(job, notify=True)

    def _finish(self, job: Job, *, notify: bool) -> None:
        with self._lock:
            if not job.running:
                return
            job.rc = job.proc.returncode if job.proc.returncode is not None else -1
            job.ended_at = time.time()
            if self.closed or not notify:
                return
            self._inflight += 1
        try:
            line = f"\n[job #{job.id} exited rc={job.rc} after {job.elapsed()}]\n"
            try:
                with (self._cwd / job.log).open("a", encoding="utf-8") as f:
                    f.write(line)
            except OSError:
                pass
            report = self._report(job)
            fn = self.deliver
            if fn is not None:
                fn(
                    job.owner,
                    mail={"kind": "job", "success": job.rc == 0, "output": report},
                    text=report,
                    author="main",
                    expects_reply=False,
                )
        finally:
            with self._lock:
                self._inflight -= 1

    def _report(self, job: Job) -> str:
        tail = self._tail(job)
        head = (
            f"🧵 job #{job.id} finished rc={job.rc} after {job.elapsed()} — "
            f"`{_short(job.command)}`"
        )
        body = [f"log: {job.log.as_posix()} (last {len(tail)} line(s))"]
        body += [ln[:TAIL_LINE_CHARS] for ln in tail]
        return "\n".join([head, *(f"  {ln}" for ln in body)])

    def _tail(self, job: Job) -> list[str]:
        try:
            text = (self._cwd / job.log).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        lines = [ln for ln in text.splitlines() if ln.strip()]
        # 우리가 쓴 머리(`$ cmd`)·꼬리(`[job … exited]`)는 보고에 이미 있다
        lines = [
            ln
            for ln in lines
            if not ln.startswith("[job #") and not ln.startswith("$ ")
        ]
        return lines[-TAIL_LINES:]


# ── 프로세스 전역 (monitor/runtime.py 와 같은 모양) ──────────────────────

_MAIN: JobRegistry | None = None


def set_job_registry(registry: JobRegistry | None) -> None:
    global _MAIN
    _MAIN = registry


def get_job_registry() -> JobRegistry | None:
    return _MAIN
