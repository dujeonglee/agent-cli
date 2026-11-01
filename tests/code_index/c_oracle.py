"""clang 이 본 C 이름 해석 — `resolve/c.py` 와 독립인 정답 (v10.98.0, DESIGN §22).

`clang -fsyntax-only -Xclang -ast-dump=json` 의 `DeclRefExpr` 마다 그것이 가리키는
선언을 읽는다: 인자·지역 변수는 ``("local", None)``, 함수·파일 범위 변수는 그 **정의가
있는 파일** — static 은 그 번역 단위 안의 정의, 아니면 프로젝트 안에서 그 이름을 정의한
파일(링크되는 프로그램이면 하나). 규칙(헤더는 전역 후보, 이름 하나면 resolved …)을 다시
적용하지 않고, clang 이 가리킨 선언 사슬에서 정의 위치를 읽는다. 매크로 전개 안에서
나온 자리는 뺀다(원문에 없는 참조).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

CLANG = shutil.which("clang")


class _Loc:
    """clang JSON 의 위치는 앞 위치와 달라진 필드만 적는다 — 순서대로 따라가며 채운다."""

    def __init__(self):
        self.file = None
        self.line = None

    def take(self, loc):
        if not loc:
            return None
        if "spellingLoc" in loc:
            sp = self.take(loc["spellingLoc"])
            self.take(loc["expansionLoc"])
            return ("macro", sp)
        if "file" in loc:
            self.file = loc["file"]
        if "line" in loc:
            self.line = loc["line"]
        if "col" not in loc:
            return None
        return (self.file, self.line, loc["col"])


def _dump(path: Path, root: Path):
    r = subprocess.run(
        [CLANG, "-fsyntax-only", "-Xclang", "-ast-dump=json", f"-I{root}", str(path)],
        capture_output=True,
        cwd=root,
        check=False,
    )
    if not r.stdout:
        raise RuntimeError(r.stderr.decode(errors="replace")[:500])
    return json.loads(r.stdout)


def answers(root: Path) -> dict:
    """``{(file, line, col0, name): ("local", None) | ("def", file) | ("several", None)
    | ("none", None)}`` — 모든 ``.c``. ``several`` 은 그 이름을 정의한 파일이 여럿(링크되지
    않는 프로젝트 — 어느 것인지 clang 도 모른다), ``none`` 은 프로젝트 안에 정의가 없다."""
    root = root.resolve()
    per_tu = []
    definitions: dict[str, set[str]] = {}  # 비-static 정의: 이름 → 파일들
    for c in sorted(root.rglob("*.c")):
        ast = _dump(c, root)
        loc = _Loc()
        decls: dict[str, tuple] = {}
        static_def: dict[str, str] = {}  # 이 TU 안에 정의가 있는 이름 → 그 파일
        sites = []

        def rel(f):
            try:
                return str(Path(f).resolve().relative_to(root))
            except (TypeError, ValueError):
                return None

        # 재귀 없이 문서 순서대로 (위치 필드는 앞 위치와 달라진 것만 적혀 순서가 중요하다)
        stack = [(ast, False)]
        while stack:
            n, in_fn = stack.pop()
            at = loc.take(n.get("loc"))
            rng = n.get("range") or {}
            begin = loc.take(rng.get("begin"))
            loc.take(rng.get("end"))
            at = at or begin
            kind = n.get("kind")
            where = at[1] if at and at[0] == "macro" else at
            if kind in ("FunctionDecl", "VarDecl", "ParmVarDecl"):
                local = kind == "ParmVarDecl" or (kind == "VarDecl" and in_fn)
                is_def = kind == "FunctionDecl" and any(
                    ch.get("kind") == "CompoundStmt" for ch in n.get("inner", ())
                )
                if kind == "VarDecl" and not in_fn:
                    is_def = n.get("storageClass") != "extern"
                decls[n["id"]] = (local, n.get("name"))
                f = rel(where[0]) if where else None
                if is_def and f is not None:
                    # 이 번역 단위 안의 정의 — clang 의 선언 사슬이 가리키는 것 (static 이든
                    # 아니든; 비-static 은 다른 파일에서 찾도록 프로젝트 목록에도)
                    static_def[n["name"]] = f
                    if n.get("storageClass") != "static":
                        definitions.setdefault(n["name"], set()).add(f)
            if kind == "DeclRefExpr" and at is not None and at[0] != "macro":
                ref = n.get("referencedDecl") or {}
                if ref.get("kind") in ("FunctionDecl", "VarDecl", "ParmVarDecl"):
                    f = rel(at[0])
                    if f is not None:
                        sites.append((f, at[1], at[2] - 1, ref.get("name"), ref["id"]))
            stack.extend(
                (ch, in_fn or kind == "FunctionDecl")
                for ch in reversed(n.get("inner", ()))
            )

        per_tu.append((decls, static_def, sites))
    out: dict = {}
    for decls, static_def, sites in per_tu:
        for f, line, col, name, did in sites:
            local, _n = decls.get(did, (False, name))
            if local:
                ans = ("local", None)
            elif name in static_def:
                ans = ("def", static_def[name])
            else:
                files = definitions.get(name, set())
                if not files:
                    ans = ("none", None)  # 프로젝트 안에 정의가 없다
                elif len(files) == 1:
                    ans = ("def", next(iter(files)))
                else:
                    ans = ("several", None)
            # 헤더 안 자리는 번역 단위마다 같은 답이어야 한다 — 다르면 비교에서 뺀다
            key = (f, line, col, name)
            if key in out and out[key] != ans:
                out[key] = None
            else:
                out.setdefault(key, ans)
    return {k: v for k, v in out.items() if v is not None}
