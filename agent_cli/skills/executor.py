"""Skill executor — substitute arguments and call run_loop."""

from __future__ import annotations

import re
import subprocess

from agent_cli.context.manager import ContextManager
from agent_cli.loop import run_loop
from agent_cli.providers.base import LLMProvider
from agent_cli.providers.compat import ModelCapabilities
from agent_cli.skills.models import Skill

# Pattern for !`command` dynamic context injection
_SHELL_INJECT_PATTERN = re.compile(r"!`([^`]+)`")


def _execute_shell_inject(m: re.Match) -> str:
    """Execute a shell command and return its output for template injection."""
    cmd = m.group(1)
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=30
        )
        output = result.stdout.strip()
        if result.returncode != 0:
            stderr = result.stderr.strip()
            return f"[error] {cmd}: {stderr or '(exit code ' + str(result.returncode) + ')'}"
        return output
    except subprocess.TimeoutExpired:
        return f"[error] {cmd}: timed out (30s)"
    except Exception as e:
        return f"[error] {cmd}: {e}"


def substitute_arguments(
    template: str,
    arguments: str,
    skill_dir: str = "",
    session_id: str = "",
) -> str:
    """Replace $ARGUMENTS, $N, ${CLAUDE_SKILL_DIR}, ${SESSION_ID}, !`cmd` in template."""
    # Dynamic context injection: !`command` → command output
    result = _SHELL_INJECT_PATTERN.sub(_execute_shell_inject, template)

    # Built-in variables
    result = result.replace("${CLAUDE_SKILL_DIR}", skill_dir)
    result = result.replace("${SESSION_ID}", session_id)

    # $ARGUMENTS[N] bracket notation (before $ARGUMENTS replacement)
    args_list = arguments.split()

    def _replace_bracket(m: re.Match) -> str:
        idx = int(m.group(1))
        return args_list[idx] if idx < len(args_list) else ""

    result = re.sub(r"\$ARGUMENTS\[(\d+)\]", _replace_bracket, result)

    # $ARGUMENTS (full string)
    result = result.replace("$ARGUMENTS", arguments)

    # $N shorthand
    for i, arg in enumerate(args_list):
        result = result.replace(f"${i}", arg)

    # Clean up unreplaced $N patterns (if more placeholders than args)
    result = re.sub(r"\$\d+", "", result)

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
    suppress_output: bool = False,
    max_depth: int = 2,
    delegate_timeout: int = 300,
    ctx: ContextManager | None = None,
    session=None,
    skill_stack: list[str] | None = None,
    graceful_interrupt: bool = False,
) -> str | None:
    """Execute a skill by substituting arguments and calling run_loop."""
    from pathlib import Path

    skill_dir = str(Path(skill.source_path).parent) if skill.source_path else ""
    session_id = (
        str(ctx.session.session_id)
        if ctx and hasattr(ctx, "session") and ctx.session
        else ""
    )
    prompt = substitute_arguments(
        skill.prompt_template, arguments, skill_dir=skill_dir, session_id=session_id
    )

    effective_max_iter = skill.max_iter if skill.max_iter > 0 else max_iter
    effective_model = skill.model if skill.model else model
    effective_ctx = None if skill.context == "fork" else ctx

    return run_loop(
        query=prompt,
        provider=provider,
        capabilities=capabilities,
        model=effective_model,
        provider_name=provider_name,
        base_url=base_url,
        api_key=api_key,
        max_iter=effective_max_iter,
        verbose=verbose,
        suppress_output=suppress_output,
        depth=0,
        max_depth=max_depth,
        delegate_timeout=delegate_timeout,
        active_tools=skill.allowed_tools,
        ctx=effective_ctx,
        session=session,
        skill_name=skill.name,
        skill_stack=skill_stack,
        skill_args=arguments,
        graceful_interrupt=graceful_interrupt,
    )
