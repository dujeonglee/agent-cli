"""Shell command execution tool."""

from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
from typing import ClassVar

from agent_cli import jobs
from agent_cli.tools.base import Tool
from agent_cli.tools.result import ToolResult

# Commands that destroy or move files irreversibly. Detection works on
# shell-tokenized form so we catch real invocations (`rm -rf x`,
# `xargs rm`, `find -exec rm {}`, `cd /tmp && rm x`) without flagging
# similarly-spelled non-commands (`rm-helper.sh`, `format-firmware`,
# string literals like `echo "rm files"`). Intentionally narrow — once
# users start mashing 'y' through warnings the protection is gone.
_DANGEROUS_KEYWORDS = frozenset(("rm", "rmdir", "mv"))

# Per-process "always allow" set: keywords the user has greenlit for the
# rest of this CLI session. Cleared when the process exits.
_session_allowlist: set[str] = set()


def _confirmation_enabled() -> bool:
    """Default-on. Set AGENT_CLI_DANGEROUS_SHELL_CONFIRM=0 to disable —
    useful for batch/CI runs where there is no human to answer y/n."""
    return os.environ.get("AGENT_CLI_DANGEROUS_SHELL_CONFIRM", "1") != "0"


def _detect_dangerous(cmd: str) -> str | None:
    """Return the first dangerous keyword found in `cmd`, or None.

    Tokenize with shlex (POSIX rules). The keyword must appear as its
    own token to match — operator tokens (`|` `&&` `;`) are tokens
    themselves so a piped/sequenced `rm` is caught, but quoted string
    literals (`echo "rm files"`) collapse into one token and don't
    match. Edge cases not caught: shell-execution wrappers like
    `bash -c "rm foo"` (the inner string is opaque to shlex) and
    `$(rm x)` substitution. Adding coverage if those start showing up
    is fine — keep the matcher narrow until the data demands more.
    """
    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError:
        # Unbalanced quotes — fall back to whitespace split. Better to
        # over-trigger than miss.
        tokens = cmd.split()
    for tok in tokens:
        if tok in _DANGEROUS_KEYWORDS:
            return tok
    return None


# A shell-token boundary is start/end-of-string or any whitespace / operator
# char — everything else (letters, digits, `-`, `.`, `/`, quotes) is "inside a
# token". A dangerous keyword highlights only when it stands as its OWN token,
# mirroring ``_detect_dangerous``'s shlex semantics: ``rm`` matches in ``rm -rf``
# and ``xargs rm`` but NOT in ``rm-helper.sh`` (followed by ``-``), ``chmod``
# (preceded by ``ch``), or the quoted ``echo "rm x"`` (preceded by ``"``). Longer
# keywords first so ``rmdir`` wins over ``rm`` at the same position.
_DANGER_BOUND = r"[^\s|&;()<>`]"
_DANGER_RE = re.compile(
    r"(?<!"
    + _DANGER_BOUND
    + r")(?:"
    + "|".join(re.escape(k) for k in sorted(_DANGEROUS_KEYWORDS, key=len, reverse=True))
    + r")(?!"
    + _DANGER_BOUND
    + r")"
)


def _danger_spans(cmd: str) -> list[tuple[int, int]]:
    """Character ranges in ``cmd`` where a dangerous keyword stands as its own
    shell token — the spans to highlight in a confirm prompt. Computed at the
    origin (single source of truth) so each renderer only paints the ranges and
    never re-derives token boundaries. Left-to-right, non-overlapping. Empty when
    the trigger sits inside quotes/another token (``_detect_dangerous`` may still
    fire via shlex there — the command shows unhighlighted rather than marking
    the wrong substring)."""
    return [(m.start(), m.end()) for m in _DANGER_RE.finditer(cmd)]


