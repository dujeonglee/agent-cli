"""모니터 테스트용 가짜 배달부 (docs/wiring §3.2).

C2 이후 모니터 보고는 `_pending` 에 쌓이지 않고 **주소로 즉시 배달**된다.
그래서 테스트는 `drain()` 대신 이 기록기를 읽는다 — 배달이 실제로 일어났고
**어느 주소로** 갔는지까지 보게 되는 것이 요점이다. 종전 `drain()` 은
주소를 안 보여줬고, 그게 "에이전트가 건 감시의 보고를 main 이 가져간다"는
버그가 4000개 테스트를 통과한 이유다.
"""

from __future__ import annotations


class RecordingDelivery:
    """`AgentRegistry.deliver` 자리에 꽂는 기록기."""

    def __init__(self, error: str = "") -> None:
        self.calls: list[tuple[str, dict]] = []
        self.error = error

    def __call__(self, addr: str, **kw) -> str:
        self.calls.append((addr, kw))
        return self.error

    # ── 읽기 편의 ──────────────────────────────────
    @property
    def addrs(self) -> list[str]:
        return [a for a, _ in self.calls]

    @property
    def reports(self) -> list[str]:
        return [kw["text"] for _, kw in self.calls]

    def to(self, addr: str) -> list[str]:
        return [kw["text"] for a, kw in self.calls if a == addr]

    def take(self) -> list[str]:
        """구 `registry.drain()` 대체 — 읽고 비운다(같은 의미)."""
        out, self.calls = self.reports, []
        return out


def wire(registry, error: str = "") -> RecordingDelivery:
    """레지스트리에 기록기를 꽂고 돌려준다."""
    rec = RecordingDelivery(error)
    registry.deliver = rec
    return rec
