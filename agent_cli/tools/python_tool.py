"""``python`` 도구 — 상태가 남는 파이썬 실행 (v10.65.0, docs/python-tool/DESIGN.md).

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


def state_line(state: list[list[str]]) -> str:
    """``state: a (int) · texts (dict, 12 keys)`` — 20개까지, 나머지는 수만."""
    if not state:
        return "state: (empty)"
    shown = state[:20]
    parts = [f"{k} ({d})" for k, d in shown]
    rest = len(state) - len(shown)
    if rest > 0:
        parts.append(f"+{rest} more")
    return "state: " + " · ".join(parts)


class PythonTool(Tool):
    name = "python"
    #: 세션 레지스트리(`runtime.build_py_registry`)가 없으면 광고되지 않는다 —
    #: `snapshot`·`browser` 와 같은 규칙(없는 기능을 약속하지 않는다).
    requires_handler = "python"
    description = (
        "Run Python code in a session that KEEPS STATE between calls, like a "
        "notebook cell: variables, imports, functions and data you loaded stay "
        "available in the next call. Use it for data work — load or OCR once "
        "into a variable, then iterate on the parsing/transform function only. "
        "The result shows the output, the value of the last expression and a "
        "`state:` line listing what is held in memory. Open matplotlib figures "
        "are saved and shown to you as images (plot coordinates or paths "
        "instead of reading numbers). Print only what you need to see "
        "(slices, len(), head()). Write final deliverables to files — memory "
        "is not an output."
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
                    "timeout the interpreter is killed and ALL state is lost — "
                    "keep calls short rather than raising this."
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
                    f"python: timed out after {timeout}s — the interpreter was killed "
                    f"and its state is gone{had}. Re-create what you need in a fresh "
                    "call and keep each call short."
                ),
            )
        parts: list[str] = []
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
