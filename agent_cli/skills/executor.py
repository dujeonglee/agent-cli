"""Skill executor — substitute arguments and call run_loop."""

from __future__ import annotations

import re
import subprocess

from agent_cli.constants import SHELL_COMMAND_TIMEOUT, DELEGATE_DEFAULT_TIMEOUT
from agent_cli.context.manager import ContextManager
from agent_cli.loop import run_loop
from agent_cli.providers.base import LLMProvider
from agent_cli.providers.compat import ModelCapabilities
from agent_cli.skills.models import Skill
from agent_cli.tools.result import ToolResult

# Pattern for !`command` dynamic context injection
_SHELL_INJECT_PATTERN = re.compile(r"!`([^`]+)`")


def _execute_shell_inject(m: re.Match) -> str:
    """Execute a shell command and return its output for template injection."""
    cmd = m.group(1)
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=SHELL_COMMAND_TIMEOUT,
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
    """Replace $ARGUMENTS, $N, ${SKILL_DIR}, ${SESSION_ID}, !`cmd` in template.

    ${CLAUDE_SKILL_DIR} is supported as an alias for Claude Code compatibility.

    When a skill's prompt must document the template syntax itself (e.g.
    create-skill showing what ${SKILL_DIR} looks like), put the
    placeholder-bearing docs in a `references/` file and instruct the
    LLM to read_file it at runtime. Tool results do not pass through
    this substitution, so the literal placeholder survives to the LLM.
    """
    # Dynamic context injection: !`command` → command output
    result = _SHELL_INJECT_PATTERN.sub(_execute_shell_inject, template)

    # Built-in variables (${SKILL_DIR} is the primary form; ${CLAUDE_SKILL_DIR} for compat)
    result = result.replace("${SKILL_DIR}", skill_dir)
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
    provider_name: str = "openai",
    base_url: str = "",
    api_key: str = "",
    max_turns: int = 0,
    verbose: bool = False,
    max_depth: int = 2,
    delegate_timeout: int = DELEGATE_DEFAULT_TIMEOUT,
    ctx: ContextManager | None = None,
    session=None,
    skill_stack: list[str] | None = None,
    graceful_interrupt: bool = False,
    stop_event=None,
    parent_tools: list[str] | None = None,
    parent_role: str = "",
    parent_hooks_config: dict | None = None,
    parent_depth: int = 0,
):
    """Execute a skill by substituting arguments and calling run_loop.

    Tool intersection: skill's allowed_tools ∩ parent_tools.
    If intersection is empty, execution is rejected.
    Parent role is inherited for system prompt.

    Hooks: parent_hooks_config is merged with the skill's own frontmatter
    hooks (skill.hooks) so skill-local matchers layer on top of the
    caller's hooks for the duration of this skill's execution.
    """
    from pathlib import Path

    from agent_cli.hooks import merge_hooks_configs

    # Tool intersection: skill tools ∩ parent tools
    effective_tools = skill.allowed_tools
    if effective_tools and parent_tools:
        effective_tools = [t for t in effective_tools if t in parent_tools]
        if not effective_tools:
            skill_tools = ", ".join(skill.allowed_tools)
            parent_str = ", ".join(parent_tools)
            return ToolResult(
                False,
                error=f"Skill '{skill.name}' cannot run: "
                f"no tools in common between skill ({skill_tools}) "
                f"and parent ({parent_str})",
            )
    elif not effective_tools and parent_tools:
        # Skill has no tool restriction → use parent's tools
        effective_tools = parent_tools

    skill_dir = str(Path(skill.source_path).parent) if skill.source_path else ""
    session_id = ""
    prompt = substitute_arguments(
        skill.prompt_template, arguments, skill_dir=skill_dir, session_id=session_id
    )

    effective_max_turns = skill.max_turns if skill.max_turns > 0 else max_turns
    effective_model = skill.model if skill.model else model

    # Skill creates its own subdir with history.jsonl
    skill_ctx = None
    skill_dir_name = ""
    if ctx:
        import os
        import time as _time

        name = skill.name or "skill"
        hash_part = os.urandom(3).hex()
        ts = _time.strftime("%Y%m%dT%H%M%S", _time.gmtime())
        ms = f"{int(_time.time() * 1000) % 1000:03d}"
        skill_dir_name = f"skill_{name}_{hash_part}_{ts}{ms}"
        skill_session_dir = ctx.session_dir / skill_dir_name
        skill_ctx = ContextManager(
            session_dir=skill_session_dir,
            max_context_tokens=ctx.max_context_tokens,
            wire_format=ctx.wire_format,
        )

    effective_hooks_config = merge_hooks_configs(parent_hooks_config, skill.hooks)

    loop_result = run_loop(
        query=prompt,
        provider=provider,
        capabilities=capabilities,
        model=effective_model,
        provider_name=provider_name,
        base_url=base_url,
        api_key=api_key,
        max_turns=effective_max_turns,
        verbose=verbose,
        depth=parent_depth + 1,
        max_depth=max_depth,
        delegate_timeout=delegate_timeout,
        active_tools=effective_tools,
        ctx=skill_ctx or ctx,
        session=session,
        hooks_config=effective_hooks_config,
        skill_name=skill.name,
        skill_stack=skill_stack,
        skill_args=arguments,
        graceful_interrupt=graceful_interrupt,
        stop_event=stop_event,
        agent_role=parent_role,
    )

    result = loop_result.output if loop_result.success else None

    # Save result.md in skill subdir
    if skill_ctx and skill_dir_name and result:
        try:
            result_path = skill_ctx.session_dir / "result.md"
            result_path.write_text(result, encoding="utf-8")
        except Exception:
            pass

    artifact = f"{skill_dir_name}/" if skill_dir_name else ""
    if result:
        return ToolResult(True, output=result, artifact=artifact)
    else:
        return ToolResult(False, error="Skill returned no result", artifact=artifact)
