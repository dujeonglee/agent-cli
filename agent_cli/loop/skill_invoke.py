"""Agent loop: ReAct pattern with M1/M2 module integration."""

from __future__ import annotations

import time

from agent_cli.constants import (
    OBS_SUCCESS,
)
from agent_cli.providers.capabilities import ModelCapabilities
from agent_cli.render import (
    render_group_end,
    render_group_start,
    render_pop_depth,
    render_push_depth,
)
from agent_cli.tools.result import ToolResult
from agent_cli.verbose import debug_log as _debug_log

# Max shrink-and-retry attempts per turn when the server rejects the
# prompt as too long (flow 2 reactive recovery). Each attempt sheds more
# history via ``ContextManager.force_fit``; the bound stops a runaway
# loop when the cache cannot shrink enough or the server keeps rejecting.


def _handle_run_skill(
    skill_input: dict,
    provider_name: str,
    base_url: str,
    api_key: str,
    capabilities: ModelCapabilities,
    model: str,
    ctx,
    session,
    parent_skill_name: str = "",
    skill_stack: list[str] | None = None,
    graceful_interrupt: bool = False,
    stop_event=None,
    hook_runner=None,
    mcp_manager=None,
    parent_hooks_config: dict | None = None,
    parent_depth: int = 0,
    max_depth: int = 2,
    compaction_enabled: bool = True,
    agent_registry=None,
):
    """Handle run_skill at loop level with full ctx access."""
    # Inline import: circular dependency — executor.py imports run_loop from this module
    from agent_cli.recovery.recursion import (
        format_depth_limit_error,
        format_recursion_error,
    )
    from agent_cli.skills import load_skills
    from agent_cli.skills.executor import execute_skill

    name = skill_input.get("name", "")
    arguments = skill_input.get("arguments", "")
    # LLM might send arguments as dict instead of string
    if not isinstance(arguments, str):
        arguments = str(arguments) if arguments else ""

    if not name:
        return ToolResult(False, error="run_skill: 'name' is required.")

    # Cycle check (A → B → A). Stack lookup is O(N) but the stack
    # is bounded by ``max_depth`` so this is effectively constant.
    if skill_stack and name in skill_stack:
        return ToolResult(
            False,
            error=format_recursion_error("skill", name, list(skill_stack)),
        )

    # Depth ceiling — belt-and-suspenders. The AgentLoop init has
    # already removed ``run_skill`` from the tools_list when we hit
    # the limit, so a model going through the normal dispatch path
    # never reaches this branch. Direct callers (tests, custom
    # active_tools, future integrations) hit it here with the same
    # message the LLM would otherwise see.
    if parent_depth >= max_depth:
        return ToolResult(
            False,
            error=format_depth_limit_error("skill", name, parent_depth, max_depth),
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

    # OnSkillStart hook
    if hook_runner:
        hook_runner.fire(
            "OnSkillStart",
            tool_name="run_skill",
            tool_input=skill_input,
            mcp_manager=mcp_manager,
        )

    render_group_start(f"skill:{name}", icon="🪄")
    render_push_depth()
    t0 = time.monotonic()

    try:
        from agent_cli.providers import create_provider

        provider = create_provider(provider_name, base_url, api_key)
        skill_result = execute_skill(
            skill=skill,
            arguments=arguments,
            provider=provider,
            capabilities=capabilities,
            model=model,
            provider_name=provider_name,
            base_url=base_url,
            api_key=api_key,
            max_depth=max_depth,
            ctx=ctx,
            session=session,
            skill_stack=skill_stack,
            graceful_interrupt=graceful_interrupt,
            stop_event=stop_event,
            parent_hooks_config=parent_hooks_config,
            parent_depth=parent_depth,
            compaction_enabled=compaction_enabled,
            agent_registry=agent_registry,
        )
    except Exception as e:
        _debug_log(f"run_skill({name}) exception: {e}")
        skill_result = ToolResult(False, error=f"run_skill({name}) failed: {e}")
    finally:
        render_pop_depth()
        render_group_end(
            f"skill:{name}",
            success=skill_result.success if skill_result else False,
            duration_s=time.monotonic() - t0,
        )

    # OnSkillEnd hook
    if hook_runner:
        hook_runner.fire(
            "OnSkillEnd",
            tool_name="run_skill",
            tool_input=skill_input,
            skill_result=skill_result,
            mcp_manager=mcp_manager,
        )

    if isinstance(skill_result, ToolResult) and not skill_result.success:
        if skill_result.error and skill_result.error.startswith("run_skill("):
            return skill_result
        _debug_log(f"run_skill({name}) failed: {skill_result.error}")

    skill_header = f"SKILL: {name}({arguments})\n" if arguments else f"SKILL: {name}\n"
    body = skill_result.output or skill_result.error or "(skill returned no result)"
    obs = OBS_SUCCESS.format(result=f"{skill_header}{body}")

    return ToolResult(skill_result.success, output=obs, artifact=skill_result.artifact)


# Regex: simple echo with no pipes, redirects, subshells, or chaining
