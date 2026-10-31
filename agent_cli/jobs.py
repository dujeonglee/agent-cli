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

작업의 몸은 **핸들**(v10.91.0, `JobHandle`): 셸 명령은 ``Popen`` 을 감싼
`ProcHandle`, 한도를 넘겨 계속 도는 `python` 셀은 `pyworker.PendingCell`. 레지스트리는
핸들에게 끝났는지(`poll`)·멈춤(`stop`/`force`)·멈추는 법 문구(`stop_hint`)·통지에 덧붙일
줄(`report_lines`)만 묻는다 — 번호·로그·꼬리·통지·수명은 둘이 같다. 셸 명령은 bash 가
있으면 bash 로 띄운다(`SHELL`): 벤치에서 `[[ ]]`·`<(…)` 가 dash 에서 깨져 턴을 잃었다.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

POLL_SECS = 1.0
#: 종료 통지에 싣는 로그 꼬리 줄 수 — 모니터 보고(5줄)와 같은 크기 감각
TAIL_LINES = 8
TAIL_LINE_CHARS = 300
KILL_GRACE_S = 3.0
LOG_DIR = Path(".agent-cli") / "tmp" / "jobs"
#: `shell` 과 백그라운드 작업이 명령을 돌리는 셸 (v10.91.0) — bash 가 있으면 bash,
#: 없으면 ``None`` 으로 두어 ``Popen(shell=True)`` 의 기본 ``/bin/sh``. GLM 벤치에서
#: 모델은 bash 문법을 쓰고 컨테이너의 sh 는 dash 였다: `diff <(…)` 두 턴 연속 실패,
#: `[[ $f == *.pdf ]]` 가 분류 루프 전체를 비웠다. 실행기를 맞추면 그 실패 종류가
#: 모델과 무관하게 사라진다. import 시점에 고정(테스트 monkeypatch seam).
SHELL: str | None = shutil.which("bash")


def shell_name() -> str:
    """Environment 절이 적는 이름 — ``"bash"`` 또는 ``"sh"``."""
    return "bash" if SHELL else "sh"


class JobHandle(Protocol):
    """레지스트리가 작업의 몸에 묻는 전부."""

    @property
    def pid(self) -> int: ...

    def poll(self) -> int | None:
        """끝났으면 종료 코드, 아직이면 ``None``. 끝난 뒤의 재호출은 같은 값."""
        ...

    def stop(self) -> None:
        """부드럽게 멈추라고 한다 (셸: 그룹에 TERM, 셀: 그룹에 INT)."""
        ...

    def force(self) -> None:
        """듣지 않으면 죽인다 (셸: 그룹에 KILL, 셀: 인터프리터째)."""
        ...

    @property
    def stop_hint(self) -> str:
        """모델이 이 작업을 멈추는 명령 — 꼬리·결과·중간 깨우기가 같은 모양."""
        ...

    def report_lines(self) -> list[str]:
        """종료 통지의 로그 꼬리 뒤에 덧붙일 줄 (셀: 값·예외·상태 줄)."""
        ...


