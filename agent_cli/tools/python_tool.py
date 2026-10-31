"""``python`` 도구 — 상태가 남는 파이썬 실행 (v10.66.0, docs/python-tool/DESIGN.md).

Jupyter 셀 하나처럼: 코드 조각을 소유자의 워커(`agent_cli/pyworker.py`)에서
실행하고, 변수·함수·읽어 둔 데이터는 다음 호출에도 남는다. 관찰은 출력 + 마지막
식의 값 + **상태 요약 한 줄**(``state: texts (dict, 12 keys) · parse (function)``)
— 모델이 "메모리에 뭐가 있는지" 를 매 호출 보게 하는 것이 이 도구의 핵심 UX 다.
꼬리에는 넣지 않는다(토큰 비용 0, 관찰에만).

열린 matplotlib figure 는 ``.agent-cli/tmp/python/`` 에 PNG 로 저장해 이미지로
첨부한다(read_file 의 이미지 경로 재사용) — 좌표·경로 데이터는 그려서 보는 게
글로 읽는 것보다 빠르다(gcode 과제: 서브에이전트 3개가 좌표를 글자로 읽다 타임아웃).

봉쇄는 shell 과 같은 best-effort: 코드 안의 절대경로·``../`` 문자열 리터럴만
본다(`_confine` 의 문서된 사각지대 — 변수 조합 경로는 못 본다).
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import ClassVar

from agent_cli.tools.base import Tool
from agent_cli.tools.result import ToolResult

#: 전경에서 기다리는 시간의 기본값이자 상한 (v10.91.0) — 그 뒤 셀은 작업이 된다.
DEFAULT_TIMEOUT = 120
#: 문자열 리터럴 안의 절대경로·상위 탈출 — 봉쇄 검사 대상.
_PATH_LITERAL = re.compile(r"""['"]((?:/|\.\./)[^'"\n]*)['"]""")


def _cell_title(code: str) -> str:
    """작업 목록·로그 머리줄에 쓰는 셀의 이름 — 첫 줄 하나."""
    first = next((ln.strip() for ln in code.splitlines() if ln.strip()), "")
    return f"python: {first[:70]}" if first else "python"


def _capture_log(worker, owner: str, code: str) -> Path:
    """이 호출의 출력이 쌓일 로그 — 소유자당 하나(`.agent-cli/tmp/python/<owner>.log`),
    호출마다 비우고 머리줄 ``$ python: <첫 줄>`` 을 적는다(셸 작업 로그와 같은 모양 —
    `jobs.Job.header_bytes`·꼬리 필터가 그걸 기대한다). 한도를 넘긴 셀이 작업이 되면
    이 파일이 그 작업의 로그라 `wait` 의 진행 신호와 종료 통지의 꼬리가 여기서 나온다.
    작업 디렉터리 기준 상대 경로를 돌려준다."""
    from agent_cli.pyworker import FIG_DIR

    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", owner) or "main"
    rel = Path(FIG_DIR) / f"{safe}.log"
    path = Path.cwd() / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"$ {_cell_title(code)}\n", encoding="utf-8")
    return rel


def pending_message(job, timeout: int, asked: int) -> str:
    """한도를 넘긴 셀이 작업이 됐다 — 도구 결과. 멈추는 법은 작업 꼬리와 같은 모양."""
    over = (
        f" (`timeout` {asked} was not waited for in the foreground: {timeout} is the limit)"
        if asked > timeout
        else ""
    )
    return (
        f"python: the step is still running after {timeout}s{over} and continues as "
        f"job #{job.id} (pid {job.pid}) — the interpreter is busy until it ends. You "
        f"will be told when it ends, with its output and the `state:` line; its log is "
        f"{job.log.as_posix()}. Keep working meanwhile, or `wait` if nothing else is "
        f"left. To stop it: {job.stop_hint} (the state is kept)."
    )


def busy_message(cell) -> str:
    """앞선 셀이 아직 돈다 — 이 호출은 받지 않는다."""
    from agent_cli.jobs import elapsed_text

    which = f"job #{cell.job_id}" if cell.job_id is not None else "a previous step"
    ago = elapsed_text(int(time.time() - cell.started_at))
    return (
        f"python: the interpreter is busy with {which} ({ago} so far) — `wait` for "
        f"its notice, or stop it with {cell.stop_hint} (the state is kept)."
    )


def literal_paths(code: str) -> list[str]:
    return [m.group(1) for m in _PATH_LITERAL.finditer(code)]


#: 데이터가 메모리에 있을 때 상태줄 끝에 붙는 말 — 다시 읽기(벤치 실측: 런당
#: 최대 61번) 를 겨냥한다.
STATE_HINT = " — still in memory: reuse it, do not reload"


def state_line(state: list[list[str]]) -> str:
    """``state: texts (dict, 12 keys) · parse (function) — still in memory…`` —
    20개까지(자식이 쓸모 순으로 보낸다: 데이터 → 코드 → 객체 → 스칼라), 나머지는
    수만. 데이터 분류가 하나라도 있으면 재사용 힌트."""
    if not state:
        return "state: (empty)"
    shown = state[:20]
    parts = [f"{r[0]} ({r[1]})" for r in shown]
    rest = len(state) - len(shown)
    if rest > 0:
        parts.append(f"+{rest} more")
    hint = STATE_HINT if any(len(r) > 2 and r[2] == "data" for r in state) else ""
    return "state: " + " · ".join(parts) + hint


# ── 꼬리의 사용 가이드 (v10.73.0, docs/tail/DESIGN.md) ─────────────────────────
#: `shell` 명령 안의 파이썬 실행 — `python3 -c …`, `python3 - <<EOF`, `python3 <<EOF`,
#: `python3 x.py`(옵션이 끼어도), 경로가 붙은 인터프리터(`/usr/bin/python3`,
#: `.venv/bin/python` — pyguide r2 financial 은 10건 전부 이 모양이었다). 뒤에 오는
#: 첫 인자로 가른다.
_SHELL_PY = re.compile(
    r"(?:^|[\s;&|(`])(?:[\w.~-]*/)*python[0-9.]*\s+(?P<rest>\S+(?:\s+\S+)?)"
)
#: 데이터 작업이 아닌 파이썬 호출 — 모듈 실행(`-m pip`·`-m pytest`·`-m venv`)과 버전 확인.
_SHELL_PY_BENIGN = ("-m", "--version", "-V", "-h", "--help")


def shell_runs_python(command: str) -> bool:
    """shell 명령이 **파이썬 코드를 돌리는가** — `python` 도구가 할 일을 shell 로
    한 경우(`-c` 인라인·heredoc·스크립트 파일). `python3 -m pip install` 같은 모듈
    실행과 `--version` 은 아니다(그건 shell 의 일)."""
    for m in _SHELL_PY.finditer(command or ""):
        rest = m.group("rest")
        first = rest.split()[0]
        if first in _SHELL_PY_BENIGN:
            continue
        if first == "-c" or first == "-" or first.startswith("<<"):
            return True
        # 옵션(-u, -B, -W…) 뒤의 .py 파일, 또는 바로 .py 파일
        for tok in rest.split():
            if tok.startswith("-"):
                continue
            return tok.endswith(".py")
    return False


#: ModuleNotFoundError 관찰에 붙는 안내 (v10.73.0). 모델은 패키지를 shell 의 `pip`
#: 으로 깔고(pyguide r2 financial t3~t22: 다른 인터프리터에 깔고 shell 로 돌아갔다)
#: 어느 인터프리터인지 알 길이 없으니 **빠진 순간** 경로와 명령을 준다 — 상태 조건부
#: 안내의 원칙 그대로(docs/tail/DESIGN.md §2). v10.78.0 부터 인터프리터는 늘 PATH 의
#: `python3`/`python` 이라 shell 과 같은 파이썬을 가리킨다.
MISSING_MODULE_HINT = (
    "This interpreter is `{exe}`; packages installed into another Python are not "
    "visible here. Install into it from `shell`: `{exe} -m pip install {pkg}` (if it "
    "has no pip: `{exe} -m ensurepip --upgrade` first)."
)
_MISSING_MODULE = re.compile(r"ModuleNotFoundError: No module named '([\w.]+)'")
#: import 이름 ≠ 배포 이름인 흔한 것들 — 안내의 pip 인자용.
_PKG_FOR_MODULE = {
    "PIL": "Pillow",
    "cv2": "opencv-python",
    "yaml": "PyYAML",
    "sklearn": "scikit-learn",
    "fitz": "PyMuPDF",
    "pdfminer": "pdfminer.six",
    "whisper": "openai-whisper",
    "skimage": "scikit-image",
    "bs4": "beautifulsoup4",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "dateutil": "python-dateutil",
}


def missing_module_hint(exc: str | None, exe: str) -> str:
    """``exc`` 가 ModuleNotFoundError 면 안내 한 줄, 아니면 ``""``."""
    m = _MISSING_MODULE.search(exc or "")
    if not m:
        return ""
    top = m.group(1).split(".")[0]
    return MISSING_MODULE_HINT.format(exe=exe, pkg=_PKG_FOR_MODULE.get(top, top))


#: PATH 에 파이썬이 없을 때 (v10.78.0) — agent-cli 자신의 인터프리터로 떨어지지
#: 않는다(그 venv 는 shell 의 pip 과 갈라진다). 워커는 뜰 때마다 다시 고른다.
NO_INTERPRETER = (
    "python: neither `python3` nor `python` is on PATH, so there is no interpreter "
    "to run this in. Install Python from `shell` so that `python3` is on PATH; the "
    "next `python` call starts on it."
)
#: 띄운 인터프리터가 시작 인사 전에 죽었다 (v10.78.0) — 3.8 미만·Python 2·버전 없는
#: pyenv shim·스텁. 코드가 프로세스를 끝낸 것(인사 뒤 죽음)과는 갈라 말한다.
STARTUP_FAILED = (
    "python: `{exe}` exited before answering — this tool needs Python 3.8 or newer. "
    "Put a working one on PATH as `python3`."
)

PYTHON_SESSION_HEADER = "## Python session"
#: 쓰는 동안만 뜨는 사용법 — 종전 도구 설명·Task Guidelines 에 있던 문장들이다.
PYTHON_SESSION_RULES = (
    "Refine the function, do not reload — the data above is still in memory. "
    "One call = one step; a long loop accumulates into a dict or list so a "
    "stopped step keeps what is done. Plot coordinates or shapes instead of reading "
    "numbers. Print only what you need to see (slices, len(), head()). "
    "Deliverables go to files — memory is not an output."
)


def tail_block(state: list[list[str]]) -> str:
    """``## Python session — holding: …`` + 사용법 한 단락. 상태가 비었으면 ``""``
    — 블록은 인터프리터에 **무언가 들어 있는 동안**만 뜬다."""
    if not state:
        return ""
    shown = state[:12]
    parts = [f"{r[0]} ({r[1]})" for r in shown]
    rest = len(state) - len(shown)
    if rest > 0:
        parts.append(f"+{rest} more")
    return (
        f"{PYTHON_SESSION_HEADER} — holding: " + " · ".join(parts) + "\n"
        f"{PYTHON_SESSION_RULES}"
    )


class PythonTool(Tool):
    name = "python"
    #: 세션 레지스트리(`runtime.build_py_registry`)가 없으면 광고되지 않는다 —
    #: `snapshot`·`browser` 와 같은 규칙(없는 기능을 약속하지 않는다).
    requires_handler = "python"
    #: 설명은 **도구의 성질**만 (v10.73.0, docs/tail/DESIGN.md §2): 상태가 남는다,
    #: 결과는 이렇게 생겼다, 그림은 이미지로 온다, 타임아웃이면 어떻게 되나. "언제
    #: 이 도구를 고르나·어떻게 쓰나" 는 사용 가이드라 꼬리에 있다 — 상시 한 줄
    #: (`TASK_GUIDELINES`, 경로 선택) + 상태 조건부(`tail_block`, 쓰는 동안).
    #: 종전엔 둘 다 여기에도 있었고, 꼬리 쪽을 지우자 35B 가 shell 안 `python3 -c`
    #: 로 갔다(2026-10-25 A/B) — 머리의 설명은 도구를 **고른 뒤에** 읽는 글이다.
    description = (
        "Run Python code in a session that keeps state between calls, like a "
        "notebook cell: variables, imports, functions and loaded data stay "
        "available in the next call. The result shows the output, the value of "
        "the last expression and a `state:` line listing what is held in "
        "memory. Open matplotlib figures are saved and shown to you as images. "
        "A step still running after the timeout continues as a background job: "
        "you are told when it ends, and the interpreter is busy until then."
    )
    oversized_retry_hint = (
        "print less: a slice (`x[:20]`), `len(x)`, `.head()`, or a summary "
        "instead of the whole object — the data is still in memory."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "minLength": 1,
                "description": "Python code to run in the persistent session",
            },
            "reset": {
                "type": "boolean",
                "description": (
                    "true starts a fresh interpreter before running `code`; all "
                    "state is dropped. Use it only when the state itself is the "
                    "problem."
                ),
            },
            "timeout": {
                "type": "integer",
                "description": (
                    f"Seconds to wait for this step (default and maximum "
                    f"{DEFAULT_TIMEOUT}). A step still running after that continues "
                    "as a background job and you are told when it ends; lower it "
                    "only for a step you want handed off sooner."
                ),
            },
        },
        "required": ["code"],
    }

    def wrap_single_op(self, flat: dict) -> dict:
        return flat

    def validate(self, args: dict) -> str | None:
        code = args.get("code")
        if not isinstance(code, str) or not code.strip():
            return "python: `code` must be a non-empty string"
        return None

    def summary_arg(self, action_input: dict) -> str:
        # shell 과 같이 전문 — 요약기와 oversized_key 가 뜻을 잃지 않게.
        return str(action_input.get("code") or "")

    def _run(self, args: dict, *, ctx=None) -> ToolResult:
        from agent_cli.jobs import get_job_registry
        from agent_cli.pyworker import FIG_DIR, get_py_registry
        from agent_cli.tools import _confine

        code = str(args.get("code") or "")
        denial = _confine.guard(literal_paths(code), "python code", command=code)
        if denial:
            return ToolResult(False, error=denial)
        reg = get_py_registry()
        if reg is None or reg.closed:
            return ToolResult(
                False,
                error="`python` is not available in this run — use `shell` instead",
            )
        owner = getattr(ctx, "owner", None) or "main"
        if args.get("reset") is True:
            reg.reset(owner)
        worker = reg.get(owner)
        try:
            asked = int(args.get("timeout") or DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            asked = DEFAULT_TIMEOUT
        # 기본값이 상한이다 (v10.91.0): 그 위로는 전경에서 기다리지 않고 셀을 작업으로
        # 넘긴다 — shell 의 전경 한도와 같은 규칙. 작업 레지스트리가 없는 런(통지가 갈
        # 곳이 없다)만 종전대로 한도에 SIGINT.
        timeout = min(asked, DEFAULT_TIMEOUT)
        # 바쁨은 캡처 로그를 비우기 전에 거절한다 (v10.91.2): 그 로그가 돌고 있는 셀의
        # 작업 로그다 — 라이브에서 거절된 `1+1` 이 머리줄을 바꾸고 앞 출력을 지웠다.
        busy = worker.pending
        if busy is not None:
            return ToolResult(False, error=busy_message(busy))
        jobs = get_job_registry()
        pend = jobs is not None and jobs.available
        capture = _capture_log(worker, owner, code) if pend else None
        try:
            res = worker.run(
                code,
                timeout=timeout,
                figdir=Path(FIG_DIR),
                capture=Path.cwd() / capture if capture else None,
                pend=pend,
            )
        except OSError as e:
            return ToolResult(
                False, error=f"python: could not start the interpreter: {e}"
            )
        if res.busy is not None:
            return ToolResult(False, error=busy_message(res.busy))
        if res.pending is not None:
            assert jobs is not None and capture is not None
            job = jobs.adopt(
                res.pending,
                command=_cell_title(code),
                log=capture,
                owner=owner,
                started_at=res.pending.started_at,
            )
            res.pending.job_id = job.id
            return ToolResult(True, output=pending_message(job, timeout, asked))
        return self._format(res, timeout, worker.interpreter)

    @staticmethod
    def _format(res, timeout: int, exe: str | None) -> ToolResult:
        from agent_cli import images as _images

        if res.no_interpreter:
            return ToolResult(False, error=NO_INTERPRETER)
        if res.startup_failed:
            return ToolResult(False, error=STARTUP_FAILED.format(exe=exe))
        if res.timed_out:
            had = f" (it held: {', '.join(res.lost)})" if res.lost else ""
            return ToolResult(
                False,
                error=(
                    f"python: timed out after {timeout}s and did not stop on "
                    f"interrupt — the interpreter was killed and its state is "
                    f"gone{had}. Re-create what you need in a fresh call and keep "
                    "each call short."
                ),
            )
        parts: list[str] = []
        if res.interrupted:
            parts.append(
                f"python: interrupted after {timeout}s (timeout) — the interpreter "
                "is alive and its state is kept. Continue from what is already "
                "done (the traceback shows where it stopped), or pass a larger "
                "`timeout` for this step."
            )
        if res.fresh and res.lost:
            parts.append(
                "(fresh interpreter — the previous state was lost: "
                + ", ".join(res.lost)
                + ")"
            )
        out = res.out.rstrip("\n")
        if out:
            parts.append(out)
        if res.value is not None:
            parts.append(f"=> {res.value}")
        refs: list[dict] = []
        for p in res.figures:
            try:
                ref = _images.make_ref(p)
            except (ValueError, OSError):
                continue
            refs.append(ref.to_dict())
            parts.append(ref.placeholder)
        if not res.ok:
            parts.append(res.exc or "python: failed")
            hint = missing_module_hint(res.exc, exe)
            if hint:
                parts.append(hint)
            parts.append(state_line(res.state))
            return ToolResult(False, error="\n".join(parts), images=refs)
        if not out and res.value is None and not refs:
            parts.append("(no output)")
        parts.append(state_line(res.state))
        return ToolResult(True, output="\n".join(parts), images=refs)
