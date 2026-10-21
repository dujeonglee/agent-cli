"""``agent-cli mcp`` — MCP 서버 등록·진단 (v9.1.0, 시안 docs/mcp-ui).

종전엔 ``.agent-cli/mcp.json`` 을 손으로 짜야 했고, 틀리면 **stderr 경고 한 줄만
남기고 도구가 조용히 사라졌다**. 틀리기 쉬운 지점이 실제로 있다 — 전송 방식이
``url`` 키의 유무로 갈리고, ``${VAR}`` 는 ``env`` 안에서만 치환되며, 미정의
변수는 에러가 아니라 빈 문자열이 되어 연결은 성공하고 인증만 실패한다.

이 마법사의 두 축:

- **저장 전에 실제로 연결해 보고 도구 개수를 센다.** 붙지 않는 설정은
  기본적으로 파일에 남기지 않는다.
- **env 변수는 현재 셸에 값이 있는지 확인해 보여준다.** 오타가 저장 전에
  드러난다.

저장 위치는 ``.agent-cli/mcp.json`` 하나다 (v9.0.0 — docs/config-scopes).
``~/.agent-cli/mcp.json`` 이 남아 있으면 **가져올지 묻는다** — 경고 없이 간
스코프 정리의 마이그레이션 경로 하나를 마법사가 흡수한다.

구조는 ``setup.SetupWizard`` 와 같다: 순수 헬퍼(파일 I/O·프로브·마스킹)는
모듈 함수로 두어 프롬프트 없이 테스트하고, ``McpWizard`` 는 그 위의 대화만
담당한다.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

from agent_cli.fsio import atomic_write_json
from agent_cli.mcp.client import humanize_error
from agent_cli.mcp.config import _parse_server_config
from agent_cli.paths import project_dir, user_dir

console = Console()

# 연결 테스트 상한. ``session.initialize()`` 자체엔 타임아웃이 없어 응답 없는
# 서버가 마법사를 영원히 세운다. 넉넉한 이유: ``npx -y`` 첫 실행은 패키지
# 설치를 겸해 수십 초가 걸릴 수 있고, 마법사는 정확히 그 "첫 실행" 자리다.
PROBE_TIMEOUT_S = 45.0

# 사용자가 고르는 건 **확실히 아는 것**(로컬이냐 원격이냐)뿐. 원격의 HTTP
# 세대(streamable-http vs 구 sse)는 문서의 URL 만 봐서는 대개 알 수 없으므로
# 묻지 않고 **연결해 보며 판별**한다 (v9.3.0) — 마법사는 어차피 저장 전에
# 붙어 보므로 추가 비용이 사실상 없다. 판별 결과는 파일에 명시 저장한다.
_TRANSPORTS = (
    ("stdio", "local process (npx, uvx, an executable)"),
    ("remote", "remote HTTP server (transport auto-detected)"),
)

# 판별 순서: 현재 권장 규격 먼저, 구 규격 폴백.
_REMOTE_ORDER = ("streamable-http", "sse")


# ── 순수 헬퍼 (프롬프트 없음) ──────────────────────────────


def mcp_json_path() -> Path:
    """``.agent-cli/mcp.json`` — 호출 시점 cwd 기준. ``config._MCP_CONFIG_PATHS``
    는 import 시점 고정이라 대화형 명령엔 호출 시점 평가가 맞다."""
    return project_dir() / "mcp.json"


def legacy_user_mcp_path() -> Path:
    """v9.0.0 전에 읽던 ``~/.agent-cli/mcp.json`` — 이제 읽지 않는다.
    마법사가 가져오기를 제안하는 용도로만 본다."""
    return user_dir() / "mcp.json"


def read_servers(path: Path) -> dict[str, dict]:
    """``mcpServers`` 원본 dict. 없거나 깨졌으면 빈 dict — 깨진 파일은
    저장 시 통째로 덮어쓰지 않도록 호출자가 ``path.exists()`` 로 구분한다."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    servers = data.get("mcpServers", {}) if isinstance(data, dict) else {}
    return {k: v for k, v in servers.items() if isinstance(v, dict)}


