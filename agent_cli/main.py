"""CLI entry point: run and chat commands."""

from __future__ import annotations

import os
import subprocess
from typing import Optional

import typer
from rich import box
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

from agent_cli.config import get_provider_defaults
from agent_cli.context.manager import ContextManager
from agent_cli.loop import run_loop
from agent_cli.providers import create_provider, get_capabilities
from agent_cli.render import C, console

app = typer.Typer(
    name="agent-cli",
    help="ReAct pattern Agentic Loop CLI",
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
            timeout=30,
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
    """Resolve provider settings from CLI args, config, and env."""
    defaults = get_provider_defaults(provider)
    resolved_url = base_url or defaults.base_url
    resolved_model = model or defaults.default_model

    if api_key is None:
        env_map = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
        api_key = os.environ.get(env_map.get(provider, ""), "")

    return resolved_url, resolved_model, api_key


_SKILL_NOT_FOUND = (
    object()
)  # Sentinel to distinguish "not a skill" from "skill returned None"


def _dispatch_skill(
    query: str,
    llm_provider,
    capabilities,
    resolved_model: str,
    provider: str,
    resolved_url: str,
    resolved_key: str,
    max_iter: int = 0,
    verbose: bool = False,
    quiet: bool = False,
    max_depth: int = 2,
    delegate_timeout: int = 300,
    ctx=None,
    session=None,
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
    return execute_skill(
        skill=skill,
        arguments=arguments,
        provider=llm_provider,
        capabilities=capabilities,
        model=resolved_model,
        provider_name=provider,
        base_url=resolved_url,
        api_key=resolved_key,
        max_iter=max_iter,
        verbose=verbose,
        quiet=quiet,
        max_depth=max_depth,
        delegate_timeout=delegate_timeout,
        ctx=ctx,
        session=session,
    )


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
    from agent_cli.providers.compat import DEFAULT_CAPABILITIES, was_runtime_detected
    from agent_cli.render import render_model_detected, render_model_loaded

    resolved_url, resolved_model, resolved_key = _resolve_provider(
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

    return llm_provider, capabilities, resolved_model, resolved_url, resolved_key


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
    max_iter: int = typer.Option(
        0,
        "--max-iter",
        "-n",
        help="Maximum iterations (0 = unlimited)",
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
    quiet: bool = typer.Option(
        False,
        "--quiet",
        hidden=True,
        help="Output only the final answer (used internally by subagents)",
    ),
    depth: int = typer.Option(
        0,
        "--depth",
        hidden=True,
        help="Current nesting depth (used internally by subagents)",
    ),
):
    """ReAct pattern Agentic Loop (single-shot)."""
    # /sh prefix: Run shell command directly without LLM
    if not quiet and (query.startswith("/sh ") or query == "/sh"):
        cmd = query[3:].strip()
        if not cmd:
            console.print(f"[{C['error']}]No command to execute.[/]")
            raise typer.Exit(1)
        _run_shell_inline(cmd)
        raise typer.Exit(0)

    llm_provider, capabilities, resolved_model, resolved_url, resolved_key = (
        _setup_provider(provider, model, base_url, api_key, quiet=quiet)
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
            max_iter=max_iter,
            verbose=verbose,
            quiet=quiet,
            max_depth=max_depth,
            delegate_timeout=delegate_timeout,
        )
        if answer is not _SKILL_NOT_FOUND:
            if answer is not None:
                if quiet:
                    print(answer)
                else:
                    console.print(f"\n[{C['final']}]{answer}[/]")
            return

    answer = run_loop(
        query=query,
        provider=llm_provider,
        capabilities=capabilities,
        model=resolved_model,
        provider_name=provider,
        base_url=resolved_url,
        api_key=resolved_key,
        max_iter=max_iter,
        verbose=verbose,
        quiet=quiet,
        depth=depth,
        max_depth=max_depth,
        delegate_timeout=delegate_timeout,
    )

    if quiet and answer:
        print(answer)


@app.command()
def plan(
    goal: str = typer.Argument(..., help="Planning task description"),
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
    max_iter: int = typer.Option(
        0,
        "--max-iter",
        "-n",
        help="Maximum iterations per step (0 = unlimited)",
    ),
    max_steps: int = typer.Option(
        20,
        "--max-steps",
        help="Maximum number of steps in plan",
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
    auto_approve: bool = typer.Option(
        False,
        "--auto-approve",
        help="Skip review, execute immediately",
    ),
    plan_only: bool = typer.Option(
        False,
        "--plan-only",
        help="Generate plan and display, don't execute",
    ),
    plan_model: Optional[str] = typer.Option(
        None,
        "--plan-model",
        help="Model for plan generation (defaults to --model)",
    ),
    step_max_iter: int = typer.Option(
        10,
        "--step-max-iter",
        help="Maximum iterations per step (default: 10)",
    ),
    save_plan: Optional[str] = typer.Option(
        None,
        "--save-plan",
        help="Save plan to file (auto-generates path if empty)",
    ),
    resume: Optional[str] = typer.Option(
        None,
        "--resume",
        help="Resume execution from a saved plan file",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Show raw LLM response",
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        hidden=True,
    ),
):
    """Plan-driven execution with review and step-by-step control."""
    import time
    from agent_cli.planning.executor import execute_plan
    from agent_cli.planning.generator import generate_plan
    from agent_cli.planning.models import Plan as PlanModel
    from agent_cli.planning.reviewer import review_plan

    llm_provider, capabilities, resolved_model, resolved_url, resolved_key = (
        _setup_provider(provider, model, base_url, api_key, quiet=quiet)
    )

    # Resolve plan-model (defaults to execution model)
    if plan_model:
        plan_model_resolved = plan_model
        plan_capabilities = get_capabilities(
            plan_model_resolved, provider, resolved_url
        )
    else:
        plan_model_resolved = resolved_model
        plan_capabilities = capabilities

    # Auto-generate save path
    if save_plan == "":
        save_plan = f".agent-cli/plans/plan-{int(time.time())}.json"

    # Resume from saved plan
    if resume:
        plan_obj = PlanModel.load(resume)
        if not quiet:
            console.print(f"[{C['accent']}]Resumed plan from {resume}[/]")
    else:
        # Phase 1: Generate plan (uses plan-model if specified)
        while True:
            plan_obj = generate_plan(
                goal=goal,
                provider=llm_provider,
                capabilities=plan_capabilities,
                model=plan_model_resolved,
                max_steps=max_steps,
                quiet=quiet,
            )
            if plan_obj is None:
                if not quiet:
                    console.print(f"[{C['error']}]Failed to generate plan.[/]")
                raise typer.Exit(1)

            if plan_only:
                from agent_cli.render import render_plan as rp

                rp(plan_obj)
                if save_plan:
                    plan_obj.save(save_plan)
                    console.print(f"[{C['muted']}]Plan saved to {save_plan}[/]")
                return

            # Phase 2: Review
            decision = review_plan(plan_obj, auto_approve=auto_approve)
            if decision == "approve":
                break
            elif decision == "regenerate":
                continue
            else:  # cancel
                if not quiet:
                    console.print(f"[{C['muted']}]Plan cancelled.[/]")
                return

    # Phase 3: Execute (uses execution model, not plan model)
    result = execute_plan(
        plan=plan_obj,
        provider=llm_provider,
        capabilities=capabilities,
        model=resolved_model,
        provider_name=provider,
        base_url=resolved_url,
        api_key=resolved_key,
        max_iter=max_iter,
        step_max_iter=step_max_iter,
        verbose=verbose,
        quiet=quiet,
        max_depth=max_depth,
        delegate_timeout=delegate_timeout,
        save_path=save_plan,
    )

    if result:
        if quiet:
            print(result)
        else:
            console.print(f"\n[{C['final']}]{result}[/]")


@app.command()
def sessions(
    workspace: Optional[str] = typer.Option(
        None, "--workspace", "-w", help="Filter by workspace path"
    ),
):
    """List previous sessions."""
    from agent_cli.context.session import list_sessions as _list_sessions, load_summary

    ws = workspace or os.getcwd()
    session_list = _list_sessions(ws)
    if not session_list:
        console.print(f"[{C['muted']}]No sessions found for {ws}[/]")
        return

    console.print(f"\n[{C['accent']}]Sessions for {ws}:[/]\n")
    for s in session_list:
        summary = load_summary(s)
        preview = ""
        if summary:
            first_line = summary.strip().split("\n")[0]
            preview = f" — {first_line[:80]}"
        console.print(
            f"  [{C['accent']}]{s.session_id}[/] [{C['muted']}]{s.created_at}{preview}[/]"
        )
    console.print()


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
    max_iter: int = typer.Option(
        0,
        "--max-iter",
        "-n",
        help="Maximum iterations per turn (0 = unlimited)",
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
):
    """Interactive chat with persistent context and automatic compression."""
    from agent_cli.context.session import (
        create_session,
        finalize_session,
        load_session,
        save_meta,
    )

    llm_provider, capabilities, resolved_model, resolved_url, resolved_key = (
        _setup_provider(provider, model, base_url, api_key)
    )

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

    ctx = ContextManager(
        provider=llm_provider,
        model=resolved_model,
        capabilities=capabilities,
        session_id=session.session_id,
    )

    console.print()
    console.print(
        Panel(
            Text("Interactive Chat Mode", justify="center", style="bold bright_cyan"),
            subtitle=Text(
                f"provider={provider}  model={resolved_model}  "
                f"ctx_window={capabilities.context_window}  /quit to exit",
                style=C["muted"],
                justify="center",
            ),
            border_style="bright_cyan",
            box=box.DOUBLE_EDGE,
            padding=(0, 2),
        )
    )
    console.print()

    from agent_cli.input_history import make_prompt, setup as _setup_input_history

    _setup_input_history()
    _prompt = make_prompt("You:")

    turn = 0
    while True:
        try:
            query = input(_prompt).strip()
        except (EOFError, KeyboardInterrupt):
            console.print(f"\n[{C['muted']}]Session ended.[/]")
            break

        if not query:
            continue

        if query in ("/quit", "/exit"):
            console.print(f"[{C['muted']}]Session ended.[/]")
            break

        if query == "/clear":
            ctx = ContextManager(
                provider=llm_provider,
                model=resolved_model,
                capabilities=capabilities,
                session_id=session.session_id,
            )
            console.print(f"[{C['accent']}]Context cleared.[/]")
            turn = 0
            continue

        if query.startswith("/sh "):
            cmd = query[4:].strip()
            if cmd:
                _run_shell_inline(cmd)
            continue

        if query == "/ctx_window":
            msgs = ctx.get_messages()
            console.print(
                f"[{C['muted']}]── context window dump ({len(msgs)} messages) ──[/]"
            )
            for i, m in enumerate(msgs):
                role = m["role"]
                content = m["content"]
                console.print(f"[{C['accent']}][{i}] {role}[/]")
                console.print(content, markup=False)
                console.print()
            tokens = ctx.get_estimated_tokens()
            console.print(
                f"[{C['muted']}]── estimated {tokens} tokens "
                f"/ {ctx.capabilities.context_window} context window ──[/]"
            )
            continue

        if query.startswith("/plan "):
            from agent_cli.planning.executor import execute_plan
            from agent_cli.planning.generator import generate_plan
            from agent_cli.planning.reviewer import review_plan

            plan_goal = query[6:].strip()
            if plan_goal:
                plan_obj = generate_plan(
                    goal=plan_goal,
                    provider=llm_provider,
                    capabilities=capabilities,
                    model=resolved_model,
                )
                if plan_obj:
                    decision = review_plan(plan_obj)
                    if decision == "approve":
                        plan_result = execute_plan(
                            plan=plan_obj,
                            provider=llm_provider,
                            capabilities=capabilities,
                            model=resolved_model,
                            provider_name=provider,
                            base_url=resolved_url,
                            api_key=resolved_key,
                            max_iter=max_iter,
                            verbose=verbose,
                            max_depth=max_depth,
                            delegate_timeout=delegate_timeout,
                        )
                        if plan_result:
                            ctx.add("user", f"/plan {plan_goal}")
                            ctx.add("assistant", plan_result)
                            console.print(f"\n[{C['final']}]{plan_result}[/]")
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
                max_iter=max_iter,
                verbose=verbose,
                max_depth=max_depth,
                delegate_timeout=delegate_timeout,
                ctx=ctx,
                session=session,
            )
            if result is _SKILL_NOT_FOUND:
                console.print(f"[{C['error']}]Unknown command: /{cmd_name}[/]")
                console.print(
                    f"[{C['muted']}]Try /skills for available skills, or /quit, /clear, /sh, /plan[/]"
                )
                continue

            if result is not None:
                turn += 1
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

        turn += 1
        if turn == 1 and not session.query:
            session.query = query[:100]
        console.print(Rule(f"[{C['muted']}]TURN {turn}[/]", style=C["muted"]))

        result = run_loop(
            query=query,
            provider=llm_provider,
            capabilities=capabilities,
            model=resolved_model,
            provider_name=provider,
            base_url=resolved_url,
            api_key=resolved_key,
            max_iter=max_iter,
            verbose=verbose,
            ctx=ctx,
            max_depth=max_depth,
            delegate_timeout=delegate_timeout,
            session=session,
        )

        if result is None:
            console.print(
                f"\n[{C['accent']}]Loop stopped without final answer. "
                f"You can:[/]\n"
                f"  - Rephrase or continue the query\n"
                f"  - /clear to reset context\n"
                f"  - /quit to exit"
            )

    # Save context window as session summary (instant, no LLM call)
    console.print(f"[{C['muted']}]Saving session...[/]")
    finalize_session(session, ctx)
    console.print(f"[{C['muted']}]Session {session.session_id} saved.[/]")
