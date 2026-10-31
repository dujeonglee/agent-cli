"""`python` 도구의 워커 — 상태가 남는 파이썬 프로세스 (v10.66.0, docs/python-tool/DESIGN.md).

`jobs.py` 의 형제다. 소유자(``main`` / ``agent:<key>``)마다 자식 프로세스 하나
(`tools/_pyworker_child.py` 를 ``python3`` 로 띄운다)가 **첫 호출 때** 서고,
변수·import·읽어 둔 데이터가 호출 사이에 남는다. 종전엔 데이터를 다루는 길이
``write_file`` 스크립트 + ``shell python3`` 뿐이라 파싱 한 줄을 고칠 때마다
파일을 통째로 다시 쓰고 OCR 같은 전처리까지 다시 했다(v10.62.2 벤치 financial:
OCR 3번·process.py 8번 재작성).

수명: 타임아웃이면 먼저 Ctrl-C 처럼 프로세스 그룹에 SIGINT 를 보낸다 — 자식의
``KeyboardInterrupt`` 가 코드를 멈추고 **상태는 산다**(루프가 dict 에 모으던 결과가
남는다; 벤치 실측 2026-10-22: 95프레임 OCR 루프가 120s 에 죽어 결과를 통째로 잃고
다시 돌렸다, 3런 5회). 몇 초 안에 돌아오지 않을 때만(시그널을 무시하는 코드)
프로세스 그룹째 죽이고 다음 호출 때 새로 띄운다 — 그때 상태가 사라졌다는 사실을
도구가 관찰에 적는다. 소유자가 죽으면(`drop_owner`) 그 워커를
죽이고, 세션이 끝나면(`stop_all`) 전부 죽인다. 영속은 없다 — resume 뒤는 빈 워커.

인터프리터는 PATH 의 ``python3``, 없으면 ``python`` — 그것뿐이다 (v10.78.0).
**모델이 shell 에서 쓰는 파이썬**과 같아야 shell 에서 깐 패키지가 보인다. 둘 다
없으면 띄우지 않고 그 사실을 돌려준다(``PyResult.no_interpreter``) — 예전엔
agent-cli 자신의 인터프리터로 떨어졌는데, 벤치 컨테이너에선 그게 pip 없는 uv
venv 였고 워커가 거기에 고정돼 나중에 깔린 시스템 python3 의 패키지를 못 봤다.
워커는 뜰 때마다 다시 고르므로, 설치한 다음 호출부터 그 파이썬으로 뜬다.

자식은 셀을 받기 전에 시작 인사(``{"ready": true}``)를 보낸다. 인사 전에 죽으면
인터프리터가 이 자식을 돌릴 수 없는 것(3.8 미만·Python 2·버전 없는 pyenv shim)
이고(``PyResult.startup_failed``), 인사 뒤에 죽으면 코드가 프로세스를 끝낸 것이다.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

CHILD = Path(__file__).parent / "tools" / "_pyworker_child.py"
FIG_DIR = Path(".agent-cli") / "tmp" / "python"
KILL_GRACE_S = 2.0
#: SIGINT 뒤 회신을 기다리는 시간 — 그 안에 안 돌아오면 kill.
INTERRUPT_GRACE_S = 3.0
#: 시작 인사를 기다리는 시간 — pyenv shim 처럼 느린 실행 파일도 넉넉히.
READY_TIMEOUT_S = 15.0


def interpreter() -> str | None:
    """모델의 shell 이 보는 PATH 의 ``python3``, 없으면 ``python``. 둘 다 없으면 ``None``."""
    return shutil.which("python3") or shutil.which("python")


class _LineRead:
    """한 줄 읽기를 스레드로 — 시간 안에 안 오면 같은 읽기를 더 기다릴 수 있다
    (타임아웃 뒤 SIGINT 를 보내고 **같은** 회신을 기다린다; 읽기를 새로 띄우면
    두 스레드가 한 파이프를 다툰다)."""

    def __init__(self, stream) -> None:
        self._box: dict = {}
        self._t = threading.Thread(target=self._read, args=(stream,), daemon=True)
        self._t.start()

    def _read(self, stream) -> None:
        self._box["line"] = stream.readline()

    def wait(self, timeout: float) -> bytes | None:
        """한 줄(끝이면 ``b""``), 시간이 지나면 ``None``."""
        self._t.join(timeout)
        return None if self._t.is_alive() else self._box.get("line", b"")


@dataclass
class PyResult:
    ok: bool
    out: str = ""
    value: str | None = None
    exc: str | None = None
    state: list[list[str]] = field(default_factory=list)
    figures: list[str] = field(default_factory=list)
    #: 이 호출이 워커를 새로 띄웠다 — 직전 상태는 사라진 뒤다.
    fresh: bool = False
    #: 타임아웃에 SIGINT 로 멈췄다 — 인터프리터와 상태는 산다(``state`` 가 지금 것).
    interrupted: bool = False
    #: 타임아웃으로 죽였다(상태 소실). ``lost`` 는 죽기 전 마지막으로 본 이름들.
    timed_out: bool = False
    lost: list[str] = field(default_factory=list)
    #: PATH 에 ``python3`` 도 ``python`` 도 없어 띄우지 못했다.
    no_interpreter: bool = False
    #: 띄운 인터프리터가 시작 인사 전에 죽었다 — 이 자식을 돌릴 수 없는 파이썬.
    startup_failed: bool = False
    #: 한도 안에 끝나지 않아 셀이 계속 돈다 (v10.91.0) — 호출자가 작업으로 넘긴다.
    pending: PendingCell | None = None
    #: 앞선 셀이 아직 돌고 있어 이 호출을 받지 못했다 — 그 셀.
    busy: PendingCell | None = None


class PendingCell:
    """한도를 넘겨 계속 도는 셀 (v10.91.0) — `jobs.JobHandle` 의 python 쪽 구현.

    워커의 회신 읽기(`_LineRead`)를 그대로 쥔다. 레지스트리가 `poll` 로 물을 때
    회신이 와 있으면 그때 결과를 만들고(상태 줄 갱신) 워커를 풀어 준다. 멈춤은
    셸 작업과 달리 **INT** — 자식의 ``KeyboardInterrupt`` 가 셀만 끊고 상태는
    남긴다(모델의 `kill -INT -- -<pid>` 도 같은 길). 듣지 않으면 인터프리터째."""

    def __init__(
        self,
        worker: PyWorker,
        reading: _LineRead,
        *,
        fresh: bool,
        lost: list[str],
        started_at: float,
    ) -> None:
        self.worker = worker
        self._reading = reading
        self._fresh = fresh
        self._lost = lost
        #: 셀을 **보낸** 시각 — 작업으로 넘어간 시각이 아니다 (v10.91.1). 라이브에서
        #: 160초 셀의 통지가 "after 40s", 바쁨 안내가 "10s so far" 였다.
        self.started_at = started_at
        self.result: PyResult | None = None
        #: 레지스트리가 준 번호 — `python` 이 "busy with job #n" 에 쓴다.
        self.job_id: int | None = None

    @property
    def pid(self) -> int:
        return self.worker.pid or 0

    def poll(self) -> int | None:
        if self.result is None:
            line = self._reading.wait(0)
            if line is None:
                return None
            self.result = self.worker._parse(line, fresh=self._fresh, lost=self._lost)
            self.worker._release(self)
        return 0 if self.result.ok else 1

    def stop(self) -> None:
        pid = self.worker.pid
        if pid is None:
            return
        try:
            os.killpg(pid, signal.SIGINT)
        except OSError:
            pass

    def force(self) -> None:
        self.worker.kill()

    @property
    def stop_hint(self) -> str:
        return f"`kill -INT -- -{self.pid}`"

    def report_lines(self) -> list[str]:
        """통지의 로그 꼬리 뒤: 마지막 식의 값, 예외(인터럽트 포함), 그림, 상태 줄."""
        from agent_cli.tools.python_tool import state_line

        res = self.result
        if res is None:
            return []
        lines: list[str] = []
        if res.value is not None:
            lines.append(f"=> {res.value}")
        if res.interrupted:
            lines.append(
                "stopped on interrupt — the interpreter is alive and its state is kept"
            )
        elif not res.ok and res.exc:
            lines.extend(res.exc.rstrip().splitlines()[-3:])
        for p in res.figures:
            lines.append(f"figure saved: {p} (`view_image` shows it)")
        lines.append(state_line(res.state))
        return lines


class PyWorker:
    """소유자 하나의 파이썬 프로세스. 호출은 직렬(락)."""

    def __init__(self, cwd: str | Path | None = None) -> None:
        self.cwd = str(cwd or os.getcwd())
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._last_state: list[list[str]] = []
        #: 마지막으로 죽을 때 들고 있던 이름들 — 다음 호출이 "새 인터프리터" 를 알린다.
        self._lost: list[str] = []
        self.started_at: float | None = None
        #: 이 워커를 띄운 인터프리터 경로 — ModuleNotFoundError 관찰이 "여기에
        #: 설치하라" 고 가리키는 대상 (v10.73.0, docs/python-tool/DESIGN.md §3).
        self.interpreter: str | None = None
        #: 한도를 넘겨 계속 도는 셀 — 있는 동안 새 셀은 받지 않는다 (v10.91.0).
        self._pending: PendingCell | None = None
        #: 호출 수 — 캡처 로그의 머리줄 번호.
        self.calls = 0

    @property
    def pending(self) -> PendingCell | None:
        """돌고 있는 셀, 없으면 ``None`` (끝난 것은 `poll` 때 풀린다)."""
        cell = self._pending
        if cell is not None and cell.poll() is not None:
            return None  # `poll` 이 끝난 셀을 풀었다
        return cell

    def _release(self, cell: PendingCell) -> None:
        if self._pending is cell:
            self._pending = None

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    def _start(self) -> str | None:
        """띄우고 시작 인사를 받는다. 실패면 까닭(``"none"`` = 인터프리터 없음,
        ``"dead"`` = 인사 전에 죽음), 성공이면 ``None``. 실패면 ``_proc`` 는 비어
        다음 호출이 다시 시도한다."""
        self.interpreter = interpreter()
        if self.interpreter is None:
            return "none"
        self._proc = subprocess.Popen(
            [self.interpreter, "-u", str(CHILD)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=self.cwd,
            start_new_session=True,
        )
        self.started_at = time.time()
        self._last_state = []
        line = _LineRead(self._proc.stdout).wait(READY_TIMEOUT_S)
        try:
            ready = bool(line) and json.loads(line).get("ready") is True
        except (ValueError, AttributeError):
            ready = False
        if not ready:
            self.kill()
            return "dead"
        return None

    def kill(self) -> None:
        proc = self._proc
        self._proc = None
        if self._last_state:  # 이미 죽은 뒤의 재호출이 보관분을 지우지 않게
            self._lost = [r[0] for r in self._last_state]
            self._last_state = []
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            proc.wait(KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                proc.wait(KILL_GRACE_S)
            except subprocess.TimeoutExpired:
                pass
        for s in (proc.stdin, proc.stdout):
            try:
                s.close()
            except Exception:
                pass

    def run(
        self,
        code: str,
        *,
        timeout: float,
        figdir: str | Path | None,
        capture: str | Path | None = None,
        pend: bool = False,
    ) -> PyResult:
        """셀 하나. ``capture`` 가 있으면 자식이 출력을 그 파일에 덧붙여 쓴다(작업
        로그·진행 신호). ``pend`` 가 참이면 한도 안에 회신이 없을 때 끊지 않고
        `PendingCell` 로 돌려준다 — 호출자가 작업 레지스트리에 넘긴다; 거짓이면
        종전대로 SIGINT 로 멈춘다(상태 보존, 듣지 않으면 kill)."""
        with self._lock:
            busy = self.pending
            if busy is not None:
                return PyResult(False, busy=busy)
            fresh = False
            lost: list[str] = []
            if not self.alive:
                self.kill()  # 죽은 채 남은 핸들 정리(죽을 때의 상태 이름을 보관)
                failed = self._start()
                if failed == "none":
                    return PyResult(False, no_interpreter=True)
                if failed == "dead":
                    return PyResult(False, startup_failed=True)
                lost, self._lost = self._lost, []
                fresh = True
            proc = self._proc
            assert proc is not None and proc.stdin and proc.stdout
            self.calls += 1
            req = {
                "code": code,
                "figdir": str(figdir or ""),
                "capture": str(capture or ""),
            }
            sent_at = time.time()
            try:
                proc.stdin.write((json.dumps(req) + "\n").encode("utf-8"))
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self.kill()
                return PyResult(
                    False,
                    exc="python worker died before the call could be sent",
                    fresh=fresh,
                )
            reading = _LineRead(proc.stdout)
            line = reading.wait(timeout)
            if line is None and pend:
                cell = PendingCell(
                    self, reading, fresh=fresh, lost=lost, started_at=sent_at
                )
                self._pending = cell
                return PyResult(False, pending=cell)
            if line is None:
                # Ctrl-C 처럼 그룹째 SIGINT — 자식은 KeyboardInterrupt 를 보통의
                # 실패 회신으로 돌려주고(상태는 산다), 띄운 하위 프로세스도 같이
                # 멈춘다. 시그널을 무시하는 코드만 kill 로 간다.
                try:
                    os.killpg(proc.pid, signal.SIGINT)
                except OSError:
                    pass
                line = reading.wait(INTERRUPT_GRACE_S)
                if line is None:
                    self.kill()
                    return PyResult(False, timed_out=True, lost=self._lost, fresh=fresh)
            return self._parse(line, fresh=fresh, lost=lost)

    def _parse(self, line: bytes, *, fresh: bool, lost: list[str]) -> PyResult:
        """자식의 회신 한 줄 → 결과. 빈 줄은 자식이 죽은 것, 못 읽는 줄은 프로토콜 파손."""
        if not line:
            had = bool(self._last_state)
            self.kill()
            gone = " — its state is gone" if had else ""
            return PyResult(
                False,
                exc=f"python: the interpreter exited while running this code{gone}",
                lost=self._lost,
                fresh=fresh,
            )
        try:
            rep = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            self.kill()
            return PyResult(
                False, exc="python worker sent an unreadable reply", fresh=fresh
            )
        self._last_state = [list(r) for r in rep.get("state") or []]
        return PyResult(
            bool(rep.get("ok")),
            out=str(rep.get("out") or ""),
            value=rep.get("value"),
            exc=rep.get("exc"),
            interrupted=bool(rep.get("interrupted")),
            state=self._last_state,
            figures=[str(p) for p in rep.get("figures") or []],
            fresh=fresh,
            lost=lost,
        )


class PyWorkerRegistry:
    """소유자 → 워커. 세션당 하나 (`runtime.build_py_registry`)."""

    def __init__(self, cwd: str | Path | None = None) -> None:
        self.cwd = str(cwd or os.getcwd())
        self._workers: dict[str, PyWorker] = {}
        self._lock = threading.Lock()
        self.closed = False

    def get(self, owner: str) -> PyWorker:
        with self._lock:
            w = self._workers.get(owner)
            if w is None:
                w = self._workers[owner] = PyWorker(self.cwd)
            return w

    def reset(self, owner: str) -> None:
        with self._lock:
            w = self._workers.pop(owner, None)
        if w is not None:
            w.kill()

    def running(self) -> list[tuple[str, PyWorker]]:
        with self._lock:
            return [(o, w) for o, w in self._workers.items() if w.alive]

    def state_of(self, owner: str) -> list[list[str]]:
        """소유자의 인터프리터가 살아 있으면 마지막 호출 뒤의 상태 목록, 아니면
        ``[]``. 워커를 만들지 않는다 — 꼬리(`python_tool.tail_block`)가 턴마다
        읽는 조회다."""
        with self._lock:
            w = self._workers.get(owner)
        if w is None or not w.alive:
            return []
        return [list(r) for r in w._last_state]

    def drop_owner(self, addr: str) -> int:
        """소유자가 죽었다 — 그 워커를 죽인다. 죽인 수."""
        with self._lock:
            w = self._workers.pop(addr, None)
        if w is None or not w.alive:
            return 0
        w.kill()
        return 1

    def stop_all(self) -> int:
        """세션 종료 — 전부 죽이고 수를 돌려준다."""
        self.closed = True
        with self._lock:
            ws = list(self._workers.values())
            self._workers.clear()
        n = 0
        for w in ws:
            if w.alive:
                n += 1
            w.kill()
        return n


_MAIN: PyWorkerRegistry | None = None


def set_py_registry(registry: PyWorkerRegistry | None) -> None:
    global _MAIN
    _MAIN = registry


def get_py_registry() -> PyWorkerRegistry | None:
    return _MAIN
