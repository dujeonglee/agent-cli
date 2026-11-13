"""CLI entry point: run (single-shot) and web (interactive browser) commands."""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape

from agent_cli.config import get_provider_defaults
from agent_cli.constants import SHELL_COMMAND_TIMEOUT
from agent_cli.context.manager import ContextManager
from agent_cli.dialects import (
    DialectUnbound as _DialectUnbound,
)
from agent_cli.dialects import (
    resolve_dialect as _resolve_dialect,
)
from agent_cli.dialects import (
    set_dialect_override as _set_dialect_override,
)
from agent_cli.knobs import knob_options
from agent_cli.paths import user_dir
from agent_cli.providers import (
    UnsupportedModelError,
    create_provider,
    get_capabilities,
)
from agent_cli.render import C, console, get_renderer

app = typer.Typer(
    name="agent-cli",
    help="AI agent CLI with ReAct pattern. Supports OpenAI-compatible "
    "(OpenAI, vLLM, omlx, LM Studio) and Anthropic.\n\n"
    "Run 'agent-cli setup' to configure.\n"
    "Run 'agent-cli run <task>' for single-shot execution.\n"
    "Run 'agent-cli web' for the interactive browser UI.\n"
    "Run 'agent-cli sessions' to list previous sessions.\n"
    "Run 'agent-cli stats' to summarize turn records (counts only).\n"
    "Run 'agent-cli mcp' to register and diagnose MCP servers.",
    add_completion=False,
    # 잡히지 않은 예외의 traceback 에 지역 변수를 찍지 않는다 (v10.123.2) — `api_key`·
    # `resolved_key` 가 그대로 화면에 나갔다. 가드: tests/test_traceback_locals.py
    pretty_exceptions_show_locals=False,
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


def _run_shell_inline(cmd: str) -> int:
    """Run a shell command and print output directly — the command's exit code.

    ``run "/sh <cmd>"`` 의 종료 코드가 이것이다(v10.124.0; 종전 항상 0). 시간 초과는
    ``timeout(1)`` 의 관례대로 124."""
    console.print(f"[{C['action']}]⚡ SHELL:[/] {escape(cmd)}", soft_wrap=True)
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
                markup=False,
                highlight=False,
            )
        if result.stderr:
            console.print(
                result.stderr.decode("utf-8", errors="replace"),
                end="",
                style=C["error"],
                markup=False,
                highlight=False,
            )
        if result.returncode != 0:
            console.print(f"[{C['muted']}][exit code: {result.returncode}][/]")
        return result.returncode
    except subprocess.TimeoutExpired:
        console.print(f"[{C['error']}]Command timed out (30s)[/]")
        return 124


def _resolve_provider(
    provider: str | None,
    model: str | None,
    base_url: str | None,
    api_key: str | None,
):
    """Resolve provider settings: CLI args > config.json > provider defaults.

    ``provider`` is the ``-p`` flag value, or ``None`` when not passed.
    Precedence: explicit flag → config's provider. A config (file or env)
    that names no provider is an "openai" (OpenAI-compatible: OpenAI, vLLM,
    omlx, LM Studio) config, so its URL·model·key apply to "openai".
    An unknown provider name exits with code 2 and the available names.
    """
    from agent_cli.config import ConfigError, load_config
    from agent_cli.providers import available_providers

    try:
        config = load_config()
    except ConfigError as e:
        # 못 읽은 설정으로 계속 가면 기본값(공개 엔드포인트)이 된다 (v10.123.4)
        console.print(
            f"Could not read config {e}. Fix or delete the file, "
            "or run 'agent-cli setup'.",
            style=C["error"],
            markup=False,
            highlight=False,
            soft_wrap=True,  # 경로를 rich 가 80칸에서 끊지 않게 — 복사해 쓸 수 있도록
        )
        raise typer.Exit(2) from None

    # provider 를 안 적은 설정은 openai 의 것이다 (v10.123.1). 종전엔 빈 문자열이라
    # 아래 "같은 provider 일 때만 설정 값" 검사에 늘 떨어져 사용자가 적은
    # base_url·default_model·api_key 를 버리고 공개 엔드포인트로 갔다.
    config_provider = config.get("provider") or "openai"
    effective_provider = provider or config_provider
    names = available_providers()
    if effective_provider not in names:
        source = "--provider" if provider else "config provider"
        console.print(
            f"Unknown {source} {effective_provider!r}. Available: {', '.join(names)}",
            style=C["error"],
            markup=False,
            highlight=False,
        )
        raise typer.Exit(2)

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


def _flags_complete(base_url: str | None, model: str | None) -> bool:
    """설정 파일 없이 갈 만큼 주어졌나 (v10.125.0) — 판정은 이 한 곳.

    provider 는 늘 해석된다(안 적으면 openai, 모르는 이름은 `_resolve_provider` 가
    Exit(2)). 남는 둘 — base_url 과 모델 — 이 플래그나 환경변수로 정해졌으면 된다.
    ``model`` 은 부트가 넘기는 값이라 이미 ``-m`` > ``AGENT_CLI_MODEL`` 로 해석돼 있다.
    키는 묻지 않는다: 로컬 서버는 키가 없다."""
    url = base_url or os.environ.get("AGENT_CLI_BASE_URL", "")
    return bool(url and model)


def _maybe_setup(base_url: str | None = None, model: str | None = None) -> None:
    """설정이 없으면 마법사 — 단 대화형이고, 플래그로 다 주지 않았을 때만 (v10.125.0).

    종전엔 설정이 없으면 무조건 마법사를 띄워, 플래그를 다 준 비대화식 첫 실행
    (CI·컨테이너·스크립트)도 첫 질문에서 EOF → ``Aborted.`` rc=1 이었다.
    벤치(harbor) 컨테이너는 config.json 이 없어도 ``AGENT_CLI_BASE_URL`` 환경변수가
    `has_config` 를 참으로 만들어 이 길을 이미 지나갔다 — 그 조건은 그대로다."""
    from agent_cli.config import has_config
    from agent_cli.model_check import is_interactive

    if has_config() or _flags_complete(base_url, model):
        return
    if not is_interactive():
        console.print(
            f"No config file ({user_dir() / 'config.json'}). Run 'agent-cli setup' in a "
            "terminal, or pass --base-url and -m.",
            style=C["error"],
            markup=False,
            highlight=False,
            soft_wrap=True,  # 경로를 rich 가 80칸에서 끊지 않게
        )
        raise typer.Exit(2)
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
    # warn=False: 아래에서 서버당 한 줄을 직접 찍으므로 connect_all 의 stderr
    # [warn] 은 같은 내용의 중복이다. 그리고 그쪽은 날것이라, 전송 방식
    # 불일치 같은 원인이 안 보인다 (v9.3.0 — 사용자 제보로 드러난 화면).
    results = manager.connect_all(configs, warn=False)

    from agent_cli.mcp.client import humanize_error

    for name, status in results.items():
        if status == "connected":
            tool_count = len(manager.list_tools(name))
            console.print(f"  [green]●[/] MCP {escape(name)}: {tool_count} tools")
        else:
            console.print(
                f"  [red]●[/] MCP {escape(name)}: "
                f"{escape(humanize_error(status, configs.get(name)))}"
            )

    mcp_tools = register_mcp_tools(manager)
    return manager, mcp_tools


def _apply_style(style: str | None) -> None:
    """Apply renderer style if specified."""
    if style:
        from agent_cli.render import load_renderer_by_name

        try:
            load_renderer_by_name(style)
        except ValueError as e:
            console.print(str(e), style=C["error"], markup=False, highlight=False)
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

    def agent_result(self, result, *, ok: bool = True) -> None:
        """Final answer string from a delegated agent. ``None`` = silent.
        ``ok`` 는 run 엔진의 실제 성공 여부 — 실패 텍스트를 성공 답변처럼
        기록하지 않기 위한 신호(``--result-file``, 5.6.0)."""
        raise NotImplementedError

    def skill_not_found(self, name: str) -> None:
        """Surface an unknown slash command."""
        raise NotImplementedError

    def skill_result(self, name: str, result, *, ok: bool = True) -> None:
        """Final answer (or ``None``) from a ``/<skill>`` invocation.

        ``result is None`` means the skill stopped without producing a
        final answer (e.g. user aborted) — CLI prints a recovery hint
        so the user knows what to try next; other surfaces may ignore.
        ``ok`` 는 스킬 루프의 실제 성공 여부 — 실패하면 ``result`` 는 오류문이지
        답이 아니다(v10.124.0, ``agent_result`` 와 같은 신호).
        """
        raise NotImplementedError

    def agent_dispatch_result(self, text: str, success: bool) -> None:
        """``@agt-<key>``/``@<profile>-spawn`` 의 결과/에러 (기본: 무시)."""


