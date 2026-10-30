"""``monitor`` 도구 — 조건 → 보고 감시 (docs/monitor/DESIGN.md §4).

도구 **설명은 이 파일의 산출물**이다. 대상이 로컬 35B 급이라 추상 지시를 구체
행동으로 잘 못 바꾸므로(§3.2), 실행 관용구를 설명에 박고 테스트로 고정한다 —
특히 `2>&1` 은 빠뜨리면 `shell` 도구가 120초 막힌다(실측).

v10.79.0 (docs/monitor/DESIGN.md §13, docs/tool-review/TRACKER.md): 첫 문장이
"작업 종료는 이미 통지된다" 를 말한다 — 평평한 인자 모양이 된 뒤의 벤치 `add` 9번
중 다섯이 작업 로그의 끝 줄이나 `ps -p` 를 지켜봤는데, 그건 `shell` `background`
의 종료 통지가 이미 하는 일이다. 보고 전에 하니스가 돌리던 ``run`` 은 지웠다(보고만
한다; 반응은 모델이 `shell` 로). 모델이 보는 문구는 전부 인자 짝(`file` + `pattern`)
으로 — 내부 조건 이름(`match`·`silence`·`command`)과 중괄호 모양은 모델이 쓰는
어휘가 아니었다.
"""

from __future__ import annotations

from typing import ClassVar

from agent_cli.tools.base import Tool
from agent_cli.tools.result import ToolResult

MODES = ("add", "list", "delete")

#: ``run`` 을 주면 한 릴리스 동안 명시 거절한다 (v10.79.0). 알 수 없는 인자는 검증이
#: 조용히 버리므로(`Tool.schema_gaps` — "Unknown keys are not a validation failure")
#: 그대로 두면 옛 습관으로 ``run: "kill 123"`` 을 보낸 모델이 kill 이 실행됐다고 믿는다.
RUN_REMOVED = (
    "monitor: `run` is no longer an argument — act on the report with `shell` instead."
)


def _registry():
    from agent_cli.monitor.runtime import get_monitor_registry

    return get_monitor_registry()


