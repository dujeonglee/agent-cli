"""``monitor`` 도구 — 조건 → 보고 감시 (docs/monitor/DESIGN.md §4).

도구 **설명은 이 파일의 산출물**이다. 대상이 로컬 35B 급이라 추상 지시를 구체
행동으로 잘 못 바꾸므로(§3.2), 실행 관용구를 설명에 박고 테스트로 고정한다 —
특히 `2>&1` 은 빠뜨리면 `shell` 도구가 120초 막힌다(실측).
"""

from __future__ import annotations

from typing import ClassVar

from agent_cli.tools.base import Tool
from agent_cli.tools.result import ToolResult

MODES = ("add", "list", "delete")


def _registry():
    from agent_cli.monitor.runtime import get_monitor_registry

    return get_monitor_registry()


class MonitorTool(Tool):
    name = "monitor"
    #: 설명은 성질만 (v10.74.0, docs/tail/DESIGN.md §9). 종전의 셸 요리법(`nohup … >
    #: /tmp/x.log 2>&1 &`, `python -u`/PYTHONUNBUFFERED, `EXIT:$?`)은 v10.47.0 의
    #: shell background=true 가 맡은 뒤로 낡았고 꼬리의 `/tmp` 금지와 충돌했다.
    description = (
        "Watch a condition in the background and report back to you when it "
        "fires — for long-running work you started with shell. Modes: "
        "add (exactly ONE condition + optional run/deadline/once), list, delete (id).\n"
        "A condition is one of these key pairs — never mix them:\n"
        "  command + every — runs the command periodically, fires when it exits 0 "
        "(its stdout becomes the report)\n"
        "  file + pattern — fires on a NEW line of the file matching the regex\n"
        "  file + idle — fires when the file STOPS changing for that long (a dead "
        "script is silent, not successful)\n"
        "deadline defaults to 2h (60s-24h). A `match` also retires (with a "
        "report) when the file stays unchanged for `silent` (default 10m, 0 = "
        "off) — a dead writer wakes you instead of hanging. once defaults to "
        "true — the monitor retires after one report. Optional run='<cmd>' executes before the "
        "report and its exit code is included. You are always notified; there "
        "is no silent action.\n"
        "A monitor is not saved with the session and does not survive a restart "
        "(`schedule` is and does)."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": list(MODES),
                "description": "add | list | delete",
            },
            "command": {
                "type": "string",
                "description": "condition command+every: the shell command to run",
            },
            "every": {
                "type": "string",
                "description": "condition command+every: interval — '60s', '5m' (min 60s)",
            },
            "file": {
                "type": "string",
                "description": "conditions file+pattern / file+idle: the log file (never alone)",
            },
            "pattern": {
                "type": "string",
                "description": "condition file+pattern: regex matched against NEW lines",
            },
            "idle": {
                "type": "string",
                "description": "condition file+idle: no change for this long — '30s', '5m'",
            },
            "run": {
                "type": "string",
                "description": "add: optional command to run when it fires",
            },
            "deadline": {
                "type": "string",
                "description": "add: how long to watch — '2h', '30m', '600' (default 2h)",
            },
            "once": {
                "type": "boolean",
                "description": "add: retire after the first report (default true)",
            },
            "silent": {
                "type": "string",
                "description": (
                    "file+pattern only: give up (with a report) when the file has not "
                    "changed for this long — '10m', '600', '0' = off (default 10m)"
                ),
            },
            "id": {"type": "string", "description": "delete: monitor id from list"},
        },
        "required": ["mode"],
    }

    def validate(self, args: dict) -> str | None:
        mode = str(args.get("mode", "")).strip()
        if mode not in MODES:
            return f"monitor: mode must be one of {', '.join(MODES)}"
        if mode == "add" and not any(
            args.get(k) not in (None, "") for k in ("file", "command")
        ):
            from agent_cli.monitor.conditions import SHAPES

            return f"monitor: add requires a condition — one of {SHAPES}"
        if mode == "delete" and not str(args.get("id", "")).strip():
            return "monitor: delete requires `id` (see mode:'list')"
        return None

    def summary_arg(self, action_input: dict) -> str:
        return str(action_input.get("mode", ""))

    def _run(self, args: dict, *, ctx=None) -> ToolResult:
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
            lines = [
                f"- [{m.id}] {m.cond.describe()}"
                f" · {int(m.deadline_at - __import__('time').time())}s left"
                f"{'' if m.once else ' · repeating'}"
                f"{f' · run={m.run!r}' if m.run else ''}"
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
                    False, error=f"monitor: unknown id {mon_id!r} (see mode:'list')"
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
                return ToolResult(False, error=f"monitor: deadline — {exc}")

        run_cmd = str(args.get("run") or "").strip()
        gate = _gate_commands(cond, run_cmd)
        if gate:
            return ToolResult(False, error=gate)
        # `command`/`run` 은 셸 권한이다 — `shell` 이 없는 루프(감사자처럼
        # 읽기 전용 프로파일)에선 `match`/`silence` 만 된다 (v10.42.0). 종전엔
        # monitor 를 가진 프로파일이 전부 shell 도 가져 가정으로만 지켜졌다.
        if (
            (run_cmd or cond.type == "command")
            and ctx is not None
            and ctx.tools_available
            and "shell" not in ctx.tools_available
        ):
            return ToolResult(
                False,
                error="monitor: `command` conditions and `run` need the shell tool, "
                "which this agent does not have — use `match` or `silence`",
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
                run=run_cmd,
                state=state,
            )
        except MonitorUnavailable as exc:
            return ToolResult(False, error=str(exc))
        import time as _t

        left = int(mon.deadline_at - _t.time())
        return ToolResult(
            True,
            output=(
                f"monitor {mon.id} watching {mon.cond.describe()} for {left}s"
                f"{'' if mon.once else ' (repeating)'}. "
                f"You will be notified automatically — do NOT poll."
                f"{' ' + note if note else ''}"
            ),
        )


def _gate_commands(cond, run_cmd: str) -> str | None:
    """`command` 조건과 `run` 은 임의 명령을 돈다 — **등록 시점에** 게이트한다.

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
    """
    from agent_cli.monitor.conditions import CommandCondition

    cmds = [c for c in (getattr(cond, "command", ""), run_cmd) if c]
    if not cmds and not isinstance(cond, CommandCondition):
        return None
    from agent_cli.tools import _confine
    from agent_cli.tools.shell import confirm_dangerous

    for cmd in cmds:
        denied, _ = confirm_dangerous(cmd)
        if denied:
            return denied
        if _confine.enabled():
            err = _confine.guard(
                _confine.extract_shell_paths(cmd), "monitor", command=cmd
            )
            if err:
                return err
    return None
