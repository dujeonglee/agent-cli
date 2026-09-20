"""MonitorRegistry — 조건 평가 스레드 + 보고 메일박스 (docs/monitor/DESIGN.md §6).

`AgentRegistry` 의 형제다: 스레드를 소유하고, `has_active_work()` 로 런 수명에
합류하며, 보고를 `_pending` 에 쌓아 **턴 경계에서** 관찰 레코드로 배달된다.

## 왜 큐가 아니라 메일박스인가 (§6.1)

보고문을 입력 큐에 넣으면 **사람 메시지로 위장된다** — `push_user_message` 가
사용자 카드를 그리고 `QUEUED_REQUEST_NOTICE`("Another *user request* arrived")가
붙으며, history·resume·검색에서 사람 턴과 구별되지 않는다. 게다가 `run` 은 턴
중 큐 주입을 안 해 배달이 런 종료 후로 밀리고, `--result-file` 이 모니터 보고에
대한 응답으로 덮인다.

`MailWaker` 가 큐에 넣는 건 **깨우기 마커**뿐이고 내용은 메일박스로 간다 —
여기도 같은 분업을 따른다. 깨우기는 waker 술어에 `or monitors.has_pending()` 를
얹어 **합치기를 공짜로** 얻는다.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from agent_cli.constants import (
    MONITOR_DEADLINE_MAX_S,
    MONITOR_DEADLINE_MIN_S,
)
from agent_cli.monitor.conditions import Condition, Match

POLL_SECS = 1.0
MIN_INTERVAL_S = 30  # 이 안에 쌓인 매치는 한 보고로 합친다 (§7.2)
MAX_WAKES = 20
REPORT_MAX_LINES = 5  # §10.3
REPORT_MAX_LINE_CHARS = 500


@dataclass
class Monitor:
    id: str
    cond: Condition
    deadline_at: float
    once: bool = True
    run: str = ""  # 선택 — 보고 **전에** 실행 (§4)
    state: dict = field(default_factory=dict)
    wakes: int = 0
    matches: int = 0
    created_at: float = field(default_factory=time.time)
    # min_interval 합치기 버퍼
    _buf: list[str] = field(default_factory=list)
    _last_report: float = 0.0
    retired: str = ""  # "" = 살아 있음, 아니면 해제 사유

    @property
    def alive(self) -> bool:
        return not self.retired


def _clamp_deadline(seconds: int) -> int:
    return max(MONITOR_DEADLINE_MIN_S, min(seconds, MONITOR_DEADLINE_MAX_S))


def _format_report(mon: Monitor, lines: list[str], *, note: str = "") -> str:
    """보고문 — 5줄 상한 + 줄당 500자 + 머리줄에 id·타입·경과 (§10.3).

    과대 출력 캡(`apply_oversized_cap`)은 쓰지 않는다. 그건 *도구 결과*를 위한
    장치이고 이건 관찰 레코드라 그 경로를 안 탄다. 자체 상한이 훨씬 작으므로
    (5줄 × 500자 ≈ 2.5KB) 캡에 닿을 일이 없다 — **닿는다면 상한이 고장난 것**.
    """
    elapsed = int(time.time() - mon.created_at)
    h, rem = divmod(elapsed, 3600)
    m = rem // 60
    ago = f"{h}h {m}m" if h else f"{m}m"
    head = f"🔔 monitor {mon.id} ({mon.cond.describe()})   {ago} 경과 · {len(lines)}건"
    body = [ln[:REPORT_MAX_LINE_CHARS] for ln in lines[:REPORT_MAX_LINES]]
    if len(lines) > REPORT_MAX_LINES:
        body.append(f"… {len(lines) - REPORT_MAX_LINES}건 더")
    if note:
        body.append(note)
    return "\n".join([head, *(f"  {ln}" for ln in body)])


class MonitorRegistry:
    """스레드 하나가 모든 모니터를 1s 틱으로 본다.

    전부 값싼 `os.stat`/읽기/주기 spawn 이라 100개라도 틱당 한 자릿수 ms다.
    이벤트 구동(kqueue/inotify)은 쓰지 않는다: `silence` 는 어차피 타이머가
    필요하고, 표준 라이브러리에 감시자가 없어 의존성을 하나 더 들여야 하는데
    (on-premise 배포 제약) 얻는 건 최대 1s 지연이다.
    """

    def __init__(self, session_dir=None) -> None:
        self._monitors: dict[str, Monitor] = {}
        self._pending: list[str] = []
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._path = Path(session_dir) / "monitors.json" if session_dir else None

    # ── 등록/조회 ──────────────────────────────

    def add(
        self,
        cond: Condition,
        *,
        deadline_s: int,
        once: bool = True,
        run: str = "",
    ) -> Monitor:
        mon = Monitor(
            id=f"mon-{uuid.uuid4().hex[:6]}",
            cond=cond,
            deadline_at=time.time() + _clamp_deadline(deadline_s),
            once=once,
            run=run,
        )
        # `silence` 가 등록 직후 즉시 발화하지 않도록 기준 시각을 심는다.
        mon.state["registered_at"] = mon.created_at
        with self._lock:
            self._monitors[mon.id] = mon
        self._save()
        self.start()
        return mon

    def delete(self, mon_id: str) -> bool:
        with self._lock:
            gone = self._monitors.pop(mon_id, None) is not None
        if gone:
            self._save()
        return gone

    def list_all(self) -> list[Monitor]:
        with self._lock:
            return [m for m in self._monitors.values() if m.alive]

    def get(self, mon_id: str) -> Monitor | None:
        with self._lock:
            return self._monitors.get(mon_id)

    # ── 런 수명 / 메일박스 ─────────────────────

    def has_active_work(self) -> bool:
        """살아 있는 모니터가 있으면 런이 끝나면 안 된다 (§7.1).

        `run` 펌프와 web 의 `web_instance_is_active` **양쪽**이 이걸 본다 —
        `run` 만 고치면 board 인스턴스가 모니터를 데리고 조용히 사라진다.
        """
        with self._lock:
            return any(m.alive for m in self._monitors.values()) or bool(self._pending)

    def has_pending(self) -> bool:
        with self._lock:
            return bool(self._pending)

    def drain(self) -> list[str]:
        """미배달 보고 전부 — 턴 경계에서 관찰 레코드로 주입된다."""
        with self._lock:
            out, self._pending = self._pending, []
            return out

    # ── 평가 스레드 ────────────────────────────

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="monitor-poll", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(POLL_SECS):
            try:
                self.tick(time.time())
            except Exception:  # 감시가 런을 죽이면 안 된다
                pass
            with self._lock:
                if not any(m.alive for m in self._monitors.values()):
                    return

    def tick(self, now: float) -> None:
        """한 틱 — 테스트가 직접 부를 수 있도록 스레드와 분리."""
        with self._lock:
            live = [m for m in self._monitors.values() if m.alive]
        for mon in live:
            if now >= mon.deadline_at:
                self._retire(mon, "deadline 만료", now)
                continue
            try:
                hit: Match | None = mon.cond.check(mon.state, now=now)
            except Exception:
                continue
            if hit is None:
                continue
            mon.matches += len(hit.lines) or 1
            mon._buf.extend(hit.lines)
            if mon.once:
                self._retire(mon, "1회 발화 — 모니터 은퇴", now, flush=True)
            elif now - mon._last_report >= MIN_INTERVAL_S:
                self._flush(mon, now)

    # ── 발화 ───────────────────────────────────

    def _run_side_effect(self, mon: Monitor) -> str:
        """`run` 필드 — 보고 **전에** 실행하고 결과를 보고문에 싣는다.

        notify 없는 shell 을 표현할 수 없게 만든 것이 설계 의도다(§4): 새벽
        3시에 명령이 돌았는데 사람도 모델도 영영 모르는 상태를 막는다.
        """
        if not mon.run:
            return ""
        import subprocess

        from agent_cli.monitor.conditions import COMMAND_TIMEOUT_S

        try:
            proc = subprocess.run(
                mon.run,
                shell=True,
                capture_output=True,
                timeout=COMMAND_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return f"↳ run {mon.run!r}: 타임아웃"
        tail = proc.stdout.decode("utf-8", errors="replace").strip().splitlines()
        tail_s = f" · {tail[-1][:200]}" if tail else ""
        return f"↳ run {mon.run!r}: exit {proc.returncode}{tail_s}"

    def _flush(self, mon: Monitor, now: float, *, note: str = "") -> None:
        lines, mon._buf = mon._buf, []
        side = self._run_side_effect(mon)
        extra = " · ".join(x for x in (side, note) if x)
        report = _format_report(mon, lines, note=extra)
        mon.wakes += 1
        mon._last_report = now
        with self._lock:
            self._pending.append(report)
        if mon.alive and mon.wakes >= MAX_WAKES:
            self._retire(mon, f"알림 상한({MAX_WAKES}) 도달 — 해제됨", now)

    def _retire(
        self, mon: Monitor, why: str, now: float, *, flush: bool = False
    ) -> None:
        """**모든 해제 경로가 마지막 보고를 남긴다** (§7.1).

        초판은 `max_wakes` 만 알리고 `deadline` 만료는 조용히 끝나게 뒀다 —
        `run` 에선 펌프가 그냥 종료돼 세션이 아무 말 없이 끝난다. 같은 규칙을
        셋(once·deadline·max_wakes) 모두에 적용한다.
        """
        if mon.retired:
            return
        mon.retired = why
        if flush or mon._buf:
            lines, mon._buf = mon._buf, []
            side = self._run_side_effect(mon)
            report = _format_report(
                mon, lines, note=" · ".join(x for x in (side, why) if x)
            )
        else:
            report = _format_report(mon, [f"({why}) 누적 매치 {mon.matches}건"])
        mon.wakes += 1
        with self._lock:
            self._pending.append(report)

    # ── 영속 — 기록하되 **부활시키지 않는다** (§8) ─────────

    def _save(self) -> None:
        """살아 있는 모니터 목록을 기록한다. 실패해도 조용히 넘어간다 —
        감시는 보조 기능이고 디스크 오류로 런을 죽일 이유가 없다."""
        if self._path is None:
            return
        with self._lock:
            rows = [
                {"id": m.id, "desc": m.cond.describe(), "once": m.once, "run": m.run}
                for m in self._monitors.values()
                if m.alive
            ]
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({"monitors": rows}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass


def describe_previous(session_dir) -> list[str]:
    """이전 세션의 모니터 설명 줄 — **복원하지 않고 알리기만** 한다 (§8).

    부활을 자른 이유 넷:

    1. **희소하다.** `deadline` 상한 24h + `once=True` 기본이라 resume 시점에
       살아 있을 모니터가 애초에 적다.
    2. **커서가 낡는다.** `match` 의 바이트 오프셋은 중단 기간 동안 무의미해진다
       — 되감으면 옛 매치를 다시 보고하고 건너뛰면 그동안의 매치를 잃는데,
       **둘 다 틀렸고 어느 쪽이 맞는지 알 방법이 없다.**
    3. **권한 구멍.** 세션 디렉터리 기본값이 워크스페이스 안(`paths.py`)이라
       에이전트가 `write_file` 로 이 파일을 고칠 수 있다. 되살리면 **아무도
       승인하지 않은 `command`/`run` 이 실행된다** — 파일 안의 플래그는
       "이 프로세스에서 사람이 답했다"를 증명하지 못한다.
    4. 재검증 매트릭스(PID×파일×deadline×확인)가 통째로 사라진다.

    사용자가 다시 걸면 **확인도 다시 받는다** — 그게 정직한 상태다.
    """
    path = Path(session_dir) / "monitors.json"
    try:
        rows = json.loads(path.read_text(encoding="utf-8")).get("monitors", [])
    except (OSError, json.JSONDecodeError, AttributeError):
        return []
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        extra = f" · run={r['run']!r}" if r.get("run") else ""
        out.append(f"[{r.get('id', '?')}] {r.get('desc', '?')}{extra}")
    return out