class MonitorTool(Tool):
    name = "monitor"
    #: 설명은 성질만 (v10.74.0, docs/tail/DESIGN.md §11). 종전의 셸 요리법(`nohup … >
    #: /tmp/x.log 2>&1 & echo $!`, `python -u`/PYTHONUNBUFFERED, `( CMD; echo "EXIT:$?" )`)은
    #: 모델이 직접 백그라운드를 띄우던 설계(§4.4 실측)의 산물이다. v10.47.0 부터 `shell`
    #: `background=true` 가 로그·종료 코드·통지를 맡아 낡았고, `/tmp` 는 꼬리의 스크래치
    #: 규칙(`.agent-cli/tmp/`)과 충돌했다. 버퍼링은 로그 파일을 보는 조건(file+pattern·
    #: file+idle)에 지금도 해당하는 성질이라 실행 파일 이름 없이 한 문장으로 남긴다.
    description = (
        "Watch a condition in the background and report to you when it fires. A "
        "`shell` job started with `background` true already reports when it ends; a "
        "monitor is for other conditions — a line appearing in a file, a file going "
        'quiet, a command starting to succeed. `mode` is "add" (one condition, plus '
        'optional `deadline` and `once`), "list" or "delete" (with `id`).\n'
        "A condition is exactly one of these argument pairs:\n"
        "  `command` + `every` — runs the command at that interval; fires when it "
        "exits 0, and its stdout is the report.\n"
        "  `file` + `pattern` — fires on a new line of the file that matches the "
        'regex. If the file stays unchanged for `silent` (default "10m", "0" turns it '
        "off), the monitor retires with a report instead, so a dead writer does not "
        "leave you waiting.\n"
        "  `file` + `idle` — fires when the file has not changed for that long; a "
        "writer that died looks the same as one that finished.\n"
        "A program that buffers its output writes nothing to the file until it exits, "
        "so a healthy job looks silent — run it unbuffered.\n"
        '`deadline` defaults to "2h" (from "60s" to "24h"). `once` defaults to true: '
        "the monitor retires after one report.\n"
        "A monitor is not saved with the session and does not survive a restart; a "
        "`schedule` is and does."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": list(MODES),
                "description": '"add" | "list" | "delete"',
            },
            "command": {
                "type": "string",
                "description": "The shell command to run, with `every`.",
            },
            "every": {
                "type": "string",
                "description": (
                    'Interval for `command`, such as "60s" or "5m" (at least "60s").'
                ),
            },
            "file": {
                "type": "string",
                "description": "The file to watch, with `pattern` or `idle`.",
            },
            "pattern": {
                "type": "string",
                "description": "Regex matched against new lines of `file`.",
            },
            "idle": {
                "type": "string",
                "description": (
                    'Fire when `file` has not changed for this long, such as "30s" '
                    'or "5m".'
                ),
            },
            "deadline": {
                "type": "string",
                "description": 'How long to watch, such as "30m" or "2h" (default "2h").',
            },
            "once": {
                "type": "boolean",
                "description": (
                    "true retires the monitor after its first report (default true)."
                ),
            },
            "silent": {
                "type": "string",
                "description": (
                    "With `pattern`: retire with a report when `file` has not changed "
                    'for this long (default "10m", "0" turns it off).'
                ),
            },
            "id": {
                "type": "string",
                "description": 'The monitor to delete, as shown by "list".',
            },
        },
        "required": ["mode"],
    }

    def validate(self, args: dict) -> str | None:
        mode = str(args.get("mode", "")).strip()
        if mode not in MODES:
            return f"monitor: mode must be one of {', '.join(MODES)}"
        if args.get("run") not in (None, ""):
            return RUN_REMOVED
        if mode == "add" and not any(
            args.get(k) not in (None, "") for k in ("file", "command")
        ):
            from agent_cli.monitor.conditions import SHAPES

            return f"monitor: add requires a condition — one of {SHAPES}"
        if mode == "delete" and not str(args.get("id", "")).strip():
            return 'monitor: "delete" requires `id` (see `mode` "list")'
        return None

    def summary_arg(self, action_input: dict) -> str:
        return str(action_input.get("mode", ""))

    def _run(self, args: dict, *, ctx=None) -> ToolResult:
        import time as _t

        from agent_cli.constants import format_duration

        mode = str(args.get("mode", "")).strip()
        reg = _registry()
        if reg is None:
            return ToolResult(
                False, error="monitor: not available in this run (no registry)"
            )
        if mode == "list":
            live = reg.list_all()
            if not live:
                return ToolResult(True, output="No active monitors.")
            now = _t.time()
            lines = [
                f"- [{m.id}] {m.cond.describe()}"
                f" · {format_duration(m.deadline_at - now)} left"
                f"{'' if m.once else ' · repeating'}"
                for m in live
            ]
            return ToolResult(True, output="Active monitors:\n" + "\n".join(lines))
        if mode == "delete":
            mon_id = str(args.get("id", "")).strip()
            ok = reg.delete(mon_id)
            return (
                ToolResult(ok, output=f"deleted {mon_id}")
                if ok
                else ToolResult(
                    False, error=f'monitor: unknown id {mon_id!r} (see `mode` "list")'
                )
            )

        # ── add ──
        from agent_cli.constants import MONITOR_DEADLINE_DEFAULT_S, parse_duration
        from agent_cli.monitor import build

        try:
            cond = build(args)  # 조건 키는 도구 인자에 평평하게 (v10.67.0)
        except ValueError as exc:
            # 문법 오류는 **ToolResult** 로 — 예외는 도구 경계를 못 넘는다
            # (constants.parse_duration 과 같은 분업).
            return ToolResult(False, error=f"monitor: {exc}")

        raw_deadline = args.get("deadline")
        if raw_deadline in (None, ""):
            deadline_s = MONITOR_DEADLINE_DEFAULT_S
        else:
            try:
                deadline_s = parse_duration(str(raw_deadline))
            except ValueError as exc:
                return ToolResult(False, error=f"monitor: `deadline` — {exc}")

        gate = _gate_command(cond)
        if gate:
            return ToolResult(False, error=gate)
        # `command` 는 셸 권한이다 — `shell` 이 없는 루프(감사자처럼 읽기 전용
        # 프로파일)에선 파일 조건만 된다 (v10.42.0). 종전엔 monitor 를 가진
        # 프로파일이 전부 shell 도 가져 가정으로만 지켜졌다.
        if (
            cond.type == "command"
            and ctx is not None
            and ctx.tools_available
            and "shell" not in ctx.tools_available
        ):
            return ToolResult(
                False,
                error="monitor: `command` conditions need the `shell` tool, which "
                "this agent does not have — use a `file` + `pattern` or "
                "`file` + `idle` condition",
            )

        once = args.get("once")
        from agent_cli.monitor.registry import MonitorUnavailable

        # 등록 시점의 파일 상태 → 초기 커서 + 모델에게 줄 근거 (v10.34.0,
        # conditions.MatchCondition.register). `observed_at` 은 모델이 마지막
        # 도구 결과를 받은 시각 — "그 뒤에 수정됐다" 가 "모델이 못 본 내용" 이다.
        observed_at = ctx.observed_at() if ctx is not None and ctx.observed_at else None
        state, note = cond.register(observed_at=observed_at)
        try:
            mon = reg.add(
                cond,
                # 보고는 **건 쪽으로** 간다. `ctx` 가 없는 경로(직접 호출)는
                # main 으로 — 그 경우 애초에 상주 루프가 아니다.
                owner=ctx.owner if ctx is not None else "main",
                deadline_s=deadline_s,
                once=True if once is None else bool(once),
                state=state,
            )
        except MonitorUnavailable as exc:
            return ToolResult(False, error=str(exc))

        left = format_duration(mon.deadline_at - _t.time())
        return ToolResult(
            True,
            output=(
                f"monitor {mon.id} watching {mon.cond.describe()} for {left}"
                f"{'' if mon.once else ' (repeating)'}. "
                f"It reports to you when it fires; there is nothing to check in the "
                f"meantime.{' ' + note if note else ''}"
            ),
        )


