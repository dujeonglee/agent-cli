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
from pathlib import Path
from typing import ClassVar

from agent_cli.tools.base import Tool
from agent_cli.tools.result import ToolResult

DEFAULT_TIMEOUT = 120
#: 문자열 리터럴 안의 절대경로·상위 탈출 — 봉쇄 검사 대상.
_PATH_LITERAL = re.compile(r"""['"]((?:/|\.\./)[^'"\n]*)['"]""")


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


PYTHON_SESSION_HEADER = "## Python session"
#: 쓰는 동안만 뜨는 사용법 — 종전 도구 설명·Task Guidelines 에 있던 문장들이다.
PYTHON_SESSION_RULES = (
    "Refine the function, do not reload — the data above is still in memory. "
    "One call = one step; a long loop accumulates into a dict or list so a "
    "timeout keeps what is done. Plot coordinates or shapes instead of reading "
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
        "A call that hits the timeout is interrupted; what it had already "
        "stored stays in memory."
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
                    "true = start a fresh interpreter first (all state is dropped) "
                    "— only when the state itself is the problem."
                ),
            },
            "timeout": {
                "type": "integer",
                "description": (
                    f"Max seconds for this call (default {DEFAULT_TIMEOUT}). On "
                    "timeout the running code is interrupted (KeyboardInterrupt) "
                    "and the state is kept; only code that ignores the interrupt "
                    "gets the interpreter killed. Raise this for a step you know "
                    "is long (a model load, a big OCR batch)."
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
        from agent_cli.pyworker import FIG_DIR, get_py_registry
        from agent_cli.tools import _confine

        code = str(args.get("code") or "")
        denial = _confine.guard(literal_paths(code), "python code", command=code)
        if denial:
            return ToolResult(False, error=denial)
        reg = get_py_registry()
        if reg is None or reg.closed:
            return ToolResult(
                False, error="python is not available in this run — use shell instead"
            )
        owner = getattr(ctx, "owner", None) or "main"
        if args.get("reset") is True:
            reg.reset(owner)
        worker = reg.get(owner)
        try:
            timeout = int(args.get("timeout") or DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT
        try:
            res = worker.run(code, timeout=timeout, figdir=Path(FIG_DIR))
        except OSError as e:
            return ToolResult(
                False, error=f"python: could not start the interpreter: {e}"
            )
        return self._format(res, timeout)

    @staticmethod
    def _format(res, timeout: int) -> ToolResult:
        from agent_cli import images as _images

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
            parts.append(state_line(res.state))
            return ToolResult(False, error="\n".join(parts), images=refs)
        if not out and res.value is None and not refs:
            parts.append("(no output)")
        parts.append(state_line(res.state))
        return ToolResult(True, output="\n".join(parts), images=refs)
