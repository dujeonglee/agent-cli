"""Agent loop: ReAct pattern with M1/M2 module integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_cli.loop.core import AgentLoop

if TYPE_CHECKING:
    from agent_cli.context.manager import ContextManager
    from agent_cli.loop.kind import LoopKind, Start
    from agent_cli.tools.result import ToolResult


def run_loop(kind: LoopKind, start: Start, ctx: ContextManager | None) -> ToolResult:
    """Run the agent loop. Returns ToolResult — `AgentLoop(kind, start, ctx).run()` 의 함수 꼴.

    엔진 이음매(v10.132.0): 종류(`LoopKind.run`)가 기본 엔진으로 부르고, 테스트가
    ``patch("agent_cli.loop.run_loop")`` 로 다섯 종류 전부를 가로채는 자리다. 정체·설정은
    종류가, 런마다 다른 것(질의·귀속·이미지·중단 핸들)은 ``start`` 가 든다.
    """
    return AgentLoop(kind, start, ctx).run()
