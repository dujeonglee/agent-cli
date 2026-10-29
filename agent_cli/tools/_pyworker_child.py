"""`python` 도구의 자식 프로세스 (v10.66.0, docs/python-tool/DESIGN.md).

하니스가 ``python3 <이 파일>`` 로 띄운다 — **표준 라이브러리만** 쓰고 agent_cli 를
import 하지 않는다: 모델이 쓰는 인터프리터(작업 환경의 ``python3``)는 agent-cli 가
설치된 인터프리터와 다를 수 있고(harbor 컨테이너의 시스템 python), 거기엔 우리
패키지가 없다. 문법은 3.8 수준으로 둔다.

프로토콜은 JSON 한 줄씩: 시작하면 먼저 ``{"ready": true}`` 를 보내고(부모는 이
인사 전에 죽은 것을 "이 인터프리터로는 안 된다" 로 가른다, v10.78.0), stdin 으로 ``{"code": …, "figdir": …}`` 를 받고,
시작 때 복제해 둔 원래 stdout(``_proto``)으로 ``{"ok", "out", "value", "exc",
"state", "figures"}`` 를 보낸다. 실행 중의 fd 1·2 는 호출마다 임시 파일로 돌려
``print`` 와 자식 프로세스의 출력이 **같은 순서로 한 흐름에** 잡히고, 프로토콜
채널은 더럽혀지지 않는다.

상태는 호출 사이에 그대로 남는다 — 이 도구의 존재 이유. 셀은 진짜 ``__main__``
모듈(``sys.modules["__main__"]``)의 이름 공간에서 돈다(v10.78.0, Jupyter 와 같다):
dict 만 쓰면 셀의 함수는 ``__module__ == "__main__"`` 인데 ``__main__`` 은 이
스크립트라, pickle(multiprocessing fork Pool)·``unittest.main()``·``doctest``·
dataclass 의 문자열 ``ClassVar`` 가 셀의 이름을 찾지 못했다. spawn 방식의 Pool
은 자식이 ``__main__`` 을 다시 import 할 수 없어 여전히 안 된다(멈추면 타임아웃이
끊는다 — 대화형 python 과 같은 한계). 타임아웃에
부모가 보내는 SIGINT 는 실행 중일 때만 ``KeyboardInterrupt`` 로 받고(회신에
``interrupted``), 쉬는 동안은 무시한다 — 회신 직후에 닿은 시그널이 프로세스를
죽이지 않게.

상태 요약은 **쓸모 순**: 데이터(컬렉션·긴 문자열·배열) → 함수·클래스 → 그 밖의
객체 → 스칼라. 파일 핸들은 뺀다. 벤치 실측(2026-10-22)에서 줄의 40~65% 가 루프
변수·핸들이었고 상한 20 에 데이터가 밀려났다.
"""

import ast
import io
import json
import os
import signal
import sys
import tempfile
import traceback
import types

FILENAME = "<python>"
VALUE_MAX = 4000


def _desc(v):
    """상태 요약 한 칸 — 타입과 크기만, 내용은 적지 않는다."""
    t = type(v)
    name = t.__name__
    try:
        if isinstance(v, bool) or v is None:
            return repr(v)
        if isinstance(v, (int, float)):
            r = repr(v)
            return r if len(r) <= 24 else name
        if isinstance(v, str):
            return f"str, {len(v):,} chars"
        if isinstance(v, bytes):
            return f"bytes, {len(v):,}"
        if isinstance(v, dict):
            return f"dict, {len(v):,} keys"
        if isinstance(v, (list, tuple, set, frozenset)):
            return f"{name}, {len(v):,} items"
        shape = getattr(v, "shape", None)
        if isinstance(shape, tuple) and shape:
            return "{} {}".format(name, "×".join(str(d) for d in shape))
        if isinstance(v, type):
            return "class"
        if callable(v):
            return "function"
        if hasattr(v, "__len__"):
            return f"{name}, {len(v):,} items"
    except Exception:
        pass
    return name


#: 상태 요약의 분류 — 앞에 올 것부터. ``None`` 은 뺀다.
_KIND_ORDER = {"data": 0, "code": 1, "object": 2, "scalar": 3}


def _kind(v):
    if isinstance(v, io.IOBase):
        return None
    if isinstance(v, bool) or v is None or isinstance(v, (int, float)):
        return "scalar"
    if isinstance(v, str):
        return "data" if len(v) >= 200 else "scalar"
    if isinstance(v, (bytes, dict, list, tuple, set, frozenset)):
        return "data"
    if isinstance(v, type) or callable(v):
        return "code"
    try:
        if isinstance(getattr(v, "shape", None), tuple) or hasattr(v, "__len__"):
            return "data"
    except Exception:
        pass
    return "object"


def _state(g):
    """``[name, desc, kind]`` 행들 — 쓸모 순(데이터 → 코드 → 객체 → 스칼라), 같은
    분류 안에서는 정의 순. 파일 핸들·모듈·``_`` 이름은 뺀다."""
    rows = []
    for i, (k, v) in enumerate(g.items()):
        if k.startswith("_") or k in ("__builtins__",):
            continue
        if type(v).__name__ == "module":
            continue
        kind = _kind(v)
        if kind is None:
            continue
        rows.append((_KIND_ORDER[kind], i, [k, _desc(v), kind]))
    rows.sort(key=lambda r: (r[0], r[1]))
    return [r[2] for r in rows]


