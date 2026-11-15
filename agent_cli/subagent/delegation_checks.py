"""에이전트를 띄우기 전의 거부 판정 — run·spawn 공통 진입 (docs/agent-engine/DESIGN.md §2).

판정만 한다: 무엇도 쓰거나 띄우지 않고, 거부면 모델에게 보낼 문장을, 통과면 ``""`` 을
돌려준다. 문구는 종전 일회성 엔진(`OneShotLoop.make_ctx`)의 것 그대로다.

- `depth_or_recursion_refusal` — 같은 이름이 이미 호출 스택에 있다(A → B → A) / 다음 단계가
  깊이 상한을 넘는다. run 과 spawn 이 같은 장치를 부른다.
- `run_refusal` — run 만의 앞 검사(빈 과제·provider 없음) + 위의 둘, 종전 순서대로.

같은 층의 다른 `subagent/` 모듈을 import 하지 않는 잎이다.
"""

from __future__ import annotations

from agent_cli.recovery.recursion import (
    format_depth_limit_error,
    format_recursion_error,
)


def depth_or_recursion_refusal(
    agent_name: str, agent_stack: list[str], depth: int, max_depth: int
) -> str:
    """순환이면 그 거부문, 깊이 상한이면 그 거부문, 아니면 ``""``.

    이름 있는 위임만 스택에 오르니 익명은 깊이로만 막힌다. 깊이 상한은 엔진 필터가 상한에서
    `agent` 도구를 빼므로 보통 디스패치는 여기 오지 않는다 — 직접 호출(테스트·custom tools)이
    같은 문구를 받게 남긴다."""
    if agent_name and agent_name in agent_stack:
        return format_recursion_error("agent", agent_name, list(agent_stack))
    if depth >= max_depth:
        return format_depth_limit_error(
            "agent", agent_name or "(anonymous)", depth, max_depth
        )
    return ""


def run_refusal(
    *,
    task: str,
    binding,
    agent_name: str,
    agent_stack: list[str],
    depth: int,
    max_depth: int,
) -> str:
    """`agent` ``mode`` "run" 과제 하나의 앞 검사 — 빈 과제 → provider 없음 → 순환 → 깊이."""
    if not task.strip():
        return "Delegation rejected: empty task"
    if binding is None or binding.provider is None or binding.capabilities is None:
        return "Delegation rejected: missing provider/capabilities"
    return depth_or_recursion_refusal(agent_name, agent_stack, depth, max_depth)
