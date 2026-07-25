"""CLI entry point: run (single-shot) and web (interactive browser) commands."""

from __future__ import annotations

import dataclasses
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import typer

from agent_cli.config import get_provider_defaults
from agent_cli.constants import AGENT_DEFAULT_TIMEOUT, SHELL_COMMAND_TIMEOUT
from agent_cli.context.manager import ContextManager
from agent_cli.loop import run_loop
from agent_cli.providers import (
    UnsupportedModelError,
    create_provider,
    get_capabilities,
)
from agent_cli.render import C, console, get_renderer
from agent_cli.wire_formats import (
    get as _get_wire_format,
)
from agent_cli.wire_formats import (
    resolve_wire_format as _resolve_wire_format,
)

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


# Bind addresses for which auto-opening a browser on THIS machine makes sense:
# loopback (local-only) and the wildcards (default `agent-cli web` on your own
# machine, reachable at localhost). A specific non-loopback IP means a remote
# bind — the operator browses from elsewhere, so don't auto-open there.
_LOCAL_BIND_HOSTS = frozenset({"0.0.0.0", "::", "127.0.0.1", "localhost", "::1"})


def _is_local_bind(host: str) -> bool:
    """Whether `host` is a loopback/wildcard bind (browser auto-open is useful)."""
    return host.strip().lower() in _LOCAL_BIND_HOSTS


def _run_shell_inline(cmd: str) -> None:
    """Run a shell command and print output directly. Shared by run and web."""
    console.print(f"[{C['action']}]⚡ SHELL:[/] {cmd}")
    try:
        # Bytes + replace (not ``text=True``) so non-UTF-8 output (git show,
        # binary diffs) doesn't raise UnicodeDecodeError mid-run.
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            timeout=SHELL_COMMAND_TIMEOUT,
            check=False,
        )
        if result.stdout:
            console.print(
                result.stdout.decode("utf-8", errors="replace"),
                end="",
                highlight=False,
            )
        if result.stderr:
            console.print(
                f"[{C['error']}]{result.stderr.decode('utf-8', errors='replace')}[/]",
                end="",
            )
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
    from agent_cli.mcp.adapter import register_mcp_tools
    from agent_cli.mcp.client import McpClientManager
    from agent_cli.mcp.config import load_mcp_config

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

    def list_agents(self, agents: list[tuple[str, str]], live_status: str = "") -> None:
        """``@agents`` — 프로파일 카탈로그 + (있으면) live roster 통합 표시.

        ``agents`` 는 ``(name, description)``, ``live_status`` 는
        레지스트리의 ``format_status()`` 텍스트 (레지스트리 없으면 빈 문자열).
        """
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

    def agent_dispatch_result(self, text: str, success: bool) -> None:
        """``@agt-<key>``/``@<profile>-spawn`` 의 결과/에러 (기본: 무시)."""


def _parse_at_profile(name: str) -> tuple[str, str]:
    """``@<profile>[-run|-spawn]`` 토큰 파싱 → (profile, mode).

    규칙 (설계 §3.5): **전체 토큰이 실존 프로파일이면 그것 우선**, 아니면
    ``-run``/``-spawn`` 접미사 분리 — 하이픈 포함 프로파일명
    (code-writer)과 극단 케이스(foo-run 이라는 프로파일)까지 결정적.
    """
    from agent_cli.subagent.profiles import load_profile

    _body, _, err = load_profile(name) if name else (None, {}, "empty")
    if err is None:
        return name, "run"
    for suffix, mode in (("-run", "run"), ("-spawn", "spawn")):
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)], mode
    return name, "run"


def _try_dispatch_agent_command(
    message: str, output: DispatchOutput, agent_registry
) -> bool:
    """상주 에이전트 방향의 ``@`` 명령 — LLM 을 거치지 않는 직접 표면.

    처리 범위 (처리했으면 True — 호출자는 LLM/run 경로를 건너뜀):

    - ``@agents`` — 프로파일 카탈로그 + live roster 통합 목록.
    - ``@agt-<key> [메시지]`` — 상주 인스턴스 직접 전송 / 상태 (D8:
      회신은 main LLM 컨텍스트에 안 섞이고 🤝 창(웹)/콘솔 라인(CLI)).
    - ``@<profile>-spawn [task]`` — 상주 스폰 (+초기 task 전달).

    ``@<profile>[-run] <task>`` (일회성 run)는 여기서 처리하지 않고
    False 로 폴스루 — ``_dispatch_agent`` 가 run 엔진으로 실행한다.
    """
    if not message.startswith("@"):
        return False
    parts = message.split(maxsplit=1)
    name = parts[0][1:]

    if not name or name == "agents":
        live = agent_registry.format_status() if agent_registry is not None else ""
        output.list_agents(_collect_agents(), live)
        return True

    if name.startswith("agt-"):
        if agent_registry is None:
            output.agent_dispatch_result(
                "(에이전트 레지스트리가 없는 모드입니다)", False
            )
            return True
        if len(parts) < 2:
            # 메시지 없음 → 그 인스턴스의 상태
            output.agent_dispatch_result(agent_registry.format_status(name), True)
            return True
        error = agent_registry.request(name, parts[1], author="user")
        if error:
            output.agent_dispatch_result(f"@{name}: {error}", False)
        else:
            output.agent_dispatch_result(
                f"@{name} 에 전송됨 — 회신은 🤝 창(웹) / 콘솔 라인(CLI)으로 "
                f"도착합니다 (main LLM 대화에는 섞이지 않음).",
                True,
            )
        return True

    profile, mode = _parse_at_profile(name)
    if mode == "spawn":
        if agent_registry is None:
            output.agent_dispatch_result(
                "(에이전트 레지스트리가 없는 모드입니다 — spawn 불가)", False
            )
            return True
        key, error = agent_registry.spawn(profile=profile)
        if error:
            output.agent_dispatch_result(f"@{name}: {error}", False)
            return True
        task = parts[1] if len(parts) > 1 else ""
        if task:
            send_error = agent_registry.request(key, task, author="user")
            if send_error:
                output.agent_dispatch_result(f"@{key}: {send_error}", False)
                return True
        output.agent_dispatch_result(
            f"@{key} ({profile}) 상주 시작"
            + (" — 초기 task 전달됨" if task else "")
            + ". 이후 @agt-<key> <메시지> 로 직접 소통.",
            True,
        )
        return True

    return False


