"""테스트가 엔진을 직접 세우는 길 하나 (v10.131.2) — `tests/loop_ports.py`·`tests/loop_kinds.py` 의 짝.

프로덕션에서 엔진에 들어가는 길은 종류(`LoopKind.run`) 하나뿐이다. 테스트는 종류 없이
부품(``provider``·``capabilities``·``model``·``ports``·``ctx``·``dialect``)과 설정 몇 개로
엔진을 돌리거나 세운다 — 274곳이 같은 키워드 집합으로 부르므로 여기 도우미 둘이 받는다.

- `run_loop(...)` — 루프를 돌려 `ToolResult` 를 돌려준다.
- `make_loop(...)` — `AgentLoop` 를 세우기만 한다(메서드를 직접 부르는 테스트).

지금은 제품의 `run_loop`·`AgentLoop` 에 같은 인자를 그대로 넘긴다.
"""

from __future__ import annotations

from agent_cli.loop.core import AgentLoop
from agent_cli.loop.run import run_loop as _run_loop
from agent_cli.tools.result import ToolResult


def run_loop(
    *,
    query: str,
    provider=None,
    capabilities=None,
    model: str = "",
    ports=None,
    ctx=None,
    dialect=None,
    **attrs,
) -> ToolResult:
    """엔진을 한 번 돌린다 — ``attrs`` 는 설정(``max_turns``·``depth``·…)과 시작의 귀속
    (``query_author``·``query_request_id``·…)."""
    return _run_loop(
        query=query,
        provider=provider,
        capabilities=capabilities,
        model=model,
        ports=ports,
        ctx=ctx,
        dialect=dialect,
        **attrs,
    )


def make_loop(
    *,
    query: str,
    provider=None,
    capabilities=None,
    model: str = "",
    ports=None,
    ctx=None,
    dialect=None,
    **attrs,
) -> AgentLoop:
    """`AgentLoop` 를 세운다(돌리지 않는다) — 인자는 `run_loop` 와 같다."""
    return AgentLoop(
        query=query,
        provider=provider,
        capabilities=capabilities,
        model=model,
        ports=ports,
        ctx=ctx,
        dialect=dialect,
        **attrs,
    )
