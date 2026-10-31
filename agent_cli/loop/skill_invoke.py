"""`run_skill` op 의 루프 레벨 처리 — 이름·인자 검사 뒤 `SkillLoop` 를 인스턴스화한다."""

from __future__ import annotations

from agent_cli.constants import OBS_SUCCESS
from agent_cli.loop.kind import Caller
from agent_cli.tools.result import ToolResult
from agent_cli.verbose import debug_log as _debug_log


def _handle_run_skill(skill_input: dict, caller: Caller) -> ToolResult:
    """``run_skill`` — 스킬을 찾아 `SkillLoop(skill, arguments, caller).run()` 하고 관찰로
    포장한다. 순환·깊이 가드는 여기(호출자 스택·깊이로 판정); 스코프·훅·ctx·result.md 는
    종류의 것이다(v10.87.0 — 종전엔 이 함수와 `main._dispatch_skill` 이 같은 래퍼를 두 벌
    들고 있었다)."""
    from agent_cli.recovery.recursion import (
        format_depth_limit_error,
        format_recursion_error,
    )
    from agent_cli.skills import load_skills
    from agent_cli.skills.executor import SkillLoop

    name = skill_input.get("name", "")
    arguments = skill_input.get("arguments", "")
    # LLM 이 인자를 dict 로 보내기도 한다
    if not isinstance(arguments, str):
        arguments = str(arguments) if arguments else ""

    if not name:
        return ToolResult(False, error="run_skill: `name` is required.")

    # 순환(A → B → A) — 스택은 max_depth 로 유계라 사실상 상수 시간
    if caller.skill_stack and name in caller.skill_stack:
        return ToolResult(
            False,
            error=format_recursion_error("skill", name, list(caller.skill_stack)),
        )
    # 깊이 상한 — 엔진 필터가 상한에서 `run_skill` 을 뺐으므로 보통 디스패치는 여기 오지
    # 않는다; 직접 호출(테스트·custom tools)이 같은 문구를 받게 남긴다.
    if caller.depth >= caller.max_depth:
        return ToolResult(
            False,
            error=format_depth_limit_error(
                "skill", name, caller.depth, caller.max_depth
            ),
        )

    skills = load_skills()
    if name not in skills:
        available = ", ".join(skills.keys()) if skills else "(none)"
        return ToolResult(
            False, error=f"Skill '{name}' not found. Available: {available}"
        )
    skill = skills[name]
    if skill.disable_model_invocation:
        return ToolResult(
            False, error=f"Skill '{name}' is user-only (disable-model-invocation)."
        )

    skill_result = SkillLoop(skill, arguments, caller, tool_input=skill_input).run()

    if isinstance(skill_result, ToolResult) and not skill_result.success:
        if skill_result.error and skill_result.error.startswith("run_skill("):
            return skill_result
        _debug_log(f"run_skill({name}) failed: {skill_result.error}")

    skill_header = f"SKILL: {name}({arguments})\n" if arguments else f"SKILL: {name}\n"
    body = skill_result.output or skill_result.error or "(skill returned no result)"
    obs = OBS_SUCCESS.format(result=f"{skill_header}{body}")
    return ToolResult(skill_result.success, output=obs, artifact=skill_result.artifact)
