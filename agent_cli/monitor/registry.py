"""MonitorRegistry — 조건 평가 스레드 (docs/monitor/DESIGN.md §6, docs/wiring §3.2).

`AgentRegistry` 의 형제다: 스레드를 소유하고 `has_active_work()` 로 런 수명에
합류한다. 보고는 **설치한 주소로** 간다 — `deliver` seam 을 통해 main 이면
메일박스, `agent:<key>` 면 그 에이전트의 inbox(항목 1개 = 런 1개).

## 왜 주소 배달인가 (docs/wiring §1.2)

종전엔 보고를 `_pending` 리스트 하나에 쌓고 main 의 턴 경계가 `drain()` 으로
전부 가져갔다. 주소가 없으니 **에이전트가 건 감시의 보고도 main 이 가져갔다** —
요구가 안 되는 게 아니라 정반대로 동작했다. 게다가 소유자별 drain 만 덧붙이면
main 이 빈 wake 턴을 무한히 도는 라이브락이 된다(설계 2판이 그랬다).

질문·답·독촉이 이미 같은 주소 어휘로 같은 두 백엔드에 배달하고 있었다. 모니터는
그 **네 번째 고객**이고, 그래서 깨우기 술어도 배달 지점도 새로 만들지 않는다 —
main 소유 보고는 메일박스에 들어가므로 `MailWaker` 가 **이미** 깨운다.

## 왜 큐가 아니라 메일박스인가 (§6.1)

보고문을 입력 큐에 넣으면 **사람 메시지로 위장된다** — `push_user_message` 가
사용자 카드를 그리고 `QUEUED_REQUEST_NOTICE`("Another *user request* arrived")가
붙으며, history·resume·검색에서 사람 턴과 구별되지 않는다. 게다가 `run` 은 턴
중 큐 주입을 안 해 배달이 런 종료 후로 밀리고, `--result-file` 이 모니터 보고에
대한 응답으로 덮인다.

"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable
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


class MonitorUnavailable(RuntimeError):
    """배달 배선이 없거나 세션이 닫혔다 — 등록해 봐야 보고가 갈 곳이 없다.

    조용히 등록해 두고 발화 때 잃는 것보다 **등록을 거부하는 쪽**이 낫다:
    이 저장소의 배선 사고는 전부 "조용히 아무 일도 안 일어남" 이었다.
    """


@dataclass
class Monitor:
    id: str
    cond: Condition
    deadline_at: float
    #: 보고가 갈 **주소** — ``"main"`` | ``"agent:<key>"`` (docs/wiring §3.2).
    #: 기본값을 두지 않는다: 주소 없는 모니터는 보고를 잃는다.
    owner: str
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
    #: 소유자 사망으로 **폐기**됐는가. `retired` 로는 구분이 안 된다 —
    #: `_retire` 가 배달 **전에** 사유를 세우므로(멱등성 가드) 은퇴하는
    #: 모니터는 전부 truthy 다.
    dropped: bool = False

    @property
    def alive(self) -> bool:
        return not self.retired


def _log_delivery_failure(mon: Monitor, err: str) -> None:
    """배달 실패를 남긴다 — **도달 불가가 목표**인 경로다 (docs/wiring §3.6).

    조기 드롭이 `kill`/`shutdown_all` 양쪽에서 `stop_event.set()` 보다 앞서고
    배달 직전 `alive` 재확인이 있으므로 여기 오면 안 된다. 그래서 통지를
    만들지 않고(도달 불가 분기를 UI 에 남기지 않는다) 로그만 남긴다 —
    미래의 회귀가 조용하지 않게.
    """
    from agent_cli.verbose import debug_log

    debug_log(f"monitor {mon.id} 배달 실패 (owner={mon.owner}): {err}")


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
    head = (
        f"🔔 monitor {mon.id} ({mon.cond.describe()})   {ago} ago · {len(lines)} hit(s)"
    )
    body = [ln[:REPORT_MAX_LINE_CHARS] for ln in lines[:REPORT_MAX_LINES]]
    if len(lines) > REPORT_MAX_LINES:
        body.append(f"… {len(lines) - REPORT_MAX_LINES} more")
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
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._path = Path(session_dir) / "monitors.json" if session_dir else None
        #: 주소 배달 seam (`AgentRegistry.deliver`) — 부트스트랩이 꽂는다.
        self.deliver: Callable[..., str] | None = None
        #: 세션 종료 표식 — 종료 뒤의 등록·배달을 막는다. 전체 드롭 대신
        #: 이걸 쓰는 이유: 드롭이 `_save()` 를 부르면 정상 종료가
        #: `monitors.json` 을 비워 다음 세션의 "이전 세션 모니터" 통지가
        #: 사라진다.
        self.closed = False
        #: 배달 중(부작용 실행 포함) 건수 — 생존 판정이 이걸 센다. 안 세면
        #: `run` 펌프가 보고를 날리며 종료할 수 있다.
        self._inflight = 0

    # ── 등록/조회 ──────────────────────────────

    def add(
        self,
        cond: Condition,
        *,
        owner: str,
        deadline_s: int,
        once: bool = True,
        run: str = "",
    ) -> Monitor:
        """감시 등록. ``owner`` 로 보고가 간다.

        배달 배선이 없거나 세션이 닫혔으면 :class:`MonitorUnavailable`.
        도달하면 배선이 깨졌다는 뜻이다 — 조립기가 어떤 루프보다 먼저
        `deliver` 를 꽂는다(docs/wiring §3.3).
        """
        if self.deliver is None:
            raise MonitorUnavailable("monitor: delivery is not wired (internal error)")
        if self.closed:
            raise MonitorUnavailable("monitor: the session is shutting down")
        mon = Monitor(
            id=f"mon-{uuid.uuid4().hex[:6]}",
            cond=cond,
            deadline_at=time.time() + _clamp_deadline(deadline_s),
            owner=owner,
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
            mon = self._monitors.pop(mon_id, None)
            if mon is not None:
                # pop 만으로는 부족하다 — `tick` 은 `live` 를 **스냅샷**한 뒤
                # 락 밖에서 돌므로, 삭제된 모니터가 한 번 더 발화할 수 있다.
                # `alive` 로 거르지 않는 이유는 `drop_owner` 와 같다: 은퇴
                # 중(부작용 실행 중)이면 이미 `alive == False` 라, 거기서
                # 건너뛰면 방금 지운 감시의 은퇴 통지가 그대로 나간다.
                if mon.alive:
                    mon.retired = "deleted"
                mon.dropped = True
        if mon is not None:
            self._save()
        return mon is not None

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
            return any(m.alive for m in self._monitors.values()) or self._inflight > 0

    def drop_owner(self, addr: str) -> int:
        """소유자가 죽었다 — 그 주소의 감시를 폐기. 살아 있던 건수 반환.

        재부모화하지 않는다: 에이전트가 자기 목적으로 건 감시를 main 이
        물려받을 이유가 없다.

        **`_save()` 를 부르지 않는다.** `delete` 처럼 저장하면 살아 있는
        행만 쓰는 `_save` 가 종료 시 `monitors.json` 을 비워, 다음 세션의
        "이전 세션 모니터" 통지가 사라진다(문서화된 §8 동작의 회귀).
        """
        n = 0
        with self._lock:
            for mon in self._monitors.values():
                if mon.owner != addr:
                    continue
                # **`alive` 로 거르지 않는다.** `_retire` 는 멱등성 가드로
                # 사유를 배달 **전에** 세우므로, 부작용이 도는 사이에
                # 소유자가 죽으면 그 모니터는 이미 `alive == False` 다.
                # 거기서 건너뛰면 마지막 보고가 죽은 주소로 간다(테스트가
                # 잡았다). 이미 배달을 마친 모니터에 `dropped` 를 세우는
                # 것은 무해하다.
                if mon.alive:
                    mon.retired = f"owner gone ({addr})"
                    n += 1
                mon.dropped = True
        return n

    def _send(self, mon: Monitor, report: str, *, retiring: bool = False) -> str:
        """소유자에게 배달 — 에러 문자열 또는 "".

        배달 **직전** `_lock` 아래서 살아 있는지 재확인한다. 그 사이
        `drop_owner`/`delete` 가 들어왔으면 보내지 않는다 — 그래서 "죽은
        소유자에게 보내고 실패를 통지" 라는 분기가 아예 필요 없어진다.

        ``retiring``: `_retire` 는 멱등성 가드로 `retired` 를 **먼저**
        세우므로 그 시점의 모니터는 이미 `alive == False` 다. 은퇴 보고는
        나가야 하므로 그때는 **폐기**(`dropped`)만 본다.
        """
        with self._lock:
            gone = mon.dropped if retiring else not mon.alive
            if gone or self.closed:
                return ""
            fn = self.deliver
        if fn is None:
            return "no delivery wiring"
        return fn(
            mon.owner,
            mail={
                "kind": "monitor",
                # 없으면 `_deliver_agent_mail` 이 실패 관찰로 그린다.
                "success": True,
                "output": report,
            },
            text=report,
            # 에이전트 inbox 로 갈 때 이 값이 `tm.current_author` 가 되고,
            # 그 런에서 거는 ask 의 **대상**이 된다. 주소가 아닌 값이면
            # 그 ask 가 "unroutable" 로 취소된다.
            author="main",
            # 이 런의 산출물은 어디로도 되돌아가지 않는다 — 질문·독촉·peer
            # 회신과 같은 의미다.
            expects_reply=False,
        )

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
                self._retire(mon, "deadline expired", now)
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
                self._retire(mon, "fired once — monitor retired", now, flush=True)
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
            return f"↳ run {mon.run!r}: timed out"
        tail = proc.stdout.decode("utf-8", errors="replace").strip().splitlines()
        tail_s = f" · {tail[-1][:200]}" if tail else ""
        return f"↳ run {mon.run!r}: exit {proc.returncode}{tail_s}"

    def _flush(self, mon: Monitor, now: float, *, note: str = "") -> None:
        # 카운터는 **부작용 전에** 올린다 — `run` 서브프로세스가
        # `COMMAND_TIMEOUT_S` 까지 걸리는데, 그 사이 생존 판정이 거짓이 되면
        # 펌프가 보고를 날리며 종료한다. 내리는 것은 반드시 `finally` 에서:
        # `_loop` 가 `tick` 의 예외를 삼키므로 한 번만 새면 카운터가 영원히
        # 0 이 아니고, 그러면 세션이 **영영 안 끝난다**.
        with self._lock:
            if not mon.alive or self.closed:
                return
            self._inflight += 1
        try:
            lines, mon._buf = mon._buf, []
            side = self._run_side_effect(mon)
            extra = " · ".join(x for x in (side, note) if x)
            report = _format_report(mon, lines, note=extra)
            mon.wakes += 1
            mon._last_report = now
            err = self._send(mon, report)
            if err:
                _log_delivery_failure(mon, err)
        finally:
            with self._lock:
                self._inflight -= 1
        if mon.alive and mon.wakes >= MAX_WAKES:
            self._retire(mon, f"report cap ({MAX_WAKES}) reached — released", now)

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
        # `retired` 를 **먼저** 세운다 — 폴링 스레드와 `drop_owner`/`delete`
        # 가 겹칠 때의 멱등성 가드다. 그래서 배달 시점엔 은퇴하는 모니터가
        # 전부 `retired` 이고, 폐기와 구별하려면 `dropped` 가 필요하다.
        mon.retired = why
        with self._lock:
            if self.closed:
                return
            self._inflight += 1
        try:
            if flush or mon._buf:
                lines, mon._buf = mon._buf, []
                side = self._run_side_effect(mon)
                report = _format_report(
                    mon, lines, note=" · ".join(x for x in (side, why) if x)
                )
            else:
                report = _format_report(mon, [f"({why}) {mon.matches} match(es) total"])
            mon.wakes += 1
            err = self._send(mon, report, retiring=True)
            if err:
                _log_delivery_failure(mon, err)
        finally:
            with self._lock:
                self._inflight -= 1

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