class ProcHandle:
    """셸 명령 하나 — 자기 세션(`start_new_session`)의 ``Popen``."""

    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc = proc

    @property
    def pid(self) -> int:
        return self.proc.pid

    def poll(self) -> int | None:
        return self.proc.poll()

    def stop(self) -> None:
        self._signal(signal.SIGTERM)

    def force(self) -> None:
        self._signal(signal.SIGKILL)

    def _signal(self, sig) -> None:
        try:
            os.killpg(self.proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    @property
    def stop_hint(self) -> str:
        """**프로세스 묶음** 전체 (v10.79.0). 작업은 자기 세션이고 ``pid`` 는 `sh -c`
        리더다. `kill <pid>` 는 셸만 죽이고 `xargs`·`tesseract` 같은 자식은
        살아남는다 — :meth:`JobRegistry.kill` 이 `killpg` 인 것과 같은 이유로 네 자리
        (꼬리·`shell` 결과·시스템 프롬프트·중간 깨우기 알림)가 이 한 모양을 쓴다.
        ``--`` 는 음수를 신호 번호로 읽지 않게 하는 표기다."""
        return f"`kill -- -{self.proc.pid}`"

    def report_lines(self) -> list[str]:
        return []


@dataclass
class Job:
    id: int
    pid: int
    command: str
    log: Path  # 작업 디렉터리 기준 상대 경로 (꼬리·통지에 그대로 보인다)
    owner: str  # "main" | "agent:<key>"
    handle: JobHandle = field(repr=False)
    started_at: float = field(default_factory=time.time)
    rc: int | None = None
    ended_at: float | None = None

    @property
    def running(self) -> bool:
        return self.rc is None

    def elapsed(self, now: float | None = None) -> str:
        end = self.ended_at if self.ended_at is not None else (now or time.time())
        return elapsed_text(int(end - self.started_at))

    @property
    def header_bytes(self) -> int:
        """로그 첫 줄(하니스가 쓴 ``$ cmd``)의 바이트 수 — 로그가 이 크기면
        명령은 아직 아무것도 쓰지 않았다 (v10.79.0 중간 깨우기의 기준)."""
        return len(f"$ {self.command}\n".encode())

    @property
    def stop_hint(self) -> str:
        """모델이 이 작업을 멈추는 명령 — 핸들이 정한다 (셸 `kill -- -P`, 셀 `kill -INT -- -P`)."""
        return self.handle.stop_hint


def elapsed_text(seconds: int) -> str:
    """``5s`` · ``2m31s`` · ``1h05m`` — 작업 꼬리·통지·`python` 의 바쁨 안내가 같은 모양."""
    s = max(0, seconds)
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

    @property
    def available(self) -> bool:
        """띄워도 통지가 갈 곳이 있다 — `python` 이 한도 뒤 셀을 작업으로 넘길지 정할 때 본다."""
        return self.deliver is not None and not self.closed

    def _ensure_available(self) -> None:
        if self.deliver is None:
            raise JobsUnavailable(
                "background jobs: delivery is not wired (internal error)"
            )
        if self.closed:
            raise JobsUnavailable("background jobs: the session is shutting down")

    def _next_id(self) -> int:
        with self._lock:
            job_id = self._next
            self._next += 1
        return job_id

    def start(self, command: str, *, owner: str) -> Job:
        self._ensure_available()
        log_dir = self._cwd / LOG_DIR
        log_dir.mkdir(parents=True, exist_ok=True)
        job_id = self._next_id()
        rel = LOG_DIR / f"{job_id}.log"
        path = self._cwd / rel
        with path.open("ab") as logf:
            logf.write(f"$ {command}\n".encode())
            logf.flush()
            proc = subprocess.Popen(
                command,
                shell=True,
                executable=SHELL,
                cwd=str(self._cwd),
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=logf,
                stderr=subprocess.STDOUT,
            )
        return self._register(
            job_id, ProcHandle(proc), command=command, log=rel, owner=owner
        )

    def adopt(
        self,
        handle: JobHandle,
        *,
        command: str,
        log: Path,
        owner: str,
        started_at: float,
    ) -> Job:
        """이미 돌고 있는 몸을 작업으로 받는다 (v10.91.0) — 한도를 넘긴 `python` 셀.
        ``log`` 는 작업 디렉터리 기준 상대 경로이고 그 첫 줄은 ``$ {command}`` 여야
        한다(`header_bytes`·꼬리 필터가 셸 작업과 같은 모양을 기대한다).
        ``started_at`` 은 몸이 **실제로 시작한** 시각 — 경과 시간(꼬리·통지)은
        거기서 센다 (v10.91.1)."""
        self._ensure_available()
        return self._register(
            self._next_id(),
            handle,
            command=command,
            log=log,
            owner=owner,
            started_at=started_at,
        )

    def _register(
        self,
        job_id: int,
        handle: JobHandle,
        *,
        command: str,
        log: Path,
        owner: str,
        started_at: float | None = None,
    ) -> Job:
        job = Job(
            id=job_id,
            pid=handle.pid,
            command=command,
            log=log,
            owner=owner,
            handle=handle,
            started_at=started_at if started_at is not None else time.time(),
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
            # 상태만 적는다 (v10.68.0): "기다릴 땐 wait" 는 Task Guidelines 의 규칙과
            # `## Open Items` 꼬리말에 있다 — 같은 턴에 세 번 말하지 않는다.
            "## Background jobs (running — you are told when each one ends; do not "
            "poll)"
        )
        lines = [head]
        for j in jobs:
            lines.append(
                f"- job #{j.id} · pid {j.pid} · {j.elapsed(now)} · `{_short(j.command)}` "
                f"→ {j.log.as_posix()} · stop it: {j.stop_hint}"
            )
        return "\n".join(lines)

    # ── 로그 변화 (v10.79.0, `wait` 중간 깨우기의 진행 신호) ──────────

    def log_size(self, job: Job) -> int:
        """로그의 현재 바이트 수 — `wait` 때 적어 두는 기준."""
        try:
            return (self._cwd / job.log).stat().st_size
        except OSError:
            return 0

    def log_growth(self, job: Job, since: int) -> str:
        """``since`` 바이트 이후 로그가 얼마나 늘었는지 — 알림의 ``log:`` 줄.

        늘었으면 "+N lines since your wait (last 2 lines)" 와 그 두 줄, 안 늘었으면
        "unchanged since your wait" — 명령이 아직 한 줄도 안 썼으면(크기가 머리줄
        그대로) "nothing after the command line" 을 앞에 붙인다. 새로 쓰인 부분만
        읽으므로 로그가 커도 비용은 새 바이트만큼이다."""
        path = self._cwd / job.log
        try:
            size = path.stat().st_size
            with path.open("rb") as f:
                f.seek(min(since, size))
                chunk = f.read()
        except OSError:
            return f"log: {job.log.as_posix()} · unreadable"
        text = chunk.decode("utf-8", errors="replace")
        new = [ln for ln in text.splitlines() if ln.strip()]
        if not new:
            empty = size <= job.header_bytes
            state = (
                "nothing after the command line, unchanged since your wait"
                if empty
                else "unchanged since your wait"
            )
            return f"log: {job.log.as_posix()} · {state}"
        tail = [ln[:TAIL_LINE_CHARS] for ln in new[-2:]]
        head = (
            f"log: {job.log.as_posix()} · +{len(new)} line"
            f"{'s' if len(new) != 1 else ''} since your wait "
            f"(last {len(tail)} line{'s' if len(tail) != 1 else ''})"
        )
        return "\n".join([head, *tail])

    # ── 종료/정리 ─────────────────────────────

    def kill(self, job: Job) -> None:
        """부드럽게 멈추라 하고(`stop`), 잠시 뒤 남았으면 죽인다(`force`). 이미
        끝났으면 no-op."""
        if not job.running:
            return
        job.handle.stop()
        rc = self._await(job.handle)
        if rc is None:
            job.handle.force()
            rc = self._await(job.handle)
        self._finish(job, rc if rc is not None else -1, notify=False)

    @staticmethod
    def _await(handle: JobHandle) -> int | None:
        deadline = time.time() + KILL_GRACE_S
        while True:
            rc = handle.poll()
            if rc is not None or time.time() >= deadline:
                return rc
            time.sleep(0.05)

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
            rc = job.handle.poll()
            if rc is not None:
                self._finish(job, rc, notify=True)

    def _finish(self, job: Job, rc: int, *, notify: bool) -> None:
        with self._lock:
            if not job.running:
                return
            job.rc = rc
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
        body += job.handle.report_lines()
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
