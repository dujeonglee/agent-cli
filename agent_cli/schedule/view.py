"""예약의 화면용 뷰 — web API 와 SSE 가 같은 모양을 보낸다."""

from __future__ import annotations

from agent_cli.schedule import cron
from agent_cli.schedule.registry import ScheduleRegistry

_LOG_ROWS = 20


def schedules_view(registry: ScheduleRegistry) -> dict:
    rows = []
    for s in registry.list_all():
        nxt = registry.next_fire(s)
        rows.append(
            {
                "id": s.id,
                "source": s.source,
                "cron": s.cron,
                "human": cron.describe(s.cron),
                "prompt": s.prompt,
                "label": s.label,
                "nickname": s.nickname,
                "effective_nickname": s.effective_nickname,
                "enabled": s.enabled,
                "next_fire": nxt.isoformat(timespec="minutes") if nxt else None,
                "last_fired_at": s.last_fired_at,
                "missed_at": s.missed_at,
            }
        )
    return {"schedules": rows, "log": registry.recent_log(_LOG_ROWS)}
