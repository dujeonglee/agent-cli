"""P1 결과 어댑터 — run 에이전트의 회신을 종전 도구 결과 바이트로 (docs/agent-engine/DESIGN.md §8).

**P2 에서 지운다 — 결과는 회신으로(설계 §8).** 이 모듈·함수 이름·``STATUS: …`` 조립 문자열은
P1 임시물 목록(`tests/test_agent_engine_transitional.py`, docs/agent-engine/DESIGN.md §4)에 있고,
P2 가 그 테스트를 "저장소 어디에도 없다" 로 뒤집는다.

run 에이전트가 보내는 회신(`RunReply` — 엔진 결과·걸린 시간·런이 거둔 것 한 줄·대화)을
모델이 지금까지 받던 모양으로 바꾼다: 단건은 ``STATUS: success\\nRESULT:\\n…`` /
``STATUS: error\\nERROR: Subagent did not complete\\n…`` 에 ``artifact`` (``run_…/``)와
``<run_dir>/result.md`` 저장, 병렬은 ``[Task i] …`` 묶음. 조립은 `subagent.report` 의 포맷터를
그대로 부른다(옮기지 않는다). 다른 코드는 이 모듈을 이름으로만 부른다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_cli.subagent.report import (
    DelegateResult,
    _extract_activity_log,
    _extract_last_actions,
    _format_delegate_output,
    _format_parallel_results,
    _persist_run_result,
)
from agent_cli.tools.result import ToolResult


@dataclass(frozen=True)
class RunReply:
    """run 에이전트 하나가 요청자에게 돌려준 것.

    ``refusal`` 이 있으면 런이 열리지 못했다(ctx 전 거부) — 나머지는 비어 있다. 아니면
    ``result`` 는 엔진 결과, ``ctx`` 는 그 런의 대화(활동 로그·턴 수를 읽는다), ``run_dir`` 은
    결과 파일 자리(세션 디렉터리 아래 ``run_…``)."""

    refusal: str = ""
    result: Any = None
    ctx: Any = None
    run_dir: Path | None = None
    secs: float = 0.0
    released: str = ""


def write_result_md_and_tool_result(reply: RunReply) -> ToolResult:
    """회신 하나 → ``<run_dir>/result.md`` 를 쓰고 단건 run 의 도구 결과를 돌려준다.
    거부(``refusal``)면 쓰지 않는다."""
    if reply.refusal:
        return ToolResult(False, error=reply.refusal)
    result, ctx = reply.result, reply.ctx
    result_str = result.output if result.success else None
    delegate_result = DelegateResult(output=result_str, duration_secs=reply.secs)
    delegate_result.activity_log = _extract_activity_log(ctx.get_raw_messages())
    # 턴 수 = 루프가 실제 돈 턴 (활동 로그는 상한이 있어 세면 틀린다, v9.24.1)
    delegate_result.iterations = ctx.current_turn
    if result_str is None:
        delegate_result.last_actions = _extract_last_actions(ctx.get_raw_messages())
    formatted = _format_delegate_output(delegate_result)
    if reply.released:
        formatted = f"{formatted}\n\n{reply.released}"
    _persist_run_result(formatted, reply.run_dir)
    artifact = f"{reply.run_dir.name}/"
    if result_str is not None:
        return ToolResult(
            True, output=f"STATUS: success\nRESULT:\n{formatted}", artifact=artifact
        )
    return ToolResult(
        False,
        error=f"STATUS: error\nERROR: Subagent did not complete\n{formatted}",
        artifact=artifact,
    )


def batch_tool_result(
    specs: list[dict], results: list[ToolResult | None]
) -> ToolResult:
    """한 턴의 run 여럿 → 묶음 결과. ``None`` 은 회신 없이 끝난 스레드(종전 문구 그대로)."""
    return _format_parallel_results(specs, results)
