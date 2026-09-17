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
import shutil
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

from agent_cli.fsio import atomic_write_json
from agent_cli.mcp.config import McpServerConfig, _parse_server_config
from agent_cli.paths import project_dir, user_dir

console = Console()

# 연결 테스트 상한. ``session.initialize()`` 자체엔 타임아웃이 없어 응답 없는
# 서버가 마법사를 영원히 세운다. 넉넉한 이유: ``npx -y`` 첫 실행은 패키지
# 설치를 겸해 수십 초가 걸릴 수 있고, 마법사는 정확히 그 "첫 실행" 자리다.
PROBE_TIMEOUT_S = 45.0

_TRANSPORTS = (
    ("stdio", "로컬 프로세스로 실행 (npx, uvx, 실행 파일)"),
    ("sse", "이미 떠 있는 HTTP 서버에 연결"),
)


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
    name: str, entry: dict, timeout: float = PROBE_TIMEOUT_S
) -> tuple[bool, str, list[str], float]:
    """실제로 연결해 도구 목록을 받아 본다.

    Returns ``(ok, message, tool_names, seconds)``. 실패 message 는 사람이
    다음에 뭘 할지 알 수 있게 원인별로 다르게 쓴다. 연결은 프로브 후 항상
    끊는다 — 마법사가 띄운 서버 프로세스를 남기지 않는다."""
    from agent_cli.mcp.client import McpClientManager

    cfg = _parse_server_config(name, entry)
    if not (cfg.is_stdio or cfg.is_sse):
        return False, "command(stdio) 또는 url(sse) 중 하나가 필요합니다", [], 0.0
    if cfg.is_stdio and resolve_command(cfg.command) is None:
        return (
            False,
            f"실행 파일을 PATH 에서 찾을 수 없습니다: {cfg.command}",
            [],
            0.0,
        )

    manager = McpClientManager()
    t0 = time.perf_counter()
    try:
        results = _connect_with_timeout(manager, {name: cfg}, timeout)
        status = results.get(name, "error: unknown")
        if status != "connected":
            return False, _humanize_error(status, cfg), [], time.perf_counter() - t0
        tools = [t.name for t in manager.list_tools(name)]
        return True, "연결됨", tools, time.perf_counter() - t0
    except TimeoutError:
        return (
            False,
            (
                f"{int(timeout)}s 안에 응답이 없습니다 — 서버가 뜨긴 했지만 초기화에 "
                "답하지 않습니다 (패키지 이름·인자를 확인하세요)"
            ),
            [],
            time.perf_counter() - t0,
        )
    finally:
        try:
            manager.disconnect_all()
        except Exception:
            pass


def _connect_with_timeout(manager, configs: dict, timeout: float) -> dict[str, str]:
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
            box["result"] = manager.connect_all(configs)
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