def _gate_command(cond) -> str | None:
    """`command` 조건은 임의 명령을 돈다 — **등록 시점에** 게이트한다.

    발화 시점이 아닌 이유 둘:

    1. 사람은 에이전트가 monitor 를 거는 순간엔 있지만 새벽 3시 발화 때는 없다.
    2. **폴링 스레드에서 `renderer.confirm` 을 부르면 안 된다.** `interactive_lock`
       이 모든 사용자 읽기를 직렬화하는데, 턴이 도는 중에 백그라운드 스레드가
       그 락을 잡으면 교착이다. 등록 시점 확인은 그 호출이 메인 스레드에서만
       일어남을 보장한다.

    정책은 `shell` 과 **같은 함수를 재사용**한다. 초판 설계는 "shell.py 와
    똑같이 거부"라고 적었지만 실제로는 달랐다 — shell 확인은 위험 키워드
    게이트이고 env 로 우회된다. 모든 명령에 확인을 요구하면 harbor/CI 에서
    `command` 가 아예 못 쓰이는데, **그 headless 환경이 monitor 를 만드는
    근거였다**(§3.1). 시점만 다르고 정책은 동일하게 둔다.

    종전엔 ``run`` 도 같은 게이트를 지났다 — v10.79.0 에 지웠으니 명령은 하나다.
    """
    from agent_cli.monitor.conditions import CommandCondition

    if not isinstance(cond, CommandCondition):
        return None
    from agent_cli.tools import _confine
    from agent_cli.tools.shell import confirm_dangerous

    cmd = cond.command
    denied, _ = confirm_dangerous(cmd)
    if denied:
        return denied
    if _confine.enabled():
        err = _confine.guard(_confine.extract_shell_paths(cmd), "monitor", command=cmd)
        if err:
            return err
    return None