def _ask_confirmation(cmd: str, keyword: str) -> tuple[str, str]:
    """Prompt user for y / n / a, optionally followed by a comment.

    Returns ``(decision, comment)``. ``decision`` is one of
    ``"y"`` / ``"n"`` / ``"a"``. ``comment`` is whatever the user
    typed after the decision token (may be empty).

    Format: ``y do this next``, ``n wrong path``, ``a only in /tmp``.
    Pure ``y`` / ``yes`` / ``n`` / ``a`` works as before with empty
    comment. EOF/Ctrl+C → ``("n", "")`` (safe default — never run a
    dangerous command on input failure). If the first token isn't
    y/n/a we treat it as ``n`` and keep the full input as the comment
    so the user's reasoning still surfaces to the LLM.
    """
    # The command itself is passed structured (``command`` + ``danger_spans``)
    # so each renderer can DISPLAY it with the dangerous token highlighted —
    # CLI in bold-red, web in a ``.danger`` span. The prompt string carries only
    # the warning + question; keeping the command out of it avoids showing the
    # command twice (once plain in the prompt, once highlighted).
    prompt = (
        f"\n⚠ Dangerous command detected (`{keyword}`). "
        f"Allow? (y=once, n=deny, a=always allow `{keyword}` this session)\n"
        f"  [y/n/a, optional comment after]: "
    )
    from agent_cli.render import get_renderer
    from agent_cli.render.base import ConfirmOption

    options = [
        # Affirmative synonyms map to "once" so a natural yes doesn't
        # collapse to the safe-default deny. ``allow`` maps to "always"
        # because the prompt labels that option "always allow".
        ConfirmOption(
            key="y",
            label="once (allow this command)",
            aliases=("yes", "ok", "okay", "yep", "yeah", "sure"),
        ),
        ConfirmOption(key="n", label="deny", aliases=("no", "nope")),
        ConfirmOption(
            key="a",
            label=f"always allow `{keyword}` this session",
            aliases=("always", "allow"),
        ),
    ]
    # Renderer guarantees ``key`` is one of the option keys (or default
    # on EOF / empty / unrecognized) and preserves any free-text comment
    # so the user's intent surfaces upstream to the LLM observation.
    return get_renderer().confirm(
        prompt,
        options,
        default_key="n",
        command=cmd,
        danger_spans=_danger_spans(cmd),
    )


def confirm_dangerous(cmd: str) -> tuple[str | None, str]:
    """위험 키워드 확인 — ``(거부 사유 | None, 사용자 코멘트)``.

    `shell` 실행과 `monitor` **등록**이 공유한다 (v9.11.0). monitor 는 발화
    시점이 아니라 등록 시점에 이걸 부른다: 사람은 모니터를 거는 순간엔 있지만
    새벽 3시 발화 때는 없고, 무엇보다 **폴링 스레드에서 `renderer.confirm` 을
    부르면 `interactive_lock` 교착**이다. 같은 함수를 쓰므로 정책(키워드 게이트
    + `AGENT_CLI_DANGEROUS_SHELL_CONFIRM=0` 우회)이 갈릴 수 없다 — 모니터만
    모든 명령에 확인을 요구하면 harbor/CI 에서 못 쓰게 되는데, 그 headless
    환경이 monitor 를 만드는 근거였다(docs/monitor/DESIGN.md §7.3).
    """
    if not _confirmation_enabled():
        return None, ""
    keyword = _detect_dangerous(cmd)
    if not keyword or keyword in _session_allowlist:
        return None, ""

    from agent_cli.render import get_renderer, interactive_lock

    # "Can we ask the user?" is a renderer capability, not a TTY fact: the CLI
    # renderer needs a terminal, the web renderer needs a connected client (no
    # TTY). Gating on the renderer lets web prompt over SSE and stops the CLI
    # from trying when no prompt could be shown.
    if not get_renderer().can_prompt():
        return (
            f"Refused: command contains `{keyword}` but this interface can't "
            "prompt for confirmation right now (non-interactive shell, or no "
            "connected client). Set AGENT_CLI_DANGEROUS_SHELL_CONFIRM=0 to "
            "bypass for non-interactive runs."
        ), ""
    # Hold the shared interactive lock (re-entrant) across the allowlist
    # re-check + prompt: it serializes against confirm AND ask everywhere, and
    # ``renderer.confirm`` re-acquires it internally on this same thread.
    # Re-check the allowlist inside — another worker may have been granted
    # "always" while we waited.
    with interactive_lock:
        if keyword in _session_allowlist:
            return None, ""
        decision, user_comment = _ask_confirmation(cmd, keyword)
        if decision == "n":
            err = f"User denied command containing `{keyword}`: {cmd}"
            if user_comment:
                err += f". User said: {user_comment}"
            return err, ""
        if decision == "a":
            _session_allowlist.add(keyword)
            # 세션 정책이 바뀌는 순간이라 **기록**한다 (v9.8.0). 이후 같은
            # 키워드는 묻지도 않으므로, 대화에 흔적이 없으면 나중에 "이게 왜
            # 확인 없이 돌았지" 를 알 수 없다. `y`(1회)는 안 남긴다 — 바로 위
            # `⚡` 카드가 맥락을 준다. 거절은 이미 `ToolResult.error` 로 남는다.
            get_renderer().note_next(
                f"🔓 사용자가 `{keyword}` 포함 명령을 승인 — 이 세션 내내 재확인 없음"
            )
        return None, user_comment


