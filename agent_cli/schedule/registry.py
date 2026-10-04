"""세션의 예약을 소유하고 발화하는 레지스트리 (docs/schedule/DESIGN.md §4–§5).

`MonitorRegistry` 와 같은 뼈대다: 데몬 스레드 하나가 깨울 때를 판단하고,
켜진 예약이 있는 동안 `has_active_work()` 가 프로세스를 살려 둔다. 다른 점은
둘이다 — 상태가 `schedules.json` 에 남아 재기동을 넘기고, 꺼져 있던 동안 지난
발화를 **자동 실행하지 않고** 질문(`missed_at`)으로 남긴다.

폴링 틱이 아니라 sleep-until-next + rearm 이다. 가장 이른 다음 발화까지 자되
``max_sleep``(300s) 을 넘기지 않는다 — 타이머는 시스템 절전 동안 멈추므로,
상한이 없으면 깨어난 뒤 임의로 늦게 발화한다. 상한 덕에 5분 안에 다시 정산하고,
자는 동안 지난 것은 ``miss_threshold`` 를 넘겨 질문이 된다.

시각은 전부 서버 로컬 naive datetime 이다 (cron 의 의미가 로컬 벽시계다).

한 세션은 한 프로세스만 연다(`context/session_lock.py`). 그래서 이 파일을
동시에 고치는 다른 프로세스는 없다고 전제한다.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from agent_cli.fsio import append_line, atomic_write_json
from agent_cli.schedule import cron

MISS_THRESHOLD_S = 120.0  # 이보다 오래 지난 발화 = 놓침 → 질문 (자동 실행 금지)
MAX_SLEEP_S = 300.0  # 절전·시계 점프 안전망
AGENT_CAP = 5  # 에이전트가 등록할 수 있는 예약 수 (세션당)
DEFAULT_NICKNAME = "⏰ Scheduler"

_STATE = "schedules.json"
_LOG = "schedule-log.jsonl"

#: ``enqueue(prompt, nickname)`` — 프롬프트를 사용자 요청처럼 입력 큐에 넣는다.
Enqueue = Callable[[str, str], None]


class ScheduleError(ValueError):
    """예약을 받아들일 수 없다 — 메시지는 모델·사용자에게 그대로 간다."""


@dataclass
class Schedule:
    id: str
    source: str  # "user" | "agent"
    cron: str
    prompt: str
    label: str
    nickname: str
    enabled: bool
    created_at: str
    #: 이 시각까지의 발화는 전부 정리됐다 — 만들 때·켤 때·발화·건너뛰기에 갱신.
    #: exactly-once 가드이자 "만들기 전·꺼 둔 동안은 놓친 게 아니다" 의 근거.
    settled_at: str
    last_fired_at: str | None = None
    missed_at: str | None = None

    @property
    def effective_nickname(self) -> str:
        return self.nickname or DEFAULT_NICKNAME


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


class ScheduleRegistry:
    def __init__(
        self,
        session_dir=None,
        *,
        clock: Callable[[], datetime] = datetime.now,
        miss_threshold: float = MISS_THRESHOLD_S,
        max_sleep: float = MAX_SLEEP_S,
    ) -> None:
        self._dir = Path(session_dir) if session_dir else None
        self._clock = clock
        self._miss_threshold = miss_threshold
        self._max_sleep = max_sleep
        self._lock = threading.RLock()
        self._rearm = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: 입력 큐 seam — 부트스트랩이 꽂는다. 없으면 발화는 실패(→ missed).
        self.enqueue: Enqueue | None = None
        #: 상태가 바뀔 때마다 불린다 (web 이 SSE 로 알린다).
        self.on_change: Callable[[], None] | None = None
        self._schedules: dict[str, Schedule] = self._load()

    # ── 저장 ────────────────────────────────────────────────

    def _load(self) -> dict[str, Schedule]:
        if self._dir is None:
            return {}
        try:
            raw = json.loads((self._dir / _STATE).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        out: dict[str, Schedule] = {}
        for row in raw.get("schedules", []):
            try:
                s = Schedule(**row)
            except TypeError:
                continue  # 손상된 행 하나가 나머지를 막지 않는다
            out[s.id] = s
        return out

    def _save(self) -> None:
        if self._dir is not None:
            atomic_write_json(
                self._dir / _STATE,
                {"v": 1, "schedules": [asdict(s) for s in self._schedules.values()]},
                indent=1,
            )
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:  # 화면 알림 실패가 예약을 죽이면 안 된다
                pass

    def _log(self, s: Schedule, event: str, due: datetime | None, **extra) -> None:
        if self._dir is None:
            return
        row = {
            "ts": self._clock().isoformat(timespec="seconds"),
            "id": s.id,
            "label": s.label,
            "event": event,
            "due": due.isoformat(timespec="seconds") if due else None,
            **extra,
        }
        try:
            append_line(self._dir / _LOG, json.dumps(row, ensure_ascii=False))
        except OSError:
            pass

    def recent_log(self, limit: int = 20) -> list[dict]:
        if self._dir is None:
            return []
        try:
            lines = (self._dir / _LOG).read_text(encoding="utf-8").splitlines()
        except (FileNotFoundError, OSError):
            return []
        out = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    # ── 등록/조회 ───────────────────────────────────────────

    def add(
        self,
        cron_expr: str,
        prompt: str,
        *,
        label: str = "",
        nickname: str = "",
        source: str = "agent",
    ) -> Schedule:
        cron_expr = (cron_expr or "").strip()
        prompt = (prompt or "").strip()
        try:
            cron.parse(cron_expr)
        except ValueError as e:
            raise ScheduleError(f"invalid cron {cron_expr!r}: {e}") from None
        if not prompt:
            raise ScheduleError("prompt is empty")
        with self._lock:
            if source == "agent":
                mine = sum(1 for s in self._schedules.values() if s.source == "agent")
                if mine >= AGENT_CAP:
                    raise ScheduleError(
                        f"this session already has {AGENT_CAP} agent schedules — "
                        "delete one first"
                    )
            now = self._clock().isoformat(timespec="seconds")
            s = Schedule(
                id=uuid.uuid4().hex[:8],
                source=source,
                cron=cron_expr,
                prompt=prompt,
                label=label.strip(),
                nickname=nickname.strip(),
                enabled=True,
                created_at=now,
                settled_at=now,
            )
            self._schedules[s.id] = s
            self._save()
        self._rearm.set()
        self.start()
        return s

    def delete(self, sched_id: str) -> bool:
        with self._lock:
            if self._schedules.pop(sched_id, None) is None:
                return False
            self._save()
        self._rearm.set()
        return True

    def set_enabled(self, sched_id: str, enabled: bool) -> Schedule | None:
        with self._lock:
            s = self._schedules.get(sched_id)
            if s is None:
                return None
            if enabled and not s.enabled:
                # 꺼 둔 동안 지난 것은 놓친 게 아니다.
                s.settled_at = self._clock().isoformat(timespec="seconds")
            s.enabled = enabled
            self._save()
        self._rearm.set()
        if enabled:
            self.start()
        return s

    def get(self, sched_id: str) -> Schedule | None:
        with self._lock:
            return self._schedules.get(sched_id)

    def list_all(self) -> list[Schedule]:
        with self._lock:
            return list(self._schedules.values())

    def has_active_work(self) -> bool:
        """켜진 예약이 있으면 프로세스는 꺼지지 않는다 (web 유휴 종료 술어와
        `run` 펌프가 같이 본다)."""
        with self._lock:
            return any(s.enabled for s in self._schedules.values())

    def next_fire(self, s: Schedule) -> datetime | None:
        if not s.enabled:
            return None
        try:
            return cron.next_fire(cron.parse(s.cron), self._clock())
        except ValueError:
            return None

    def next_wake(self) -> datetime | None:
        """켜진 예약 전체에서 가장 이른 다음 발화 (없으면 None)."""
        fires = [nf for s in self.list_all() if (nf := self.next_fire(s))]
        return min(fires) if fires else None

    # ── 정산 ────────────────────────────────────────────────

    def _due(self, s: Schedule, now: datetime):
        """None(빚 없음) | ("fire", due) | ("missed", due)."""
        if not s.enabled:
            return None
        try:
            spec = cron.parse(s.cron)
        except ValueError:
            return None  # 손상된 행이 루프를 죽이면 안 된다
        due = cron.prev_fire(spec, now)
        settled = _parse(s.settled_at)
        if due is None or (settled is not None and settled >= due):
            return None  # 이미 정리됨 — exactly-once
        if (now - due).total_seconds() > self._miss_threshold:
            return ("missed", due)
        return ("fire", due)

    def settle(self) -> None:
        """지금 기준으로 빚진 것을 전부 정리한다: 제때인 것은 발화하고, 늦은
        것은 질문으로 남긴다."""
        now = self._clock()
        with self._lock:
            for s in list(self._schedules.values()):
                verdict = self._due(s, now)
                if verdict is None:
                    continue
                kind, due = verdict
                if kind == "fire":
                    self._fire(s, due)
                else:
                    self._mark_missed(s, due, "missed")

    def _fire(self, s: Schedule, due: datetime | None) -> bool:
        now = self._clock()
        try:
            if self.enqueue is None:
                raise RuntimeError("no input queue is attached")
            self.enqueue(s.prompt, s.effective_nickname)
        except Exception as e:
            # 재시도 루프 대신 질문으로 내린다.
            self._mark_missed(s, due or now, "failed", error=str(e))
            return False
        stamp = now.isoformat(timespec="seconds")
        s.last_fired_at = stamp
        s.settled_at = stamp
        s.missed_at = None
        self._save()
        self._log(s, "fired", due)
        return True

    def _mark_missed(self, s: Schedule, due: datetime, event: str, **extra) -> None:
        stamp = due.isoformat(timespec="seconds")
        s.missed_at = stamp  # 여러 주기를 놓쳐도 덮어써서 질문 1건
        # 이 발화는 "질문으로 넘김" 으로 정리됐다 — 다시 판정하지 않는다
        # (실패한 발화를 깰 때마다 재시도하는 루프도 여기서 끊긴다).
        s.settled_at = max(s.settled_at, stamp)
        self._save()
        self._log(s, event, due, **extra)

    def run_now(self, sched_id: str) -> bool:
        """즉시 발화 — 놓친 발화의 "지금 실행" 과 수동 실행 버튼이 같이 쓴다."""
        with self._lock:
            s = self._schedules.get(sched_id)
            if s is None:
                return False
            due = _parse(s.missed_at)
            return self._fire(s, due)

    def dismiss(self, sched_id: str) -> bool:
        """놓친 발화를 건너뛴다."""
        with self._lock:
            s = self._schedules.get(sched_id)
            if s is None or s.missed_at is None:
                return False
            due = _parse(s.missed_at)
            s.missed_at = None
            s.settled_at = self._clock().isoformat(timespec="seconds")
            self._save()
            self._log(s, "skipped", due)
        return True

    # ── 스레드 ──────────────────────────────────────────────

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="schedule", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._rearm.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.settle()
            except Exception:  # 예약이 런을 죽이면 안 된다
                pass
            self._rearm.wait(self._sleep_for())
            self._rearm.clear()

    def _sleep_for(self) -> float:
        nxt = self.next_wake()
        if nxt is None:
            return self._max_sleep
        return max(0.05, min((nxt - self._clock()).total_seconds(), self._max_sleep))
