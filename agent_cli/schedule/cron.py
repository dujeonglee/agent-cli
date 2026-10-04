"""Minimal cron expression engine for session schedules (docs/schedule/DESIGN.md §3).

Moved here from agent-board in v10.12.0, when the scheduler moved into the
agent-cli process.

Pure module — zero dependencies, server-local naive datetimes. Supports the
5-field form ``분 시 일 월 요일`` with ``*``, numbers, lists (``1,3,5``), ranges
(``1-5``), and steps (``*/15``, ``1-9/2``). Weekday 0=Sun (7 accepted as Sun).

Day-of-month / day-of-week combine per standard (vixie) cron: when BOTH are
restricted the date matches if EITHER matches; when one is ``*`` the other
alone restricts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

_FIELDS = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day", 1, 31),
    ("month", 1, 12),
    ("weekday", 0, 6),
)

_WEEKDAY_KO = "일월화수목금토"  # cron 0=일 … 6=토

# next_fire 룩어헤드 상한 — 어떤 유효식도 366일 안에 발화일이 있다
# (2/29 지정 같은 극단도 4년이 아니라... 2/29는 못 잡으므로 명시 한계로 둠).
_LOOKAHEAD_DAYS = 366


@dataclass(frozen=True)
class CronSpec:
    minutes: frozenset
    hours: frozenset
    days: frozenset
    months: frozenset
    weekdays: frozenset
    dom_star: bool  # 일(day-of-month) 필드가 '*' 였는가
    dow_star: bool  # 요일 필드가 '*' 였는가


def _parse_field(text: str, name: str, lo: int, hi: int) -> frozenset:
    """One cron field → the set of matching ints. Raises ValueError loudly."""
    out: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"cron {name}: empty list item in {text!r}")
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            try:
                step = int(step_s)
            except ValueError:
                raise ValueError(f"cron {name}: bad step in {text!r}") from None
            if step < 1:
                raise ValueError(f"cron {name}: step must be >= 1 in {text!r}")
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            try:
                start, end = int(a), int(b)
            except ValueError:
                raise ValueError(f"cron {name}: bad range in {text!r}") from None
        else:
            try:
                start = end = int(part)
            except ValueError:
                raise ValueError(f"cron {name}: bad value in {text!r}") from None
        # weekday alias: 7 == Sunday == 0
        if name == "weekday":
            if start == 7:
                start = 0
            if end == 7:
                end = 0
        if start > end:
            raise ValueError(f"cron {name}: reversed range in {text!r}")
        if start < lo or end > hi:
            raise ValueError(
                f"cron {name}: {part!r} out of range {lo}-{hi} in {text!r}"
            )
        out.update(range(start, end + 1, step))
    return frozenset(out)


def parse(expr: str) -> CronSpec:
    """``'분 시 일 월 요일'`` → CronSpec. Invalid expressions raise ValueError."""
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"cron expression must have 5 fields, got {len(fields)}")
    parsed = [
        _parse_field(f, name, lo, hi) for f, (name, lo, hi) in zip(fields, _FIELDS)
    ]
    return CronSpec(
        minutes=parsed[0],
        hours=parsed[1],
        days=parsed[2],
        months=parsed[3],
        weekdays=parsed[4],
        dom_star=fields[2] == "*",
        dow_star=fields[4] == "*",
    )


def _day_matches(spec: CronSpec, d: date) -> bool:
    if d.month not in spec.months:
        return False
    dom_ok = d.day in spec.days
    dow_ok = ((d.weekday() + 1) % 7) in spec.weekdays  # python Mon=0 → cron Sun=0
    if spec.dom_star and spec.dow_star:
        return True
    if spec.dom_star:
        return dow_ok
    if spec.dow_star:
        return dom_ok
    return dom_ok or dow_ok  # standard cron: both restricted → OR


def next_fire(spec: CronSpec, after: datetime) -> datetime:
    """The first fire time STRICTLY AFTER ``after`` (naive, server-local).

    Iterates matching days (≤366 lookahead), then the sorted hour/minute sets
    within each — no minute-by-minute scan. Raises ValueError if no fire time
    exists in the lookahead window (unsatisfiable date combos like 2/30)."""
    start = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    hours = sorted(spec.hours)
    minutes = sorted(spec.minutes)
    for day_offset in range(_LOOKAHEAD_DAYS + 1):
        d = start.date() + timedelta(days=day_offset)
        if not _day_matches(spec, d):
            continue
        floor = start.time() if day_offset == 0 else time(0, 0)
        for h in hours:
            if h < floor.hour:
                continue
            for m in minutes:
                if h == floor.hour and m < floor.minute:
                    continue
                return datetime.combine(d, time(h, m))
    raise ValueError("no fire time within 366 days (unsatisfiable expression?)")


def prev_fire(spec: CronSpec, before: datetime) -> datetime | None:
    """The last fire time AT OR BEFORE ``before`` — the scheduler's "직전 발화"
    for missed-run detection. None if none exists in the lookback window."""
    end = before.replace(second=0, microsecond=0)
    hours = sorted(spec.hours, reverse=True)
    minutes = sorted(spec.minutes, reverse=True)
    for day_offset in range(_LOOKAHEAD_DAYS + 1):
        d = end.date() - timedelta(days=day_offset)
        if not _day_matches(spec, d):
            continue
        ceil = end.time() if day_offset == 0 else time(23, 59)
        for h in hours:
            if h > ceil.hour:
                continue
            for m in minutes:
                if h == ceil.hour and m > ceil.minute:
                    continue
                return datetime.combine(d, time(h, m))
    return None


def describe(expr: str) -> str:
    """사람이 읽는 한국어 라벨 — 대표 패턴만, 그 외엔 원문 반환.

    UI 병기용 best-effort. ``parse`` 를 통과한 식만 넘어온다고 가정하지 않고
    자체적으로도 안전(파싱 실패 시 원문)."""
    try:
        spec = parse(expr)
    except ValueError:
        return expr
    f = expr.split()
    single_min = len(spec.minutes) == 1
    single_hour = len(spec.hours) == 1
    every_min = f[0] == "*"
    step_min = f[0].startswith("*/")

    def hhmm() -> str:
        return f"{next(iter(spec.hours)):02d}:{next(iter(spec.minutes)):02d}"

    if f[1] == f[2] == f[3] == f[4] == "*":
        if every_min:
            return "매분"
        if step_min:
            return f"{f[0][2:]}분마다"
        if single_min:
            m = next(iter(spec.minutes))
            return "매시 정각" if m == 0 else f"매시 {m}분"
    if single_min and single_hour and f[2] == "*" and f[3] == "*":
        if f[4] == "*":
            return f"매일 {hhmm()}"
        days = "·".join(_WEEKDAY_KO[d] for d in sorted(spec.weekdays))
        return f"매주 {days} {hhmm()}"
    if (
        single_min
        and single_hour
        and f[3] == "*"
        and f[4] == "*"
        and len(spec.days) == 1
    ):
        return f"매월 {next(iter(spec.days))}일 {hhmm()}"
    return expr