def _guard(cmd: str) -> tuple[str | None, str]:
    """위험 키워드 확인 + 워크스페이스 봉쇄 — 포그라운드·백그라운드가 같은 문.
    ``(거부 사유 | None, 승인 코멘트)``."""
    denied, user_comment = confirm_dangerous(cmd)
    if denied:
        return denied, ""
    from agent_cli.tools import _confine

    denial = _confine.guard(
        _confine.extract_shell_paths(cmd), "shell command", command=cmd
    )
    return (denial or None), user_comment


#: 전경 명령이 기다리는 시간의 기본값이자 상한 (v10.91.0). 종전엔 기본 120 에 상한이
#: 없어 모델이 600 을 넣고 10분을 막혀 있다가 죽는 것을 봤다(GLM 벤치, 380장 중
#: 352장이 끝난 OCR 루프) — "올리지 말라" 는 문구는 있었다. 이제 그 위의 값은
#: 백그라운드 작업으로 바꾸고 결과에 말한다(`ShellTool._run`).
FOREGROUND_LIMIT = 120

#: 모델이 제 손으로 떼어 내는 조각 — `nohup`·`setsid`·`disown` 단어(토큰 경계), 명령
#: 끝(혹은 `)`·`;` 앞)의 `&`, 그리고 `& echo $!` 처럼 뒤에 짧은 꼬리를 단 `&`.
#: 인용부 안은 보지 않는다(`_unquoted_mask`). `&&` 는 아니다.
_DETACH_WORD = re.compile(r"(?:(?<=^)|(?<=[\s;|(&]))(nohup|setsid|disown)\b[ \t]*")
_TRAILING_AMP = re.compile(r"[ \t]*(?<!&)&(?!&)(?=\s*(?:\)|;|$))")
_AMP_THEN_TAIL = re.compile(r"(?<!&)&(?!&)(?=\s+(?:echo|sleep|true|wait)\b)")