class _ConsoleSurface:
    """``agent-cli run`` 의 펌프 표면 (``pump.PumpSurface``, v10.33.0).

    web 과 다른 것만 여기 있다: 유휴/바쁨 신호는 없고(프런트가 없다), 깨우기는
    따로 찍지 않고(관찰 줄이 말한다), 중단은 Ctrl-C(``graceful_interrupt=False``), 런 예외는 전파,
    런 성공은 ``--result-file``. ``/sh`` 는 run 에 핸들러가 없어 라우팅하지
    않는다(web 은 ``handle_slash_command`` 가 먼저 받는다).

    ``exit_code`` (v10.124.0) — ``run`` 의 종료 코드. 결과 파일을 쓰는 바로 그
    분기에서 정한다(두 판정이 갈라지지 않게): argv 질의의 답 사슬(질의 런 +
    ``_continuing`` 인 동안의 깨우기 런, 또는 질의가 라우팅 명령이면 그 결과)의
    **마지막** 판정이 성공이면 0, 실패면 1. 사슬 밖의 런(예약 발화·사슬이 닫힌
    뒤의 깨우기·런 도중 주입된 명령)은 바꾸지 않는다. 사슬이 아무 판정도 못
    냈으면 1(답이 없다)."""

    graceful_interrupt = False

    def __init__(self, *, result_file: str, dispatch, jobs=None):
        self._result_file = result_file
        self._jobs = jobs
        #: argv 질의 런이 백그라운드 작업을 썼다 (v10.47.0/10.47.1) — 그 작업의
        #: 통지로 깨어난 런들의 답이 질의의 나머지 답이므로 파일에 (덮어)쓴다.
        #: 사슬은 깨우기 런이 작업을 새로 띄우지도, 돌리고 있지도 않을 때 닫힌다.
        self._continuing = False
        self.exit_code = 1
        #: 방금 꺼낸 아이템이 argv 질의인가 — 펌프는 꺼낸 직후 ``echo`` → ``route``
        #: 순으로 부른다. ``route`` 가 한 번 소비한다: 그 뒤의 ``route`` 는 런 도중
        #: 주입(``SessionPump.route``)이라 질의의 답이 아니다.
        self._item_is_query = False
        self._routing_query = False
        self._run_started = 0.0
        self._dispatch = dispatch  # (text, stop_event) -> bool
        self._stop: threading.Event | None = None

    def idle(self) -> None:
        pass

    def busy(self) -> None:
        self._run_started = time.time()  # 이 런이 띄운 작업을 가르는 기준

    def echo(self, item: dict, *, wake: bool) -> None:
        # 그리지 않는다 — 깨우기 런은 그 런의 관찰 줄이 말한다 (v10.51.0).
        # 판정용으로 질의 여부만 기억한다(``RunRequest.is_query`` 와 같은 규칙).
        self._item_is_query = not wake and not item.get("nickname")

    def bind_stop(self, event: threading.Event | None) -> None:
        self._stop = event

    def route(self, text: str) -> bool:
        if text.startswith("/sh"):
            return False
        self._routing_query, self._item_is_query = self._item_is_query, False
        try:
            return self._dispatch(text, self._stop)
        finally:
            self._routing_query = False

    def routed(self, answer, ok: bool) -> None:
        """라우팅 명령의 결과 — ``_ConsoleDispatchOutput`` 이 알린다 (v10.124.0).

        argv 질의였던 명령만 판정·결과 파일이 된다(``run_ended`` 와 같은 규칙).
        ``answer`` 는 파일에 쓸 원문(목록·오류는 None)."""
        if not self._routing_query:
            return
        self.exit_code = 0 if ok else 1
        if ok:
            _write_result_file(self._result_file, answer)

    def run_ended(self, result, req) -> None:
        # 런이 끝난 자리에서 바로 쓴다 — 펌프는 살아 있는 모니터가 있으면 그
        # deadline(기본 2h)까지 돌므로, 펌프 뒤에 쓰면 `complete` 한 답이
        # 파일로는 안 나온다(v10.31.2). **argv 질의 런의 답만** 쓴다(v10.34.1):
        # 깨우기 런(에이전트 회신·모니터 보고/만료 통지)과 예약 런의 답은 그
        # 통지에 대한 반응이지 사용자 질의의 답이 아니다 — 모니터 만료에 깨어난
        # 런의 "No new action required" 가 진짜 답을 덮어쓴 실측(v10.34.0 스모크).
        # "파일이 없을 때만 깨우기 답을 쓴다" 도 두지 않는다: 질의 런이 max-turns
        # 로 실패한 뒤 그 문장이 성공 답으로 둔갑한다.
        # v10.47.0 예외: 질의 런이 **자기 백그라운드 작업**(`shell background:true`)
        # 이 돌고 있는 채로 끝났으면, 모델은 "끝나면 알려준다" 에 기대어 답을
        # 미룬 것이다(실측 D-fix-1: 결과 파일엔 "still waiting on job #2" 만
        # 남고 진짜 요약은 깨우기 런에 있었다). 그래서 그 뒤 첫 깨우기 런의
        # 답까지 쓴다(덮어씀). 작업 없이 끝난 질의는 종전 규칙 그대로.
        # 종료 코드(v10.124.0)도 같은 분기 — 실패 런도 사슬 안이면 판정에 든다.
        if not (req.is_query or req.wake and self._continuing):
            return
        self.exit_code = 0 if result.success else 1
        if not result.success:
            return  # 파일은 안 쓰고, 사슬 상태도 그대로
        _write_result_file(self._result_file, result.output)
        # ``wait`` 로 끝난 런(v10.65.0)도 답을 미룬 것 — 작업뿐 아니라 모니터·
        # 에이전트 회신을 기다릴 때도 다음 깨우기 런의 답이 질의의 답이다.
        self._continuing = self._used_jobs() or bool(getattr(result, "waiting", False))

    def _used_jobs(self) -> bool:
        """이 런이 작업을 띄웠거나(끝났어도 — 1초짜리 작업의 통지가 런 종료와
        겹치는 경합, 재측정 E-fix-2) 아직 돌고 있는 작업이 있는가."""
        if self._jobs is None:
            return False
        return bool(self._jobs.running() or self._jobs.started_since(self._run_started))

    def run_starting(self, req) -> None:
        pass  # 못 보는 모델의 이미지는 부트가 막았다 (`_require_vision`)

    def run_failed(self, exc: Exception) -> bool:
        return False


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
            output.agent_dispatch_result("(no agent registry in this mode)", False)
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
                f"Sent to @{name} — the reply arrives in the 🤝 panel (web) / as a console line (CLI) "
                f"(it is not mixed into the main LLM conversation).",
                True,
            )
        return True

    profile, mode = _parse_at_profile(name)
    if mode == "spawn":
        if agent_registry is None:
            output.agent_dispatch_result(
                "(no agent registry in this mode — cannot spawn)", False
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
            f"@{key} ({profile}) is now resident"
            + (" — initial task delivered" if task else "")
            + ". Talk to it directly with @agt-<key> <message>.",
            True,
        )
        return True

    return False


