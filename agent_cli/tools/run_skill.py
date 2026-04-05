"""run_skill tool — lets LLM invoke skills programmatically."""

from __future__ import annotations

from agent_cli.tools.result import ToolResult


def tool_run_skill(args: dict, **kwargs) -> ToolResult:
    """Execute a skill by name. Called by the loop as a virtual tool.

    The actual execution requires provider/capabilities/model context,
    which are passed via **kwargs from _do_execute_tool.
    """

    name = args.get("name", "")
    if not name:
        return ToolResult(
            False, error="'name' is required. Specify the skill name to run."
        )

    arguments = args.get("arguments", "")

    from agent_cli.skills import load_skills

    skills = load_skills()
    if name not in skills:
        available = ", ".join(skills.keys()) if skills else "(none)"
        return ToolResult(
            False, error=f"skill '{name}' not found. Available: {available}"
        )

    skill = skills[name]

    # Check if skill disables model invocation
    if skill.disable_model_invocation:
        return ToolResult(
            False,
            error=f"skill '{name}' is user-only (disable-model-invocation: true).",
        )

    from agent_cli.skills.executor import execute_skill

    provider = kwargs.get("provider")
    capabilities = kwargs.get("capabilities")
    model = kwargs.get("model", "")

    if not provider or not capabilities:
        return ToolResult(
            False, error="run_skill requires provider context (internal error)."
        )

    result = execute_skill(
        skill=skill,
        arguments=arguments,
        provider=provider,
        capabilities=capabilities,
        model=model,
        provider_name=kwargs.get("provider_name", ""),
        base_url=kwargs.get("base_url", ""),
        api_key=kwargs.get("api_key", ""),
        max_turns=skill.max_turns,
        suppress_output=True,
        session=kwargs.get("session"),
        graceful_interrupt=kwargs.get("graceful_interrupt", False),
    )

    return ToolResult(True, output=result or "(skill returned no result)")