class _ConsoleDispatchOutput(DispatchOutput):
    """CLI-flavoured output — colour, Rich markup, plain ``console.print``."""

    def agent_dispatch_result(self, text: str, success: bool) -> None:
        # markup=False — 상태 텍스트의 [code-analyst] 같은 브래킷이 rich
        # 스타일 태그로 삼켜지지 않게 (v4.62.1 교훈과 동일 클래스).
        color = C["final"] if success else C["error"]
        console.print(text, style=color, markup=False)

    def list_agents(self, agents: list[tuple[str, str]], live_status: str = "") -> None:
        console.print(f"\n[{C['accent']}]Agent profiles:[/]")
        if not agents:
            console.print(f"[{C['muted']}]No profiles found.[/]")
        else:
            for name, desc in agents:
                suffix = f"  — {desc}" if desc else ""
                console.print(f"  @{name}{suffix}")
        if live_status:
            console.print(f"\n[{C['accent']}]Live agents:[/]")
            console.print(live_status, markup=False)
        console.print(
            f"\n[{C['muted']}]Usage: @<profile> <task> (일회성 run) · "
            f"@<profile>-spawn <task> (상주) · @agt-<key> <메시지> (직접 전송)[/]"
        )

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


def _collect_agents() -> list[tuple[str, str]]:
    """Sorted ``(name, description)`` for every available agent.

    Uses the same loader as the delegate path (``_agent_loader``) so the
    ``@agents`` listing shows the same agents — now WITH their descriptions,
    matching the ``/`` skill listing. Unlike the model's Agents prompt section,
    this user listing does NOT hide ``disable-model-invocation`` agents (the
    user may still inspect/@-dispatch them).
    """
    from agent_cli.subagent.profiles import _profile_loader

    resources = _profile_loader.load_all()
    return sorted(
        ((name, res.meta.get("description", "")) for name, res in resources.items()),
        key=lambda x: x[0].lower(),
    )


_SLASH_CMD_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


def looks_like_slash_command(message: str) -> bool:
    """``/...`` 메시지가 **명령**인지 — 경로/일반 텍스트 오인 방지 (v4.51.1).

    이전엔 ``startswith("/")`` 만으로 skill dispatch 로 보내서
    ``/Users/me/proj 분석해줘`` 같은 경로-선두 메시지가 SKILL_NOT_FOUND
    로 먹혔다. 판별 규칙(보수적 — 명령이라고 확신할 때만 True):

    1. 첫 토큰에 두 번째 ``/`` 가 있으면 경로 (``/tmp/a.py``) → False
    2. 첫 토큰이 파일시스템에 실존하면 경로 (``/etc``, ``/Users``) → False
    3. 이름이 명령 문법(영숫자·-_)이 아니면 → False
    4. 그 외 → True — unknown 이름은 기존대로 not-found 안내(오타 안전망)

    ``@agent`` 라우팅과 web 의 정확-매치 명령(/help·/sh·/compact)은 무관.
    """
    first = message.split(maxsplit=1)[0]
    body = first[1:]
    if "/" in body:  # 규칙 1
        return False
    if not _SLASH_CMD_NAME.match(body):  # 규칙 3 (빈 문자열 포함)
        return False
    try:
        if Path(first).exists():  # 규칙 2 — /etc, /Users 등 한 단어 경로
            return False
    except OSError:
        pass
    return True


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
    agent_timeout: int,
    ctx,
    session,
    graceful_interrupt: bool = True,
    stop_event=None,
    agent_registry=None,
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
        # 상주 방향 명령이 먼저 — ``@agents`` 통합 목록 / ``@agt-<key>``
        # 직접 조작 / ``@<profile>-spawn`` (run 은 폴스루).
        if _try_dispatch_agent_command(message, output, agent_registry):
            return True
        parts = message.split(maxsplit=1)
        name = parts[0][1:]
        # Any ``@<x>`` with no task — including unknown profile names —
        # triggers a listing rather than an error. Typing ``@`` to
        # discover what's available is a documented UX pattern.
        if len(parts) < 2:
            live = agent_registry.format_status() if agent_registry is not None else ""
            output.list_agents(_collect_agents(), live)
            return True
        result, _ok = _dispatch_agent(
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
            agent_timeout=agent_timeout,
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

    if message.startswith("/") and looks_like_slash_command(message):
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
            agent_timeout=agent_timeout,
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
    agent_timeout: int = AGENT_DEFAULT_TIMEOUT,
    ctx=None,
    session=None,
    graceful_interrupt: bool = False,
    stop_event=None,
):
    """Dispatch @agent-name query → ``(answer, success)``.

    미발견 프로파일은 ``(_AGENT_NOT_FOUND, False)`` — 호출자가 sentinel 로
    분기. success 는 run 엔진의 실제 성공 여부 (``--result-file`` 이 실패
    텍스트를 성공 답변처럼 기록하지 않기 위한 신호, 5.6.0).
    """
    from agent_cli.subagent.oneshot import tool_delegate

    parts = query.split(maxsplit=1)
    # ``-run`` 접미사 허용 (기본 모드의 명시 표기) — 실존 프로파일 우선.
    agent_name, _ = _parse_at_profile(parts[0][1:])
    task = parts[1] if len(parts) > 1 else ""

    if not task:
        return _AGENT_NOT_FOUND, False  # No task = not a valid agent call

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
        timeout=agent_timeout,
        session=session,
        hooks_config=_parent_hooks,
        stop_event=stop_event,
    )

    if not result.success and "not found" in (result.error or ""):
        return _AGENT_NOT_FOUND, False

    answer = result.output if result.success else result.error

    # Record result in context with artifact path
    if ctx and answer:
        ctx.add(
            {
                "role": "user",
                "tool": "agent",
                "args": {"agent": agent_name, "task": task[:60]},
                "content": answer,
                "artifact": result.artifact,
            }
        )

    return answer, result.success


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
    agent_timeout: int = AGENT_DEFAULT_TIMEOUT,
    ctx=None,
    session=None,
    graceful_interrupt: bool = False,
    stop_event=None,
):
    """Dispatch a /skill-name command. Returns _SKILL_NOT_FOUND if not a skill."""
    from agent_cli.skills import execute_skill, load_skills

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

    import time as _time
    import uuid as _uuid

    from agent_cli.render import (
        render_begin_scope,
        render_end_scope,
        render_pop_depth,
        render_push_depth,
    )

    # One scope for the whole skill: begin_scope registers this thread so the
    # skill's inner turns route into ONE card (web), fixing the old bug where a
    # user's ``/orchestrate`` ran but drew no card (it emitted an un-handled
    # group_start instead of a routed scope). execute_skill runs synchronously
    # in this thread, so the routing covers its subloop.
    _scope_id = f"skill-{cmd_name}-{_uuid.uuid4().hex[:8]}"
    render_begin_scope(_scope_id, "skill", f"skill:{cmd_name}")
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
            agent_timeout=agent_timeout,
            ctx=ctx,
            session=session,
            graceful_interrupt=graceful_interrupt,
            stop_event=stop_event,
            parent_hooks_config=_parent_hooks,
        )
    finally:
        render_pop_depth()
        render_end_scope(
            _scope_id,
            "skill",
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
            supports_thinking=supports_thinking,
            thinking_budget=thinking_budget,
            thinking_format=thinking_format,
        )

        # Wire format 바인딩 (바인딩 UX ② — multi-wire-format): 엔트리가
        # 대화형으로 생성되는 바로 이 지점에서 선택. auto/빈 입력 = 필드
        # 미기록(해석 체인 위임 — 기본 json_fc), 등록된 이름만 수용
        # (D2: 조용한 오타 폴백 금지 — 재질문).
        from agent_cli.wire_formats import list_names

        names = list_names()
        wf_input = (
            renderer.prompt_user(
                f"  Wire format ({' / '.join(['auto'] + names)}) [auto]: ",
                multiline=False,
            )
            .strip()
            .lower()
        )
        while wf_input and wf_input != "auto" and wf_input not in names:
            console.print(
                f"[{C['muted']}]  Unknown wire format '{wf_input}'. "
                f"Available: auto, {', '.join(names)}[/]"
            )
            wf_input = (
                renderer.prompt_user("  Wire format [auto]: ", multiline=False)
                .strip()
                .lower()
            )

        from agent_cli.providers.capabilities import caps_to_entry

        entry = caps_to_entry(caps)
        if wf_input and wf_input != "auto":
            entry["wire_format"] = wf_input
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