def _figures(figdir):
    """열린 matplotlib figure 를 PNG 로 저장하고 닫는다 — 경로 목록."""
    plt = sys.modules.get("matplotlib.pyplot")
    if plt is None or not figdir:
        return []
    try:
        nums = list(plt.get_fignums())
    except Exception:
        return []
    out = []
    for n in nums:
        try:
            os.makedirs(figdir, exist_ok=True)
            fd, path = tempfile.mkstemp(prefix="fig-", suffix=".png", dir=figdir)
            os.close(fd)
            plt.figure(n).savefig(path, dpi=100, bbox_inches="tight")
            out.append(path)
        except Exception:
            pass
    try:
        plt.close("all")
    except Exception:
        pass
    return out


def _split_last_expr(code):
    """REPL 처럼: 마지막 문장이 식이면 그 값을 돌려주기 위해 둘로 나눈다."""
    tree = ast.parse(code, FILENAME, "exec")
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last = tree.body[-1]
        body = ast.Module(body=tree.body[:-1], type_ignores=[])
        expr = ast.Expression(body=last.value)
        return body, expr
    return tree, None


def _run(code, g):
    """``(ok, value_repr | None, exc_text | None, interrupted)``.

    실행 중에만 SIGINT 를 ``KeyboardInterrupt`` 로 받는다(부모의 타임아웃) — 코드가
    멈추고 전역 상태는 그대로다. 그 밖의 시간엔 무시."""
    signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        try:
            body, expr = _split_last_expr(code)
        except SyntaxError as e:
            # 실행 전의 오류 — 모델의 코드 줄만 뜻이 있다(ast.py 프레임 없이).
            text = "".join(traceback.format_exception_only(type(e), e)).rstrip()
            return False, None, text, False
        exec(compile(body, FILENAME, "exec"), g)  # noqa: S102 — 이 파일의 존재 이유
        if expr is not None:
            val = eval(compile(expr, FILENAME, "eval"), g)
            if val is None:
                return True, None, None, False
            r = repr(val)
            if len(r) > VALUE_MAX:
                r = r[:VALUE_MAX] + f"… ({len(r):,} chars)"
            return True, r, None, False
        return True, None, None, False
    except SystemExit as e:
        # 문자열 코드는 이미 출력에 찍혔다 — 정수만 괄호로(unittest 는 bool 을 준다).
        code_ = int(e.code) if isinstance(e.code, int) else None
        head = "SystemExit" if code_ is None else f"SystemExit({code_})"
        text = (
            f"{head} — the interpreter is still running and its state is kept. "
            "Code here should end by finishing, not by `exit`, `quit` or `sys.exit`."
        )
        return False, None, text, False
    except BaseException as e:
        lines = traceback.format_exception(*sys.exc_info())
        # 우리 프레임(이 파일) 은 뺀다 — 모델에겐 자기 코드의 줄만 뜻이 있다.
        kept = [
            ln for ln in lines if __file__ not in ln and "_pyworker_child" not in ln
        ]
        return False, None, "".join(kept).rstrip(), isinstance(e, KeyboardInterrupt)
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)


def main():
    # 대화형 python3 처럼 지금 폴더가 import 경로의 맨 앞 (v10.77.1). 스크립트로 떠서
    # ``sys.path[0]`` 이 이 파일의 폴더(agent_cli/tools/)였다 — 작업 폴더의 모듈은
    # import 되지 않고(라이브 GLM: 방금 쓴 fib.py → "pip install fib" 안내),
    # ``import shell``·``import history`` 는 하니스의 도구 모듈을 집었다. "" 는
    # 셀이 ``os.chdir`` 해도 따라간다(REPL 과 같다).
    sys.path[0] = ""
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # 실행 중에만 받는다(`_run`)
    proto = os.fdopen(os.dup(1), "w", buffering=1)
    # 이 스크립트의 모듈은 루프가 끝날 때까지 쥐어 둔다 — 함수들의 globals 가 거기 있다.
    script = sys.modules["__main__"]  # noqa: F841
    main_mod = types.ModuleType("__main__")
    main_mod.__builtins__ = __builtins__
    sys.modules["__main__"] = main_mod
    g = main_mod.__dict__
    proto.write(json.dumps({"ready": True}) + "\n")
    proto.flush()
    stdin_fd = os.dup(0)
    reader = os.fdopen(stdin_fd, "r")
    while True:
        line = reader.readline()
        if not line:
            break
        try:
            req = json.loads(line)
        except ValueError:
            continue
        code = req.get("code") or ""
        figdir = req.get("figdir") or ""
        with tempfile.TemporaryFile(mode="w+b") as cap:
            saved = (os.dup(1), os.dup(2))
            os.dup2(cap.fileno(), 1)
            os.dup2(cap.fileno(), 2)
            old = (sys.stdout, sys.stderr, sys.stdin)
            # 버퍼 0: print 와 자식 프로세스의 출력이 실제 순서대로 섞인다.
            sys.stdout = io.TextIOWrapper(
                os.fdopen(os.dup(1), "wb", 0), write_through=True
            )
            sys.stderr = sys.stdout
            sys.stdin = io.StringIO()  # input() → EOFError, 블로킹 없음
            try:
                ok, value, exc, interrupted = _run(code, g)
                figures = _figures(figdir)
            finally:
                try:
                    sys.stdout.flush()
                except Exception:
                    pass
                sys.stdout, sys.stderr, sys.stdin = old
                os.dup2(saved[0], 1)
                os.dup2(saved[1], 2)
                os.close(saved[0])
                os.close(saved[1])
            cap.seek(0)
            out = cap.read().decode("utf-8", "replace")
        reply = {
            "ok": ok,
            "out": out,
            "value": value,
            "exc": exc,
            "interrupted": interrupted,
            "state": _state(g),
            "figures": figures,
        }
        proto.write(json.dumps(reply, ensure_ascii=False) + "\n")
        proto.flush()


if __name__ == "__main__":
    main()
