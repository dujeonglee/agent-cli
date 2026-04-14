"""CLI entry point: run and chat commands."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

import typer

from agent_cli.config import get_provider_defaults
from agent_cli.constants import SHELL_COMMAND_TIMEOUT, DELEGATE_DEFAULT_TIMEOUT
from agent_cli.context.manager import ContextManager
from agent_cli.loop import run_loop
from agent_cli.providers import create_provider, get_capabilities
from agent_cli.render import C, console

app = typer.Typer(
    name="agent-cli",
    help="AI agent CLI with ReAct pattern. Supports Ollama, OpenAI, Anthropic.\n\n"
    "Run 'agent-cli setup' to configure.\n"
    "Run 'agent-cli chat' for interactive mode.\n"
    "Run 'agent-cli run <task>' for single-shot execution.\n"
    "Run 'agent-cli sessions' to list previous sessions.",
    add_completion=False,
)


def _run_shell_inline(cmd: str) -> None:
    """Run a shell command and print output directly. Shared by run and chat."""
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
    provider: str,
    model: str | None,
    base_url: str | None,
    api_key: str | None,
):
    """Resolve provider settings: CLI args > config.json > provider defaults."""
    from agent_cli.config import load_config

    config = load_config()

    # CLI -p flag overrides config provider; "ollama" is the CLI default,
    # so only use it as fallback when config has no provider set.
    config_provider = config.get("provider", "")
    if provider != "ollama":
        # Explicit CLI flag: use it
        effective_provider = provider
    elif config_provider:
        # No explicit flag, but config has a provider
        effective_provider = config_provider
    else:
        effective_provider = provider  # fallback to "ollama"

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


def _setup_mcp(quiet: bool = False):
    """Initialize MCP servers from mcp.json. Returns (manager, mcp_tools) or (None, {})."""
    from agent_cli.mcp.config import load_mcp_config
    from agent_cli.mcp.client import McpClientManager
    from agent_cli.mcp.adapter import register_mcp_tools

    configs = load_mcp_config()
    if not configs:
        return None, {}

    manager = McpClientManager()
    results = manager.connect_all(configs)

    if not quiet:
        for name, status in results.items():
            if status == "connected":
                tool_count = len(manager.list_tools(name))
                console.print(f"  [green]●[/] MCP {name}: {tool_count} tools")
            else:
                console.print(f"  [red]●[/] MCP {name}: {status}")

    mcp_tools = register_mcp_tools(manager)
    return manager, mcp_tools


def _handle_mcp_command(query: str, mcp_manager) -> None:
    """Handle /mcp CLI commands."""
    parts = query.split()
    subcmd = parts[1] if len(parts) > 1 else ""

    if not mcp_manager:
        console.print(f"[{C['muted']}]No MCP servers configured.[/]")
        console.print(
            f"[{C['muted']}]Add servers to .agent-cli/mcp.json or ~/.agent-cli/mcp.json[/]"
        )
        return

    if not subcmd:
        # Status: show all servers
        connected = mcp_manager.connected_servers
        if not connected:
            console.print(f"[{C['muted']}]No MCP servers connected.[/]")
            return
        console.print(f"\n[{C['accent']}]MCP servers:[/]")
        for name in connected:
            tool_count = len(mcp_manager.list_tools(name))
            console.print(f"  [green]●[/] {name} ({tool_count} tools)")
        console.print()
        return

    server = parts[2] if len(parts) > 2 else ""

    if subcmd == "tools":
        if not server:
            # List all tools from all servers
            tools = mcp_manager.list_tools()
        else:
            tools = mcp_manager.list_tools(server)
        if not tools:
            console.print(f"[{C['muted']}]No tools found.[/]")
            return
        console.print(f"\n[{C['accent']}]MCP tools:[/]")
        for t in tools:
            desc = f" — {t.description}" if t.description else ""
            console.print(f"  {t.server}.{t.name}{desc}")
        console.print()
        return

    if subcmd == "resources":
        if not server:
            console.print(f"[{C['error']}]Usage: /mcp resources <server>[/]")
            return
        resources = mcp_manager.list_resources(server)
        if not resources:
            console.print(f"[{C['muted']}]No resources found for '{server}'.[/]")
            return
        console.print(f"\n[{C['accent']}]MCP resources ({server}):[/]")
        for r in resources:
            desc = f" — {r.description}" if r.description else ""
            console.print(f"  {r.uri}{desc}")
        console.print()
        return

    if subcmd == "connect":
        if not server:
            console.print(f"[{C['error']}]Usage: /mcp connect <server>[/]")
            return
        from agent_cli.mcp.config import load_mcp_config

        configs = load_mcp_config()
        if server not in configs:
            console.print(f"[{C['error']}]Server '{server}' not in mcp.json[/]")
            return
        results = mcp_manager.connect_all({server: configs[server]})
        status = results.get(server, "unknown")
        if status == "connected":
            from agent_cli.mcp.adapter import register_mcp_tools
            from agent_cli.tools import TOOLS

            new_tools = register_mcp_tools(mcp_manager)
            TOOLS.update(new_tools)
            tool_count = len(mcp_manager.list_tools(server))
            console.print(f"  [green]●[/] {server}: connected ({tool_count} tools)")
        else:
            console.print(f"  [red]●[/] {server}: {status}")
        return

    if subcmd == "disconnect":
        if not server:
            console.print(f"[{C['error']}]Usage: /mcp disconnect <server>[/]")
            return
        # Remove tools from TOOLS dict
        from agent_cli.tools import TOOLS

        prefix = f"{server}."
        to_remove = [k for k in TOOLS if k.startswith(prefix)]
        for k in to_remove:
            del TOOLS[k]
        mcp_manager.disconnect(server)
        console.print(f"  [{C['muted']}]{server}: disconnected[/]")
        return

    console.print(
        f"[{C['error']}]Unknown /mcp command: {subcmd}[/]\n"
        f"[{C['muted']}]Usage: /mcp [tools|resources|connect|disconnect] [server][/]"
    )


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

    from agent_cli.render import render_status

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
        suppress_output=False,
        session=session,
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

    from agent_cli.render import render_status

    render_status("running", f"Running skill: {cmd_name}...")
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
        suppress_output=True,
        max_depth=max_depth,
        delegate_timeout=delegate_timeout,
        ctx=ctx,
        session=session,
        graceful_interrupt=graceful_interrupt,
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
    from agent_cli.providers.compat import ModelCapabilities

    console.print(
        f"\n[{C['accent']}]Model '{model}' not found in registry and detection failed.[/]"
    )
    console.print(
        f"[{C['muted']}]Please provide model info (saved for future use):[/]\n"
    )

    try:
        ctx_input = input("  Context window size [4096]: ").strip()
        context_window = int(ctx_input) if ctx_input else 4096

        thinking_input = input("  Supports thinking? (y/n) [n]: ").strip().lower()
        supports_thinking = thinking_input in ("y", "yes")

        thinking_budget = 0
        thinking_format = ""
        if supports_thinking:
            budget_input = input("  Thinking budget tokens [4096]: ").strip()
            thinking_budget = int(budget_input) if budget_input else 4096
            thinking_format = "think"

        max_output = min(context_window // 4, 4096)

        caps = ModelCapabilities(
            context_window=context_window,
            max_output_tokens=max_output,
            supports_structured_output=False,
            supports_tool_calling=False,
            supports_thinking=supports_thinking,
            thinking_budget=thinking_budget,
            supports_strict_schema=False,
            thinking_format=thinking_format,
        )

        entry = {
            "context_window": caps.context_window,
            "max_output_tokens": caps.max_output_tokens,
            "supports_structured_output": caps.supports_structured_output,
            "supports_tool_calling": caps.supports_tool_calling,
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
    from agent_cli.providers.compat import DEFAULT_CAPABILITIES, was_runtime_detected
    from agent_cli.render import render_model_detected, render_model_loaded

    provider, resolved_url, resolved_model, resolved_key = _resolve_provider(
        provider,
        model,
        base_url,
        api_key,
    )
    llm_provider = create_provider(provider, resolved_url, resolved_key)
    capabilities = get_capabilities(
        resolved_model, provider, resolved_url, resolved_key
    )

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
    provider: str = typer.Option(
        "ollama",
        "--provider",
        "-p",
        help="LLM provider: anthropic | openai | ollama",
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
    depth: int = typer.Option(
        0,
        "--depth",
        hidden=True,
        help="Current nesting depth (used internally by subagents)",
    ),
    headless: bool = typer.Option(
        False,
        "--headless",
        hidden=True,
        help="No session/rendering; volatile tmpdir context (used by subagents)",
    ),
    style: Optional[str] = typer.Option(
        None,
        "--style",
        help="Renderer style: minimal (default), fancy, or custom renderer name",
    ),
):
    """Execute a task in single-shot mode. The agent uses tools (read_file, shell, etc.) to complete the task and returns the result."""
    _apply_style(style)
    # /sh prefix: Run shell command directly without LLM
    if not headless and (query.startswith("/sh ") or query == "/sh"):
        cmd = query[3:].strip()
        if not cmd:
            console.print(f"[{C['error']}]No command to execute.[/]")
            raise typer.Exit(1)
        _run_shell_inline(cmd)
        raise typer.Exit(0)

    llm_provider, capabilities, resolved_model, resolved_url, resolved_key, provider = (
        _setup_provider(provider, model, base_url, api_key, quiet=headless)
    )

    # MCP servers
    mcp_manager, mcp_tools = _setup_mcp(quiet=headless)
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

    session = None
    ctx = None
    _tmpdir = None  # prevent GC of TemporaryDirectory

    if headless:
        import tempfile
        from pathlib import Path as _Path

        _tmpdir = tempfile.TemporaryDirectory(prefix="agent-cli-")
        ctx = ContextManager(
            session_dir=_Path(_tmpdir.name), max_context_tokens=max_context_tokens
        )
    else:
        from agent_cli.context.session import create_session, save_meta

        session = create_session()
        session.query = query[:100]
        save_meta(session)
        ctx = ContextManager(
            session_dir=Path(".agent-cli") / "sessions" / session.session_id,
            max_context_tokens=max_context_tokens,
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
                if headless:
                    print(answer)
                else:
                    console.print(f"\n[{C['final']}]{answer}[/]")
            _finalize_run(session, ctx, headless)
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
                if headless:
                    print(answer)
                else:
                    console.print(f"\n[{C['final']}]{answer}[/]")
            _finalize_run(session, ctx, headless)
            return

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
            suppress_output=headless,
            depth=depth,
            max_depth=max_depth,
            delegate_timeout=delegate_timeout,
            ctx=ctx,
            session=session,
            mcp_manager=mcp_manager,
        )
        answer = loop_result.output if loop_result.success else None
    except KeyboardInterrupt:
        answer = None
        if not headless:
            console.print(f"\n[{C['accent']}]⚡ Interrupted.[/]")

    if headless and answer:
        print(answer)

    _finalize_run(session, ctx, headless, mcp_manager)


def _finalize_run(session, ctx, headless: bool, mcp_manager=None) -> None:
    """Finalize session after run command (save summary, print session ID)."""
    from agent_cli.render import render_spinner_stop

    render_spinner_stop()
    if mcp_manager:
        mcp_manager.disconnect_all()
    if session is None:
        return
    from agent_cli.context.session import finalize_session

    finalize_session(session, ctx)
    if not headless:
        console.print(
            f"[{C['muted']}]Session {session.session_id} saved. "
            f"Resume with: agent-cli chat --resume {session.session_id}[/]"
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
    """List previous chat sessions. Use with 'chat --resume <id>' to continue."""
    from agent_cli.context.session import list_sessions as _list_sessions

    ws = workspace or os.getcwd()
    session_list = _list_sessions(ws)
    if not session_list:
        console.print(f"[{C['muted']}]No sessions found for {ws}[/]")
        return

    console.print(f"\n[{C['accent']}]Sessions for {ws}:[/]\n")
    for s in session_list:
        query_preview = f"  {s.query}" if s.query else ""
        console.print(
            f"  [{C['accent']}]{s.session_id}[/] [{C['muted']}]{s.updated_at}{query_preview}[/]"
        )
    console.print()


def _read_user_input(prompt: str) -> str:
    """Read user input with paste detection and explicit multiline support.

    - Single line: Enter sends immediately (unchanged behavior)
    - Paste: detects buffered lines in stdin and reads them all
    - Explicit multiline: start with \"\"\" and end with \"\"\"
    """
    import select
    import sys

    first_line = input(prompt).strip()
    if not first_line:
        return ""

    # Explicit multiline: """ ... """
    if first_line == '"""':
        lines: list[str] = []
        while True:
            try:
                line = input("... ")
            except EOFError:
                break
            if line.strip() == '"""':
                break
            lines.append(line)
            # Drain any pasted lines buffered in stdin
            try:
                while select.select([sys.stdin], [], [], 0.1)[0]:
                    buf_line = sys.stdin.readline()
                    if not buf_line:
                        break
                    stripped = buf_line.rstrip("\n")
                    if stripped.strip() == '"""':
                        return "\n".join(lines)
                    lines.append(stripped)
            except (OSError, ValueError):
                pass
        return "\n".join(lines)

    # Paste detection: check if stdin has more data immediately available
    lines = [first_line]
    try:
        while select.select([sys.stdin], [], [], 0.1)[0]:
            line = sys.stdin.readline()
            if not line:  # EOF
                break
            lines.append(line.rstrip("\n"))
    except (OSError, ValueError):
        pass  # select not supported (e.g. Windows) — single line only

    return "\n".join(lines)