@dataclass(frozen=True)
class SessionBootstrap:
    """`run`/`web` 이 공유하는 부트스트랩 산출물 (C4).

    provider 6-튜플 + fail-fast 해석된 wire format + 폴백 적용된 토큰
    예산을 한 번에 — 두 커맨드가 각자 unpack/try-except/분기를 복제하던
    동형 시퀀스의 단일 소유자. 세션 획득(create vs resume UX)·renderer·
    worker 배선은 진짜 다른 부분이라 커맨드별에 남는다.
    """

    llm_provider: object
    capabilities: object
    resolved_model: str
    resolved_url: str
    resolved_key: str
    provider_name: str
    wire_format: object
    max_context_tokens: int


def _bootstrap_provider(
    provider,
    model,
    base_url,
    api_key,
    response_format: str | None,
    max_context_tokens: int,
    *,
    session_format: str | None = None,
    quiet: bool = False,
) -> SessionBootstrap:
    """provider 셋업 → wire format 해석 fail-fast → 예산 폴백(70% 통일 공식).

    wire format 해석 체인 (multi-wire-format Phase 1): 명시
    ``--response-format`` > resume 세션 메타(``session_format``) >
    models.json 모델 바인딩 > DEFAULT. unknown 이름은 어느 소스든
    세션 생성 전에 fail-fast (D2).
    """
    llm_provider, capabilities, resolved_model, resolved_url, resolved_key, name = (
        _setup_provider(provider, model, base_url, api_key, quiet=quiet)
    )
    try:
        wire_format_plugin = _resolve_wire_format(
            explicit=response_format,
            session_format=session_format,
            model=resolved_model,
        )
    except KeyError as exc:
        console.print(f"[{C['error']}]{exc.args[0] if exc.args else exc}[/]")
        raise typer.Exit(2) from exc
    if max_context_tokens <= 0:
        from agent_cli.context.manager import compute_token_budget

        max_context_tokens = compute_token_budget(capabilities.context_window)
    return SessionBootstrap(
        llm_provider=llm_provider,
        capabilities=capabilities,
        resolved_model=resolved_model,
        resolved_url=resolved_url,
        resolved_key=resolved_key,
        provider_name=name,
        wire_format=wire_format_plugin,
        max_context_tokens=max_context_tokens,
    )


def _load_resume_session(resume_id: str):
    """``--resume <id>`` 공용 경로: unknown ID 는 provider 핸드셰이크 전에
    fail-fast, 성공 시 SessionMeta 반환 + 배너. run/web 동형."""
    from agent_cli.context.session import load_session

    session = load_session(resume_id)
    if session is None:
        console.print(f"[{C['error']}]Session '{resume_id}' not found.[/]")
        raise typer.Exit(code=1)
    console.print(f"[{C['accent']}]Resuming session {resume_id}[/]")
    return session


def _build_context(session, boot: SessionBootstrap, *, resume: bool = False):
    """세션 + 부트스트랩 산출물로 ContextManager 조립 (run/web 단일 경로)."""
    from agent_cli.context.session import get_session_dir

    return ContextManager(
        get_session_dir(session),
        max_context_tokens=boot.max_context_tokens,
        resume=resume,
        wire_format=boot.wire_format,
    )


