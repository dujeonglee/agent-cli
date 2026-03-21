"""Plan generation: Phase 1 of Planning Mode."""

from __future__ import annotations

from agent_cli.parsing.plan_parser import parse_plan_steps
from agent_cli.planning.models import Plan
from agent_cli.prompts.system_prompt import build_plan_generation_prompt
from agent_cli.providers.base import LLMProvider
from agent_cli.providers.compat import ModelCapabilities
from agent_cli.render import render_status
from agent_cli.tools import TOOLS


def generate_plan(
    goal: str,
    provider: LLMProvider,
    capabilities: ModelCapabilities,
    model: str,
    max_steps: int = 20,
    include_delegate: bool = False,
    quiet: bool = False,
    max_retries: int = 3,
) -> Plan | None:
    """Generate a step-by-step plan from a goal description.

    Retries up to max_retries times on parse failure (LLM non-determinism).
    Network/API errors fail immediately without retry.
    Returns a Plan object, or None if all attempts fail.
    """
    system = build_plan_generation_prompt(
        capabilities=capabilities,
        active_tools=list(TOOLS.keys()),
        include_delegate=include_delegate,
        max_steps=max_steps,
    )

    if not quiet:
        render_status("running", "Generating plan...")

    # Disable constrained decoding for plan generation — we need free-form
    # text (numbered list), not JSON ReAct format.
    plan_caps = ModelCapabilities(
        context_window=capabilities.context_window,
        max_output_tokens=capabilities.max_output_tokens,
        supports_structured_output=False,
        supports_tool_calling=capabilities.supports_tool_calling,
        supports_thinking=capabilities.supports_thinking,
        thinking_budget=capabilities.thinking_budget,
        supports_strict_schema=False,
    )

    for attempt in range(max_retries):
        try:
            response = provider.call(
                messages=[{"role": "user", "content": goal}],
                system=system,
                model=model,
                capabilities=plan_caps,
                skip_json_format=True,
            )
        except Exception as e:
            # Network/API error — don't retry
            if not quiet:
                render_status("error", f"Plan generation failed: {e}")
            return None

        steps = parse_plan_steps(response.content)
        if steps:
            if not quiet:
                render_status("done", f"Plan generated: {len(steps)} steps")
            return Plan(goal=goal, steps=steps)

        # Parse failure — retry
        if not quiet and attempt < max_retries - 1:
            render_status(
                "running",
                f"Plan parsing failed, retrying ({attempt + 2}/{max_retries})...",
            )

    if not quiet:
        render_status("error", "No plan steps could be parsed after retries.")
    return None
