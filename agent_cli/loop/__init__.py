"""AgentLoop 패키지 — C1(Option 3) 파일 물리 분할.

기존 단일 모듈 ``agent_cli/loop.py`` 의 공개 표면을 그대로 재수출한다
(``from agent_cli.loop import X`` 전부 무변경). 모듈 배치 = 소유권:
state(불변 배선+공유 상태+센티널) / prompt / tool_bridge / llm /
dispatch / skill_invoke / core(AgentLoop 오케스트레이터) / run(진입점).
테스트가 monkeypatch 하는 모듈 전역(render_step 등)은 **사용하는
서브모듈**을 patch 해야 한다 (__init__ 바인딩 patch 는 서브모듈 전역에
닿지 않음).
"""

from agent_cli.loop.state import (
    LoopConfig,
    LoopState,
    _CONTINUE,
    _NOT_HANDLED,
    _RETRY,
)
from agent_cli.loop.prompt import SystemPromptSvc, build_inspector_sections
from agent_cli.loop.tool_bridge import ToolBridge
from agent_cli.loop.llm import LLMCaller, _build_token_stats
from agent_cli.loop.dispatch import (
    TurnDispatcher,
    _append_observation,
    _combined_tool_label,
    _extract_questions,
    _handle_ask,
    _sanitize_truncated_edit,
    _try_echo_as_final,
)
from agent_cli.loop.skill_invoke import _handle_run_skill
from agent_cli.loop.core import AgentLoop
from agent_cli.loop.run import run_loop

__all__ = [
    "AgentLoop",
    "run_loop",
    "LoopConfig",
    "LoopState",
    "SystemPromptSvc",
    "ToolBridge",
    "LLMCaller",
    "TurnDispatcher",
    "build_inspector_sections",
    "_CONTINUE",
    "_NOT_HANDLED",
    "_RETRY",
    "_append_observation",
    "_build_token_stats",
    "_combined_tool_label",
    "_extract_questions",
    "_handle_ask",
    "_handle_run_skill",
    "_sanitize_truncated_edit",
    "_try_echo_as_final",
]