class _ConsoleDispatchOutput(DispatchOutput):
    """CLI-flavoured output — colour, Rich markup, plain ``console.print``.

    ``report(answer, ok)`` (v10.124.0): 분기마다 결과를 표면
    (``_ConsoleSurface.routed``)에 알린다 — 종료 코드와 ``--result-file`` 은 표면이
    한 곳에서 정한다. ``answer`` 는 파일에 쓸 원문(목록·오류·상태는 None).
    종전(v10.33.0~)엔 이 어댑터가 결과 파일을 직접 썼다."""

    def __init__(self, report=None):
        self._report = report or (lambda answer, ok: None)

    def agent_dispatch_result(self, text: str, success: bool) -> None:
        # markup=False — 상태 텍스트의 [code-analyst] 같은 브래킷이 rich
        # 스타일 태그로 삼켜지지 않게 (v4.62.1 교훈과 동일 클래스).
        color = C["final"] if success else C["error"]
        console.print(text, style=color, markup=False)
        self._report(None, success)

    def list_agents(self, agents: list[tuple[str, str]], live_status: str = "") -> None:
        console.print(f"\n[{C['accent']}]Agent profiles:[/]")
        if not agents:
            console.print(f"[{C['muted']}]No profiles found.[/]")
        else:
            for name, desc in agents:
                suffix = f"  — {desc}" if desc else ""
                console.print(f"  @{name}{suffix}", markup=False, highlight=False)
        if live_status:
            console.print(f"\n[{C['accent']}]Live agents:[/]")
            console.print(live_status, markup=False)
        console.print(
            f"\n[{C['muted']}]Usage: @<profile> <task> (one-shot run) · "
            f"@<profile>-spawn <task> (resident) · @agt-<key> <message> (direct send)[/]"
        )
        self._report(None, True)

    def list_skills(self, skills: dict) -> None:
        self._report(None, True)
        user_skills = {k: v for k, v in skills.items() if v.user_invocable}
        if not user_skills:
            console.print(f"[{C['muted']}]No skills found.[/]")
            return
        console.print(f"\n[{C['accent']}]Available skills:[/]")
        for s in user_skills.values():
            hint = f" {s.argument_hint}" if s.argument_hint else ""
            console.print(
                f"  /{s.name}{hint}  — {s.description}", markup=False, highlight=False
            )
        console.print()

    def agent_not_found(self, name: str) -> None:
        console.print(
            f"Agent not found: @{name}",
            style=C["error"],
            markup=False,
            highlight=False,
        )
        console.print(f"[{C['muted']}]Type @ to list available agents[/]")
        self._report(None, False)

    def agent_result(self, result, *, ok: bool = True) -> None:
        # ``_dispatch_agent`` already streams the agent's thoughts /
        # tool calls through the renderer; this is the CLI's
        # trailing "headline" green print so the final answer is
        # also immediately visible above the next prompt.
        if result is not None:
            console.print(
                f"\n{result}", style=C["final"], markup=False, highlight=False
            )
        # 결과 파일엔 관찰 래퍼(STATUS/RESULT/[activity])를 벗긴 원문 — 메인
        # 경로(loop_result.output=원문)와 계약 일치. 쓸지는 표면이 ok 로 정한다.
        answer = None
        if ok and result is not None:
            from agent_cli.subagent.report import extract_result_body

            answer = extract_result_body(str(result))
        self._report(answer, ok)

    def skill_not_found(self, name: str) -> None:
        console.print(
            f"Unknown command: /{name}",
            style=C["error"],
            markup=False,
            highlight=False,
        )
        # 둘째 줄은 실제로 되는 것을 가리킨다 (v10.127.1; 종전 "Type /help" 는 run 에서
        # 그 /help 도 Unknown command 였다)
        console.print(
            "Run 'agent-cli run /help' for available commands",
            style=C["muted"],
            markup=False,
            highlight=False,
            soft_wrap=True,
        )
        self._report(None, False)

    def skill_result(self, name: str, result, *, ok: bool = True) -> None:
        # `/skill` 의 답도 결과 파일로 (v10.34.1) — `@agent task` 와 같은
        # "run 의 최종 답". 실패한 스킬의 오류문은 답이 아니다(v10.124.0): 종전엔
        # 초록 답으로 찍히고 파일에도 써졌다.
        self._report(result if ok else None, ok)
        if ok and result is not None:
            console.print(
                f"\n{result}", style=C["final"], markup=False, highlight=False
            )
            return
        if result is not None:
            console.print(
                f"\n{result}", style=C["error"], markup=False, highlight=False
            )
            return
        console.print(
            f"\n[{C['accent']}]Skill /{escape(name)} stopped without final answer. "
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
    binding,
    max_turns: int,
    verbose: bool,
    max_depth: int,
    ctx,
    session,
    graceful_interrupt: bool = True,
    stop_event=None,
    agent_registry=None,
) -> bool:
    """Detect and run ``@<name> <task>`` / ``/<skill> <args>`` invocations.

    ``stop_event`` is threaded into BOTH paths so the web Stop button can
    halt either at a turn boundary, same as a plain conversation turn:
      - ``/skill`` → ``_dispatch_skill`` → ``SkillLoop.run`` → ``run_loop``
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
            binding,
            max_turns=max_turns,
            verbose=verbose,
            max_depth=max_depth,
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
        output.agent_result(result, ok=_ok)
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
        result, _ok = _dispatch_skill(
            message,
            binding,
            max_turns=max_turns,
            verbose=verbose,
            max_depth=max_depth,
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
        output.skill_result(cmd_name, result, ok=_ok)
        return True

    return False


def _command_dispatch(spec, owners, output, *, graceful_interrupt: bool):
    """펌프 표면의 명령 라우팅 — ``(text, stop_event) -> bool`` (run·web 공용, v10.131.0).

    두 호스트가 같은 키워드로 `try_dispatch_agent_or_skill` 을 부른다; 다른 것은 결과를
    받을 표면(``output()`` — 부를 때마다 하나를 돌려주는 콜러블: run 은 표면의 판정에
    잇는 새 `_ConsoleDispatchOutput`, web 은 렌더러의 `WebDispatchOutput` 하나)과 중단
    방식(``graceful_interrupt`` — 표면의 것)뿐이다."""

    def dispatch(text: str, stop_event) -> bool:
        return try_dispatch_agent_or_skill(
            text,
            output(),
            binding=spec.binding,
            max_turns=spec.max_turns,
            verbose=spec.verbose,
            max_depth=spec.max_depth,
            ctx=spec.ctx,
            session=spec.session,
            graceful_interrupt=graceful_interrupt,
            stop_event=stop_event,
            agent_registry=owners.agent_registry,
        )

    return dispatch


def _dispatch_agent(
    query: str,
    binding,
    max_turns: int = 0,
    verbose: bool = False,
    max_depth: int = 2,
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
    from agent_cli.loop.kind import Caller
    from agent_cli.render import render_status

    _parent_hooks = _load_hooks() or None
    render_status("running", f"Running agent: {agent_name}...")
    result = tool_delegate(
        {"tasks": [{"task": task, "agent": agent_name, "context": "fork"}]},
        # 루프 **밖**의 디스패치 — 감싸는 루프가 없으니 main 세션이 호출자다.
        Caller.main_session(
            binding=binding,
            ctx=ctx,
            session=session,
            max_turns=max_turns,
            max_depth=max_depth,
            hooks_config=_parent_hooks,
            stop_event=stop_event,
        ),
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
    binding,
    max_turns: int = 0,
    verbose: bool = False,
    max_depth: int = 2,
    ctx=None,
    session=None,
    graceful_interrupt: bool = False,
    stop_event=None,
):
    """Dispatch a /skill-name command → ``(answer, success)``.

    미발견 스킬은 ``(_SKILL_NOT_FOUND, False)``. 실패하면 ``answer`` 는 스킬 루프의
    오류문이다 — 답이 아니므로 ``success`` 로 가린다(v10.124.0, ``_dispatch_agent`` 와
    같은 모양; 종전엔 오류문만 돌려줘 결과 파일·종료 코드가 실패를 성공으로 봤다).
    """
    from agent_cli.skills import load_skills
    from agent_cli.skills.executor import SkillLoop

    skills = load_skills()
    parts = query.split(maxsplit=1)
    cmd_name = parts[0][1:]  # strip leading /

    if cmd_name not in skills:
        return _SKILL_NOT_FOUND, False

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

    from agent_cli.hooks import load_hooks as _load_hooks
    from agent_cli.loop.kind import Caller
    from agent_cli.subagent.agents_live import main_registry

    # 디스크 훅(~/.agent-cli/hooks.json / .agent-cli/hooks.json)을 스킬 자신의 훅과 병합해
    # 보이게 한다. load_hooks() 는 캐시라 반복 호출이 싸다. 스코프(카드 하나)·들여쓰기·
    # OnSkillStart/End·ctx·result.md 는 종류(`SkillLoop`)의 것 (v10.87.0) — 종전엔 이 함수와
    # 루프의 `_handle_run_skill` 이 같은 래퍼를 두 벌 들고 있었다.
    skill_result = SkillLoop(
        skill,
        arguments,
        # 루프 **밖**의 디스패치 — main 세션이 호출자. 스킬은 main 의 일이라 main 레지스트리를
        # 상속해 spawn 할 수 있다(v7.17.0 배선 통일).
        Caller.main_session(
            binding=binding,
            ctx=ctx,
            session=session,
            max_turns=max_turns,
            max_depth=max_depth,
            hooks_config=_load_hooks() or None,
            stop_event=stop_event,
            graceful_interrupt=graceful_interrupt,
            verbose=verbose,
            agent_registry=main_registry(),
        ),
    ).run()

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

    return answer, skill_result.success


def _stage_query_images(paths: list[str] | None) -> list[dict]:
    """`run --image` (v10.59.0): 파일을 확인하고 워크스페이스 밖이면
    ``.agent-cli/tmp/uploads/`` 로 들여온 참조 목록. 잘못된 경로는 즉시 종료."""
    if not paths:
        return []
    from agent_cli import images as _images

    refs: list[dict] = []
    for p in paths:
        try:
            refs.append(_images.stage_upload(p).to_dict())
        except ValueError as e:
            console.print(
                f"--image: {e}", style=C["error"], markup=False, highlight=False
            )
            raise typer.Exit(1) from None
    return refs


def _require_vision(binding) -> None:
    """모델이 이미지를 보는지(등록값 또는 프로브) — 못 보면 즉시 종료.
    없는 능력을 약속하지 않는다 (docs/vision/DESIGN.md §2.5). web 은 런을 열어야
    다음 메시지를 받으므로 막지 않고 알린다(`WebSurface.run_starting`)."""
    from agent_cli.providers.capabilities import binding_sees_images

    if not binding_sees_images(binding):
        console.print(
            f"--image: model '{binding.model}' cannot view images "
            f"(supports_vision is false, or the vision probe failed).",
            style=C["error"],
            markup=False,
            highlight=False,
        )
        raise typer.Exit(1)


def _ask_dialect(renderer) -> str:
    """방언 하나를 묻는다 — 묻는 코드는 여기 하나 (v10.127.0; 탐지 실패 뒤의 질문과
    방언 없는 엔트리의 부트 질문이 같이 쓴다). 선택지마다 스펙의 한 줄 설명. 기본값·
    auto 는 없다 (v10.3.0) — 등록된 이름 하나를 받을 때까지 재질문 (D2: 조용한 오타
    폴백 금지). EOF·Ctrl-C 는 호출자에게 그대로."""
    from agent_cli.dialects import summaries

    choices = summaries()
    names = [n for n, _ in choices]
    width = max(len(n) for n in names)
    console.print("  Dialect — how this model writes tool calls:", highlight=False)
    for name, text in choices:
        console.print(
            f"    {name.ljust(width)}  {text}",
            markup=False,
            highlight=False,
            soft_wrap=True,
        )
    prompt = f"  Dialect ({' / '.join(names)}): "
    answer = renderer.prompt_user(prompt, multiline=False).strip().lower()
    while answer not in names:
        console.print(
            f"  Unknown dialect '{answer}'. Available: {', '.join(names)}",
            style=C["muted"],
            markup=False,
            highlight=False,
        )
        answer = renderer.prompt_user(prompt, multiline=False).strip().lower()
    return answer


def _prompt_model_capabilities(model: str):
    """Interactively ask user for model capabilities when detection fails."""
    from agent_cli.config import save_model_entry
    from agent_cli.providers.capabilities import (
        DEFAULT_CAPABILITIES,
        ModelCapabilities,
    )

    console.print(
        f"\nModel '{model}' not found in registry and detection failed.",
        style=C["accent"],
        markup=False,
        highlight=False,
    )
    console.print(
        f"[{C['muted']}]Please provide model info (saved for future use):[/]\n"
    )

    renderer = get_renderer()
    try:
        # 빈 입력의 기본값 = 탐지 실패 때 실제로 쓰이는 값 (v10.126.0; 종전 질문은
        # [4096] 이라 적고 빈 입력도 4096 이었는데, 묻지 않은 길은 256K 였다)
        default_window = DEFAULT_CAPABILITIES.context_window
        ctx_input = renderer.prompt_user(
            f"  Context window size [{default_window}]: ", multiline=False
        )
        context_window = int(ctx_input) if ctx_input else default_window

        thinking_input = renderer.prompt_user(
            "  Supports thinking? (y/n) [n]: ", multiline=False
        ).lower()
        supports_thinking = thinking_input in ("y", "yes")

        vision_input = renderer.prompt_user(
            "  Can view images (vision)? (y/n) [n]: ", multiline=False
        ).lower()
        supports_vision = vision_input in ("y", "yes")

        max_output = min(context_window // 4, 4096)

        caps = ModelCapabilities(
            context_window=context_window,
            max_output_tokens=max_output,
            supports_thinking=supports_thinking,
            supports_vision=supports_vision,
        )

        # Dialect 바인딩: 엔트리가 대화형으로 생성되는 바로 이 지점에서
        # 고른다. 기본값·auto 는 없다 (v10.3.0).
        dialect_input = _ask_dialect(renderer)

        from agent_cli.providers.capabilities import caps_to_entry

        entry = caps_to_entry(caps)
        entry["dialect"] = dialect_input
        save_model_entry(model, entry)
        console.print(f"[{C['muted']}]Saved to ~/.agent-cli/models.json[/]\n")
        return caps
    except (EOFError, KeyboardInterrupt, ValueError):
        return None


def _verify_model_or_exit(
    resolved_model: str,
    provider: str,
    base_url: str,
    api_key: str,
    *,
    explicit: bool,
    quiet: bool,
) -> str:
    """해석된 모델이 서버에 실재하는지 확인하고, 확정된 이름을 돌려준다.

    세 갈래 (docs/model-resolution):

    - **실재** → 그대로. ``quiet`` 가 아니면 한 줄로 무엇으로 도는지 밝힌다
      — 기본값을 남기는 대가로 "지금 무슨 모델인지" 를 보이게 하는 지점이다.
    - **목록에 없음 / 고른 적 없음** → 대화형이면 목록에서 고르게 하고
      저장까지(다음부터 안 묻는다), 아니면 **실패**. 보드가 띄운 인스턴스는
      stdin 이 파이프라 물어보면 멈추므로 절대 묻지 않는다.
    - **조회 불가** (오프라인·``/models`` 미지원) → 경고 한 줄 후 진행.
      확인할 수 없는 것을 틀렸다고 단정하면 그게 더 나쁜 회귀다.
    """
    from agent_cli.model_check import (
        ModelNotFound,
        NoModelSelected,
        is_interactive,
        verify_model,
    )

    origin = "--model" if explicit else str(user_dir() / "config.json")
    try:
        listing = verify_model(
            resolved_model, base_url, api_key, provider, origin=origin
        )
    except NoModelSelected as e:
        return _choose_model_or_exit(
            e.listing, base_url, None, origin, quiet, interactive=is_interactive()
        )
    except ModelNotFound as e:
        return _choose_model_or_exit(
            e.listing, base_url, e, origin, quiet, interactive=is_interactive()
        )
    if listing.unreachable:
        return _unreachable_or_exit(resolved_model, base_url, listing, quiet=quiet)
    if not quiet and not listing.available:
        console.print(
            f"⚠ Could not read /v1/models, so the model name was not "
            f"verified ({listing.reason}) — continuing with the configured value.",
            style=C["warn"],
            markup=False,
            highlight=False,
        )
    return resolved_model


def _unreachable_or_exit(model: str, base_url: str, listing, *, quiet: bool) -> str:
    """서버에 닿지 못함 (v10.126.0) — "묻지 못함" 중에서 원인을 그 자리에서 말하는 갈래.

    - **등록된 모델**(models.json·패키지 기본 표에 엔트리) → 계속. 능력은 엔트리에서
      나오고, 서버를 나중에 켜는 상주 방·보드 되살리기가 이 경로에 기댄다 — 막지 않는다.
    - **등록 안 된 모델** → 여기서 ``Exit(2)``(런이 시작되지 못함). 종전엔 프로브 3종 →
      문맥 창 질문 → ``No dialect for model`` 까지 10~20초를 끌고 가 엉뚱한 원인으로 끝났다.
      ``quiet`` 여도 멈춘다(종전 그 길의 끝도 2였다).
    """
    from agent_cli.config import _GLOBAL_MODELS_PATH, get_model_entry

    if get_model_entry(model) is not None:
        if not quiet:
            console.print(
                f"⚠ Cannot connect to {base_url} ({listing.reason}) — continuing with "
                "the registered settings; calls fail until the server is up.",
                style=C["warn"],
                markup=False,
                highlight=False,
                soft_wrap=True,
            )
        return model
    for line in (
        f"✗ Cannot connect to the server at {base_url} ({listing.reason}).",
        (
            f"  Model '{model}' is not registered in {_GLOBAL_MODELS_PATH}, so its "
            "capabilities cannot be known without the server."
        ),
        (
            "  Fix: start the server, or check --base-url / the base_url in "
            f"{user_dir() / 'config.json'}."
        ),
    ):
        console.print(
            line, style=C["error"], markup=False, highlight=False, soft_wrap=True
        )
    raise typer.Exit(2)


def _choose_model_or_exit(
    listing,
    base_url: str,
    err,
    origin: str,
    quiet: bool,
    *,
    interactive: bool,
) -> str:
    """검증 실패 지점. 대화형이면 고르게 하고 저장, 아니면 깔끔히 실패한다."""
    from agent_cli.config import save_config_value

    if err is not None:
        console.print(
            f"\n[{C['error']}]✗ The configured model was not found on the server.[/]"
        )
        console.print(f"  [{C['muted']}]model[/]  {escape(err.model)}")
        near = listing.suggest(err.model)
        if near:
            console.print(f"  [{C['muted']}]similar[/]  {escape(near)}")
    else:
        console.print(f"\n[{C['error']}]✗ No model is specified.[/]")
    console.print(f"  [{C['muted']}]server[/]  {escape(base_url)}", soft_wrap=True)
    console.print(f"  [{C['muted']}]source[/]  {escape(origin)}", soft_wrap=True)

    if not listing.available:
        # 고를 목록조차 없다 — 이름을 대라고만 하면 맹목 타이핑을 유도한다.
        console.print(
            f"\n  The model list could not be read either ({listing.reason}). "
            f"Check the server address and that it is running.",
            style=C["muted"],
            markup=False,
            highlight=False,
        )
        console.print(f"  [{C['muted']}]Fix: agent-cli setup  or  --model <id>[/]\n")
        raise typer.Exit(1)

    console.print(f"\n  [{C['muted']}]Available:[/]")
    for i, m in enumerate(listing.models, 1):
        console.print(f"    [{C['accent']}]{i}[/]) {escape(m)}")

    if not interactive or quiet:
        console.print(f"\n  [{C['muted']}]Fix: agent-cli setup  or  --model <id>[/]\n")
        raise typer.Exit(1)

    from rich.prompt import IntPrompt

    try:
        choice = IntPrompt.ask(
            "\n  Pick one",
            choices=[str(i) for i in range(1, len(listing.models) + 1)],
        )
    except (EOFError, KeyboardInterrupt):
        console.print()
        raise typer.Exit(1) from None
    picked = listing.models[choice - 1]
    if save_config_value("default_model", picked):
        console.print(f"  [{C['ok']}]✓ Saved — this model from now on[/]\n")
    else:
        console.print(
            f"  [{C['warn']}]⚠ Could not save the config — applies to this run only[/]\n"
        )
    return picked


def _setup_provider(
    provider: str,
    model: str | None,
    base_url: str | None,
    api_key: str | None,
    quiet: bool = False,
    explicit: bool = False,
):
    """Resolve settings + create provider + get capabilities. Returns tuple."""
    _maybe_setup(base_url, model)
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
    # 이름을 **쓰기 전에** 확인한다 (v9.5.0). 없는 모델은 종전엔 첫 LLM 호출까지
    # 살아남아 404 로 터졌고, 에이전트에선 대화 한복판의 거부 카드로 보였다.
    resolved_model = _verify_model_or_exit(
        resolved_model,
        provider,
        resolved_url,
        resolved_key,
        explicit=explicit or bool(model),
        quiet=quiet,
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
        console.print(
            f"Unsupported model: {e}", style=C["error"], markup=False, highlight=False
        )
        raise typer.Exit(1) from e
    finally:
        set_progress_callback(None)

    # Interactive fallback: ask user when detection fails — 터미널일 때만 (v10.126.0;
    # 종전엔 비대화식에서도 묻고 EOF 로 넘어갔다)
    from agent_cli.model_check import is_interactive

    if capabilities == DEFAULT_CAPABILITIES and not quiet and is_interactive():
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


def _bind_dialect_or_exit(model: str, unbound, *, quiet: bool):
    """방언이 안 묶인 모델 (v10.127.0) — 터미널이면 그 자리에서 묻고 models.json 의 그
    엔트리에 저장한 뒤 진행, 아니면 종전 오류 그대로 ``Exit(2)``.

    "기본값·auto 없음, 사용자가 모델에 방언을 묶는다"(v10.3.0)는 그대로다 — 바뀐 것은
    묶을 기회를 주는 자리뿐. 종전엔 탐지가 **성공**하면 엔트리가 방언 없이 저장되고 곧바로
    ``No dialect for model`` 이라, README 대로 설치한 첫 ``run`` 이 전부 여기서 멈췄다.
    비대화식(보드·파이프·``AGENT_CLI_NO_INPUT``)·``quiet``(web — 탐지 실패 질문과 같은
    규칙)는 묻지 않는다. ``--dialect`` 를 준 실행은 이 길에 오지 않는다.

    답을 쓸 수 없는 질문은 하지 않는다: 그 엔트리가 사용자 파일에 없으면(패키지 기본 표에만
    있거나 탐지 결과 저장이 실패) 묻기 **전에** 종전 오류 2. ``"dialect": ""``·``null`` 은
    "묶이지 않음" 이라 묻고 채운다. 판정·쓰기는 `config.bind_user_dialect` 한 곳."""
    from agent_cli.config import _GLOBAL_MODELS_PATH, bind_user_dialect
    from agent_cli.model_check import is_interactive

    def _fail():
        console.print(
            unbound.args[0] if unbound.args else str(unbound),
            style=C["error"],
            markup=False,
            highlight=False,
            soft_wrap=True,
        )
        return typer.Exit(2)

    if quiet or not is_interactive() or not bind_user_dialect(model):
        raise _fail() from unbound
    console.print(
        f"\nModel '{model}' has no dialect bound in {_GLOBAL_MODELS_PATH}.",
        style=C["accent"],
        markup=False,
        highlight=False,
        soft_wrap=True,
    )
    try:
        name = _ask_dialect(get_renderer())
    except EOFError:
        console.print()
        raise _fail() from unbound
    except KeyboardInterrupt:
        console.print()
        raise typer.Exit(130) from None
    if not bind_user_dialect(model, name):
        # 물은 뒤의 쓰기 실패(디스크·권한) — 이번 런만의 바인딩은 만들지 않는다
        # (``--dialect`` 는 프로세스 전체 강제라 뜻이 다르다).
        console.print(
            f'Could not save the dialect to {_GLOBAL_MODELS_PATH}. Set "dialect" '
            f"on the '{model}' entry there, or pass --dialect {name}.",
            style=C["error"],
            markup=False,
            highlight=False,
            soft_wrap=True,
        )
        raise typer.Exit(2)
    console.print(
        f"Saved dialect '{name}' to {_GLOBAL_MODELS_PATH}",
        style=C["muted"],
        markup=False,
        highlight=False,
        soft_wrap=True,
    )
    return _resolve_dialect(model)


def _bootstrap_provider(
    provider,
    model,
    base_url,
    api_key,
    dialect: str | None,
    max_context_tokens: int,
    *,
    quiet: bool = False,
    explicit_model: bool = False,
):
    """provider 셋업 → dialect 해석 fail-fast → 세션의 `ModelBinding` 하나.

    dialect 해석 체인 (v10.3.0): 명시 ``--dialect`` (프로세스 전체 강제)
    > models.json 모델 바인딩. 둘 다 없거나 unknown 이름이면 세션 생성
    전에 fail-fast (D2) — 기본값 없음, 사용자가 모델에 방언을 묶는다.
    """
    llm_provider, capabilities, resolved_model, resolved_url, resolved_key, name = (
        _setup_provider(
            provider, model, base_url, api_key, quiet=quiet, explicit=explicit_model
        )
    )
    _set_dialect_override(dialect)
    try:
        dialect_plugin = _resolve_dialect(resolved_model)
    except _DialectUnbound as exc:
        dialect_plugin = _bind_dialect_or_exit(resolved_model, exc, quiet=quiet)
    except KeyError as exc:
        console.print(
            str(exc.args[0] if exc.args else exc),
            style=C["error"],
            markup=False,
            highlight=False,
        )
        raise typer.Exit(2) from exc
    if getattr(dialect_plugin, "server_parsed", False) and name != "openai":
        # v10.2.0: native_fc 는 OpenAI 호환 `tools`/`tool_calls` 만 안다 (NATIVE.md N4)
        console.print(
            f"dialect '{dialect_plugin.name}' needs an OpenAI-compatible "
            f"provider (tools/tool_calls); provider is '{name}'.",
            style=C["error"],
            markup=False,
            highlight=False,
        )
        raise typer.Exit(2)
    from agent_cli.providers.binding import ModelBinding

    # 세션이 드는 모델 하나 (v10.43.0) — 루프·ctx·팀원·web 은 이 객체를 참조만
    # 하고, 토큰 예산은 창에서 매번 계산한다(명시 --max-context-tokens 만 고정).
    return ModelBinding(
        provider=llm_provider,
        provider_name=name,
        base_url=resolved_url,
        api_key=resolved_key,
        model=resolved_model,
        capabilities=capabilities,
        dialect=dialect_plugin,
        context_budget_override=max(0, int(max_context_tokens or 0)),
    )


def _apply_title(session, title: str) -> None:
    """``--title`` 을 세션에 (v10.71.0). 빈 값은 저장된 제목을 그대로 둔다 — resume 이
    제목을 잃지 않게. 보드는 spawn 마다 글의 topic 을 넘기므로 고친 제목이 다음
    재시작에 들어온다."""
    t = (title or "").strip()
    if t:
        session.title = t


def _load_resume_session(resume_id: str):
    """``--resume <id>`` 공용 경로: unknown ID 는 provider 핸드셰이크 전에
    fail-fast, 성공 시 SessionMeta 반환 + 배너. run/web 동형."""
    from agent_cli.context.session import load_session

    session = load_session(resume_id)
    if session is None:
        console.print(
            f"Session '{resume_id}' not found.",
            style=C["error"],
            markup=False,
            highlight=False,
        )
        raise typer.Exit(code=1)
    _claim_session(session)
    console.print(
        f"Resuming session {resume_id}",
        style=C["accent"],
        markup=False,
        highlight=False,
    )
    return session


def _claim_session(session) -> None:
    """한 세션 = 한 프로세스 (docs/schedule/DESIGN.md §4.3). 다른 프로세스가
    이미 연 세션이면 여기서 끝낸다 — 둘이 같은 history 에 쓰고 같은 예약을
    발화하는 것을 기동 시점에 막는다."""
    from agent_cli.context.session import get_session_dir
    from agent_cli.context.session_lock import SessionBusy, claim_session

    try:
        claim_session(get_session_dir(session))
    except SessionBusy as e:
        console.print(f"{e}.", style=C["error"], markup=False, highlight=False)
        raise typer.Exit(code=1) from None


def _build_context(session, binding, *, resume: bool = False):
    """세션 + 모델 바인딩으로 ContextManager 조립 (run/web 단일 경로). 방언과
    토큰 예산은 바인딩에서 매번 읽는다. 노브(압축 비율·무진전 한도·사고·문법)는
    여기서 받지 않는다 — 소유자들이 전부 선 뒤 `knobs.apply_all` 이 CLI > env >
    기본값으로 적용한다 (v10.43.0)."""
    from agent_cli.context.session import get_session_dir
    from agent_cli.paths import scratch_dir

    # 모델이 쓰는 임시 디렉토리는 세션과 함께 생긴다 (v10.90.0) — 프롬프트가 가리키는
    # 곳이 실제로 있어야 첫 쓰기가 실패하지 않는다.
    scratch_dir().mkdir(parents=True, exist_ok=True)
    return ContextManager(get_session_dir(session), binding=binding, resume=resume)


def _boot_model(knob_args) -> str | None:
    """부팅 모델: 노브 `model` 의 CLI > env; 없으면 None(config `default_model` →
    프로바이더 기본)."""
    from agent_cli.knobs import KNOB_BY_NAME

    return KNOB_BY_NAME["model"].resolve((knob_args or {}).get("model"))


def _dialect_help() -> str:
    """``--dialect`` 도움말 (run·web) — 선택지 설명은 방언 스펙의 ``summary`` 에서
    (v10.127.0; 종전엔 넷을 손으로 적어 native_fc 가 빠져 있었다)."""
    from agent_cli.dialects import summaries

    choices = "; ".join(f"{name}: {text}" for name, text in summaries())
    return (
        "Dialect (tool-call wire shape) name — forces it for the whole session (main "
        "and every sub-agent). Unset: each agent uses its model's models.json "
        "'dialect' binding — `run` in a terminal asks for a missing one and saves "
        f"it; otherwise an unbound model is an error. Registered: {choices}."
    )


_DIALECT_HELP = _dialect_help()


def _answers_before_boot(query: str, *, resume: bool) -> bool:
    """``run`` 의 부트 전 명령 (v10.127.1) — 답했으면 True(종료 코드 0).

    ``/help`` 는 늘(세션에 기대는 것이 없다). 목록(``/skills``·``@agents``·``@``)은
    ``--resume`` 이 없을 때만 — 이어 가는 세션에선 살아난 상주 에이전트가 목록에
    들어가야 하므로 종전 경로(펌프 라우팅)로 간다. ``@<name>`` 처럼 이름을 단 것은
    여기서 다루지 않는다(상주 상태·spawn 은 부트 뒤의 일)."""
    head = query.split(maxsplit=1)[0] if query.strip() else ""
    if head == "/help":
        from agent_cli.command_table import RUN, help_text

        console.print(help_text(RUN), markup=False, highlight=False, soft_wrap=True)
        return True
    if resume:
        return False
    out = _ConsoleDispatchOutput()
    if head == "/skills":
        from agent_cli.skills import load_skills

        out.list_skills(load_skills())
        return True
    if head in ("@", "@agents"):
        out.list_agents(_collect_agents())
        return True
    return False


#: `run --image` (v10.59.0) — 모듈 단일체: 가변 타입(list) 인자 기본값의 함수
#: 호출을 ruff B008 이 막는다(다른 옵션은 불변 타입이라 통과).
_TITLE_OPTION = typer.Option(
    "",
    "--title",
    help="Room title (v10.71.0) — shown in the web header and browser tab, the "
    "startup banner and web.json; saved in the session so --resume keeps it "
    "(giving --title again replaces it). agent-board passes the post's topic.",
)
_IMAGE_OPTION = typer.Option(
    None,
    "--image",
    help="Attach an image to QUERY (PNG/JPEG/GIF/WebP, repeatable). Files outside the "
    "workspace are copied to .agent-cli/tmp/uploads/. Fails immediately when the "
    "model cannot view images (models.json supports_vision / probe).",
)


@app.command()
@knob_options
def run(
    query: str = typer.Argument(..., help="Task to execute"),
    provider: str | None = typer.Option(
        None,
        "--provider",
        "-p",
        help="LLM provider: openai | anthropic (default: openai)",
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
    dialect: str | None = typer.Option(
        None,
        "--dialect",
        help=_DIALECT_HELP,
    ),
    resume: str = typer.Option(
        "",
        "--resume",
        help="Continue a previous session by ID — loads the same on-disk session as web "
        "and injects QUERY as a follow-up request on the restored context.",
    ),
    result_file: str = typer.Option(
        "",
        "--result-file",
        help="Write the final answer (raw) to this path — machine-readable, without renderer "
        "decoration (scripting, headless automation). On failure or no answer the "
        "file is not created; use the exit code (0 answered, 1 failed or no answer).",
    ),
    image: list[str] | None = _IMAGE_OPTION,
    title: str = _TITLE_OPTION,
    knob_args: dict | None = None,  # `@knob_options` 가 채운다 (v10.43.0)
):
    """Execute a task in single-shot mode. The agent uses tools (read_file, shell, etc.) to complete the task and returns the result.

    Exit status: 0 the run succeeded, 1 it failed (LLM error, max turns, failed skill, unknown command), 2 configuration or usage error, 130 interrupted. "/sh <cmd>" exits with the command's own status (124 on timeout).
    """
    _apply_style(style)
    query_images = _stage_query_images(image)
    # /sh prefix: Run shell command directly without LLM
    if query.startswith("/sh ") or query == "/sh":
        cmd = query[3:].strip()
        if not cmd:
            console.print(f"[{C['error']}]No command to execute.[/]")
            raise typer.Exit(1)
        raise typer.Exit(_run_shell_inline(cmd))

    # 목록 명령은 부트 전에 답한다 (v10.127.1) — provider·서버·세션이 필요 없다
    # (종전엔 부트와 세션 생성을 거쳐 `sessions` 에 빈 세션을 남겼다).
    if _answers_before_boot(query, resume=bool(resume)):
        raise typer.Exit(0)

    # C4: run/web 공용 부트스트랩 — provider 6-튜플 + dialect fail-fast +
    # 예산 폴백(70% 통일 공식). resume pre-check 는 핸드셰이크보다 먼저
    # (unknown ID fail-fast).
    session_resumed = _load_resume_session(resume) if resume else None
    binding = _bootstrap_provider(
        provider,
        _boot_model(knob_args),
        base_url,
        api_key,
        dialect,
        max_context_tokens,
        explicit_model="model" in (knob_args or {}),
    )
    if query_images:
        _require_vision(binding)

    # MCP servers
    mcp_manager, mcp_tools = _setup_mcp()
    if mcp_tools:
        from agent_cli.tools import TOOLS

        TOOLS.update(mcp_tools)

    from agent_cli.context.session import create_session, save_meta

    session = session_resumed if session_resumed is not None else create_session()
    _apply_title(session, title)
    _claim_session(session)
    save_meta(session)
    if session.title:
        console.print(f"  Title:   {session.title}", markup=False, highlight=False)
    ctx = _build_context(session, binding, resume=session_resumed is not None)

    from agent_cli.input_queue import InputQueue
    from agent_cli.pump import QuietPolicy
    from agent_cli.runtime import (
        SessionSpec,
        build_session_owners,
        build_session_stores,
        wire_session,
    )

    # 입력 큐는 저장소보다 먼저 선다 — 예약의 발화 자리를 저장소를 만드는 자리에서
    # 묶는다(web 은 서버가 이미 들고 있다, v10.130.5).
    input_queue = InputQueue()
    spec = SessionSpec.for_session(
        binding=binding,
        session=session,
        ctx=ctx,
        mcp_manager=mcp_manager,
        knob_args=knob_args,
        max_turns=max_turns,
        max_depth=max_depth,
        verbose=verbose,
        record_turns=record_turns,
    )
    # 종료 시퀀스는 아래 finally 하나로 수렴 (teardown 단일화 — 리뷰 §4.1). 조립도
    # try 안이다(v10.131.0): 저장소·레지스트리·감사자를 만들다 죽어도 MCP 해제·세션
    # 마감이 돈다. ``warn_stuck`` 은 메인 펌프 경로에서만 켠다 (종전 표면 보존).
    stores = owners = None
    warn_stuck = False
    exit_code = 1  # 펌프가 판정을 못 내고 끝나는 길은 없다 — 그래도 "답 없음"
    try:
        # 세션 펌프 (v10.33.0, docs/pump/DESIGN.md): web 과 같은 루프 —
        # 초기 질의를 InputQueue 에 넣고, teammate 회신의 wake 아이템
        # (MailWaker)·예약 발화도 같은 큐로 들어온다. ``/skill``·``@agent``
        # 는 펌프 안의 라우팅(_ConsoleSurface.route)이 web 의 route_one 과
        # 같은 공용 입구(try_dispatch_agent_or_skill)로 처리한다. 정지 =
        # 큐·에이전트·모니터·예약 전부 조용(QuietPolicy).
        # monitor·schedule 은 native — 시간을 재는 것도 발화도 이 프로세스 안에서
        # 일어난다. board 도 env 도 없이 항상 조립된다.
        stores = build_session_stores(spec, queue=input_queue)
        owners = build_session_owners(spec, stores, renderer=get_renderer())
        surface = _ConsoleSurface(
            result_file=result_file,
            jobs=stores.jobs,  # 질의가 작업을 남기고 끝나면 깨우기 답도 결과 파일에
            # 결과는 표면이 판정한다 — 종료 코드·결과 파일 한 곳 (v10.124.0)
            dispatch=_command_dispatch(
                spec,
                owners,
                lambda: _ConsoleDispatchOutput(report=surface.routed),
                graceful_interrupt=_ConsoleSurface.graceful_interrupt,
            ),
        )
        policy = QuietPolicy(
            queue=input_queue,
            agent_registry=owners.agent_registry,
            monitors=stores.monitors,
            schedules=stores.schedules,
            harness=owners.auditor,
            jobs=stores.jobs,
            on_wait=lambda kind: console.print(
                f"[{C['muted']}]"
                + (
                    _jobs_wait_notice(stores.jobs)
                    if kind == "jobs"
                    else _monitor_wait_notice(stores.monitors)
                    if kind == "monitors"
                    else _HARNESS_WAIT_NOTICE
                    if kind == "harness"
                    else _schedule_wait_notice(stores.schedules)
                )
                + "[/]"
            ),
        )
        wired = wire_session(
            spec,
            stores,
            owners,
            queue=input_queue,
            surface=surface,
            policy=policy,
            on_mail_notice=_agent_mail_notice,
        )

        agent_registry = owners.agent_registry
        if wired.revived:
            console.print(
                f"[{C['muted']}]🤝 {wired.revived} resident agent(s) revived[/]"
            )
        if _auditor_boot_line(owners.auditor):
            console.print(f"[{C['muted']}]{_auditor_boot_line(owners.auditor)}[/]")
        if agent_registry.stale_questions:
            # 열린 질문은 되살린다(§3.9) — 여기 세는 것은 **되살리지 못한**
            # 것, 즉 묻거나 답할 쪽이 돌아오지 않은 질문뿐이다.
            console.print(
                f"[{C['muted']}]❓ {agent_registry.stale_questions} unanswered question(s) "
                f"not restored — the asking agent did not come back[/]"
            )
        if wired.auto:
            console.print(
                f"[{C['muted']}]🤝 {wired.auto} auto-spawn specialist(s) now resident[/]"
            )
        _mon_notice = _previous_monitors_notice(spec.session_dir)
        if _mon_notice:
            console.print(_mon_notice, style=C["muted"], markup=False, highlight=False)
        _sched_notice = _missed_schedules_notice(stores.schedules)
        if _sched_notice:
            console.print(
                _sched_notice, style=C["muted"], markup=False, highlight=False
            )

        for ref in query_images:
            console.print(
                f"  🖼 {ref['path']} ({ref['width']}×{ref['height']})",
                style=C["muted"],
                markup=False,
                highlight=False,
                soft_wrap=True,
            )
        input_queue.enqueue(None, query, images=query_images)
        warn_stuck = True
        try:
            wired.pump.run()
            exit_code = surface.exit_code
        except KeyboardInterrupt:
            console.print(f"\n[{C['accent']}]⚡ Interrupted.[/]")
            exit_code = 130
    finally:
        _finalize_run(spec, stores, owners, warn_stuck=warn_stuck)
    # 정리가 끝난 **뒤에** 판정으로 끝낸다 (v10.124.0) — 0 답 · 1 실패/무답 · 130 중단.
    # 잡히지 않은 예외는 여기 오지 않고 전파된다(typer 가 1).
    raise typer.Exit(exit_code)


def web_instance_is_active(
    renderer,
    server,
    agent_registry,
    monitors=None,
    schedules=None,
    harness=None,
    jobs=None,
) -> bool:
    """idle self-reap 의 활동 술어 (--idle-timeout, v7.10.0 에이전트 가드).

    True 조건: 라이브 뷰어 존재 ∨ main worker busy ∨ 상주 에이전트 활동
    (working / 미처리 inbox — main 유휴여도 백그라운드 작업 소실 방지)
    ∨ 대기 메시지 큐 비어있지 않음 ∨ **살아 있는 모니터/미배달 보고**
    ∨ **켜진 예약** (v10.12.0 — 예약이 있으면 "유지" 처럼 꺼지지 않는다).
    ``agent_registry`` 는 web() 의 worker 부트스트랩이 늦게 채우는 nonlocal
    이라 None 허용.

    ``monitors`` (v9.11.0): 설계 초판은 "web 은 무한 대기라 수명 문제는 run
    에만 해당"이라고 봤는데 **틀렸다** — board 가 띄운 인스턴스는 뷰어가 없으면
    여기서 자기를 거두고, 그러면 살아 있는 모니터가 조용히 죽는다. run 펌프의
    수명 정책(`pump.QuietPolicy`)과 같은 술어(`runtime.session_has_live_work`)를
    본다."""
    from agent_cli.runtime import session_has_live_work

    return bool(
        renderer.hub.has_live_connections()
        or renderer.worker_is_busy()
        # any_activity (미배달 회신 제외) — run 펌프의 has_active_work 와
        # 다른 뜻이라 공용 술어 밖에 둔다 (runtime.session_has_live_work).
        or (agent_registry is not None and agent_registry.any_activity())
        or session_has_live_work(
            pending_count=server.pending_count(),
            monitors=monitors,
            schedules=schedules,
            harness=harness,
            jobs=jobs,
        )
    )


def _jobs_wait_notice(jobs) -> str:
    """`run` 이 돌고 있는 백그라운드 작업 때문에 끝나지 않고 기다릴 때의 한 줄 (v10.47.0)."""
    live = jobs.running()
    names = ", ".join(f"#{j.id} pid {j.pid}" for j in live)
    return (
        f"🧵 Waiting on {len(live)} background job(s) ({names}). The run continues on their notice; "
        f"Ctrl-C quits."
    )


def _monitor_wait_notice(monitors) -> str:
    """`run` 이 살아 있는 모니터 때문에 끝나지 않고 기다릴 때의 한 줄 (v10.34.0)
    — 예약 대기는 알리면서 모니터 대기는 조용하던 비대칭의 수리. 모니터는
    deadline 이 유계라 그 시각을 적는다."""
    live = monitors.list_all()
    latest = max((m.deadline_at for m in live), default=time.time())
    when = time.strftime("%m-%d %H:%M", time.localtime(latest))
    return (
        f"🔔 Waiting on {len(live)} monitor(s) — latest expiry {when}. "
        f"The run continues when a report arrives; Ctrl-C quits."
    )


def _schedule_wait_notice(schedules) -> str:
    """`run` 이 예약 때문에 끝나지 않고 기다릴 때의 한 줄."""
    n = sum(1 for s in schedules.list_all() if s.enabled)
    nxt = schedules.next_wake()
    when = nxt.strftime("%m-%d %H:%M") if nxt else "?"
    return f"⏰ {n} schedule(s) pending — next fire {when}. Ctrl-C quits."


def _missed_schedules_notice(schedules) -> str:
    """꺼져 있던 동안 지난 예약을 **실행하지 않고 알린다** (자동 실행 없음 —
    docs/schedule/DESIGN.md §6.5). `run` 에는 물을 화면이 없어 알리기만 한다."""
    schedules.settle()
    rows = [s for s in schedules.list_all() if s.missed_at]
    if not rows:
        return ""
    listing = "\n".join(f"  {s.label or s.cron} — {s.missed_at} ({s.id})" for s in rows)
    return (
        f"⏰ {len(rows)} missed schedule(s) were not run — choose Run now / Skip "
        f"in the web UI:\n{listing}"
    )


def _previous_monitors_notice(session_dir) -> str:
    """이전 세션의 모니터를 **복원하지 않고 알린다** (docs/monitor/DESIGN.md §8).

    부활을 자른 이유는 `monitor.registry.describe_previous` docstring 에 —
    요약하면 커서가 낡아 되감아도 건너뛰어도 틀리고, `monitors.json` 이
    워크스페이스 안이라 승인받지 않은 명령이 되살아날 수 있다.
    """
    if session_dir is None:
        return ""
    from agent_cli.monitor.registry import describe_previous

    rows = describe_previous(session_dir)
    if not rows:
        return ""
    listing = "\n".join(f"  {r}" for r in rows)
    return (
        f"🔔 {len(rows)} monitor(s) from the previous session were not restored — "
        f"register them again if needed:\n{listing}"
    )


_HARNESS_WAIT_NOTICE = "🔎 Waiting for the audit to finish — its result goes to main, and the process exits when that run ends"


def _auditor_boot_line(auditor) -> str:
    """감사자 부팅 상태 한 줄 — run 콘솔·web 상태 공용. 켜져 있을 때만 알린다
    (기본 꺼짐; web 은 🔎 칩으로 세션 도중 켜고 끈다). 빈 문자열 = 알릴 것 없음."""
    if auditor is None or not auditor.enabled:
        return ""
    return "🔎 Session auditor on (--auditor · AGENT_CLI_AUDITOR · 🔎 chip) — every user run is audited from its own records"


def _announce_agent_boot(
    renderer, revived: int, auto: int, stale: int = 0, *, auditor=None
) -> None:
    """web 부트스트랩의 teammate 재생성/auto-spawn/감사자 알림.

    v4.60.1 사고의 재발 방지 지점: 인라인 status() 오호출(시그니처)이
    worker 스레드를 부트에서 죽여 "큐만 쌓이고 소비 안 됨"으로 보였다 —
    실렌더러(Web/Minimal)로 단위 테스트되는 헬퍼로 추출.
    """
    if revived:
        renderer.status("running", f"🤝 {revived} resident agent(s) revived")
    if stale:
        # §3.9 — 되살리지 **못한** 건수 (묻거나 답할 쪽이 미복귀).
        renderer.status(
            "running",
            f"❓ {stale} unanswered question(s) not restored — the asking agent did not come back",
        )
    if auto:
        renderer.status("running", f"🤝 {auto} auto-spawn specialist(s) now resident")
    if _auditor_boot_line(auditor):
        renderer.status("running", _auditor_boot_line(auditor))


def _agent_mail_notice(reply: dict, busy: bool) -> None:
    """CLI: main 앞으로 메일이 왔다 (v10.51.0).

    web 이 입력창 위 대기열로 하는 일의 CLI 판 — 터미널은 찍은 줄을 지울 수
    없어서 **main 이 바쁠 때만** 한 줄 찍는다. 유휴면 곧 깨우기 런이 열려
    관찰 줄이 바로 나오므로 따로 알릴 것이 없다. 줄의 글은 곧 들어갈 관찰의
    머리줄(``mail_queue_item``)이라, 종류마다 문구를 손으로 짓지 않는다
    (종전엔 사망 통지가 기본 문구 "회신 도착" 으로 떨어졌다). v10.77.0: 에이전트가
    main 에게 묻는 질문 메일은 없어졌다(질문은 사람에게만)."""
    from agent_cli.render import get_renderer
    from agent_cli.subagent.agents_live import mail_queue_item

    if not busy:
        return
    kind = reply.get("kind", "reply")
    it = mail_queue_item(reply)
    line = f"⏳ Queued · [{it['nickname']}] {it['text']} — delivered to main when this turn ends"
    try:
        get_renderer().agent_mail_hint(key=reply.get("key", ""), kind=kind, text=line)
    except Exception:
        pass  # 알림은 best-effort


def _write_result_file(path: str, answer) -> None:
    """``--result-file`` — 성공 답변만 원문 기록 (실패/무답/빈 답=파일 미생성).

    원자적으로 쓴다(같은 디렉터리의 temp → ``os.replace``): 파일을 폴링하는
    소비자가 반쪽을 읽지 않게 — ``status.json`` 과 같은 관례(v10.34.1).
    """
    if not path or answer is None or not str(answer):
        return
    target = Path(path)
    try:
        import tempfile

        fd, tmp = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent or ".")
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(str(answer))
        os.replace(tmp, target)
    except OSError as e:
        console.print(
            f"--result-file write failed: {e}",
            style=C["error"],
            markup=False,
            highlight=False,
            soft_wrap=True,
        )


def _finalize_run(spec, stores, owners, *, warn_stuck: bool) -> None:
    """Finalize session after run command — 공용 정리(`runtime.close_session` →
    `teardown_session`: 생산자 정지→registry 종료→스피너→MCP 해제→세션 저장 순서를
    한 곳이 소유) + run 표면의 resume 힌트 출력."""
    from agent_cli.runtime import close_session

    close_session(spec, stores, owners, warn_stuck=warn_stuck)
    session = spec.session
    if session is None:
        return
    console.print(
        f"[{C['muted']}]Session {session.session_id} saved. "
        f"Resume with: agent-cli run/web --resume {session.session_id}[/]"
    )


@app.command()
def setup():
    """Configure provider, model, and connection. Runs automatically on first use. Re-run anytime to change settings."""
    from agent_cli.setup import SetupWizard

    SetupWizard().run()


# ── agent-cli mcp ─────────────────────────────────────────────
# 서브커맨드 그룹 (v9.1.0, docs/mcp-ui 시안). 인자 없이 `agent-cli mcp` 면
# 목록+진단 — setup 처럼 대화형이라 로직은 mcp/wizard.py 가 소유하고 여기는
# 진입점만. 저장 위치는 .agent-cli/mcp.json 하나 (v9.0.0 스코프 정리).
mcp_app = typer.Typer(
    name="mcp",
    help="Register and diagnose MCP servers. Without arguments, connects to every registered server and shows its status.",
    invoke_without_command=True,
    no_args_is_help=False,
    add_completion=False,
    pretty_exceptions_show_locals=False,  # 위 `app` 과 같은 이유 (v10.123.2)
)
app.add_typer(mcp_app, name="mcp")


@mcp_app.callback(invoke_without_command=True)
def mcp_root(
    ctx: typer.Context,
    no_test: bool = typer.Option(
        False,
        "--no-test",
        help="List the config only, without connecting (for slow servers)",
    ),
):
    """List the registered MCP servers and check that each one connects."""
    if ctx.invoked_subcommand is not None:
        return
    from agent_cli.mcp.wizard import McpWizard

    failures = McpWizard().list(test=not no_test)
    raise typer.Exit(code=1 if failures else 0)


@mcp_app.command("add")
def mcp_add():
    """Add a server interactively; it is connected to before it is saved."""
    from agent_cli.mcp.wizard import McpWizard

    raise typer.Exit(code=0 if McpWizard().add() else 1)


@mcp_app.command("test")
def mcp_test(name: str = typer.Argument(..., help="server name")):
    """Connect to one registered server and list its tools."""
    from agent_cli.mcp.wizard import McpWizard

    raise typer.Exit(code=0 if McpWizard().test(name) else 1)


@mcp_app.command("remove")
def mcp_remove(name: str = typer.Argument(..., help="server name")):
    """Remove a server from .agent-cli/mcp.json."""
    from agent_cli.mcp.wizard import McpWizard

    raise typer.Exit(code=0 if McpWizard().remove(name) else 1)


def _knob_state(name: str, server) -> dict | None:
    from agent_cli.knobs import KNOB_BY_NAME, web_state

    knob = KNOB_BY_NAME.get(name)
    return web_state(knob, server.knob_session()) if knob else None


@app.command()
def knobs(
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Print the knob table as JSON (for tools like agent-board).",
    ),
):
    """List the session knobs — one table for run flags, env and the web chips."""
    import json

    from agent_cli.knobs import table_spec

    spec = table_spec()
    if as_json:
        typer.echo(json.dumps(spec, ensure_ascii=False, indent=2))
        return
    from rich.table import Table

    table = Table(show_header=True, header_style=C["accent"], box=None, pad_edge=False)
    for col in ("knob", "flag", "env", "kind", "default", "label"):
        table.add_column(col)
    for k in spec:
        choices = "|".join(k["choices"]) if k["choices"] else ""
        table.add_row(
            k["name"],
            k["flag"] + (f" / {k['short']}" if k["short"] else ""),
            k["env"],
            k["kind"] + (f" ({choices})" if choices else ""),
            "" if k["default"] is None else str(k["default"]),
            k["ui"]["icon"] + " " + k["label"] if k["ui"]["icon"] else k["label"],
        )
    console.print(table)


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
    # 경로가 든 줄은 마크업으로 읽지 않고(`[x]` 가 든 경로) 80칸에서 끊지 않는다 — 복사해 쓸 수
    # 있도록 (v10.127.6, 1-A·1-B 의 경로 줄과 같은 방식).
    if not session_list:
        console.print(
            f"No sessions found for {ws}",
            style=C["muted"],
            markup=False,
            highlight=False,
            soft_wrap=True,
        )
        return

    console.print(
        f"\nSessions for {ws}:\n",
        style=C["accent"],
        markup=False,
        highlight=False,
        soft_wrap=True,
    )
    for s in session_list:
        _print_session(s)
    console.print()


@app.command()
def stats(
    paths: Annotated[
        list[Path] | None,
        typer.Argument(
            help="Directories (or turns.jsonl files) to scan recursively. "
            "Default: this project's sessions directory.",
            show_default=False,
        ),
    ] = None,
    since: str | None = typer.Option(
        None, "--since", help="Only count records on or after this day (YYYY-MM-DD)."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print one JSON object."),
    out: Annotated[
        Path | None,
        typer.Option(
            "--out", help="Write the output to this file instead of the terminal."
        ),
    ] = None,
):
    """Summarize turn records as counts only — no prompts, responses or paths.

    Reads only files named turns.jsonl (symlinks are not followed) and needs no
    model server or config, so the output can be checked and shared. "sessions"
    counts turns.jsonl files, so an agent's own record counts as one."""
    import json
    from datetime import date
    from typing import NoReturn

    from agent_cli.paths import sessions_dir
    from agent_cli.recovery.stats import collect, format_table

    def fail(msg: str) -> NoReturn:
        console.print(
            msg, style=C["error"], markup=False, highlight=False, soft_wrap=True
        )
        raise typer.Exit(2)

    if since is not None:
        try:
            ok = re.fullmatch(r"\d{4}-\d{2}-\d{2}", since) and date.fromisoformat(since)
        except ValueError:
            ok = False
        if not ok:
            fail(f"--since must be a day as YYYY-MM-DD, got {since!r}")
    roots = list(paths) if paths else [sessions_dir()]
    for root in roots:
        # 직접 준 PATH 가 링크면 조용히 0건이 아니라 사실을 말한다(트리 안의 링크는 조용히 건너뜀).
        if root.is_symlink():
            fail(f"Not following a symbolic link: {root}")
        if not root.exists():
            fail(f"No such path: {root}")
    data = collect(roots, since=since)
    text = (
        json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        if as_json
        else format_table(data)
    )
    if out is None:
        typer.echo(text, nl=False)
        return
    try:
        out.write_text(text, encoding="utf-8")
    except OSError as e:
        fail(f"Cannot write {out}: {e.strerror or type(e).__name__}")
    console.print(
        f"Wrote stats to {out}", markup=False, highlight=False, soft_wrap=True
    )


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _print_session(s, indent: str = "  ") -> None:
    """Print one session's summary block — id/time + last user request +
    last result (or 'in progress'). Shared by the ``sessions`` command and
    the resume prompt so both show the same format. ``session_summary`` reads
    the last user↔complete pair from history (the old ``query`` field is gone).

    답이 없는 세션은 쥔 프로세스가 살아 있을 때만 ``(in progress)`` (v10.127.2) —
    `session_lock.session_is_open`. 닫혀 있으면 ``(no answer)``(종전엔 LLM 실패로 끝난
    세션까지 전부 "in progress" 였다).
    """
    from agent_cli.context.session import get_session_dir, session_summary
    from agent_cli.context.session_lock import session_is_open

    console.print(
        f"{indent}[{C['accent']}]{s.session_id}[/] [{C['muted']}]{s.updated_at}[/]"
    )
    # 요청·결과는 사용자·모델이 쓴 글이라 마크업으로 읽지 않는다 (v10.127.8) — `[bold]` 가
    # 서식으로 먹히고 `[/x]` 는 MarkupError 로 `sessions`·resume 프롬프트를 죽였다. 80자로
    # 자른 요약이라 soft_wrap 은 켜지 않는다(종전처럼 터미널 폭에서 rich 가 줄을 바꾼다).
    user, result = session_summary(s)
    if user:
        console.print(
            f"{indent}    ↳ {_truncate(user, 80)}",
            style=C["muted"],
            markup=False,
            highlight=False,
        )
    if result == "(no completion)":
        state = "in progress" if session_is_open(get_session_dir(s)) else "no answer"
        console.print(f"{indent}    [{C['muted']}]→ ({state})[/]")
    elif result:
        console.print(
            f"{indent}    → {_truncate(result, 80)}",
            style=C["final"],
            markup=False,
            highlight=False,
        )


