"""테스트용 `Caller` 팩토리 (v10.87.0 LoopKind) — `tests/loop_ports.py` 의 짝.

`Caller` 는 **기본값이 없다** — 중첩 루프를 여는 자리가 호출자 정보를 빠뜨리면 생성 시
`TypeError` 가 나는 것이 설계의 전부다(주소 상속이 조용히 ``"main"`` 으로 새던 사고).
그 강제는 프로덕션 조립 지점(`Caller.from_loop`·`Caller.main_session`)에 걸려야 의미가
있고, 테스트는 필요한 몇 개만 덮어쓰면 된다 — 여기 팩토리가 그 선이다.
"""

from __future__ import annotations

from typing import Any

from agent_cli.loop.kind import Caller


def make_caller(**overrides: Any) -> Caller:
    """전부 비어 있는 main 호출자 + 덮어쓰기. ``owner`` 기본값은 ``"main"``, 깊이 0."""
    base: dict[str, Any] = {
        "owner": "main",
        "depth": 0,
        "max_depth": 2,
        "max_turns": 0,
        "binding": None,
        "tools": None,
        "hooks_config": None,
        "hook_runner": None,
        "mcp_manager": None,
        "agent_registry": None,
        "session": None,
        "compaction_enabled": True,
        "graceful_interrupt": False,
        "verbose": False,
        "agent_role": "",
        "skill_stack": [],
        "agent_stack": [],
        "ctx": None,
        "stop_event": None,
    }
    base.update(overrides)
    return Caller(**base)


def fake_oneshot_run(body):
    """`OneShotLoop.run` 대역 — 카드(스코프)는 진짜로 열고 닫되 ctx·엔진 대신 ``body(self)``
    를 돈다. 종전 테스트가 `_run_single` 을 가짜로 바꾸던 자리: 카드 수명과 호출자 인자는
    보고, 루프는 돌리지 않는다."""
    import time

    def run(self, start=None):
        start = start or self.start()
        handle = self.open_scope(start)
        t0 = time.monotonic()
        result = None
        try:
            result = body(self)
            return result
        finally:
            self.close_scope(handle, result, time.monotonic() - t0)

    return run
