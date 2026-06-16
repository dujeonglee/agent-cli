"""CLI entry point: run (single-shot) and web (interactive browser) commands."""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path
from typing import Optional

import typer

from agent_cli.config import get_provider_defaults
from agent_cli.constants import SHELL_COMMAND_TIMEOUT, DELEGATE_DEFAULT_TIMEOUT
from agent_cli.context.manager import ContextManager
from agent_cli.loop import run_loop
from agent_cli.providers import (
    create_provider,
    get_capabilities,
    UnsupportedModelError,
)
from agent_cli.render import C, console, get_renderer
from agent_cli.wire_formats import DEFAULT_WIRE_FORMAT, get as _get_wire_format

app = typer.Typer(
    name="agent-cli",
    help="AI agent CLI with ReAct pattern. Supports OpenAI-compatible "
    "(OpenAI, vLLM, omlx, LM Studio) and Anthropic.\n\n"
    "Run 'agent-cli setup' to configure.\n"
    "Run 'agent-cli run <task>' for single-shot execution.\n"
    "Run 'agent-cli web' for the interactive browser UI.\n"
    "Run 'agent-cli sessions' to list previous sessions.",
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        from agent_cli import __version__

        typer.echo(f"agent-cli {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(
        None,
        "--version",
        "-V",
        help="Show the agent-cli version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """AI agent CLI with ReAct pattern."""


def _run_shell_inline(cmd: str) -> None:
    """Run a shell command and print output directly. Shared by run and web."""
    console.print(f"[{C['action']}]⚡ SHELL:[/] {cmd}")
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=SHELL_COMMAND_TIMEOUT,
        )
        if result.stdout:
            console.print(result.stdout, end="", highlight=False)
        if result.stderr:
            console.print(f"[{C['error']}]{result.stderr}[/]", end="")
        if result.returncode != 0:
            console.print(f"[{C['muted']}][exit code: {result.returncode}][/]")
    except subprocess.TimeoutExpired:
        console.print(f"[{C['error']}]Command timed out (30s)[/]")


def _resolve_provider(
    provider: str | None,
    model: str | None,
    base_url: str | None,
    api_key: str | None,
):
    """Resolve provider settings: CLI args > config.json > provider defaults.

    ``provider`` is the ``-p`` flag value, or ``None`` when not passed.
    Precedence: explicit flag → config.json's provider → "openai"
    (OpenAI-compatible: covers OpenAI, vLLM, omlx, LM Studio).
    """
    from agent_cli.config import load_config

    config = load_config()

    config_provider = config.get("provider", "")
    if provider:
        # Explicit CLI flag (-p): use it.
        effective_provider = provider
    elif config_provider:
        # No flag, but config.json specifies a provider.
        effective_provider = config_provider
    else:
        # No flag, no config — default to OpenAI-compatible.
        effective_provider = "openai"

    # Resolve: CLI args > config (only if same provider) > provider defaults
    defaults = get_provider_defaults(effective_provider)
    if config_provider == effective_provider:
        resolved_url = base_url or config.get("base_url", "") or defaults.base_url
        resolved_model = (
            model or config.get("default_model", "") or defaults.default_model
        )
    else:
        # Different provider than config — don't use config's URL/model
        resolved_url = base_url or defaults.base_url
        resolved_model = model or defaults.default_model

    if api_key is None:
        if config_provider == effective_provider:
            api_key = config.get("api_key", "")
        else:
            api_key = ""
        if not api_key:
            env_map = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
            api_key = os.environ.get(env_map.get(effective_provider, ""), "")

    return effective_provider, resolved_url, resolved_model, api_key


def _maybe_setup() -> None:
    """Trigger setup wizard if no config exists."""
    from agent_cli.config import has_config

    if not has_config():
        from agent_cli.setup import SetupWizard

        console.print(
            f"[{C['accent']}]No configuration found. Starting setup wizard...[/]\n"
        )
        SetupWizard().run()


def _setup_mcp():
    """Initialize MCP servers from mcp.json. Returns (manager, mcp_tools) or (None, {})."""
    from agent_cli.mcp.config import load_mcp_config
    from agent_cli.mcp.client import McpClientManager
    from agent_cli.mcp.adapter import register_mcp_tools

    configs = load_mcp_config()
    if not configs:
        return None, {}

    manager = McpClientManager()
    results = manager.connect_all(configs)

    for name, status in results.items():
        if status == "connected":
            tool_count = len(manager.list_tools(name))
            console.print(f"  [green]●[/] MCP {name}: {tool_count} tools")
        else:
            console.print(f"  [red]●[/] MCP {name}: {status}")

    mcp_tools = register_mcp_tools(manager)
    return manager, mcp_tools


def _apply_style(style: str | None) -> None:
    """Apply renderer style if specified."""
    if style:
        from agent_cli.render import load_renderer_by_name

        try:
            load_renderer_by_name(style)
        except ValueError as e:
            console.print(f"[{C['error']}]{e}[/]")
            raise typer.Exit(1)


_SKILL_NOT_FOUND = (
    object()
)  # Sentinel to distinguish "not a skill" from "skill returned None"

_AGENT_NOT_FOUND = object()


# ────────────────────────────────────────────────────────────
# Shared ``@<agent>`` / ``/<skill>`` dispatch (web worker + run)
# ────────────────────────────────────────────────────────────

# Why a Protocol-based dispatcher instead of two parallel branches:
# the prefix semantics (``@agents`` lists, ``@<name>`` invokes,
# ``/skills`` lists, ``/<name>`` invokes, both have not-found
# fallbacks) are identical across surfaces. Only the output format
# differs — CLI wants coloured ``console.print``; web wants
# ``renderer.observation`` events. The Protocol pins the contract so
# adding a new surface (a future TUI, an HTTP API for IDE plugins)
# means writing one ~30-line output adapter, not re-implementing the
# 80-line prefix block.


class DispatchOutput:
    """Output adapter for ``try_dispatch_agent_or_skill``.

    All methods are called from the dispatcher — implementors decide
    how to surface each branch (coloured print, SSE event, log line).
    Methods MUST NOT raise; failures should degrade to silence so a
    broken adapter cannot wedge the dispatch loop.
    """

    def list_agents(self, names: list[str]) -> None:
        """Render the available agent names. Empty list = "none found"."""
        raise NotImplementedError

    def list_skills(self, skills: dict) -> None:
        """Render user-invocable skills. ``skills`` keyed by name."""
        raise NotImplementedError

    def agent_not_found(self, name: str) -> None:
        """Surface an unknown agent name."""
        raise NotImplementedError

    def agent_result(self, result) -> None:
        """Final answer string from a delegated agent. ``None`` = silent."""
        raise NotImplementedError

    def skill_not_found(self, name: str) -> None:
        """Surface an unknown slash command."""
        raise NotImplementedError

    def skill_result(self, name: str, result) -> None:
        """Final answer (or ``None``) from a ``/<skill>`` invocation.

        ``result is None`` means the skill stopped without producing a
        final answer (e.g. user aborted) — CLI prints a recovery hint
        so the user knows what to try next; other surfaces may ignore.
        """
        raise NotImplementedError


class _ConsoleDispatchOutput(DispatchOutput):
    """CLI-flavoured output — colour, Rich markup, plain ``console.print``."""

    def list_agents(self, names: list[str]) -> None:
        console.print(f"\n[{C['accent']}]Available agents:[/]")
        if not names:
            console.print(f"[{C['muted']}]No agents found.[/]")
        else:
            for name in names:
                console.print(f"  @{name}")
        console.print(f"\n[{C['muted']}]Usage: @agent-name <task>[/]")

    def list_skills(self, skills: dict) -> None:
        user_skills = {k: v for k, v in skills.items() if v.user_invocable}
        if not user_skills:
            console.print(f"[{C['muted']}]No skills found.[/]")
            return
        console.print(f"\n[{C['accent']}]Available skills:[/]")
        for s in user_skills.values():
            hint = f" {s.argument_hint}" if s.argument_hint else ""
            console.print(f"  /{s.name}{hint}  — {s.description}")
        console.print()

    def agent_not_found(self, name: str) -> None:
        console.print(f"[{C['error']}]Agent not found: @{name}[/]")
        console.print(f"[{C['muted']}]Type @ to list available agents[/]")

    def agent_result(self, result) -> None:
        # ``_dispatch_agent`` already streams the agent's thoughts /
        # tool calls through the renderer; this is the CLI's
        # trailing "headline" green print so the final answer is
        # also immediately visible above the next prompt.
        if result is not None:
            console.print(f"\n[{C['final']}]{result}[/]")

    def skill_not_found(self, name: str) -> None:
        console.print(f"[{C['error']}]Unknown command: /{name}[/]")
        console.print(f"[{C['muted']}]Type /help for available commands[/]")

    def skill_result(self, name: str, result) -> None:
        if result is not None:
            console.print(f"\n[{C['final']}]{result}[/]")
            return
        console.print(
            f"\n[{C['accent']}]Skill /{name} stopped without final answer. "
            f"You can:[/]\n"
            f"  - Retry the skill with different arguments\n"
            f"  - /clear to reset context\n"
            f"  - /quit to exit"
        )


def _collect_agent_names() -> list[str]:
    """Sorted, deduped list of agent names from the delegate search paths.

    A single listing helper so every surface (web worker, run) walks the
    same paths and applies the same dedup rule (first hit wins by
    ``_AGENT_SEARCH_PATHS`` order).
    """
    from agent_cli.tools.delegate import _AGENT_SEARCH_PATHS

    seen: list[str] = []
    seen_set: set[str] = set()
    for search_dir in _AGENT_SEARCH_PATHS:
        if not search_dir.is_dir():
            continue
        for md_file in sorted(search_dir.glob("*.md")):
            name = md_file.stem
            if name in seen_set:
                continue
            seen_set.add(name)
            seen.append(name)
    return seen


def try_dispatch_agent_or_skill(
    message: str,
    output: DispatchOutput,
    *,
    llm_provider,
    capabilities,
    resolved_model: str,
    provider: str,
    resolved_url: str,
    resolved_key: str,
    max_turns: int,
    verbose: bool,
    max_depth: int,
    delegate_timeout: int,
    ctx,
    session,
    graceful_interrupt: bool = True,
    stop_event=None,
) -> bool:
    """Detect and run ``@<name> <task>`` / ``/<skill> <args>`` invocations.

    ``stop_event`` is threaded into BOTH paths so the web Stop button can
    halt either at a turn boundary, same as a plain conversation turn:
      - ``/skill`` → ``_dispatch_skill`` → ``execute_skill`` → ``run_loop``
      - ``@agent`` → ``_dispatch_agent`` → ``tool_delegate`` → the delegate
        worker's ``run_loop`` (shared Event across parallel workers).

    Returns ``True`` when the message was handled (caller skips its
    LLM path); ``False`` when nothing matched and the caller should
    treat ``message`` as a normal conversation turn.

    Listings (``@``, ``@agents``, ``@<name>`` with no task,
    ``/skills``) and errors (unknown agent, unknown skill) are
    surfaced through ``output`` — same code path as a successful
    invocation, just a different rendering. Unknown commands do NOT
    fall through to the LLM: a typo shouldn't accidentally trigger a
    round-trip with the LLM (the web UI's dispatch contract).
    """
    from agent_cli.context.session import save_meta

    if message.startswith("@"):
        parts = message.split(maxsplit=1)
        name = parts[0][1:]
        # Any ``@<x>`` with no task — including unknown agent names —
        # triggers a listing rather than an error. Typing ``@`` to
        # discover what's available is a documented UX pattern.
        if not name or name == "agents" or len(parts) < 2:
            output.list_agents(_collect_agent_names())
            return True
        result = _dispatch_agent(
            message,
            llm_provider,
            capabilities,
            resolved_model,
            provider,
            resolved_url,
            resolved_key,
            max_turns=max_turns,
            verbose=verbose,
            max_depth=max_depth,
            delegate_timeout=delegate_timeout,
            ctx=ctx,
            session=session,
            graceful_interrupt=graceful_interrupt,
            stop_event=stop_event,
        )
        if result is _AGENT_NOT_FOUND:
            output.agent_not_found(name)
            return True
        if session is not None:
            save_meta(session)  # refresh updated_at (query field removed)
        output.agent_result(result)
        return True

    if message.startswith("/"):
        parts = message.split(maxsplit=1)
        cmd_name = parts[0][1:]
        # ``/skills`` is a synthetic command — it isn't a real skill
        # file, but it serves the listing role symmetric to
        # ``@agents``. Handle it BEFORE ``_dispatch_skill`` because
        # the latter would (correctly) report it as not-found.
        if cmd_name == "skills":
            from agent_cli.skills import load_skills as _load_skills

            output.list_skills(_load_skills())
            return True
        result = _dispatch_skill(
            message,
            llm_provider,
            capabilities,
            resolved_model,
            provider,
            resolved_url,
            resolved_key,
            max_turns=max_turns,
            verbose=verbose,
            max_depth=max_depth,
            delegate_timeout=delegate_timeout,
            ctx=ctx,
            session=session,
            graceful_interrupt=graceful_interrupt,
            stop_event=stop_event,
        )
        if result is _SKILL_NOT_FOUND:
            output.skill_not_found(cmd_name)
            return True
        if session is not None:
            save_meta(session)  # refresh updated_at (query field removed)
        output.skill_result(cmd_name, result)
        return True

    return False


def _dispatch_agent(
    query: str,
    llm_provider,
    capabilities,
    resolved_model: str,
    provider: str,
    resolved_url: str,
    resolved_key: str,
    max_turns: int = 0,
    verbose: bool = False,
    max_depth: int = 2,
    delegate_timeout: int = DELEGATE_DEFAULT_TIMEOUT,
    ctx=None,
    session=None,
    graceful_interrupt: bool = False,
    stop_event=None,
):
    """Dispatch @agent-name query. Returns _AGENT_NOT_FOUND if agent not found."""
    from agent_cli.tools.delegate import tool_delegate

    parts = query.split(maxsplit=1)
    agent_name = parts[0][1:]  # strip leading @
    task = parts[1] if len(parts) > 1 else ""

    if not task:
        return _AGENT_NOT_FOUND  # No task = not a valid agent call

    # Record agent invocation in context
    if ctx:
        ctx.add({"role": "user", "content": f"Delegate to @{agent_name}: {task}"})

    from agent_cli.hooks import load_hooks as _load_hooks
    from agent_cli.render import render_status

    _parent_hooks = _load_hooks() or None
    render_status("running", f"Running agent: {agent_name}...")
    result = tool_delegate(
        args={"tasks": [{"task": task, "agent": agent_name, "context": "fork"}]},
        parent_ctx=ctx,
        provider=llm_provider,
        model=resolved_model,
        capabilities=capabilities,
        provider_name=provider,
        base_url=resolved_url,
        api_key=resolved_key,
        depth=0,
        max_depth=max_depth,
        max_turns=max_turns,
        timeout=delegate_timeout,
        session=session,
        hooks_config=_parent_hooks,
        stop_event=stop_event,
    )

    if not result.success and "not found" in (result.error or ""):
        return _AGENT_NOT_FOUND

    answer = result.output if result.success else result.error

    # Record result in context with artifact path
    if ctx and answer:
        ctx.add(
            {
                "role": "user",
                "tool": "delegate",
                "args": {"agent": agent_name, "task": task[:60]},
                "content": answer,
                "artifact": result.artifact,
            }
        )

    return answer


def _dispatch_skill(
    query: str,
    llm_provider,
    capabilities,
    resolved_model: str,
    provider: str,
    resolved_url: str,
    resolved_key: str,
    max_turns: int = 0,
    verbose: bool = False,
    max_depth: int = 2,
    delegate_timeout: int = DELEGATE_DEFAULT_TIMEOUT,
    ctx=None,
    session=None,
    graceful_interrupt: bool = False,
    stop_event=None,
):
    """Dispatch a /skill-name command. Returns _SKILL_NOT_FOUND if not a skill."""
    from agent_cli.skills import load_skills, execute_skill

    skills = load_skills()
    parts = query.split(maxsplit=1)
    cmd_name = parts[0][1:]  # strip leading /

    if cmd_name not in skills:
        return _SKILL_NOT_FOUND

    skill = skills[cmd_name]
    arguments = parts[1] if len(parts) > 1 else ""

    # Record skill invocation in context
    if ctx:
        ctx.add(
            {
                "role": "user",
                "content": f"Used skill: {cmd_name}({arguments}) — results follow",
            }
        )

    from agent_cli.render import (
        render_group_start,
        render_group_end,
        render_push_depth,
        render_pop_depth,
    )
    import time as _time

    render_group_start(f"skill:{cmd_name}", icon="🪄")
    render_push_depth()
    _t0 = _time.monotonic()
    skill_result = None
    # Pull disk-loaded shell hooks (~/.agent-cli/hooks.json /
    # .agent-cli/hooks.json) so the skill sees them merged with its own
    # frontmatter hooks. load_hooks() caches, so repeat calls are cheap.
    from agent_cli.hooks import load_hooks as _load_hooks

    _parent_hooks = _load_hooks() or None
    try:
        skill_result = execute_skill(
            skill=skill,
            arguments=arguments,
            provider=llm_provider,
            capabilities=capabilities,
            model=resolved_model,
            provider_name=provider,
            base_url=resolved_url,
            api_key=resolved_key,
            max_turns=max_turns,
            verbose=verbose,
            max_depth=max_depth,
            delegate_timeout=delegate_timeout,
            ctx=ctx,
            session=session,
            graceful_interrupt=graceful_interrupt,
            stop_event=stop_event,
            parent_hooks_config=_parent_hooks,
        )
    finally:
        render_pop_depth()
        render_group_end(
            f"skill:{cmd_name}",
            success=bool(skill_result and skill_result.success),
            duration_s=_time.monotonic() - _t0,
        )

    answer = skill_result.output if skill_result.success else skill_result.error

    # Record skill result in context with artifact path
    if ctx and answer:
        ctx.add(
            {
                "role": "user",
                "tool": "run_skill",
                "args": {"name": cmd_name, "arguments": arguments},
                "content": answer,
                "artifact": skill_result.artifact,
            }
        )

    return answer


def _prompt_model_capabilities(model: str):
    """Interactively ask user for model capabilities when detection fails."""
    from agent_cli.config import save_model_entry
    from agent_cli.providers.capabilities import ModelCapabilities

    console.print(
        f"\n[{C['accent']}]Model '{model}' not found in registry and detection failed.[/]"
    )
    console.print(
        f"[{C['muted']}]Please provide model info (saved for future use):[/]\n"
    )

    renderer = get_renderer()
    try:
        ctx_input = renderer.prompt_user(
            "  Context window size [4096]: ", multiline=False
        )
        context_window = int(ctx_input) if ctx_input else 4096

        thinking_input = renderer.prompt_user(
            "  Supports thinking? (y/n) [n]: ", multiline=False
        ).lower()
        supports_thinking = thinking_input in ("y", "yes")

        thinking_budget = 0
        thinking_format = ""
        if supports_thinking:
            budget_input = renderer.prompt_user(
                "  Thinking budget tokens [4096]: ", multiline=False
            )
            thinking_budget = int(budget_input) if budget_input else 4096
            thinking_format = "think"

        max_output = min(context_window // 4, 4096)

        caps = ModelCapabilities(
            context_window=context_window,
            max_output_tokens=max_output,
            supports_structured_output=False,
            supports_thinking=supports_thinking,
            thinking_budget=thinking_budget,
            supports_strict_schema=False,
            thinking_format=thinking_format,
        )

        entry = {
            "context_window": caps.context_window,
            "max_output_tokens": caps.max_output_tokens,
            "supports_structured_output": caps.supports_structured_output,
            "supports_thinking": caps.supports_thinking,
            "thinking_budget": caps.thinking_budget,
            "supports_strict_schema": caps.supports_strict_schema,
            "thinking_format": caps.thinking_format,
        }
        save_model_entry(model, entry)
        console.print(f"[{C['muted']}]Saved to ~/.agent-cli/models.json[/]\n")
        return caps
    except (EOFError, KeyboardInterrupt, ValueError):
        return None


def _setup_provider(
    provider: str,
    model: str | None,
    base_url: str | None,
    api_key: str | None,
    quiet: bool = False,
):
    """Resolve settings + create provider + get capabilities. Returns tuple."""
    _maybe_setup()
    from agent_cli.providers.capabilities import (
        DEFAULT_CAPABILITIES,
        set_progress_callback,
        was_runtime_detected,
    )
    from agent_cli.render import (
        render_model_detected,
        render_model_loaded,
        render_status,
    )

    provider, resolved_url, resolved_model, resolved_key = _resolve_provider(
        provider,
        model,
        base_url,
        api_key,
    )
    llm_provider = create_provider(provider, resolved_url, resolved_key)
    # Surface runtime-detection probe steps to the user. Only emits
    # when the model isn't cached in models.json — silent fast path
    # otherwise. Cleared in finally so a later detection doesn't
    # inherit this callback.
    if not quiet:
        set_progress_callback(lambda msg: render_status("running", msg))
    try:
        capabilities = get_capabilities(
            resolved_model, provider, resolved_url, resolved_key
        )
    except UnsupportedModelError as e:
        # Context window below the agent's minimum — fail fast with a
        # clear message rather than degrading to a 4K default.
        console.print(f"[{C['error']}]Unsupported model: {e}[/]")
        raise typer.Exit(1) from e
    finally:
        set_progress_callback(None)

    # Interactive fallback: ask user when detection fails
    if capabilities == DEFAULT_CAPABILITIES and not quiet:
        user_caps = _prompt_model_capabilities(resolved_model)
        if user_caps:
            capabilities = user_caps

    if not quiet:
        if was_runtime_detected():
            from agent_cli.config import _GLOBAL_MODELS_PATH

            render_model_detected(
                resolved_model, capabilities, provider, str(_GLOBAL_MODELS_PATH)
            )
        else:
            render_model_loaded(resolved_model, capabilities)

    return (
        llm_provider,
        capabilities,
        resolved_model,
        resolved_url,
        resolved_key,
        provider,
    )


@app.command()
def run(
    query: str = typer.Argument(..., help="Task to execute"),
    provider: Optional[str] = typer.Option(
        None,
        "--provider",
        "-p",
        help="LLM provider: openai | anthropic (default: openai)",
    ),
    model: Optional[str] = typer.Option(
        None,
        "--model",
        "-m",
        help="Model ID (uses provider default if not specified)",
    ),
    base_url: Optional[str] = typer.Option(
        None,
        "--base-url",
        help="API base URL (uses provider default if not specified)",
    ),
    api_key: Optional[str] = typer.Option(
        None,
        "--api-key",
        help="API key (auto-detects from environment if not specified)",
    ),
    max_turns: int = typer.Option(
        0,
        "--max-turns",
        "-n",
        help="Maximum iterations (0 = unlimited)",
    ),
    max_context_tokens: int = typer.Option(
        0,
        "--max-context-tokens",
        help="Max tokens in context window (0 = auto from model)",
    ),
    max_depth: int = typer.Option(
        2,
        "--max-depth",
        help="Maximum subagent nesting depth",
    ),
    delegate_timeout: int = typer.Option(
        300,
        "--delegate-timeout",
        help="Timeout in seconds for subagent delegation",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Show raw LLM response",
    ),
    style: Optional[str] = typer.Option(
        None,
        "--style",
        help="Renderer style: minimal (default) or custom renderer name",
    ),
    record_turns: bool = typer.Option(
        True,
        "--record-turns/--no-record-turns",
        help="Append per-turn observability data to {session_dir}/turns.jsonl (recovery analysis; structural metadata only, no prompts/responses)",
    ),
    no_compaction: bool = typer.Option(
        False,
        "--no-compaction",
        help="Disable context compaction (LLM summarisation at 90% budget). Falls back to plain FIFO drop. Useful for measurement baseline / debugging. ``AGENT_CLI_COMPACTION=off`` env var has the same effect.",
    ),
    response_format: str = typer.Option(
        DEFAULT_WIRE_FORMAT,
        "--response-format",
        help="Wire format plugin name (default: md_array — markdown ## Thought/## Action with a flat op array; supports multi-op turns). Other built-in: react. Plugins live in agent_cli/wire_formats/; the registered names list is the set of valid values.",
    ),
):
    """Execute a task in single-shot mode. The agent uses tools (read_file, shell, etc.) to complete the task and returns the result."""
    _apply_style(style)
    # /sh prefix: Run shell command directly without LLM
    if query.startswith("/sh ") or query == "/sh":
        cmd = query[3:].strip()
        if not cmd:
            console.print(f"[{C['error']}]No command to execute.[/]")
            raise typer.Exit(1)
        _run_shell_inline(cmd)
        raise typer.Exit(0)

    llm_provider, capabilities, resolved_model, resolved_url, resolved_key, provider = (
        _setup_provider(provider, model, base_url, api_key)
    )

    # MCP servers
    mcp_manager, mcp_tools = _setup_mcp()
    if mcp_tools:
        from agent_cli.tools import TOOLS

        TOOLS.update(mcp_tools)

    # Session & context setup
    # Auto-compute token budget from model capabilities if not specified
    if max_context_tokens <= 0:
        from agent_cli.context.manager import compute_token_budget

        max_context_tokens = compute_token_budget(
            capabilities.context_window, capabilities.max_output_tokens
        )

    # Resolve the wire-format plugin name from --response-format up front
    # so any unknown name fails before the session is even created, and so
    # the ContextManager below is born with the right plugin attached.
    try:
        wire_format_plugin = _get_wire_format(response_format)
    except KeyError as exc:
        console.print(f"[{C['error']}]{exc}[/]")
        raise typer.Exit(2) from exc

    from agent_cli.context.session import create_session, save_meta

    session = create_session(response_format=response_format)
    save_meta(session)
    ctx = ContextManager(
        session_dir=Path(".agent-cli") / "sessions" / session.session_id,
        max_context_tokens=max_context_tokens,
        wire_format=wire_format_plugin,
    )

    # Skill dispatch: /skill-name args
    if query.startswith("/") and not query.startswith("/sh"):
        answer = _dispatch_skill(
            query,
            llm_provider,
            capabilities,
            resolved_model,
            provider,
            resolved_url,
            resolved_key,
            max_turns=max_turns,
            verbose=verbose,
            max_depth=max_depth,
            delegate_timeout=delegate_timeout,
            ctx=ctx,
            session=session,
        )
        if answer is not _SKILL_NOT_FOUND:
            if answer is not None:
                console.print(f"\n[{C['final']}]{answer}[/]")
            _finalize_run(session, ctx)
            return

    # Agent dispatch: @agent-name task
    if query.startswith("@"):
        answer = _dispatch_agent(
            query,
            llm_provider,
            capabilities,
            resolved_model,
            provider,
            resolved_url,
            resolved_key,
            max_turns=max_turns,
            verbose=verbose,
            max_depth=max_depth,
            delegate_timeout=delegate_timeout,
            ctx=ctx,
            session=session,
        )
        if answer is not _AGENT_NOT_FOUND:
            if answer is not None:
                console.print(f"\n[{C['final']}]{answer}[/]")
            _finalize_run(session, ctx)
            return

    from agent_cli.hooks import load_hooks as _load_hooks

    _disk_hooks = _load_hooks() or None
    try:
        loop_result = run_loop(
            query=query,
            provider=llm_provider,
            capabilities=capabilities,
            model=resolved_model,
            provider_name=provider,
            base_url=resolved_url,
            api_key=resolved_key,
            max_turns=max_turns,
            verbose=verbose,
            max_depth=max_depth,
            delegate_timeout=delegate_timeout,
            ctx=ctx,
            session=session,
            mcp_manager=mcp_manager,
            hooks_config=_disk_hooks,
            record_turns=record_turns,
            wire_format=wire_format_plugin,
            compaction_enabled=not no_compaction,
        )
        answer = loop_result.output if loop_result.success else None
    except KeyboardInterrupt:
        answer = None
        console.print(f"\n[{C['accent']}]⚡ Interrupted.[/]")

    _finalize_run(session, ctx, mcp_manager)


def _finalize_run(session, ctx, mcp_manager=None) -> None:
    """Finalize session after run command (save summary, print session ID)."""
    from agent_cli.render import render_spinner_stop

    render_spinner_stop()
    if mcp_manager:
        mcp_manager.disconnect_all()
    if session is None:
        return
    from agent_cli.context.session import finalize_session

    finalize_session(session, ctx)
    console.print(
        f"[{C['muted']}]Session {session.session_id} saved. "
        f"Resume with: agent-cli web --resume {session.session_id}[/]"
    )


@app.command()
def setup():
    """Configure provider, model, and connection. Runs automatically on first use. Re-run anytime to change settings."""
    from agent_cli.setup import SetupWizard

    SetupWizard().run()


@app.command()
def sessions(
    workspace: Optional[str] = typer.Option(
        None, "--workspace", "-w", help="Filter by workspace path"
    ),
):
    """List previous sessions. Use with 'web --resume <id>' to continue."""
    from agent_cli.context.session import list_sessions as _list_sessions

    ws = workspace or os.getcwd()
    session_list = _list_sessions(ws)
    if not session_list:
        console.print(f"[{C['muted']}]No sessions found for {ws}[/]")
        return

    console.print(f"\n[{C['accent']}]Sessions for {ws}:[/]\n")
    for s in session_list:
        _print_session(s)
    console.print()


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _print_session(s, indent: str = "  ") -> None:
    """Print one session's summary block — id/time + last user request +
    last result (or 'in progress'). Shared by the ``sessions`` command and
    the resume prompt so both show the same format. ``session_summary`` reads
    the last user↔complete pair from history (the old ``query`` field is gone).
    """
    from agent_cli.context.session import session_summary

    console.print(
        f"{indent}[{C['accent']}]{s.session_id}[/] [{C['muted']}]{s.updated_at}[/]"
    )
    user, result = session_summary(s)
    if user:
        console.print(f"{indent}    [{C['muted']}]↳ {_truncate(user, 80)}[/]")
    if result == "(no completion)":
        console.print(f"{indent}    [{C['muted']}]→ (in progress)[/]")
    elif result:
        console.print(f"{indent}    [{C['final']}]→ {_truncate(result, 80)}[/]")


def _maybe_resume_recent(workspace: str, response_format: str, prompt_fn) -> tuple:
    """No ``--resume`` given: offer the most recent session (shown in the same
    format as the ``sessions`` command) and ask [y/N]. 'y' resumes it; anything
    else (incl. Enter) starts a new session.

    ``prompt_fn`` is the y/N reader — ``input`` on a TTY, ``None`` when
    non-interactive (pipes / cron), in which case we never prompt and always
    start new. Returns ``(SessionMeta, is_resume)``.
    """
    from agent_cli.context.session import (
        create_session,
        list_sessions,
        load_session,
    )

    if prompt_fn is not None:
        recent = list_sessions(workspace)
        if recent:
            last = recent[-1]  # list_sessions sorts by id (timestamp) ascending
            console.print(f"\n[{C['muted']}]Most recent session:[/]")
            _print_session(last)
            if prompt_fn("\nResume it? [y/N] ").strip().lower() == "y":
                resumed = load_session(last.session_id)
                if resumed is not None:
                    return resumed, True
    return create_session(response_format=response_format), False


_GH_REPO = "dujeonglee/agent-cli"


def _parse_version(v: str) -> tuple:
    """`v2.3.1` / `2.3.1-dev` → comparable int tuple (pre-release suffix
    dropped). Non-numeric parts coerce to 0 so a malformed tag never crashes
    the comparison."""
    core = v.lstrip("vV").split("-")[0].split("+")[0]
    out = []
    for part in core.split("."):
        try:
            out.append(int(part))
        except ValueError:
            out.append(0)
    return tuple(out)


def _is_editable_install() -> bool:
    """True when running from a source checkout / editable install — pip
    overwriting it would clobber the working tree, so ``update`` refuses
    (unless --force) and points at ``git pull`` instead."""
    try:
        import json as _json
        from importlib.metadata import distribution

        durl = distribution("agent-cli").read_text("direct_url.json")
        if durl:
            return bool(_json.loads(durl).get("dir_info", {}).get("editable"))
    except Exception:
        pass
    import agent_cli

    root = Path(agent_cli.__file__).resolve().parent.parent
    return (root / ".git").exists() and (root / "pyproject.toml").exists()


@app.command()
def update(
    check: bool = typer.Option(
        False, "--check", help="Only check for a newer release; don't install."
    ),
    yes: bool = typer.Option(False, "-y", "--yes", help="Skip the confirmation."),
    force: bool = typer.Option(
        False, "--force", help="Update even a dev / editable install."
    ),
):
    """Check GitHub for a newer release and update (via ``gh`` + ``pip``).

    Uses the GitHub CLI (``gh``) so private-repo auth is handled by your
    ``gh`` login — no token setup. The release's attached wheel is downloaded
    and installed with pip. ``--check`` only reports availability.
    """
    import shutil
    import subprocess
    import sys
    import tempfile

    from agent_cli import __version__ as current

    if shutil.which("gh") is None:
        console.print(
            "[red]gh CLI not found.[/red] Install GitHub CLI: https://cli.github.com"
        )
        raise typer.Exit(1)

    r = subprocess.run(
        [
            "gh",
            "release",
            "view",
            "-R",
            _GH_REPO,
            "--json",
            "tagName",
            "-q",
            ".tagName",
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        console.print(
            f"[red]Could not fetch latest release:[/red] {r.stderr.strip()[:200]}"
        )
        raise typer.Exit(1)
    latest = r.stdout.strip()
    if not latest:
        console.print("No releases found.")
        raise typer.Exit(1)

    console.print(f"current: [cyan]v{current}[/cyan]   latest: [cyan]{latest}[/cyan]")
    if _parse_version(latest) <= _parse_version(current):
        console.print("[green]✓ Already up to date.[/green]")
        raise typer.Exit(0)

    console.print(f"[yellow]Update available: v{current} → {latest}[/yellow]")
    if check:
        console.print("Run [cyan]agent-cli update[/cyan] to install.")
        raise typer.Exit(0)
    if _is_editable_install() and not force:
        console.print(
            "Dev / editable install detected — update with [cyan]git pull[/cyan] "
            "(or [cyan]--force[/cyan] to pip-overwrite)."
        )
        raise typer.Exit(1)
    if not yes and not typer.confirm(f"Install {latest} now?"):
        raise typer.Exit(0)

    with tempfile.TemporaryDirectory() as d:
        dl = subprocess.run(
            [
                "gh",
                "release",
                "download",
                latest,
                "-R",
                _GH_REPO,
                "-p",
                "*.whl",
                "-D",
                d,
            ],
            capture_output=True,
            text=True,
        )
        wheels = list(Path(d).glob("*.whl"))
        if dl.returncode != 0 or not wheels:
            console.print(f"[red]Download failed:[/red] {dl.stderr.strip()[:200]}")
            raise typer.Exit(1)
        pip = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", str(wheels[0])]
        )
        if pip.returncode != 0:
            console.print("[red]pip install failed.[/red]")
            raise typer.Exit(1)
    console.print(f"[green]✓ Updated to {latest}.[/green] Restart agent-cli to use it.")


@app.command()
def web(
    provider: Optional[str] = typer.Option(
        None,
        "--provider",
        "-p",
        help="LLM provider: openai | anthropic (default: openai)",
    ),
    model: Optional[str] = typer.Option(
        None,
        "--model",
        "-m",
        help="Model ID (uses provider default if not specified)",
    ),
    base_url: Optional[str] = typer.Option(
        None,
        "--base-url",
        help="API base URL (uses provider default if not specified)",
    ),
    api_key: Optional[str] = typer.Option(
        None,
        "--api-key",
        help="API key (auto-detects from environment if not specified)",
    ),
    max_turns: int = typer.Option(
        0, "--max-turns", "-n", help="Max iterations per chat turn (0=unlimited)"
    ),
    max_context_tokens: int = typer.Option(
        0, "--max-context-tokens", help="Max tokens in context window (0=auto)"
    ),
    max_depth: int = typer.Option(2, "--max-depth", help="Subagent nesting depth"),
    delegate_timeout: int = typer.Option(
        DELEGATE_DEFAULT_TIMEOUT, "--delegate-timeout", help="Subagent timeout (s)"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    record_turns: bool = typer.Option(True, "--record-turns/--no-record-turns"),
    no_compaction: bool = typer.Option(
        False,
        "--no-compaction",
        help="Disable context compaction (LLM summarisation at 90% budget). Falls back to plain FIFO drop. ``AGENT_CLI_COMPACTION=off`` env var has the same effect.",
    ),
    response_format: str = typer.Option(
        DEFAULT_WIRE_FORMAT,
        "--response-format",
        help="Wire format plugin name (default: md_array).",
    ),
    host: str = typer.Option(
        "0.0.0.0", "--host", help="Bind address (default: 0.0.0.0 — LAN)"
    ),
    port: Optional[int] = typer.Option(
        None,
        "--port",
        help=(
            "Listen port. Omitted: prefer 8080, fall back to an OS-assigned "
            "free port if 8080 is busy. Explicit: bind that port exactly "
            "(uvicorn raises if it's in use)."
        ),
    ),
    token: Optional[str] = typer.Option(
        None,
        "--token",
        help="Auth token. Random 32-byte URL-safe string when omitted.",
    ),
    no_browser: bool = typer.Option(
        False, "--no-browser", help="Do not open the browser automatically"
    ),
    resume: Optional[str] = typer.Option(
        None,
        "--resume",
        help="Resume a previous session by ID (use 'agent-cli sessions' to list).",
    ),
) -> None:
    """Start an LAN web UI for the agent loop.

    Single AgentLoop, multiple equal SSE viewers (all may input/queue). Token
    is generated on startup unless ``--token`` is provided; the URL with
    the token is printed to stdout for the operator to share.

    Phase A scope: server + WebRenderer only. The frontend HTML/JS UI
    arrives in Phase B; for now use ``curl`` to drive the API.
    """
    # Lazy imports — optional ``web`` extra. Surface a friendlier error
    # when the dependency is missing rather than a raw ImportError out
    # of the network stack.
    try:
        import uvicorn

        from agent_cli.render.web import WebRenderer
        from agent_cli.web.server import (
            WebServer,
            create_app,
            pick_port,
            suppress_incomplete_response_log,
        )
    except ImportError as e:
        console.print(
            f"[red]Missing optional dependency for 'web' command: {e}.[/]\n"
            "Install with: [bright_cyan]pip install agent-cli[web][/]"
        )
        raise typer.Exit(code=1)

    from agent_cli.context.session import load_session as _load_session_pre

    # Validate ``--resume`` BEFORE the provider handshake so an unknown
    # session ID fails fast without touching the network. The session
    # load itself is repeated below (after provider setup) so the
    # workspace can flow into the renderer / context manager.
    if resume and _load_session_pre(resume) is None:
        console.print(f"[{C['error']}]Session '{resume}' not found.[/]")
        raise typer.Exit(code=1)

    from agent_cli.render import set_renderer

    # 1. Resolve provider + capabilities (shared provider-setup helper).
    llm_provider, capabilities, resolved_model, resolved_url, resolved_key, provider = (
        _setup_provider(provider, model, base_url, api_key, quiet=True)
    )

    try:
        wire_format_plugin = _get_wire_format(response_format)
    except KeyError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(code=2)

    # 2. Session + ContextManager.
    from agent_cli.context.session import (
        finalize_session,
        get_session_dir,
        load_session,
        save_meta,
    )

    import sys

    if resume:
        # Pre-check above guarantees this exists; re-load to materialise
        # the SessionMeta (workspace etc.) for the renderer/context.
        session = load_session(resume)
        console.print(f"[{C['accent']}]Resuming session {resume}[/]")
        is_resume = True
    else:
        # No --resume: offer the most recent session ([y/N]) or start new.
        prompt_fn = input if sys.stdin.isatty() else None
        session, is_resume = _maybe_resume_recent(
            os.getcwd(), response_format, prompt_fn
        )
        if is_resume:
            console.print(f"[{C['accent']}]Resuming session {session.session_id}[/]")
    save_meta(session)
    if max_context_tokens <= 0:
        max_context_tokens = (capabilities.context_window * 7) // 10
    ctx = ContextManager(
        get_session_dir(session),
        max_context_tokens=max_context_tokens,
        resume=is_resume,
        wire_format=wire_format_plugin,
    )

    # 3. Renderer + server + worker thread.
    renderer = WebRenderer(workspace=session.workspace)
    set_renderer(renderer)
    server = WebServer(renderer, token=token)

    # Prime the session-info ``ready`` so a client opening the page
    # before the first chat turn already sees the top-bar populated.
    # AgentLoop calls ``header()`` again on each user message; the
    # WebRenderer slot-replaces, so the buffer doesn't accumulate.
    renderer.header(provider, resolved_model, max_turns)

    # On resume, fold prior turns back into the persistent event
    # buffer BEFORE any SSE client connects so the snapshot replay
    # carries the conversation history. Replay walks ``ctx``'s raw
    # cache (already populated by ``ContextManager(..., resume=True)``)
    # and re-emits the same event sequence the live loop would have
    # produced — see :meth:`WebRenderer.replay_from_history`.
    if is_resume:
        renderer.replay_from_history(ctx)

    from agent_cli.web.server import WebDispatchOutput, handle_slash_command

    def _worker_loop() -> None:
        """Pop chat messages and drive AgentLoop in a background thread.

        Each ``run_loop`` invocation reuses the same ``ctx``, so history
        accumulates across messages — the web UI is the interactive,
        multi-turn surface. Dispatch order per message:
          1. ``handle_slash_command`` — web-specific stateless cmds
             (``/help``, ``/sh``)
          2. ``try_dispatch_agent_or_skill`` — shared with ``run``,
             covers ``@``/``/`` listings + invocations + not-found
          3. Otherwise the message is a conversation turn → ``run_loop``.
        """
        web_output = WebDispatchOutput(renderer)
        while True:
            # Tell the frontend we're waiting for the next user
            # message. Goes through ``_latest_worker_state`` so a
            # refreshed client also lands on the right send-button
            # state via snapshot replay, not just live listeners.
            renderer.worker_idle()
            item = server.dequeue_blocking()
            if item is server.SHUTDOWN:
                # Server shutdown — break out so the worker thread
                # can exit cleanly instead of being killed daemon-style.
                # No worker_busy flip here: SHUTDOWN isn't a user
                # message, and the connections are being torn down
                # anyway.
                break
            message = item["text"]
            nickname = item["nickname"]
            # Real user message — flip to busy until the next dequeue
            # (after handle_slash_command / try_dispatch_agent_or_skill /
            # run_loop finish). Anything that follows — including a
            # ``prompt_user`` / ``confirm`` wait — keeps the worker in
            # the busy state until the next loop iteration.
            renderer.worker_busy()
            # Echo the dequeued message as a conversation card (input no
            # longer echoes — it sits in the live queue display until popped).
            renderer.push_user_message(f"[{nickname}]: {message}")
            # Fresh stop handle for this turn so the web "Stop" button
            # (POST /api/stop → server.trigger_stop) can signal the loop
            # to exit at the next turn boundary — the same ``stop_event``
            # path Ctrl+C uses in the CLI. Threaded into chat, /skill, and
            # @agent (delegate) runs. Cleared in ``finally`` so a stop
            # press between turns (no active handle) is a no-op.
            stop_event = threading.Event()
            server.set_stop_handle(stop_event)
            try:
                # Single routing path shared by the run-STARTER and every
                # mid-run injected (queued) message: ``/help``·``/sh``·
                # ``/compact`` then ``@agent``·``/skill``. Returns True when the
                # message was a command (handled here). Threaded into the loop
                # as ``route_message`` so an injected ``/sh`` / ``@agent``
                # behaves exactly as one typed at run-start instead of leaking
                # in as literal chat text.
                def route_one(text: str) -> bool:
                    if handle_slash_command(text, renderer, ctx=ctx):
                        return True
                    return try_dispatch_agent_or_skill(
                        text,
                        web_output,
                        llm_provider=llm_provider,
                        capabilities=capabilities,
                        resolved_model=resolved_model,
                        provider=provider,
                        resolved_url=resolved_url,
                        resolved_key=resolved_key,
                        max_turns=max_turns,
                        verbose=verbose,
                        max_depth=max_depth,
                        delegate_timeout=delegate_timeout,
                        ctx=ctx,
                        session=session,
                        graceful_interrupt=True,
                        stop_event=stop_event,
                    )

                if route_one(message):
                    continue
                try:
                    run_loop(
                        query=message,
                        query_author=nickname,
                        dequeue_user_message=server.dequeue_nowait,
                        route_message=route_one,
                        provider=llm_provider,
                        capabilities=capabilities,
                        model=resolved_model,
                        provider_name=provider,
                        base_url=resolved_url,
                        api_key=resolved_key,
                        max_turns=max_turns,
                        verbose=verbose,
                        ctx=ctx,
                        max_depth=max_depth,
                        delegate_timeout=delegate_timeout,
                        session=session,
                        graceful_interrupt=True,
                        stop_event=stop_event,
                        record_turns=record_turns,
                        wire_format=wire_format_plugin,
                        compaction_enabled=not no_compaction,
                    )
                except Exception as exc:  # noqa: BLE001 — worker boundary
                    # Push the error into the renderer so the frontend
                    # sees it rather than dying silently. Worker keeps
                    # spinning to handle the next message.
                    renderer.error(f"Worker error: {exc}", 0)
            finally:
                server.set_stop_handle(None)

    worker = threading.Thread(target=_worker_loop, daemon=True, name="agent-loop")
    worker.start()

    # 4. Print URL + start uvicorn.
    # When ``--port`` is omitted, prefer 8080 but fall back to an
    # OS-assigned port if 8080 is busy. Explicit ``--port N`` skips the
    # probe — let uvicorn surface the bind error so the operator notices.
    resolved_port = port if port is not None else pick_port(host, 8080)
    display_host = "localhost" if host in ("0.0.0.0", "::") else host
    ui_url = f"http://{display_host}:{resolved_port}/?token={server.token}"
    console.print(f"\n[bright_cyan]agent-cli web[/]  ({provider} · {resolved_model})")
    console.print(f"  UI:      [yellow]{ui_url}[/]")
    console.print(f"  Token:   [yellow]{server.token}[/]")
    console.print(f"  Session: [{C['muted']}]{session.session_id}[/]\n")

    if not no_browser:
        # Best-effort browser open. Quiet on failure (headless envs).
        try:
            import webbrowser

            webbrowser.open(ui_url)
        except Exception:  # noqa: BLE001
            pass

    app_obj = create_app(server)
    # ``uvicorn.Server(config).run()`` instead of ``uvicorn.run(...)``
    # so we can wrap it in a try/except that swallows the
    # ``KeyboardInterrupt`` uvicorn re-raises after its own SIGINT
    # handler has done graceful shutdown. The lifespan ``shutdown``
    # hook (server.create_app → @app.on_event("shutdown")) already
    # closed the SSE generators, so the only thing left is to tear
    # down the worker and finalise the session — done in ``finally``.
    config = uvicorn.Config(app_obj, host=host, port=resolved_port, log_level="warning")
    server_obj = uvicorn.Server(config)
    # Silence uvicorn's cosmetic "ASGI callable returned without completing
    # response" line that fires on Ctrl+C while an SSE client is connected
    # (sse-starlette cancels the stream task before the final body chunk).
    # The session still finalises normally; see the filter's docstring.
    suppress_incomplete_response_log()
    try:
        try:
            server_obj.run()
        except KeyboardInterrupt:
            # Second Ctrl+C arrives here (the first was caught inside
            # uvicorn's serve loop). Suppress the traceback — finally
            # finishes the teardown.
            pass
    finally:
        renderer.shutdown_all_connections()
        server.shutdown()
        worker.join(timeout=2.0)
        console.print(f"[{C['muted']}]Saving session...[/]")
        try:
            finalize_session(session, ctx)
            console.print(f"[{C['muted']}]Session {session.session_id} saved.[/]")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Failed to save session: {exc}[/]")
