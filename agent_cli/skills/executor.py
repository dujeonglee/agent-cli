"""Skill executor — substitute arguments and call run_loop."""
from __future__ import annotations

import re

from agent_cli.loop import run_loop
from agent_cli.providers.base import LLMProvider
from agent_cli.providers.compat import ModelCapabilities
from agent_cli.skills.models import Skill


def substitute_arguments(template: str, arguments: str) -> str:
    """Replace $ARGUMENTS and $1, $2, ... in template."""
    result = template.replace("$ARGUMENTS", arguments)

    args_list = arguments.split()
    for i, arg in enumerate(args_list, 1):
        result = result.replace(f"${i}", arg)

    # Clean up unreplaced $N patterns (if more placeholders than args)
    result = re.sub(r'\$\d+', '', result)

    return result


def execute_skill(
    skill: Skill,
    arguments: str,
    provider: LLMProvider,
    capabilities: ModelCapabilities,
    model: str,
    provider_name: str = "ollama",
    base_url: str = "",
    api_key: str = "",
    max_iter: int = 0,
    verbose: bool = False,
    quiet: bool = False,
    max_depth: int = 2,
    delegate_timeout: int = 300,
) -> str | None:
    """Execute a skill by substituting arguments and calling run_loop."""
    prompt = substitute_arguments(skill.prompt_template, arguments)

    effective_max_iter = skill.max_iter if skill.max_iter > 0 else max_iter

    return run_loop(
        query=prompt,
        provider=provider,
        capabilities=capabilities,
        model=model,
        provider_name=provider_name,
        base_url=base_url,
        api_key=api_key,
        max_iter=effective_max_iter,
        verbose=verbose,
        quiet=quiet,
        depth=0,
        max_depth=max_depth,
        delegate_timeout=delegate_timeout,
        active_tools=skill.active_tools,
    )