def write_servers(path: Path, servers: dict[str, dict]) -> None:
    """``{"mcpServers": {...}}`` 로 원자적 저장 (다른 최상위 키는 보존)."""
    data: dict = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (json.JSONDecodeError, OSError):
            data = {}
    data["mcpServers"] = servers
    atomic_write_json(path, data, indent=2)


def save_server(name: str, entry: dict, path: Path | None = None) -> Path:
    path = path or mcp_json_path()
    servers = read_servers(path)
    servers[name] = entry
    write_servers(path, servers)
    return path


def remove_server(name: str, path: Path | None = None) -> bool:
    path = path or mcp_json_path()
    servers = read_servers(path)
    if name not in servers:
        return False
    del servers[name]
    write_servers(path, servers)
    return True


def mask_secret(value: str) -> str:
    """토큰을 화면에 낼 때 — 앞 4·뒤 4만. 짧으면 전부 가린다."""
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:4]}{'•' * 8}{value[-4:]}"


def env_ref_status(value: str) -> tuple[str | None, bool, str]:
    """``${VAR}`` 참조면 ``(VAR, 현재 셸에 있나, 마스킹 값)``, 아니면
    ``(None, True, "")``. 미정의는 **에러가 아니라 빈 문자열**이 되어 연결은
    성공하고 인증만 실패하므로 여기서 미리 보여주는 게 이 함수의 존재 이유."""
    v = value.strip()
    if not (v.startswith("${") and v.endswith("}")):
        return None, True, ""
    var = v[2:-1]
    raw = os.environ.get(var)
    return var, raw is not None, mask_secret(raw or "")


def resolve_command(cmd: str) -> str | None:
    """PATH 에서 실행 파일을 찾는다 — 못 찾으면 연결 테스트가 ENOENT 로 끝나니
    그 전에 알려준다."""
    return shutil.which(cmd) if cmd else None


def probe_server(
    name: str, entry: dict, timeout: float = PROBE_TIMEOUT_S, *, warn: bool = False
) -> tuple[bool, str, list[str], float]:
    """실제로 연결해 도구 목록을 받아 본다.

    Returns ``(ok, message, tool_names, seconds)``. 실패 message 는 사람이
    다음에 뭘 할지 알 수 있게 원인별로 다르게 쓴다. 연결은 프로브 후 항상
    끊는다 — 마법사가 띄운 서버 프로세스를 남기지 않는다."""
    from agent_cli.mcp.client import McpClientManager

    cfg = _parse_server_config(name, entry)
    if not (cfg.is_stdio or cfg.is_remote):
        return False, "either command (stdio) or url (remote) is required", [], 0.0
    if cfg.is_stdio and resolve_command(cfg.command) is None:
        return (
            False,
            f"executable not found on PATH: {cfg.command}",
            [],
            0.0,
        )

    manager = McpClientManager()
    t0 = time.perf_counter()
    try:
        results = _connect_with_timeout(manager, {name: cfg}, timeout, warn=warn)
        status = results.get(name, "error: unknown")
        if status != "connected":
            return False, humanize_error(status, cfg), [], time.perf_counter() - t0
        tools = [t.name for t in manager.list_tools(name)]
        return True, "connected", tools, time.perf_counter() - t0
    except TimeoutError:
        return (
            False,
            (
                f"no response within {int(timeout)}s — the server started but does not "
                "answer initialize (check the package name and arguments)"
            ),
            [],
            time.perf_counter() - t0,
        )
    finally:
        try:
            manager.disconnect_all()
        except Exception:
            pass


