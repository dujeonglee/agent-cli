"""웹 프런트 정적 소스를 테스트가 읽는 공용 창구 (v10.116.0, docs/web-modules/DESIGN.md §5).

``static/app.js`` 는 진입 모듈이고 실제 코드는 ``static/js/<name>.js`` 에 있다.
소스를 글자로 긁는 테스트는 파일 경로를 직접 열지 말고 여기를 쓴다 — 적재
순서는 진입 파일의 ``import "./js/<name>.js";`` 줄에서 읽으므로 모듈이 늘거나
순서가 바뀌어도 테스트가 따라온다.

- ``STATIC``: ``agent_cli.web`` 패키지의 ``static/`` 디렉터리.
- ``module_order()``: 진입 파일이 적재하는 모듈 이름 목록(적재 순서).
- ``js_text(name=None)``: ``name`` 이면 그 모듈 원문, 없으면 적재 순서로 이어붙인
  전체(파일 사이에 ``\\n``).
- ``js_fn_body(js, name)``: 모듈 스코프 함수 ``function NAME(`` 의 정의를 중괄호
  짝 맞추기로 잘라 돌려준다. 모듈 스코프 = 열 0, 또는 조기 탈출 가드를 뒤집은
  블록(모듈 top-level ``return`` 은 문법 오류, DESIGN §3) 안인 열 2. 같은 이름이
  두 번 이상 나오면 AssertionError — ``js_fn_body(js_text("schedule"), "load")``
  처럼 모듈을 지정해 모호함을 없앤다.
"""

from __future__ import annotations

import re
from pathlib import Path

import agent_cli.web

STATIC = Path(agent_cli.web.__file__).resolve().parent / "static"
ENTRY = STATIC / "app.js"

_IMPORT_RE = re.compile(r'^import "\./js/([a-z_]+)\.js";$', re.MULTILINE)


def module_order() -> list[str]:
    """진입 파일의 ``import "./js/<name>.js";`` 줄을 순서대로 읽은 이름 목록."""
    names = _IMPORT_RE.findall(ENTRY.read_text(encoding="utf-8"))
    assert names, "no module imports found in static/app.js"
    return names


def js_text(name: str | None = None) -> str:
    """모듈 ``name`` 의 원문, 또는 적재 순서로 이어붙인 전체 프런트 소스."""
    if name is not None:
        return (STATIC / "js" / f"{name}.js").read_text(encoding="utf-8")
    return "\n".join(js_text(n) for n in module_order())


def js_fn_body(js: str, name: str) -> str:
    """``js`` 안 모듈 스코프 함수 ``name`` 의 정의(``function`` 부터 닫는 ``}`` 까지)."""
    pat = re.compile(
        r"^(?:  )?(?:export )?(?:async )?function " + re.escape(name) + r"\(",
        re.MULTILINE,
    )
    hits = list(pat.finditer(js))
    assert hits, f"function {name} not found"
    assert len(hits) == 1, (
        f"function {name} defined {len(hits)} times — pass js_text(<module>) to disambiguate"
    )
    start = hits[0].start() + (len(hits[0].group(0)) - len(hits[0].group(0).lstrip()))
    brace = js.index("{", js.index(")", hits[0].end()))
    depth = 0
    for i in range(brace, len(js)):
        if js[i] == "{":
            depth += 1
        elif js[i] == "}":
            depth -= 1
            if depth == 0:
                return js[start : i + 1]
    raise AssertionError(f"unbalanced braces for {name}")