def _humanize_error(status: str, cfg: McpServerConfig) -> str:
    """``connect_all`` 의 ``error: ...`` 를 다음 행동이 보이는 문장으로."""
    msg = status.removeprefix("error: ").strip()
    low = msg.lower()
    if "no such file" in low or "errno 2" in low:
        return f"실행 파일을 찾을 수 없습니다: {cfg.command}"
    if (
        "connection refused" in low
        or "connect call failed" in low
        or "all connection attempts failed" in low  # httpx (sse)
    ):
        return f"연결 거부 — {cfg.url} 에 서버가 없습니다"
    if "no module named 'mcp'" in low:
        return "mcp SDK 가 없습니다 (pip install mcp)"
    return msg or "알 수 없는 오류"


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
                f"[dim]등록된 MCP 서버가 없습니다 ({path}).[/]\n"
                "  [bold]agent-cli mcp add[/] 로 추가하세요."
            )
            return 0

        table = Table(box=None, pad_edge=False, show_header=True)
        table.add_column("", width=1)
        table.add_column("이름", style="bold")
        table.add_column("전송")
        table.add_column("상태")
        table.add_column("도구", justify="right")
        failures = 0
        notes: list[str] = []
        for name, entry in servers.items():
            cfg = _parse_server_config(name, entry)
            if not test:
                table.add_row("·", name, cfg.transport, "[dim]미확인[/]", "–")
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
                table.add_row("[red]●[/]", name, cfg.transport, "[red]실패[/]", "–")
                notes.append(f"  [red]└ {name}: {msg}[/]")
        self.console.print(table)
        for n in notes:
            self.console.print(n)
        self.console.print(f"\n[dim]설정 파일: {path}[/]")
        return failures

    # ── mcp add ──
    def add(self) -> bool:
        path = mcp_json_path()
        existing = read_servers(path)
        self._offer_import(existing, path)
        existing = read_servers(path)

        self.console.print("\n[bold]MCP 서버 추가[/]\n")
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
                "   [1] 설정 고치기  [2] 그래도 저장  [3] 취소",
                default=1,
                choices=["1", "2", "3"],
            )
            if choice == 3:
                self.console.print("   [dim]취소했습니다. 파일은 바뀌지 않았습니다.[/]")
                return False
            if choice == 2:
                break
            # 1 → 처음부터 다시 (이름은 유지)

        save_server(name, entry, path)
        self.console.print(f"\n   [green]✓[/] {path} 에 [bold]{name}[/] 저장")
        self.console.print("   [dim]다음 실행부터 적용됩니다.[/]")
        return True

    # ── mcp test <name> ──
    def test(self, name: str) -> bool:
        servers = read_servers(mcp_json_path())
        if name not in servers:
            self.console.print(f"[red]'{name}' 은 등록돼 있지 않습니다.[/]")
            self._hint_names(servers)
            return False
        return self._run_probe(name, servers[name])

    # ── mcp remove <name> ──
    def remove(self, name: str) -> bool:
        path = mcp_json_path()
        if not remove_server(name, path):
            self.console.print(f"[red]'{name}' 은 등록돼 있지 않습니다.[/]")
            self._hint_names(read_servers(path))
            return False
        self.console.print(f"[green]✓[/] {name} 제거 ({path})")
        return True

    # ── 내부 ──
    def _hint_names(self, servers: dict) -> None:
        if servers:
            self.console.print(f"  [dim]등록됨: {', '.join(servers)}[/]")

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
            f"[yellow]ⓘ[/] {legacy} 에 서버 {len(new)}개가 있습니다 "
            f"([bold]{', '.join(new)}[/]). v9.0.0 부터 이 파일은 읽지 않습니다."
        )
        if Confirm.ask("   이 프로젝트로 가져올까요?", default=True):
            merged = {**existing, **new}
            write_servers(path, merged)
            self.console.print(
                f"   [green]✓[/] {len(new)}개를 {path} 로 복사했습니다.\n"
            )

    def _ask_name(self, existing: dict) -> str | None:
        while True:
            name = Prompt.ask("   서버 이름").strip()
            if not name:
                self.console.print("   [red]이름은 비울 수 없습니다.[/]")
                continue
            if "." in name or " " in name:
                # 도구 이름이 {서버}.{도구} 라 점·공백은 파싱을 깨뜨린다
                self.console.print("   [red]이름에 점(.)이나 공백은 쓸 수 없습니다.[/]")
                continue
            if name in existing and not Confirm.ask(
                f"   '{name}' 이 이미 있습니다. 덮어쓸까요?", default=False
            ):
                return None
            return name

    def _ask_transport_and_target(self) -> dict | None:
        self.console.print("\n   [bold]전송 방식[/]")
        for i, (key, desc) in enumerate(_TRANSPORTS, 1):
            self.console.print(f"     [cyan]{i})[/] {key:<5} — {desc}")
        choice = IntPrompt.ask("   선택", default=1, choices=["1", "2"])
        transport = _TRANSPORTS[choice - 1][0]

        if transport == "stdio":
            self.console.print("\n   [bold]stdio 설정[/]")
            command = Prompt.ask("   실행 명령").strip()
            if not command:
                self.console.print("   [red]실행 명령은 비울 수 없습니다.[/]")
                return None
            resolved = resolve_command(command)
            if resolved:
                self.console.print(f"   [dim]{command} → {resolved}[/]")
            else:
                self.console.print(
                    f"   [yellow]⚠[/] [dim]{command} 를 PATH 에서 찾지 못했습니다 — "
                    "연결 테스트가 실패할 수 있습니다.[/]"
                )
            args_raw = Prompt.ask("   인자 (공백 구분)", default="").strip()
            entry: dict = {"command": command}
            if args_raw:
                entry["args"] = args_raw.split()
            return entry

        self.console.print("\n   [bold]sse 설정[/]")
        url = Prompt.ask("   URL").strip()
        if not url:
            self.console.print("   [red]URL 은 비울 수 없습니다.[/]")
            return None
        # ``url`` 키의 존재만으로 sse 로 판별된다 — transport 는 명시해 둔다
        return {"url": url, "transport": "sse"}

    def _ask_env(self, entry: dict) -> dict:
        self.console.print("\n   [bold]환경 변수[/] [dim](없으면 빈 줄로 종료)[/]")
        env: dict[str, str] = {}
        while True:
            var = Prompt.ask("   이름", default="").strip()
            if not var:
                break
            value = Prompt.ask("   값", default=f"${{{var}}}").strip()
            ref, present, masked = env_ref_status(value)
            if ref is not None:
                if present:
                    self.console.print(
                        f"     [green]✓[/] [dim]현재 셸에 {ref} 있음 ({masked})[/]"
                    )
                else:
                    self.console.print(
                        f"     [yellow]⚠[/] [dim]현재 셸에 {ref} 가 없습니다 — 실행 시 "
                        "빈 문자열이 됩니다 (연결은 되고 인증만 실패합니다)[/]"
                    )
            env[var] = value
        if env:
            entry = {**entry, "env": env}
            self.console.print(
                "   [dim]값을 ${VAR} 로 두면 파일에 토큰이 남지 않고 실행 시점에 읽습니다.[/]"
            )
        return entry

    def _run_probe(self, name: str, entry: dict) -> bool:
        self.console.print("\n   [bold]연결 테스트[/]")
        cfg = _parse_server_config(name, entry)
        target = (
            f"{cfg.command} {' '.join(cfg.args)}".strip() if cfg.is_stdio else cfg.url
        )
        self.console.print(
            f"   [dim]{target} 실행 중… (최대 {int(PROBE_TIMEOUT_S)}s; "
            "npx 첫 실행은 패키지 설치로 오래 걸릴 수 있습니다)[/]"
        )
        ok, msg, tools, secs = probe_server(name, entry)
        if ok:
            self.console.print(f"   [green]●[/] 연결됨 [dim]({secs:.1f}s)[/]")
            self.console.print(f"   [green]●[/] 도구 [bold]{len(tools)}[/]개")
            if tools:
                shown = " · ".join(tools[:5]) + (" · …" if len(tools) > 5 else "")
                self.console.print(f"       [dim]{shown}[/]")
            self.console.print(
                f"   [dim]LLM 은 이 도구들을 {name}.<도구> 형식으로 부릅니다.[/]"
            )
            return True
        self.console.print(f"   [red]✗[/] 연결 실패 [dim]({secs:.1f}s)[/]")
        self.console.print(f"       [red]{msg}[/]")
        return False