def _unquoted_mask(cmd: str) -> str:
    """같은 길이의 문자열 — 홑·쌍따옴표 안(과 이스케이프된 문자)은 ``x`` 로 가려
    정규식이 인용부 밖만 보게 한다. 끼워 넣은 글(heredoc 본문)은 가리지 못한다 —
    종전 거부 판별도 같은 한계였고, 지운 것은 결과에 적으므로 모델이 본다."""
    out: list[str] = []
    quote = ""
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if quote:
            if ch == quote:
                quote = ""
                out.append(ch)
            elif ch == "\\" and quote == '"' and i + 1 < len(cmd):
                out.append("xx")
                i += 1
            else:
                out.append("x" if ch != "\n" else "\n")
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "\\" and i + 1 < len(cmd):
            out.append("xx")
            i += 1
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def strip_detachers(cmd: str) -> tuple[str, list[str]]:
    """백그라운드 명령에서 모델의 데몬화 조각을 지운다 (v10.91.0) — ``(새 명령,
    지운 것들)``. 하니스가 이미 자식으로 띄우고 로그로 받고 끝을 통지하므로
    `nohup`·`setsid`·`disown`·끝의 `&` 는 뜻이 없고, 있으면 `sh` 가 1초 만에 끝나
    진짜 일이 추적 밖으로 떨어진다(v10.47.1 재측정 — 그땐 거부했다; 지우고 말하는
    쪽이 다시 보내는 턴을 아낀다, 사용자 결정 2026-10-31). `& echo $!` 는 `; echo $!`
    로 — `$!` 는 비지만 결과가 pid 를 준다."""
    removed: list[str] = []
    mask = _unquoted_mask(cmd)

    def _edit(pattern: re.Pattern, label, replacement: str = "") -> None:
        """패턴의 모든 자리를 ``replacement`` 로 — 뒤에서부터 바꿔 앞 자리의 위치를
        지킨다. 지운 것은 패턴 순서(단어 → 끝의 `&` → 꼬리 앞 `&`), 같은 패턴 안에서는
        왼쪽부터 적는다."""
        nonlocal cmd, mask
        hits = list(pattern.finditer(mask))
        for m in reversed(hits):
            cmd = cmd[: m.start()] + replacement + cmd[m.end() :]
            mask = mask[: m.start()] + replacement + mask[m.end() :]
        removed.extend(label(m) for m in hits)

    _edit(_DETACH_WORD, lambda m: f"`{m.group(1)}`")
    _edit(_TRAILING_AMP, lambda m: "the trailing `&`")
    _edit(_AMP_THEN_TAIL, lambda m: "`&` before the tail (now `;`)", ";")
    return cmd.strip(), removed


def tool_shell_background(args: dict, *, owner: str, note: str = "") -> ToolResult:
    """``background: true`` (v10.47.0, docs/jobs/DESIGN.md) — 하니스가 자식으로
    띄우고 로그로 보낸 뒤 즉시 돌아온다. 끝나면 소유자에게 통지가 간다. ``note``
    는 결과 앞에 붙는 사정(전경 한도 초과로 여기 왔다)."""
    from agent_cli.jobs import JobsUnavailable, get_job_registry

    cmd, removed = strip_detachers(args.get("command", ""))
    if removed:
        note += (
            f"Removed {', '.join(removed)} — a background job is already detached, "
            "logged and reported by the harness; use the pid below instead of `$!`. "
        )
    if not cmd:
        return ToolResult(False, error=f"{note}Nothing is left to run.")
    denied, _comment = _guard(cmd)
    if denied:
        return ToolResult(False, error=denied)
    reg = get_job_registry()
    if reg is None:
        return ToolResult(
            False,
            error="background jobs are not available in this run — run it in the "
            "foreground with a `timeout` instead",
        )
    try:
        job = reg.start(cmd, owner=owner)
    except JobsUnavailable as exc:
        return ToolResult(False, error=str(exc))
    except OSError as exc:
        return ToolResult(False, error=f"could not start the job: {exc}")
    return ToolResult(
        True,
        output=(
            f"{note}job #{job.id} started (pid {job.pid}) — output goes to "
            f"{job.log.as_posix()}. You will be told when it ends, with its exit "
            f"code and the log's last lines; keep working meanwhile. To stop it: "
            f"{job.stop_hint}."
        ),
    )