@app.command()
def run(
    query: str = typer.Argument(..., help="Task to execute"),
    provider: str | None = typer.Option(
        None,
        "--provider",
        "-p",
        help="LLM provider: openai | anthropic (default: openai)",
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        "-m",
        help="Model ID (uses provider default if not specified)",
    ),
    base_url: str | None = typer.Option(
        None,
        "--base-url",
        help="API base URL (uses provider default if not specified)",
    ),
    api_key: str | None = typer.Option(
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
    agent_timeout: int = typer.Option(
        300,
        "--agent-timeout",
        help="Timeout in seconds for subagent delegation",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Show raw LLM response",
    ),
    style: str | None = typer.Option(
        None,
        "--style",
        help="Renderer style: minimal (default) or custom renderer name",
    ),
    record_turns: bool = typer.Option(
        True,
        "--record-turns/--no-record-turns",
        help="Append per-turn observability data to {session_dir}/turns.jsonl (recovery analysis; structural metadata only, no prompts/responses)",
    ),
    response_format: str | None = typer.Option(
        None,
        "--response-format",
        help="Wire format plugin name. Unset resolves: resumed session's recorded format > models.json per-model 'wire_format' binding > json_fc (plain-prose reasoning + a flat JSON op array; multi-op). Other built-in: xml_fc (tag-parameter <tool_call>/<function=>/<parameter=> — raw values, no JSON escaping). Plugins live in agent_cli/wire_formats/; the registered names list is the set of valid values.",
    ),
    resume: str = typer.Option(
        "",
        "--resume",
        help="이전 세션 ID 를 이어서 실행 — web 과 동일한 on-disk 세션을 로드해 "
        "복원된 컨텍스트 위에 QUERY 를 이어지는 요청으로 주입 (v4.46.0).",
    ),
    result_file: str = typer.Option(
        "",
        "--result-file",
        help="최종 답변(원문)을 이 경로에 기록 — 렌더러 장식 없는 기계 소비용 "
        "(스크립팅·헤드리스 자동화). 실패/무답 시 "
        "파일을 만들지 않고 exit code 로 판별.",
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

    # C4: run/web 공용 부트스트랩 — provider 6-튜플 + wire format fail-fast +
    # 예산 폴백(70% 통일 공식). resume pre-check 는 핸드셰이크보다 먼저 —
    # 그 메타의 response_format 이 해석 체인 순위 2 (명시 플래그 다음).
    session_resumed = _load_resume_session(resume) if resume else None
    boot = _bootstrap_provider(
        provider,
        model,
        base_url,
        api_key,
        response_format,
        max_context_tokens,
        session_format=(session_resumed.response_format if session_resumed else None),
    )
    llm_provider = boot.llm_provider
    capabilities = boot.capabilities
    resolved_model = boot.resolved_model
    resolved_url = boot.resolved_url
    resolved_key = boot.resolved_key
    provider = boot.provider_name
    wire_format_plugin = boot.wire_format
    max_context_tokens = boot.max_context_tokens

    # MCP servers
    mcp_manager, mcp_tools = _setup_mcp()
    if mcp_tools:
        from agent_cli.tools import TOOLS

        TOOLS.update(mcp_tools)

    from agent_cli.context.session import create_session, save_meta

    if session_resumed is not None:
        session = session_resumed
    else:
        session = create_session(response_format=boot.wire_format.name)
    # 활성 포맷을 메타에 기록 — 명시 플래그로 전환한 resume 도 다음
    # resume 가 이어받는다 (meta = 마지막 실행의 truth).
    session.response_format = boot.wire_format.name
    save_meta(session)
    ctx = _build_context(session, boot, resume=session_resumed is not None)

    from agent_cli.hooks import load_hooks as _load_hooks

    _disk_hooks = _load_hooks() or None
    # teammate P1: main 루프에만 레지스트리 주입 (서브에이전트는 도구
    # 자동 strip). run 은 프로세스 단명이라 종료 시 전원 정리 — 미배달
    # 회신의 세션-넘김 보존은 P3(manifest/outbox).
    from agent_cli.subagent.agents_live import AgentRegistry, set_main_registry

    # runtime 프리필: restore/auto-spawn 된 teammate 가 도구 호출(스폰)
    # 없이 첫 접촉(웹 창 인간 개입 등)을 받아도 provider 배선이 있도록.
    agent_registry = AgentRegistry(
        ctx.session_dir if ctx else None,
        runtime={
            "provider": llm_provider,
            "capabilities": capabilities,
            "model": resolved_model,
            "provider_name": provider,
            "base_url": resolved_url,
            "api_key": resolved_key,
            "max_turns": max_turns,
            "depth": 0,
            "max_depth": max_depth,
            "timeout": agent_timeout,
            "session": session,
            "hooks_config": _disk_hooks,
        },
    )
    # main registry 슬롯 등록 (v7.17.0 배선 통일): 아래 skill dispatch 를
    # 포함한 모든 skill 실행이 execute_skill 에서 이 registry 를 자동 상속
    # (spawn 가능). 생성을 dispatch 앞으로 이동 — 부수효과(restore/
    # auto_spawn/waker)는 종전 위치 그대로.
    set_main_registry(agent_registry)

    # Skill dispatch: /skill-name args — web 의 route_one 과 같은 공용 입구
    # (try_dispatch_agent_or_skill → _dispatch_skill) 로 v7.17.0 통일. 종전
    # run 전용 fast-path 는 _dispatch_skill 을 직접 불러 (a) registry 미배선
    # (spawn "main-session only" 거부 사고의 ③경로) (b) not-found 폴스루
    # (오타 /명령이 LLM 쿼리로 샘) 로 web 과 갈렸다. 이제 not-found 도 web
    # 과 같이 소비+표시, /skills 리스팅도 동작. 외곽 가드는 run 특유 보존:
    # /sh 는 run 에 핸들러가 없고, 경로-선두 쿼리는 통과(looks_like).
    if (
        query.startswith("/")
        and not query.startswith("/sh")
        and looks_like_slash_command(query)
        # 단락 평가: 가드 통과 시에만 dispatch 실행(부수효과 포함)
        and try_dispatch_agent_or_skill(
            query,
            _ConsoleDispatchOutput(),
            llm_provider=llm_provider,
            capabilities=capabilities,
            resolved_model=resolved_model,
            provider=provider,
            resolved_url=resolved_url,
            resolved_key=resolved_key,
            max_turns=max_turns,
            verbose=verbose,
            max_depth=max_depth,
            agent_timeout=agent_timeout,
            ctx=ctx,
            session=session,
            graceful_interrupt=False,
            agent_registry=agent_registry,
        )
    ):
        _finalize_run(session, ctx)
        return

    # P5 (D3 완성): run 도 web 과 같은 큐 펌프 — 초기 질의를 InputQueue 에
    # 넣고, teammate 회신의 wake 아이템(MailWaker)도 같은 큐로 들어온다.
    # 정지 = 큐 비고 + teammate 활성 작업 없음(quiescence). teammate 를
    # 안 쓰면 종전과 동일하게 1회 실행 후 즉시 종료.
    from agent_cli.input_queue import InputQueue
    from agent_cli.subagent.agents_live import MailWaker

    input_queue = InputQueue()
    waker = MailWaker(input_queue.enqueue, agent_registry.has_pending_replies)

    def _on_agent_mail(reply: dict) -> None:
        _agent_mail_notice(reply)
        waker.on_mail()

    agent_registry.on_reply = _on_agent_mail
    # P3 (D7): --resume 세션이면 이전 teammate 자동 재생성 + 미배달 회신
    # 복원 (fresh 세션은 agents.json 이 없어 no-op).
    revived = agent_registry.restore(parent_ctx=ctx)
    if revived:
        console.print(f"[{C['muted']}]🤝 상주 에이전트 {revived}명 재생성됨[/]")
    auto = agent_registry.auto_spawn(parent_ctx=ctx)
    if auto:
        console.print(f"[{C['muted']}]🤝 auto-spawn 전문가 {auto}명 상주 시작[/]")

    # 상주 방향 @ 명령: @agents / @agt-<key> [메시지] / @<profile>-spawn.
    # --resume 세션에서 재생성된 상주 에이전트에게 CLI 로 직접 말 걸기.
    if query.startswith("@") and _try_dispatch_agent_command(
        query, _ConsoleDispatchOutput(), agent_registry
    ):
        # 회신까지 대기 — MinimalRenderer.agent_message 가 콘솔로 출력.
        try:
            while agent_registry.has_active_work():
                time.sleep(0.3)
        except KeyboardInterrupt:
            pass
        finally:
            agent_registry.shutdown_all()
        _finalize_run(session, ctx, mcp_manager)
        return

    # Agent dispatch: @agent-name task
    if query.startswith("@"):
        answer, dispatch_ok = _dispatch_agent(
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
            agent_timeout=agent_timeout,
            ctx=ctx,
            session=session,
        )
        if answer is not _AGENT_NOT_FOUND:
            if answer is not None:
                console.print(f"\n[{C['final']}]{answer}[/]")
            # 성공 시에만, 관찰 래퍼(STATUS/RESULT/[activity])를 벗긴 원문만
            # 기록 — 메인 경로(loop_result.output=원문)와 계약 일치.
            if dispatch_ok and answer is not None:
                from agent_cli.subagent.report import extract_result_body

                _write_result_file(result_file, extract_result_body(str(answer)))
            agent_registry.shutdown_all()
            _finalize_run(session, ctx)
            return

    answer = None

    def _run_one(text: str, *, wake: bool) -> None:
        nonlocal answer
        if wake:
            console.print(f"[{C['muted']}]🤝 에이전트 회신 배달 — 이어서 진행[/]")
        loop_result = run_loop(
            query=text,
            provider=llm_provider,
            capabilities=capabilities,
            model=resolved_model,
            provider_name=provider,
            base_url=resolved_url,
            api_key=resolved_key,
            max_turns=max_turns,
            verbose=verbose,
            max_depth=max_depth,
            agent_timeout=agent_timeout,
            ctx=ctx,
            session=session,
            mcp_manager=mcp_manager,
            hooks_config=_disk_hooks,
            record_turns=record_turns,
            wire_format=wire_format_plugin,
            agent_registry=agent_registry,
        )
        if loop_result.success:
            answer = loop_result.output

    input_queue.enqueue(None, query)
    try:
        _run_message_pump(input_queue, waker, agent_registry, _run_one)
    except KeyboardInterrupt:
        answer = None
        console.print(f"\n[{C['accent']}]⚡ Interrupted.[/]")
    finally:
        stuck = agent_registry.waiting_ask_keys()
        if stuck:
            console.print(
                f"[{C['accent']}]⚠ 에이전트 {', '.join(stuck)} 이(가) 답변 대기 "
                f"중인 채 종료 — resume 시 질문은 STALE 처리됩니다[/]"
            )
        agent_registry.shutdown_all()

    _write_result_file(result_file, answer)
    _finalize_run(session, ctx, mcp_manager)


def resume_wire_format(session, current, explicit: str | None):
    """후결정(대화형) resume 의 포맷 재해석 — 해석 체인 순위 2 와 동형.

    명시 플래그가 없고 세션 기록 포맷이 현재와 다르면 기록 이름으로
    재해석한다. 미등록 이름(rename/제거된 포맷)은 Exit(2) — G1: silent
    format switch 금지 (v7.11.4 에서 typer 본문 인라인을 추출해 고정)."""
    if explicit is not None or session.response_format == current.name:
        return current
    try:
        return _get_wire_format(session.response_format)
    except KeyError as exc:
        console.print(f"[{C['error']}]{exc.args[0] if exc.args else exc}[/]")
        raise typer.Exit(2) from exc


def web_instance_is_active(renderer, server, agent_registry) -> bool:
    """idle self-reap 의 활동 술어 (--idle-timeout, v7.10.0 에이전트 가드).

    True 조건: 라이브 뷰어 존재 ∨ main worker busy ∨ 상주 에이전트 활동
    (working / 미처리 inbox — main 유휴여도 백그라운드 작업 소실 방지)
    ∨ 대기 메시지 큐 비어있지 않음. ``agent_registry`` 는 web() 의
    worker 부트스트랩이 늦게 채우는 nonlocal 이라 None 허용."""
    return bool(
        renderer.has_live_connections()
        or renderer.worker_is_busy()
        or (agent_registry is not None and agent_registry.any_activity())
        or server.pending_count() > 0
    )


def _run_message_pump(input_queue, waker, registry, run_one, *, poll_secs=0.5):
    """CLI ``run`` 의 큐 펌프 (teammate P5) — web ``_worker_loop`` 와 같은
    "큐에 뭔가 있으면 재기동" 모델을 공용 InputQueue 위에서 돈다.

    정지 판정: 큐가 비었고 registry 에 활성 작업(busy·큐잉 요청·미배달
    회신)이 없으면 종료. 활성 작업이 남아 있으면 poll 간격으로 재확인 —
    회신이 도착하면 MailWaker 가 wake 아이템을 큐에 넣어 즉시 깨어난다.
    KeyboardInterrupt 는 호출자 정책(중단 처리)이라 그대로 전파.
    """
    from agent_cli.input_queue import InputQueue

    while True:
        if input_queue.pending_count() == 0 and not registry.has_active_work():
            return
        # mark_idle (not bare idle.set): if a reply is already pending (e.g. it
        # landed in the on_run_end()→idle window), arm a wake now so the reply
        # is delivered instead of the pump spinning on the poll timeout.
        waker.mark_idle()
        item = input_queue.dequeue_blocking(timeout=poll_secs)
        waker.idle.clear()
        if item is InputQueue.SHUTDOWN:
            return
        if item is None:
            continue  # timeout — 정지 조건을 다시 판정
        verdict = waker.handle_dequeued(item["text"])
        if verdict == "skip":
            continue  # 이미 배달 완료된 wake — 빈 run 을 열지 않는다
        run_one(item["text"], wake=(verdict == "run"))
        waker.on_run_end()  # run 종료 직후 도착분 레이스 봉합


def _announce_agent_boot(renderer, revived: int, auto: int) -> None:
    """web 부트스트랩의 teammate 재생성/auto-spawn 알림.

    v4.60.1 사고의 재발 방지 지점: 인라인 status() 오호출(시그니처)이
    worker 스레드를 부트에서 죽여 "큐만 쌓이고 소비 안 됨"으로 보였다 —
    실렌더러(Web/Minimal)로 단위 테스트되는 헬퍼로 추출.
    """
    if revived:
        renderer.status("running", f"🤝 상주 에이전트 {revived}명 재생성됨")
    if auto:
        renderer.status("running", f"🤝 auto-spawn 전문가 {auto}명 상주 시작")


def _agent_mail_notice(reply: dict) -> None:
    """teammate mailbox 도착 알림 (D2 배달과 별개의 힌트 라인) — CLI 는
    상태 라인, web 은 transient status 이벤트. 배달 자체는 다음 턴 경계.
    kind:"question"(P2 ask 라우팅)은 teammate 가 답변까지 블록되므로
    구분해 표시한다."""
    from agent_cli.render import get_renderer

    key = reply.get("key", "?")
    kind = reply.get("kind", "reply")
    if kind == "question":
        line = f"❓ 에이전트 {key} 질문 도착 (답변 대기 중)"
    else:
        line = f"📨 에이전트 {key} 회신 도착"
    try:
        # 전용 ``agent_mail`` 표면 — web 은 전용 이벤트로 프론트에 표시,
        # CLI·커스텀 렌더러는 기본 구현이 status 로 위임(종전 동작 보존).
        # 구 status 직접 호출은 web 프론트 리스너가 없어 드롭됐다(v5.18.2).
        get_renderer().agent_mail_hint(key=key, kind=kind, text=line)
    except Exception:
        pass  # 알림은 best-effort


def _write_result_file(path: str, answer) -> None:
    """``--result-file`` — 성공 답변만 원문 기록 (실패/무답=파일 미생성)."""
    if not path or answer is None:
        return
    try:
        Path(path).write_text(str(answer), encoding="utf-8")
    except OSError as e:
        console.print(f"[{C['error']}]--result-file 기록 실패: {e}[/]")


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
        f"Resume with: agent-cli run/web --resume {session.session_id}[/]"
    )


@app.command()
def setup():
    """Configure provider, model, and connection. Runs automatically on first use. Re-run anytime to change settings."""
    from agent_cli.setup import SetupWizard

    SetupWizard().run()


@app.command()
def sessions(
    workspace: str | None = typer.Option(
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

    ``response_format`` 은 **해석 완료된** 플러그인 이름 — 새 세션 생성
    branch 의 메타 기록용. resume branch 의 포맷 재해석(기록 포맷 존중)은
    caller(web) 책임 (부트 후 결정되는 경로라 여기선 알 수 없다).

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
        check=False,
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
            check=False,
        )
        wheels = list(Path(d).glob("*.whl"))
        if dl.returncode != 0 or not wheels:
            console.print(f"[red]Download failed:[/red] {dl.stderr.strip()[:200]}")
            raise typer.Exit(1)
        pip = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", str(wheels[0])],
            check=False,
        )
        if pip.returncode != 0:
            console.print("[red]pip install failed.[/red]")
            raise typer.Exit(1)
    console.print(f"[green]✓ Updated to {latest}.[/green] Restart agent-cli to use it.")


@app.command()
def web(
    provider: str | None = typer.Option(
        None,
        "--provider",
        "-p",
        help="LLM provider: openai | anthropic (default: openai)",
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        "-m",
        help="Model ID (uses provider default if not specified)",
    ),
    base_url: str | None = typer.Option(
        None,
        "--base-url",
        help="API base URL (uses provider default if not specified)",
    ),
    api_key: str | None = typer.Option(
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
    agent_timeout: int = typer.Option(
        AGENT_DEFAULT_TIMEOUT, "--agent-timeout", help="Subagent timeout (s)"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    record_turns: bool = typer.Option(True, "--record-turns/--no-record-turns"),
    response_format: str | None = typer.Option(
        None,
        "--response-format",
        help="Wire format plugin name. Unset resolves: resumed session's "
        "recorded format > models.json per-model 'wire_format' binding > "
        "json_fc.",
    ),
    host: str = typer.Option(
        "0.0.0.0", "--host", help="Bind address (default: 0.0.0.0 — LAN)"
    ),
    port: int | None = typer.Option(
        None,
        "--port",
        help=(
            "Listen port. Omitted: prefer 0xC0DE (49374), fall back to an "
            "OS-assigned free port if it's busy. Explicit: bind that port "
            "exactly (uvicorn raises if it's in use)."
        ),
    ),
    token: str | None = typer.Option(
        None,
        "--token",
        help="Auth token. Random 32-byte URL-safe string when omitted.",
    ),
    no_browser: bool = typer.Option(
        False, "--no-browser", help="Do not open the browser automatically"
    ),
    resume: str | None = typer.Option(
        None,
        "--resume",
        help="Resume a previous session by ID (use 'agent-cli sessions' to list).",
    ),
    idle_timeout: int = typer.Option(
        0,
        "--idle-timeout",
        help="Self-exit after N seconds with no viewers AND no running work "
        "(0 = never; for orchestrators that spawn-on-demand and resume).",
    ),
    trust_local: bool = typer.Option(
        False,
        "--trust-local",
        help="Skip token auth for loopback (127.0.0.1/::1) requests — for a "
        "trusted local gateway/proxy that authenticates users itself. Only "
        "safe when bound to localhost (e.g. --host 127.0.0.1).",
    ),
    base_path: str = typer.Option(
        "",
        "--base-path",
        help="URL path prefix when served behind a reverse proxy that routes "
        "/<prefix>/* to this instance and strips the prefix (e.g. /s/doom). "
        "The UI's relative URLs resolve under it. Default '' = root.",
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

    from agent_cli.render import set_renderer

    # C4: run/web 공용 부트스트랩. --resume pre-check(fail-fast)가 provider
    # 핸드셰이크보다 먼저 — 로드된 SessionMeta 를 그대로 재사용(재로드 제거),
    # 그 메타의 response_format 이 해석 체인 순위 2 (명시 플래그 다음).
    session_resumed = _load_resume_session(resume) if resume else None
    boot = _bootstrap_provider(
        provider,
        model,
        base_url,
        api_key,
        response_format,
        max_context_tokens,
        session_format=(session_resumed.response_format if session_resumed else None),
        quiet=True,
    )
    llm_provider = boot.llm_provider
    capabilities = boot.capabilities
    resolved_model = boot.resolved_model
    resolved_url = boot.resolved_url
    resolved_key = boot.resolved_key
    provider = boot.provider_name
    wire_format_plugin = boot.wire_format
    max_context_tokens = boot.max_context_tokens

    # MCP servers (v4.46.0: run 과 동형 배선 — 이전엔 web 만 미지원이었음)
    mcp_manager, mcp_tools = _setup_mcp()
    if mcp_tools:
        from agent_cli.tools import TOOLS

        TOOLS.update(mcp_tools)

    # 2. Session + ContextManager.
    import sys

    from agent_cli.context.session import (
        finalize_session,
        get_session_dir,
        save_meta,
    )

    if session_resumed is not None:
        session = session_resumed
        is_resume = True
    else:
        # No --resume: offer the most recent session ([y/N]) or start new.
        prompt_fn = input if sys.stdin.isatty() else None
        session, is_resume = _maybe_resume_recent(
            os.getcwd(), wire_format_plugin.name, prompt_fn
        )
        if is_resume:
            console.print(f"[{C['accent']}]Resuming session {session.session_id}[/]")
            # 대화형 resume 는 부트 **후에** 결정된다 — 명시 플래그가 없으면
            # 기록된 포맷으로 재해석 (--resume 경로의 체인 순위 2 와 동형).
            reinterpreted = resume_wire_format(
                session, wire_format_plugin, response_format
            )
            if reinterpreted is not wire_format_plugin:
                wire_format_plugin = reinterpreted
                boot = dataclasses.replace(boot, wire_format=wire_format_plugin)
    # 활성 포맷을 메타에 기록 — 명시 플래그로 전환한 resume 도 다음
    # resume 가 이어받는다 (meta = 마지막 실행의 truth).
    session.response_format = wire_format_plugin.name
    save_meta(session)
    ctx = _build_context(session, boot, resume=is_resume)

    # 3. Renderer + server + worker thread.
    renderer = WebRenderer(
        workspace=session.workspace,
        session_dir=str(get_session_dir(session)),  # writes status.json here
    )
    set_renderer(renderer)
    # Pass the live ctx so the Prompt Inspector can show the dynamic context
    # (conversation + observations), not just the static system prompt.
    server = WebServer(
        renderer,
        token=token,
        ctx=ctx,
        trust_local=trust_local,
        base_path=base_path,
        # ✨ directive 생성의 LLM 배선 — 산문 직접 호출(5.7.0).
        # provider 는 콜마다 무상태라 요청 스레드에서 안전; 세션/렌더러는
        # 비공유 (메인 타임라인 무접촉 유지).
        runtime={
            "provider": llm_provider,
            "model": resolved_model,
            "capabilities": capabilities,
        },
    )

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
        # Restore the team-swimlane activity bars (skill bands / delegate
        # one-shots / teammate work spans) from the scopes.jsonl sidecar —
        # scope events have no counterpart in ctx's message history, so
        # ``replay_from_history`` alone leaves the swimlane empty.
        renderer.replay_scopes()

    # Populate the Prompt Inspector's system prompt BEFORE the first message
    # (the loop only captures on an LLM call). With ctx already restored, the
    # inspector then shows both the system prompt and the dynamic context the
    # moment the drawer opens — no need to send a message first.
    from agent_cli.web.inspector import capture_startup_system_prompt

    capture_startup_system_prompt(
        renderer,
        capabilities=capabilities,
        wire_format=wire_format_plugin,
        session_dir=str(ctx.session_dir),
        max_depth=max_depth,
        mcp_manager=mcp_manager,
    )

    from agent_cli.web.slash import WebDispatchOutput, handle_slash_command

    # teammate P1: worker 가 생성(nonlocal)하고 서버 teardown 이 정리.
    agent_registry = None

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
        # teammate P1: 서버 수명의 레지스트리 — 매 run_loop 에 주입되어
        # teammate 가 run 경계를 넘어 생존한다. 종료는 서버 teardown 의
        # shutdown_all (아래 finally).
        from agent_cli.subagent.agents_live import AgentRegistry

        nonlocal agent_registry
        agent_registry = AgentRegistry(
            ctx.session_dir if ctx else None,
            # runtime 프리필 — restore/auto-spawn 분이 스폰 도구 호출 없이
            # 웹 창 첫 접촉을 받아도 돌 수 있게 (run 경로와 동일 이유).
            runtime={
                "provider": llm_provider,
                "capabilities": capabilities,
                "model": resolved_model,
                "provider_name": provider,
                "base_url": resolved_url,
                "api_key": resolved_key,
                "max_turns": max_turns,
                "depth": 0,
                "max_depth": max_depth,
                "timeout": agent_timeout,
                "session": session,
                "hooks_config": None,
            },
        )
        _registry = agent_registry  # 클로저 고정 (nonlocal 재대입과 분리)
        # main registry 슬롯 등록 (v7.17.0 배선 통일) — 사용자 /skill
        # dispatch 등 registry 를 명시받지 못하는 skill 실행 경로가
        # execute_skill 에서 자동 상속(spawn 가능).
        from agent_cli.subagent.agents_live import set_main_registry

        set_main_registry(_registry)
        # P4: 대화 창의 인간 개입(input/kill) 엔드포인트에 레지스트리 연결.
        server.agent_registry = _registry

        # P4 (D3): idle 자동 재기동 — 조율 로직은 MailWaker (단위 테스트
        # 가능한 순수 조율자, subagent/teammate.py) 가 소유.
        from agent_cli.subagent.agents_live import MailWaker

        _waker = MailWaker(server.enqueue, _registry.has_pending_replies)

        def _on_agent_mail(reply: dict) -> None:
            _agent_mail_notice(reply)
            _waker.on_mail()

        agent_registry.on_reply = _on_agent_mail
        # P3 (D7): --resume 세션의 teammate 자동 재생성 (fresh 는 no-op).
        revived = agent_registry.restore(parent_ctx=ctx)
        auto = agent_registry.auto_spawn(parent_ctx=ctx)
        _announce_agent_boot(renderer, revived, auto)
        while True:
            # Tell the frontend we're waiting for the next user
            # message. Goes through ``_latest_worker_state`` so a
            # refreshed client also lands on the right send-button
            # state via snapshot replay, not just live listeners.
            renderer.worker_idle()
            # mark_idle (not bare idle.set): re-arms if a reply landed in the
            # on_run_end()→idle window — else web parks forever (no timeout).
            _waker.mark_idle()
            item = server.dequeue_blocking()
            _waker.idle.clear()
            if item is server.SHUTDOWN:
                # Server shutdown — break out so the worker thread
                # can exit cleanly instead of being killed daemon-style.
                # No worker_busy flip here: SHUTDOWN isn't a user
                # message, and the connections are being torn down
                # anyway.
                break
            message = item["text"]
            nickname = item["nickname"]
            _wake_verdict = _waker.handle_dequeued(message)
            if _wake_verdict == "skip":
                continue  # 이미 다른 run 이 배달 완료 — 빈 run 을 열지 않는다
            if _wake_verdict == "run":
                nickname = "🤝 agent"
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
                        agent_registry=_registry,
                        llm_provider=llm_provider,
                        capabilities=capabilities,
                        resolved_model=resolved_model,
                        provider=provider,
                        resolved_url=resolved_url,
                        resolved_key=resolved_key,
                        max_turns=max_turns,
                        verbose=verbose,
                        max_depth=max_depth,
                        agent_timeout=agent_timeout,
                        ctx=ctx,
                        session=session,
                        graceful_interrupt=True,
                        stop_event=stop_event,  # noqa: B023 — route_one runs only within this turn iteration
                    )

                if route_one(message):
                    continue
                try:

                    def _run_main(query: str, author: str):
                        return run_loop(
                            query=query,
                            query_author=author,
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
                            agent_timeout=agent_timeout,
                            session=session,
                            graceful_interrupt=True,
                            stop_event=stop_event,  # noqa: B023 — _run_main is called immediately, same iteration
                            record_turns=record_turns,
                            wire_format=wire_format_plugin,
                            mcp_manager=mcp_manager,
                            agent_registry=agent_registry,
                        )

                    _run_main(message, nickname)

                except Exception as exc:
                    # Push the error into the renderer so the frontend
                    # sees it rather than dying silently. Worker keeps
                    # spinning to handle the next message.
                    renderer.error(f"Worker error: {exc}", 0)
            finally:
                server.set_stop_handle(None)
                # 레이스 봉합 (P4 D3): run 마지막 턴 경계 이후 도착분.
                _waker.on_run_end()

    def _worker_loop_guarded() -> None:
        """worker 사망 = 세션 무력화(큐만 쌓이고 소비 불가) — 조용히 죽는
        대신 인스턴스 로그에 traceback 을 남기고 접속 클라이언트에 에러를
        알린다 (v4.60.1 — status 시그니처 오호출로 부트스트랩 사망이
        '큐가 안 빠져요'로만 보였던 사고의 재발 방지)."""
        try:
            _worker_loop()
        except BaseException as exc:
            import traceback

            traceback.print_exc()
            try:
                renderer.error(
                    f"agent worker crashed: {type(exc).__name__}: {exc} — "
                    f"이 세션은 재시작이 필요합니다 (instance.log 참조)",
                    0,
                )
            except Exception:
                pass
            raise

    worker = threading.Thread(
        target=_worker_loop_guarded, daemon=True, name="agent-loop"
    )
    worker.start()

    # 4. Print URL + start uvicorn.
    # When ``--port`` is omitted, prefer 0xC0DE (49374, "CODE") but fall back
    # to an OS-assigned port if it's busy. Explicit ``--port N`` skips the
    # probe — let uvicorn surface the bind error so the operator notices.
    resolved_port = port if port is not None else pick_port(host, 0xC0DE)
    display_host = "localhost" if host in ("0.0.0.0", "::") else host
    ui_url = f"http://{display_host}:{resolved_port}/?token={server.token}"
    console.print(f"\n[bright_cyan]agent-cli web[/]  ({provider} · {resolved_model})")
    console.print(f"  UI:      [yellow]{ui_url}[/]")
    console.print(f"  Token:   [yellow]{server.token}[/]")
    console.print(f"  Session: [{C['muted']}]{session.session_id}[/]\n")

    # Instance file: tell an external orchestrator where this session's web is
    # (host/port/token/pid). Written on start, removed in finally on exit (and
    # self-reaped via --idle-timeout), so the board can spawn-or-attach by
    # reading one file. Lives next to history.jsonl in the session dir.
    from agent_cli.web.instance_file import (
        remove_instance_file,
        remove_status_file,
        write_instance_file,
        write_status_file,
    )

    _session_dir = Path(".agent-cli") / "sessions" / session.session_id
    write_instance_file(
        _session_dir,
        session_id=session.session_id,
        host=host,
        port=resolved_port,
        token=server.token,
    )
    # Seed the live status sidecar so the board can read it immediately (before
    # the first viewer). The renderer rewrites it on each viewer/busy/awaiting
    # change; it's removed on exit alongside web.json. Best-effort: a status
    # write must never break instance startup (matches _publish_status).
    try:
        write_status_file(_session_dir, busy=False, awaiting_input=False, viewers=0)
    except OSError:
        pass

    if no_browser:
        pass
    elif _is_local_bind(host):
        # Best-effort browser open. Quiet on failure (headless envs).
        try:
            import webbrowser

            webbrowser.open(ui_url)
        except Exception:
            pass
    else:
        # Remote bind (specific non-loopback IP): opening a browser on the
        # server is useless — the operator browses from elsewhere. Just print
        # the URL (already shown above) and skip the auto-open.
        console.print(
            f"  [{C['muted']}](원격 bind — 브라우저 자동 실행 생략; 위 URL 로 접속)[/]"
        )

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

    # Idle self-reap (--idle-timeout): an orchestrator that spawns instances
    # on demand wants them to exit on their own once nobody is using them, so
    # it never has to track/kill processes (next click re-spawns with --resume).
    # A daemon poll thread checks the idle predicate and trips uvicorn's
    # should_exit → the finally block below does the normal teardown + save.
    idle_thread: threading.Thread | None = None
    if idle_timeout and idle_timeout > 0:
        from agent_cli.web.idle import IdleMonitor

        monitor = IdleMonitor(
            is_active=lambda: web_instance_is_active(renderer, server, agent_registry),
            timeout_s=idle_timeout,
            on_idle=lambda: setattr(server_obj, "should_exit", True),
        )

        def _idle_loop() -> None:
            while not server_obj.should_exit:
                monitor.tick()
                time.sleep(2.0)

        idle_thread = threading.Thread(target=_idle_loop, daemon=True)
        idle_thread.start()

    try:
        try:
            server_obj.run()
        except KeyboardInterrupt:
            # Second Ctrl+C arrives here (the first was caught inside
            # uvicorn's serve loop). Suppress the traceback — finally
            # finishes the teardown.
            pass
    finally:
        remove_instance_file(_session_dir)  # board sees the instance is gone
        remove_status_file(_session_dir)  # drop the live status sidecar too
        renderer.shutdown_all_connections()
        server.shutdown()
        worker.join(timeout=2.0)
        if agent_registry is not None:
            agent_registry.shutdown_all()  # 상주 teammate 전원 종료
        if mcp_manager:
            mcp_manager.disconnect_all()
        console.print(f"[{C['muted']}]Saving session...[/]")
        try:
            finalize_session(session, ctx)
            console.print(f"[{C['muted']}]Session {session.session_id} saved.[/]")
        except Exception as exc:
            console.print(f"[red]Failed to save session: {exc}[/]")