def _maybe_resume_recent(workspace: str, prompt_fn) -> tuple:
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
    return create_session(), False


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
            f"[red]Could not fetch latest release:[/red] {escape(r.stderr.strip()[:200])}"
        )
        raise typer.Exit(1)
    latest = r.stdout.strip()
    if not latest:
        console.print("No releases found.")
        raise typer.Exit(1)

    console.print(
        f"current: [cyan]v{current}[/cyan]   latest: [cyan]{escape(latest)}[/cyan]"
    )
    if _parse_version(latest) <= _parse_version(current):
        console.print("[green]✓ Already up to date.[/green]")
        raise typer.Exit(0)

    console.print(f"[yellow]Update available: v{current} → {escape(latest)}[/yellow]")
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
            console.print(
                f"[red]Download failed:[/red] {escape(dl.stderr.strip()[:200])}"
            )
            raise typer.Exit(1)
        pip = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", str(wheels[0])],
            check=False,
        )
        if pip.returncode != 0:
            console.print("[red]pip install failed.[/red]")
            raise typer.Exit(1)
    console.print(
        f"[green]✓ Updated to {escape(latest)}.[/green] Restart agent-cli to use it."
    )


@app.command()
@knob_options
def web(
    knob_args: dict | None = None,  # `@knob_options` 가 채운다 (v10.43.0)
    provider: str | None = typer.Option(
        None,
        "--provider",
        "-p",
        help="LLM provider: openai | anthropic (default: openai)",
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
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    record_turns: bool = typer.Option(True, "--record-turns/--no-record-turns"),
    dialect: str | None = typer.Option(
        None,
        "--dialect",
        help=_DIALECT_HELP,
    ),
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Bind address (default: 127.0.0.1 — loopback only). Use "
        "--host 0.0.0.0 to expose on the LAN (opt-in; the web UI can drive "
        "the shell, so only do this on a trusted network).",
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
    title: str = _TITLE_OPTION,
) -> None:
    """Start a browser room for one agent session.

    Any number of browser viewers share the session, and every viewer can send
    input. It binds to 127.0.0.1 unless --host says otherwise. A token is
    generated at startup unless --token is given, and the URL carrying it is
    printed (and opened in a local browser unless --no-browser).
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
        from agent_cli.extras import install_hint

        # markup=False — 요구 사항의 `uvicorn[standard]` 가 rich 태그로 삼켜지지 않게
        # (v10.123.3; 종전 `agent-cli[web]` 이 그렇게 `agent-cli` 만 보였다).
        # soft_wrap=True — 설치 명령은 80칸을 넘는다; rich 가 줄을 끊으면 복사한 명령이
        # 끊긴 자리에서 실행된다. 접기는 터미널에 맡긴다.
        console.print(
            f"Missing optional dependency for 'web' command: {e}.",
            style="red",
            markup=False,
            soft_wrap=True,
        )
        console.print(
            install_hint("web"), markup=False, highlight=False, soft_wrap=True
        )
        raise typer.Exit(code=1)

    from agent_cli.render import set_renderer

    # C4: run/web 공용 부트스트랩. --resume pre-check(fail-fast)가 provider
    # 핸드셰이크보다 먼저 — 로드된 SessionMeta 를 그대로 재사용(재로드 제거).
    session_resumed = _load_resume_session(resume) if resume else None
    binding = _bootstrap_provider(
        provider,
        _boot_model(knob_args),
        base_url,
        api_key,
        dialect,
        max_context_tokens,
        quiet=True,
        explicit_model="model" in (knob_args or {}),
    )

    # MCP servers (v4.46.0: run 과 동형 배선 — 이전엔 web 만 미지원이었음)
    mcp_manager, mcp_tools = _setup_mcp()
    if mcp_tools:
        from agent_cli.tools import TOOLS

        TOOLS.update(mcp_tools)

    # 2. Session + ContextManager.
    import sys

    from agent_cli.context.session import (
        get_session_dir,
        save_meta,
    )

    if session_resumed is not None:
        session = session_resumed
        is_resume = True
    else:
        # No --resume: offer the most recent session ([y/N]) or start new.
        prompt_fn = input if sys.stdin.isatty() else None
        session, is_resume = _maybe_resume_recent(os.getcwd(), prompt_fn)
        if is_resume:
            console.print(f"[{C['accent']}]Resuming session {session.session_id}[/]")
    _apply_title(session, title)
    _claim_session(session)
    save_meta(session)
    ctx = _build_context(session, binding, resume=is_resume)

    # 3. Renderer + server + worker thread.
    renderer = WebRenderer(
        workspace=session.workspace,
        session_dir=str(get_session_dir(session)),  # writes status.json here
        title=session.title,
    )
    set_renderer(renderer)
    # 모델 시점의 첫 뷰 (v10.6.0): resume 로 복원된 캐시(압축 경계·요약)를 첫
    # 뷰어가 바로 본다 — 복원은 렌더러보다 먼저 일어나 통지를 못 받았다.
    renderer.context_view(ctx.context_view())
    # Pass the live ctx so the Prompt Inspector can show the dynamic context
    # (conversation + observations), not just the static system prompt.
    server = WebServer(
        renderer,
        token=token,
        ctx=ctx,
        trust_local=trust_local,
        base_path=base_path,
        # 세션의 모델 바인딩 (v10.43.0) — ✨ directive 생성·🧠/📐 칩의 능력 조회가
        # 복사본이 아니라 이걸 읽는다.
        binding=binding,
    )

    # Prime the session-info ``ready`` so a client opening the page
    # before the first chat turn already sees the top-bar populated.
    # AgentLoop calls ``header()`` again on each user message; the
    # WebRenderer slot-replaces, so the buffer doesn't accumulate.
    renderer.header(binding.provider_name, binding.model, max_turns)

    # On resume, fold prior turns back into the persistent event
    # buffer BEFORE any SSE client connects so the snapshot replay
    # carries the conversation history. Replay walks ``ctx``'s raw
    # cache (already populated by ``ContextManager(..., resume=True)``)
    # and re-emits the same event sequence the live loop would have
    # produced — see :meth:`WebRenderer.replay_from_history`.
    if is_resume:
        # ONE interleaved replay: history turns + the scopes.jsonl sidecar in
        # timestamp order. Scope events have no counterpart in ctx's message
        # history (hence the sidecar), and their ORDER relative to the turns is
        # what rebuilds the nesting — a scope's card must be open before its own
        # turns arrive, or they fall back to the main timeline and the resumed
        # session shows no skill/agent cards at all.
        renderer.replay_session(ctx)

    # Populate the Prompt Inspector's system prompt BEFORE the first message
    # (the loop only captures on an LLM call). With ctx already restored, the
    # inspector then shows both the system prompt and the dynamic context the
    # moment the drawer opens — no need to send a message first.
    from agent_cli.web.inspector import capture_startup_system_prompt

    # resume: 파일로 남긴 틀(맨 위 카드·꼬리 띠·서브 스코프의 뷰)을 되살린다.
    # main 을 되살렸으면 캡처를 건너뛴다 — 화면은 "마지막 모델 호출 기준" 이고,
    # 새로 조립한 프롬프트로 덮으면 그 호출의 꼬리가 사라진다.
    if not (is_resume and renderer.restore_prompt_views()):
        capture_startup_system_prompt(
            renderer,
            capabilities=binding.capabilities,
            dialect=binding.dialect,
            session_dir=str(ctx.session_dir),
            max_depth=max_depth,
        )

    from agent_cli.pump import ForeverPolicy
    from agent_cli.runtime import (
        SessionSpec,
        build_session_owners,
        build_session_stores,
        wire_session,
    )
    from agent_cli.web.slash import WebDispatchOutput
    from agent_cli.web.surface import WebSurface

    # 디스크 훅(.agent-cli/hooks.json)은 spec 을 만들 때 읽는다 — run 과 같은 배선
    # (v8.39.0 수리: 종전 web 만 로드하지 않아 채팅 턴·상주 에이전트에서 shell 훅이
    # 조용히 미발화했다).
    spec = SessionSpec.for_session(
        binding=binding,
        session=session,
        ctx=ctx,
        mcp_manager=mcp_manager,
        knob_args=knob_args,
        max_turns=max_turns,
        max_depth=max_depth,
        verbose=verbose,
        record_turns=record_turns,
    )
    # 입력 큐는 서버가 들고 있다 — 배선은 run 과 같다 (v10.130.8)
    input_queue = server.queue
    # 저장소는 worker 부트스트랩을 기다리지 않고 여기서 조립한다(`--idle-timeout`
    # 술어가 첫 메시지 전에도 모니터·예약을 봐야 한다). 🧵 칩·status.json ``jobs``
    # (v10.121.0)와 예약 칩의 관찰자는 렌더러의 것 — run 은 화면이 없어 꽂지 않는다.
    stores = build_session_stores(
        spec,
        queue=input_queue,
        on_jobs_change=renderer.jobs_changed,
        on_schedules_change=renderer.broadcast_schedules_changed,
    )
    # teammate P1: 세션 소유자(레지스트리·감사자)는 worker 가 만들고(nonlocal) 서버
    # teardown 이 정리한다 — 만들기 전에 죽었으면 None.
    owners = None

    def _worker_loop() -> None:
        """Assemble the session and drive the session pump in a background
        thread (v10.33.1 — the loop itself is ``pump.SessionPump``, shared
        with ``run``; what differs is ``WebSurface`` + ``ForeverPolicy``).

        Each ``run_loop`` invocation reuses the same ``ctx``, so history
        accumulates across messages — the web UI is the interactive,
        multi-turn surface. Dispatch order per message (``WebSurface.route``):
          1. ``handle_slash_command`` — web-specific stateless cmds
             (``/help``, ``/sh``, ``/compact``)
          2. ``try_dispatch_agent_or_skill`` — shared with ``run``,
             covers ``@``/``/`` listings + invocations + not-found
          3. Otherwise the message is a conversation turn → ``run_loop``.
        """
        nonlocal owners
        web_output = WebDispatchOutput(renderer)
        # 서버 수명의 레지스트리 — 매 런에 주입되어 teammate 가 런 경계를 넘어
        # 생존한다. 감사 런 진행 중 → status.json `auditing` (board "결과 확인 중").
        owners = build_session_owners(
            spec, stores, renderer=renderer, on_harness_change=renderer.note_harness
        )
        registry = owners.agent_registry

        # 서버 손잡이 — 한 블록 (v10.130.7). 제약은 하나: 에이전트가 살아나기(복원·
        # auto-spawn, 아래 `wire_session`) 전에 서버가 레지스트리를 안다. 그 전까지
        # 핸들러는 None 을 견딘다(에이전트 창 503·🔌 빈 목록·소유자 없는 노브 거절).
        # P4: 대화 창의 인간 개입(input/kill) 엔드포인트에 레지스트리 연결.
        server.agent_registry = registry
        server.mcp_manager = mcp_manager  # 🔌 칩 (읽기 전용, v9.2.0)
        server.auditor = owners.auditor  # 🔎 칩(`/api/knobs/auditor`)이 켜고 끈다
        # 감사 진행 수가 바뀌면 렌더러가 🔎 상태 한 장을 다시 받아 싣는다 (v10.45.0)
        renderer.knob_state = lambda name: _knob_state(name, server)
        # 노브 전부의 초기 sticky — 칩·status.json(문법)이 첫 요청 전에도 사실을 본다.
        # 📐 sticky 는 부트 초기값이 이미 나갔으니 적용 뒤 다시 낸다.
        from agent_cli.knobs import web_states

        renderer.broadcast_knobs(web_states(server.knob_session()))
        surface = WebSurface(
            renderer=renderer,
            server=server,
            ctx=ctx,
            binding=binding,
            dispatch=_command_dispatch(
                spec,
                owners,
                lambda: web_output,
                graceful_interrupt=WebSurface.graceful_interrupt,
            ),
        )
        # 세션 펌프 (v10.33.1, docs/pump/DESIGN.md): run 과 같은 루프 —
        # 수명은 ForeverPolicy(서버 종료의 SHUTDOWN 까지), 표면은 WebSurface.
        wired = wire_session(
            spec,
            stores,
            owners,
            queue=input_queue,
            surface=surface,
            policy=ForeverPolicy(),
        )
        # 미배달 메일은 입력창 위 대기열에 사용자 요청과 같은 줄로 선다(v10.51.0)
        server.mail_source = registry.pending_mail
        registry.on_pending_change = server.broadcast_queue
        server.broadcast_queue()  # resume 이 되살린 미배달 메일도 줄을 선다
        _announce_agent_boot(
            renderer,
            wired.revived,
            wired.auto,
            registry.stale_questions,
            auditor=owners.auditor,
        )
        _mon_notice = _previous_monitors_notice(spec.session_dir)
        if _mon_notice:
            renderer.status("running", _mon_notice)
        wired.pump.run()

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
                    f"this session needs a restart (see instance.log)",
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
    console.print(
        f"\n[bright_cyan]agent-cli web[/]  "
        f"({escape(binding.provider_name)} · {escape(binding.model)})"
    )
    if session.title:
        console.print(f"  Title:   {session.title}", markup=False, highlight=False)
    console.print(f"  UI:      [yellow]{escape(ui_url)}[/]", soft_wrap=True)
    console.print(f"  Token:   [yellow]{server.token}[/]")
    console.print(f"  Session: [{C['muted']}]{session.session_id}[/]\n")

    # Instance file: tell an external orchestrator where this session's web is
    # (host/port/token/pid). Written on start, removed in finally on exit (and
    # self-reaped via --idle-timeout), so the board can spawn-or-attach by
    # reading one file. Lives next to history.jsonl in the session dir.
    from agent_cli.context.session import get_session_dir
    from agent_cli.instance_file import (
        remove_instance_file,
        remove_status_file,
        write_instance_file,
        write_status_file,
    )

    _session_dir = get_session_dir(session)
    write_instance_file(
        _session_dir,
        session_id=session.session_id,
        host=host,
        port=resolved_port,
        token=server.token,
        title=session.title,
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
            f"  [{C['muted']}](remote bind — browser not opened automatically; use the URL above)[/]"
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
            is_active=lambda: web_instance_is_active(
                renderer,
                server,
                owners.agent_registry if owners is not None else None,
                stores.monitors,
                stores.schedules,
                harness=owners.auditor if owners is not None else None,
                jobs=stores.jobs,
            ),
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
        renderer.hub.shutdown_all()
        server.shutdown()
        worker.join(timeout=2.0)
        console.print(f"[{C['muted']}]Saving session...[/]")
        # 공용 정리 (v8.39.0 조립기 → v10.131.0 `close_session`): 생산자 정지 →
        # 상주 에이전트 전원 종료 → MCP 해제 → 세션 저장 — run 과 같은 시퀀스 소유자.
        try:
            from agent_cli.runtime import close_session

            close_session(spec, stores, owners, warn_stuck=False)
            console.print(f"[{C['muted']}]Session {session.session_id} saved.[/]")
        except Exception as exc:
            console.print(
                f"Failed to save session: {exc}",
                style="red",
                markup=False,
                highlight=False,
            )