@app.command()
def chat(
    provider: str = typer.Option(
        "ollama",
        "--provider",
        "-p",
        help="LLM provider: anthropic | openai | ollama",
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
        help="Maximum iterations per turn (0 = unlimited)",
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
    resume: Optional[str] = typer.Option(
        None,
        "--resume",
        help="Resume a previous session by ID",
    ),
    style: Optional[str] = typer.Option(
        None,
        "--style",
        help="Renderer style: minimal (default), fancy, or custom renderer name",
    ),
):
    """Interactive multi-turn chat with context management, skills, and session persistence. Type /help inside for commands."""
    _apply_style(style)
    from agent_cli.context.session import (
        create_session,
        finalize_session,
        load_session,
        save_meta,
    )

    llm_provider, capabilities, resolved_model, resolved_url, resolved_key, provider = (
        _setup_provider(provider, model, base_url, api_key)
    )

    # MCP servers
    mcp_manager, mcp_tools = _setup_mcp()
    if mcp_tools:
        from agent_cli.tools import TOOLS

        TOOLS.update(mcp_tools)

    # Session setup
    if resume:
        session = load_session(resume)
        if not session:
            console.print(f"[{C['error']}]Session '{resume}' not found.[/]")
            return
        console.print(f"[{C['accent']}]Resuming session {resume}[/]")
    else:
        session = create_session()
    save_meta(session)

    # Auto-compute token budget from model capabilities if not specified
    if max_context_tokens <= 0:
        from agent_cli.context.manager import compute_token_budget

        max_context_tokens = compute_token_budget(
            capabilities.context_window, capabilities.max_output_tokens
        )

    ctx = ContextManager(
        session_dir=Path(".agent-cli") / "sessions" / session.session_id,
        max_context_tokens=max_context_tokens,
        resume=bool(resume),
    )

    console.print()
    console.print(
        f"  ● chat mode  "
        f"[{C['muted']}]{provider} · {resolved_model} · "
        f"ctx={capabilities.context_window:,}  /quit to exit[/]",
        highlight=False,
    )
    console.print()

    from agent_cli.input_history import make_prompt, setup as _setup_input_history

    # Save terminal state before readline/Rich can modify it
    import sys

    _saved_term = None
    try:
        import termios

        _saved_term = termios.tcgetattr(sys.stdin)
    except (ImportError, termios.error):
        pass

    _setup_input_history()
    _prompt = make_prompt("You:")

    while True:
        try:
            query = _read_user_input(_prompt)
        except (EOFError, KeyboardInterrupt):
            console.print(f"\n[{C['muted']}]Session ended.[/]")
            break

        if not query:
            continue

        if query in ("/quit", "/exit"):
            console.print(f"[{C['muted']}]Session ended.[/]")
            break

        if query in ("/help", "/?"):
            console.print(f"\n[{C['accent']}]Chat commands:[/]")
            console.print("  /help               Show this help")
            console.print("  /quit, /exit        End session")
            console.print("  /clear              Reset context")
            console.print("  /sh <cmd>           Run shell command")
            console.print(
                "  /compact [prompt]   Compress context (optional focus prompt)"
            )
            console.print("  /skills             List available skills")
            console.print("  /<skill> <args>     Run a skill")
            console.print("  @agents             List available agents")
            console.print("  @<agent> <task>     Delegate task to an agent")
            console.print("  /ctx_window         Dump context window (debug)")
            console.print("  /mcp                MCP server status")
            console.print("  /mcp tools <srv>    List MCP server tools")
            console.print("  /mcp resources <srv> List MCP server resources")
            console.print()
            continue

        if query == "/clear":
            ctx = ContextManager(
                session_dir=Path(".agent-cli") / "sessions" / session.session_id,
                max_context_tokens=max_context_tokens,
            )
            console.print(f"[{C['accent']}]Context cleared.[/]")
            continue

        if query.startswith("/sh "):
            cmd = query[4:].strip()
            if cmd:
                _run_shell_inline(cmd)
            continue

        if query == "/compact" or query.startswith("/compact "):
            console.print(
                f"[{C['muted']}]Context: {ctx.get_estimated_tokens():,} / {ctx.max_context_tokens:,} tokens. "
                f"No compression needed.[/]"
            )
            continue

        if query == "/ctx_window":
            msgs = ctx.get_messages()
            console.print(
                f"[{C['muted']}]── context window dump ({len(msgs)} messages, "
                f"{ctx.get_estimated_tokens():,} / {ctx.max_context_tokens:,} tokens) ──[/]"
            )
            for i, m in enumerate(msgs):
                role = m["role"]
                content = m.get("content", "")
                console.print(f"[{C['accent']}][{i}] {role}[/]")
                console.print(content, markup=False)
                console.print()
            tokens = ctx.get_estimated_tokens()
            console.print(f"[{C['muted']}]── estimated {tokens} tokens ──[/]")
            continue

        if query == "/mcp" or query.startswith("/mcp "):
            _handle_mcp_command(query, mcp_manager)
            continue

        # Agent dispatch: @agent-name task
        if query.startswith("@"):
            agent_parts = query.split(maxsplit=1)
            agent_name = agent_parts[0][1:]

            if agent_name == "agents" or not agent_name or len(agent_parts) < 2:
                # List available agents (@agents or @ alone)
                from agent_cli.tools.delegate import _AGENT_SEARCH_PATHS

                console.print(f"\n[{C['accent']}]Available agents:[/]")
                seen = set()
                for search_dir in _AGENT_SEARCH_PATHS:
                    if not search_dir.is_dir():
                        continue
                    for md_file in sorted(search_dir.glob("*.md")):
                        name = md_file.stem
                        if name in seen:
                            continue
                        seen.add(name)
                        console.print(f"  @{name}")
                if not seen:
                    console.print(f"[{C['muted']}]No agents found.[/]")
                console.print(f"\n[{C['muted']}]Usage: @agent-name <task>[/]")
                continue

            result = _dispatch_agent(
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
                graceful_interrupt=True,
            )
            if result is _AGENT_NOT_FOUND:
                console.print(f"[{C['error']}]Agent not found: @{agent_name}[/]")
                console.print(f"[{C['muted']}]Type @ to list available agents[/]")
                continue

            session.query = query[:100]
            save_meta(session)
            if result is not None:
                console.print(f"\n[{C['final']}]{result}[/]")
            continue

        # Skill dispatch: /skill-name args
        if query.startswith("/"):
            from agent_cli.skills import load_skills as _load_skills

            parts = query.split(maxsplit=1)
            cmd_name = parts[0][1:]

            if cmd_name == "skills":
                skills = _load_skills()
                user_skills = {k: v for k, v in skills.items() if v.user_invocable}
                if not user_skills:
                    console.print(f"[{C['muted']}]No skills found.[/]")
                else:
                    console.print(f"\n[{C['accent']}]Available skills:[/]")
                    for s in user_skills.values():
                        hint = f" {s.argument_hint}" if s.argument_hint else ""
                        console.print(f"  /{s.name}{hint}  — {s.description}")
                    console.print()
                continue

            result = _dispatch_skill(
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
                graceful_interrupt=True,
            )
            if result is _SKILL_NOT_FOUND:
                console.print(f"[{C['error']}]Unknown command: /{cmd_name}[/]")
                console.print(f"[{C['muted']}]Type /help for available commands[/]")
                continue

            session.query = query[:100]
            save_meta(session)
            # ctx.add already done inside _dispatch_skill
            if result is not None:
                console.print(f"\n[{C['final']}]{result}[/]")
            else:
                console.print(
                    f"\n[{C['accent']}]Skill /{cmd_name} stopped without final answer. "
                    f"You can:[/]\n"
                    f"  - Retry the skill with different arguments\n"
                    f"  - /clear to reset context\n"
                    f"  - /quit to exit"
                )
            continue

        session.query = query[:100]
        save_meta(session)

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
            ctx=ctx,
            max_depth=max_depth,
            delegate_timeout=delegate_timeout,
            session=session,
            graceful_interrupt=True,
            mcp_manager=mcp_manager,
        )
        result = loop_result.output if loop_result.success else None

        if result is None:
            console.print(
                f"\n[{C['accent']}]Loop stopped without final answer. "
                f"You can:[/]\n"
                f"  - Rephrase or continue the query\n"
                f"  - /clear to reset context\n"
                f"  - /quit to exit"
            )

    # Cleanup: ensure terminal state is restored
    from agent_cli.render import render_spinner_stop

    render_spinner_stop()

    if mcp_manager:
        mcp_manager.disconnect_all()
    console.print(f"[{C['muted']}]Saving session...[/]")
    finalize_session(session, ctx)
    console.print(f"[{C['muted']}]Session {session.session_id} saved.[/]")

    # Flush stdin (MCP/readline may leave buffered data)
    try:
        import termios

        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except (ImportError, termios.error):
        pass

    # Restore terminal state (readline/Rich may have modified it)
    if _saved_term is not None:
        try:
            termios.tcsetattr(sys.stdin, termios.TCSANOW, _saved_term)
        except (ImportError, termios.error):
            pass