def tool_shell(args: dict) -> ToolResult:
    """Run a shell command and return stdout/stderr."""

    cmd = args.get("command", "")
    if not cmd or not cmd.strip():
        return ToolResult(
            False, error="Empty command. Provide a shell command to execute."
        )

    denied, user_comment = _guard(cmd)
    if denied:
        return ToolResult(False, error=denied)

    # (Workspace confinement rides in ``_guard`` — best-effort literal path
    # tokens, blind to $(...) / python -c / variables; documented in _confine.)
    timeout = min(int(args.get("timeout", FOREGROUND_LIMIT)), FOREGROUND_LIMIT)
    try:
        # Capture bytes, not text: ``text=True`` decodes strict UTF-8 and
        # raises UnicodeDecodeError mid-run on non-UTF-8 output (git show /
        # binary diffs / latin-1 messages) — uncatchable by the timeout guard.
        # 자기 프로세스 그룹으로 띄운다 (v10.77.0): 시간 초과 때 `/bin/sh` 만
        # 죽이면 그 아래 `apt-get` 이 살아남아 dpkg 잠금을 쥐었다(GLM video —
        # 다음 apt 가 "Could not get lock … held by process (apt-get)").
        proc = subprocess.Popen(
            cmd,
            shell=True,
            executable=jobs.SHELL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            raw_out, raw_err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            raw_out, raw_err = _stop_group(proc)
            return ToolResult(False, error=_timeout_message(timeout, raw_out, raw_err))
        out = raw_out.decode("utf-8", errors="replace").strip()
        err = raw_err.decode("utf-8", errors="replace").strip()
        parts = []
        if out:
            parts.append(out)
        if err:
            parts.append(f"[stderr]\n{err}")
        if proc.returncode != 0:
            parts.append(f"[exit code: {proc.returncode}]")
        # When the user approved with a comment, surface it after the
        # command output so the LLM sees the additional instruction
        # alongside the result.
        if user_comment:
            parts.append(f"[User note when approving: {user_comment}]")
        return ToolResult(True, output="\n".join(parts) if parts else "(no output)")
    except Exception as e:
        return ToolResult(False, error=f"shell failed: {e}")


def _stop_group(proc: subprocess.Popen) -> tuple[bytes, bytes]:
    """명령의 프로세스 그룹을 끝내고 그때까지의 출력을 돌려준다 — TERM, 잠시
    뒤 남았으면 KILL (``jobs.JobRegistry.kill`` 과 같은 순서)."""
    from agent_cli.jobs import KILL_GRACE_S

    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            return proc.communicate(timeout=KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            continue
    # 그룹 밖으로 빠져나간 자식이 파이프를 쥐고 있다 — 출력 없이 돌아간다.
    return b"", b""


def _timeout_message(timeout: int, raw_out: bytes, raw_err: bytes) -> str:
    """시간 초과 결과 (v10.77.0) — 끝냈다는 사실, 그때까지의 출력 끝부분,
    background 로 다시 보내라는 교정(교정은 트리거 직후 — 설명의 작업 종류
    나열 대신)."""
    from agent_cli.jobs import TAIL_LINE_CHARS, TAIL_LINES

    text = "\n".join(
        t
        for t in (
            raw_out.decode("utf-8", errors="replace").strip(),
            raw_err.decode("utf-8", errors="replace").strip(),
        )
        if t
    )
    lines = [ln[:TAIL_LINE_CHARS] for ln in text.splitlines()][-TAIL_LINES:]
    head = (
        f"Command timed out after {timeout}s and was stopped, together with "
        "everything it started."
    )
    if lines:
        head += f"\nOutput so far (last {len(lines)} lines):\n" + "\n".join(lines)
    else:
        head += " It printed nothing before that."
    return (
        f"{head}\nA command that runs this long belongs in the background: send it "
        "again with `background` true and you will be told when it ends."
    )


class ShellTool(Tool):
    name = "shell"
    description = (
        "Run a shell command in the working directory. The result is its stdout, then "
        "its stderr under `[stderr]`, then `[exit code: N]` when the exit code is not 0"
        " — the command needs no `echo $?` or `2>&1` for that. For a command that may "
        "run longer than about a minute, set `background` to true: the harness starts "
        "it for you, sends its output to a log under .agent-cli/tmp/jobs/ and tells "
        "you when it ends (exit code + last lines) — keep working meanwhile."
    )
    oversized_retry_hint = (
        "narrow the command itself: add `grep`/`head`/`tail`, target fewer "
        "files, or pass a quieter flag (-q/--no-progress) so the bulk is never "
        "produced."
    )
    # Flat-native (consolidation roadmap Step 3): the schema is the plain
    # single-command shape — no `shell_` wire-key prefix. (fetch turned out to
    # still carry its prefix after Step 3 and was flattened last, in v4.38.0 —
    # since then no builtin carries a prefix and the prefix machinery
    # (strip/add/claims/infer/base wrap) is fully latent; it still serves MCP /
    # future prefixed tools.) `wrap_single_op` is identity; `key_prefix` is
    # left at its default (strip a no-op on flat keys, `claims` False for a
    # flat `{command}`).
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "minLength": 1,
                "description": "Shell command to run",
            },
            "timeout": {
                "type": "integer",
                "description": (
                    f"Seconds a foreground command may run (default and maximum "
                    f"{FOREGROUND_LIMIT}); at the limit it is stopped, together with "
                    "everything it started. A larger value starts the command as a "
                    "background job instead."
                ),
            },
            "background": {
                "type": "boolean",
                "description": (
                    "true = start the command as a background job owned by the "
                    "harness (returns at once with the job id, pid and log path; "
                    "you are notified when it ends). Use it for anything that may run "
                    "longer than about a minute. Give the plain "
                    "command: no trailing `&`, no redirection, no `echo EXIT` — "
                    "the harness logs it and reports the exit code."
                ),
            },
        },
        "required": ["command"],
    }

    def wrap_single_op(self, flat: dict) -> dict:
        return flat

    def summary_arg(self, action_input: dict) -> str:
        # 전문이다 (v9.22.3) — 종전 `[:60]` 은 압축 트랜스크립트에서 `-k`
        # 필터·`-d` 본문·sed 식처럼 뒤쪽에 뜻이 있는 명령을 잘라 요약기가
        # "무엇을" 했는지 못 쓰게 했고, `oversized_key` 로 쓰일 땐 앞 60자가
        # 같은 두 명령을 한 파일로 겹치게 했다. 토큰 걱정은 성립하지 않는다:
        # 이 명령은 방금까지 컨텍스트에 통째로 있던 것이라 트랜스크립트가
        # 밀려난 양을 넘지 못한다.
        return self.strip_prefix(action_input).get("command") or ""

    def _run(self, args: dict, *, ctx=None) -> ToolResult:
        background = args.get("background") is True or str(
            args.get("background", "")
        ).lower() in ("true", "1", "yes")
        note = ""
        try:
            asked = int(args.get("timeout", FOREGROUND_LIMIT))
        except (TypeError, ValueError):
            asked = FOREGROUND_LIMIT
        if asked > FOREGROUND_LIMIT and not background:
            # 상한 위의 값은 "이 명령은 길다" 는 뜻이다 — 전경에서 막혀 있다가 죽는 대신
            # 작업으로 띄우고 결과에 말한다 (v10.91.0, 사용자 결정 2026-10-31). 작업
            # 레지스트리가 없는 런만 상한으로 깎아 전경에서 돈다(`tool_shell`).
            from agent_cli.jobs import get_job_registry

            reg = get_job_registry()
            if reg is not None and reg.available:
                background = True
                note = (
                    f"`timeout` {asked} is above the foreground limit of "
                    f"{FOREGROUND_LIMIT} seconds, so the command was started as a "
                    "background job instead. "
                )
        if background:
            owner = getattr(ctx, "owner", None) or "main"
            return tool_shell_background(args, owner=owner, note=note)
        return tool_shell(args)