def _connect_with_timeout(
    manager, configs: dict, timeout: float, *, warn: bool = False
) -> dict[str, str]:
    """``connect_all`` 은 서버별 예외를 삼켜 status 문자열로 돌려주지만 타임아웃이
    없다. 여기서만 상한을 건다 — 부팅 경로(``_setup_mcp``)는 느린 첫 실행을
    기다려야 하므로 건드리지 않는다.

    매니저가 자기 이벤트 루프(``_run_sync``)를 들고 있어 asyncio 로 감싸면
    루프가 겹친다. **데몬 스레드 + join(timeout)** 이 단순하고, 타임아웃 뒤
    스레드가 남아도 데몬이라 프로세스 종료를 막지 않는다."""
    import threading

    box: dict = {}

    def _work():
        try:
            box["result"] = manager.connect_all(configs, warn=warn)
        except BaseException as e:  # 스레드 경계 — 결과로 실어 보낸다
            box["error"] = e

    t = threading.Thread(target=_work, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError
    if "error" in box:
        raise box["error"]
    return box["result"]


# ── 대화형 ──────────────────────────────────────────────


class McpWizard:
    """``agent-cli mcp`` 의 네 동작. 프롬프트는 전부 이 클래스 안에만."""

    def __init__(self, console_: Console | None = None):
        self.console = console_ or console

    # ── mcp (목록 + 진단) ──
    def list(self, *, test: bool = True) -> int:
        """등록 서버 표. ``test=True`` 면 하나씩 실제로 붙여 본다 — 지금까지
        stderr 한 줄에만 있던 실패 이유를 여기서 본다. 반환값은 실패 수."""
        path = mcp_json_path()
        servers = read_servers(path)
        # 목록은 **진단 명령**이라 스크립트·CI 에서 돈다 — 파이프 뒤에서
        # 프롬프트에 걸려 멈추면 안 된다(실장 검증에서 잡힘). 가져오기 제안은
        # TTY 일 때만; `mcp add` 는 원래 대화형이라 항상 제안한다.
        if sys.stdin.isatty():
            self._offer_import(servers, path)
            servers = read_servers(path)  # import 후 재로드
        if not servers:
            self.console.print(
                f"[dim]No MCP servers registered ({path}).[/]\n"
                "  Add one with [bold]agent-cli mcp add[/]."
            )
            return 0

        table = Table(box=None, pad_edge=False, show_header=True)
        table.add_column("", width=1)
        table.add_column("name", style="bold")
        table.add_column("transport")
        table.add_column("status")
        table.add_column("tools", justify="right")
        failures = 0
        notes: list[str] = []
        for name, entry in servers.items():
            cfg = _parse_server_config(name, entry)
            if not test:
                table.add_row("·", name, cfg.transport, "[dim]unchecked[/]", "–")
                continue
            ok, msg, tools, secs = probe_server(name, entry)
            if ok:
                table.add_row(
                    "[green]●[/]",
                    name,
                    cfg.transport,
                    f"connected [dim]({secs:.1f}s)[/]",
                    str(len(tools)),
                )
            else:
                failures += 1
                table.add_row("[red]●[/]", name, cfg.transport, "[red]failed[/]", "–")
                notes.append(f"  [red]└ {name}: {msg}[/]")
        self.console.print(table)
        for n in notes:
            self.console.print(n)
        self.console.print(f"\n[dim]config file: {path}[/]")
        return failures

    # ── mcp add ──
    def add(self) -> bool:
        path = mcp_json_path()
        existing = read_servers(path)
        self._offer_import(existing, path)
        existing = read_servers(path)

        self.console.print("\n[bold]Add an MCP server[/]\n")
        name = self._ask_name(existing)
        if name is None:
            return False

        while True:
            entry = self._ask_transport_and_target()
            if entry is None:
                return False
            entry = self._ask_env(entry)
            ok = self._run_probe(name, entry)
            if ok:
                break
            choice = IntPrompt.ask(
                "   [1] fix the config  [2] save anyway  [3] cancel",
                default=1,
                choices=["1", "2", "3"],
            )
            if choice == 3:
                self.console.print("   [dim]Cancelled. The file was not changed.[/]")
                return False
            if choice == 2:
                break
            # 1 → 처음부터 다시 (이름은 유지)

        save_server(name, entry, path)
        self.console.print(f"\n   [green]✓[/] [bold]{name}[/] saved to {path}")
        self.console.print("   [dim]Applies from the next run.[/]")
        return True

    # ── mcp test <name> ──
    def test(self, name: str) -> bool:
        servers = read_servers(mcp_json_path())
        if name not in servers:
            self.console.print(f"[red]'{name}' is not registered.[/]")
            self._hint_names(servers)
            return False
        return self._run_probe(name, servers[name])

    # ── mcp remove <name> ──
    def remove(self, name: str) -> bool:
        path = mcp_json_path()
        if not remove_server(name, path):
            self.console.print(f"[red]'{name}' is not registered.[/]")
            self._hint_names(read_servers(path))
            return False
        self.console.print(f"[green]✓[/] {name} removed ({path})")
        return True

    # ── 내부 ──
    def _hint_names(self, servers: dict) -> None:
        if servers:
            self.console.print(f"  [dim]registered: {', '.join(servers)}[/]")

    def _offer_import(self, existing: dict, path: Path) -> None:
        """v9.0.0 전 ``~/.agent-cli/mcp.json`` 이 남아 있으면 가져올지 묻는다.
        경고 없이 간 스코프 정리의 마이그레이션 경로. 원본은 지우지 않는다 —
        이제 읽지 않을 뿐이다."""
        legacy = legacy_user_mcp_path()
        legacy_servers = read_servers(legacy)
        new = {k: v for k, v in legacy_servers.items() if k not in existing}
        if not new:
            return
        self.console.print(
            f"[yellow]ⓘ[/] {legacy} holds {len(new)} server(s) "
            f"([bold]{', '.join(new)}[/]). Since v9.0.0 this file is no longer read."
        )
        if Confirm.ask("   Import them into this project?", default=True):
            merged = {**existing, **new}
            write_servers(path, merged)
            self.console.print(f"   [green]✓[/] Copied {len(new)} to {path}.\n")

    def _ask_name(self, existing: dict) -> str | None:
        while True:
            name = Prompt.ask("   Server name").strip()
            if not name:
                self.console.print("   [red]The name cannot be empty.[/]")
                continue
            if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                # 도구 이름이 mcp__{서버}__{도구} 이고 함수 이름 규칙을 따른다 —
                # 다른 문자는 `_` 로 바뀌어 적은 이름과 달라진다
                self.console.print(
                    "   [red]Names may only use letters, digits, underscore (_) and hyphen (-).[/]"
                )
                continue
            if name in existing and not Confirm.ask(
                f"   '{name}' already exists. Overwrite?", default=False
            ):
                return None
            return name

    def _ask_transport_and_target(self) -> dict | None:
        self.console.print("\n   [bold]Transport[/]")
        for i, (key, desc) in enumerate(_TRANSPORTS, 1):
            self.console.print(f"     [cyan]{i})[/] {key:<5} — {desc}")
        choice = IntPrompt.ask("   Choice", default=1, choices=["1", "2"])
        transport = _TRANSPORTS[choice - 1][0]

        if transport == "stdio":
            self.console.print("\n   [bold]stdio settings[/]")
            command = Prompt.ask("   Command").strip()
            if not command:
                self.console.print("   [red]The command cannot be empty.[/]")
                return None
            resolved = resolve_command(command)
            if resolved:
                self.console.print(f"   [dim]{command} → {resolved}[/]")
            else:
                self.console.print(
                    f"   [yellow]⚠[/] [dim]{command} was not found on PATH — "
                    "the connection test may fail.[/]"
                )
            args_raw = Prompt.ask("   Arguments (space-separated)", default="").strip()
            entry: dict = {"command": command}
            if args_raw:
                entry["args"] = args_raw.split()
            return entry

        self.console.print("\n   [bold]Remote server[/]")
        url = Prompt.ask("   URL").strip()
        if not url:
            self.console.print("   [red]The URL cannot be empty.[/]")
            return None
        # transport 는 연결 테스트가 판별해 채운다 (_run_probe). 여기서
        # 기본값을 박으면 사용자에게 추측을 떠넘기는 셈이 된다.
        return {"url": url}

    def _ask_env(self, entry: dict) -> dict:
        self.console.print(
            "\n   [bold]Environment variables[/] [dim](empty line to finish)[/]"
        )
        env: dict[str, str] = {}
        while True:
            var = Prompt.ask("   Name", default="").strip()
            if not var:
                break
            value = Prompt.ask("   Value", default=f"${{{var}}}").strip()
            ref, present, masked = env_ref_status(value)
            if ref is not None:
                if present:
                    self.console.print(
                        f"     [green]✓[/] [dim]{ref} is set in the current shell ({masked})[/]"
                    )
                else:
                    self.console.print(
                        f"     [yellow]⚠[/] [dim]{ref} is not set in the current shell — at run time "
                        "it becomes an empty string (the connection works, only auth fails)[/]"
                    )
            env[var] = value
        if env:
            entry = {**entry, "env": env}
            self.console.print(
                "   [dim]A ${VAR} value keeps the token out of the file; it is read at run time.[/]"
            )
        return entry

    def _run_probe(self, name: str, entry: dict) -> bool:
        """연결 테스트. 원격인데 transport 가 비어 있으면 세대를 **판별**하고
        ``entry`` 에 확정해 넣는다(호출자가 그대로 저장) — v9.3.0."""
        self.console.print("\n   [bold]Connection test[/]")
        if "url" in entry and not entry.get("transport"):
            return self._probe_remote_autodetect(name, entry)

        cfg = _parse_server_config(name, entry)
        target = (
            f"{cfg.command} {' '.join(cfg.args)}".strip() if cfg.is_stdio else cfg.url
        )
        self.console.print(
            f"   [dim]Running {target}… (up to {int(PROBE_TIMEOUT_S)}s; "
            "a first npx run can take a while to install the package)[/]"
        )
        ok, msg, tools, secs = probe_server(name, entry)
        return self._render_probe(name, ok, msg, tools, secs)

    def _probe_remote_autodetect(self, name: str, entry: dict) -> bool:
        """현재 권장(streamable-http) → 구(sse) 순으로 붙여 본다.

        사용자는 문서에서 받은 URL 만 알지 그게 어느 HTTP 세대인지는 대개
        모른다. 틀리게 고르면 서버가 -32600 으로 거절하는데 그 메시지만으론
        원인이 전송 방식이라는 걸 알 수 없다 — 그래서 묻지 않고 판별한다."""
        self.console.print(f"   [dim]Connecting to {entry['url']}…[/]")
        failures: list[tuple[str, str]] = []
        for transport in _REMOTE_ORDER:
            attempt = {**entry, "transport": transport}
            self.console.print(f"     [dim]trying {transport}…[/]", end="")
            ok, msg, tools, secs = probe_server(name, attempt)
            if ok:
                self.console.print(" [green]●[/]")
                entry["transport"] = transport  # 판별 결과를 명시 저장
                self.console.print(
                    f"   [green]●[/] transport: [bold]{transport}[/] [dim](auto-detected)[/]"
                )
                return self._render_probe(name, True, msg, tools, secs)
            self.console.print(" [red]✗[/]")
            failures.append((transport, msg))
        self.console.print("   [red]✗[/] both transports failed")
        for transport, msg in failures:
            self.console.print(f"       [red]{transport}: {msg}[/]")
        return False

    def _render_probe(
        self, name: str, ok: bool, msg: str, tools: list[str], secs: float
    ) -> bool:
        if ok:
            self.console.print(f"   [green]●[/] connected [dim]({secs:.1f}s)[/]")
            self.console.print(f"   [green]●[/] [bold]{len(tools)}[/] tools")
            if tools:
                shown = " · ".join(tools[:5]) + (" · …" if len(tools) > 5 else "")
                self.console.print(f"       [dim]{shown}[/]")
            self.console.print(f"   [dim]The LLM calls these as {name}.<tool>.[/]")
            return True
        self.console.print(f"   [red]✗[/] connection failed [dim]({secs:.1f}s)[/]")
        self.console.print(f"       [red]{msg}[/]")
        return False
