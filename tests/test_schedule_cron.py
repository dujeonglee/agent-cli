"""cron 엔진 (docs/schedule/DESIGN.md §3) — parse / next_fire / prev_fire / describe."""

from __future__ import annotations

from datetime import datetime

import pytest

from agent_cli.schedule.cron import describe, next_fire, parse, prev_fire


def dt(*a):
    return datetime(*a)


class TestParse:
    def test_every_minute(self):
        s = parse("* * * * *")
        assert len(s.minutes) == 60 and len(s.hours) == 24
        assert s.dom_star and s.dow_star

    def test_weekly(self):
        s = parse("0 9 * * 1")
        assert s.minutes == frozenset({0})
        assert s.hours == frozenset({9})
        assert s.weekdays == frozenset({1})
        assert s.dom_star and not s.dow_star

    def test_list_range_step(self):
        s = parse("*/15 9-17 1,15 * 1-5")
        assert s.minutes == frozenset({0, 15, 30, 45})
        assert s.hours == frozenset(range(9, 18))
        assert s.days == frozenset({1, 15})
        assert s.weekdays == frozenset({1, 2, 3, 4, 5})

    def test_weekday_seven_is_sunday(self):
        assert parse("0 0 * * 7").weekdays == frozenset({0})

    @pytest.mark.parametrize(
        "bad",
        [
            "0 9 * *",  # 4 fields
            "0 9 * * * *",  # 6 fields
            "60 * * * *",  # minute out of range
            "* 24 * * *",  # hour out of range
            "* * 0 * *",  # day 0
            "* * 32 * *",  # day 32
            "* * * 13 *",  # month 13
            "* * * * 8",  # weekday 8
            "a * * * *",  # non-numeric
            "5-1 * * * *",  # reversed range
            "*/0 * * * *",  # zero step
            ", * * * *",  # empty list item
        ],
    )
    def test_invalid_rejected(self, bad):
        with pytest.raises(ValueError):
            parse(bad)


class TestNextFire:
    def test_weekly_monday_9(self):
        s = parse("0 9 * * 1")
        # 2026-08-13 = 목요일 → 다음 월요일은 8/17
        assert next_fire(s, dt(2026, 8, 13, 12, 0)) == dt(2026, 8, 17, 9, 0)

    def test_strictly_after(self):
        s = parse("0 9 * * 1")
        # 정확히 발화 시각이면 '다음' 발화 (같은 시각 재반환 금지)
        assert next_fire(s, dt(2026, 8, 17, 9, 0)) == dt(2026, 8, 24, 9, 0)

    def test_same_day_later(self):
        s = parse("30 14 * * *")
        assert next_fire(s, dt(2026, 8, 13, 9, 0)) == dt(2026, 8, 13, 14, 30)

    def test_rolls_to_next_day(self):
        s = parse("30 14 * * *")
        assert next_fire(s, dt(2026, 8, 13, 15, 0)) == dt(2026, 8, 14, 14, 30)

    def test_month_end_rollover(self):
        s = parse("0 10 1 * *")  # 매월 1일 10시
        assert next_fire(s, dt(2026, 8, 31, 23, 0)) == dt(2026, 9, 1, 10, 0)

    def test_year_end_rollover(self):
        s = parse("0 0 1 1 *")  # 매년 1/1 자정
        assert next_fire(s, dt(2026, 12, 31, 23, 59)) == dt(2027, 1, 1, 0, 0)

    def test_day_31_skips_short_months(self):
        s = parse("0 0 31 * *")
        # 9월엔 31일이 없음 → 10/31
        assert next_fire(s, dt(2026, 9, 1, 0, 0)) == dt(2026, 10, 31, 0, 0)

    def test_dom_dow_both_restricted_is_or(self):
        # 표준 cron: 일·요일 둘 다 제한이면 OR — 13일 또는 금요일
        s = parse("0 0 13 * 5")
        # 2026-08-13 은 목요일: 8/13(dom 매치)이 8/14(금, dow 매치)보다 먼저
        assert next_fire(s, dt(2026, 8, 12, 0, 0)) == dt(2026, 8, 13, 0, 0)
        assert next_fire(s, dt(2026, 8, 13, 0, 0)) == dt(2026, 8, 14, 0, 0)

    def test_every_minute(self):
        s = parse("* * * * *")
        assert next_fire(s, dt(2026, 8, 13, 9, 0, 30)) == dt(2026, 8, 13, 9, 1)

    def test_unsatisfiable_raises(self):
        with pytest.raises(ValueError):
            next_fire(parse("0 0 30 2 *"), dt(2026, 1, 1))  # 2/30 은 없음


class TestPrevFire:
    def test_weekly(self):
        s = parse("0 9 * * 1")
        # 목요일 기준 직전 월요일 9시
        assert prev_fire(s, dt(2026, 8, 13, 12, 0)) == dt(2026, 8, 10, 9, 0)

    def test_at_or_before_inclusive(self):
        s = parse("0 9 * * 1")
        assert prev_fire(s, dt(2026, 8, 10, 9, 0)) == dt(2026, 8, 10, 9, 0)

    def test_none_when_unsatisfiable(self):
        assert prev_fire(parse("0 0 30 2 *"), dt(2026, 1, 1)) is None

    def test_roundtrip_with_next(self):
        s = parse("*/15 * * * *")
        t = dt(2026, 8, 13, 10, 7)
        nf = next_fire(s, t)
        assert prev_fire(s, nf) == nf  # inclusive
        assert prev_fire(s, t) == dt(2026, 8, 13, 10, 0)


class TestDescribe:
    @pytest.mark.parametrize(
        "expr,label",
        [
            ("* * * * *", "매분"),
            ("*/30 * * * *", "30분마다"),
            ("0 * * * *", "매시 정각"),
            ("15 * * * *", "매시 15분"),
            ("0 9 * * *", "매일 09:00"),
            ("30 14 * * *", "매일 14:30"),
            ("0 9 * * 1", "매주 월 09:00"),
            ("0 9 * * 1,3,5", "매주 월·수·금 09:00"),
            ("0 10 1 * *", "매월 1일 10:00"),
        ],
    )
    def test_common_labels(self, expr, label):
        assert describe(expr) == label

    def test_uncommon_falls_back_to_expr(self):
        assert describe("5 4 * 2 *") == "5 4 * 2 *"

    def test_invalid_falls_back_to_expr(self):
        assert describe("not a cron") == "not a cron"
