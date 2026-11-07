"""웹 프런트 모듈 사이 계약의 정적 가드 (v10.117.0, docs/web-modules/DESIGN.md §8).

메인 화면은 모듈 18개로 나뉘었다. 모듈 경계에서 지켜야 할 것을 소스 글자로 본다.

- (a) export 는 쓰이는 것만: 모듈이 export 한 이름은 다른 모듈이 import 하거나
  node 하니스(``tests/test_app_markdown.py`` 의 ``HARNESS_IMPORTS``)가 import 한다.
- (b) import 한 이름은 그 모듈이 export 한다(오타·옮긴 이름의 끊긴 고리).
- (c) 순환의 모양: 메인 화면 모듈은 함수 호출로 순환한다(코드 불변 분할로는 피할 수
  없다 — §8.4). 순환에서는 처음 import 된 모듈이 마지막에 평가되므로 진입점이
  평가 시점 안전을 정한다. 순환 밖 모듈은 앞의 모듈만 import 하고, 순환의 진입점은
  channels 다. 실제 평가(TDZ 없음)는 ``test_app_markdown.test_main_screen_graph_evaluates``
  가 node 로 본다.
- (d) ``let``/``var`` 는 정의 모듈만 대입한다: import 한 바인딩은 읽기 전용이다
  (대입하면 런타임 TypeError). 바깥 대입은 정의 모듈의 setter 를 부른다.
"""

from __future__ import annotations

import re

from tests.test_app_markdown import HARNESS_IMPORTS
from tests.web_static import js_text, module_order

_IMPORT = re.compile(r'^import \{([^}]*)\} from "\./([a-z_]+)\.js";', re.MULTILINE)
_EXPORT = re.compile(
    r"^export (?:async )?function ([\w$]+)\(|^export (?:const|let|var) ([\w$]+)",
    re.MULTILINE,
)
_TOP_LET = re.compile(r"^(?:export )?(?:let|var) ([\w$]+)", re.MULTILINE)
_STRING = re.compile(r""""(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`""")


def _code(name: str) -> str:
    """주석과 한 줄 문자열 리터럴의 내용을 걷은 모듈 소스(줄 수 유지) — 대입 검사가
    ``loading="lazy"`` 같은 HTML 조각을 대입으로 읽지 않게."""
    out = []
    in_block = False
    for line in js_text(name).splitlines():
        code = ""
        j = 0
        while j < len(line):
            if in_block:
                k = line.find("*/", j)
                if k < 0:
                    break
                in_block = False
                j = k + 2
                continue
            if line.startswith("/*", j):
                in_block = True
                j += 2
                continue
            if line.startswith("//", j):
                break
            code += line[j]
            j += 1
        out.append(_STRING.sub('""', code))
    return "\n".join(out)


def _imports(name: str) -> dict[str, set[str]]:
    """모듈 ``name`` 의 ``import { … } from "./x.js"`` — 출처 모듈 → 이름들."""
    out: dict[str, set[str]] = {}
    for names, src in _IMPORT.findall(js_text(name)):
        out.setdefault(src, set()).update(
            n.strip() for n in names.split(",") if n.strip()
        )
    return out


def _exports(name: str) -> set[str]:
    return {a or b for a, b in _EXPORT.findall(js_text(name))}


def _graph() -> dict[str, list[str]]:
    return {m: list(_imports(m)) for m in module_order()}


def _cyclic(graph: dict[str, list[str]]) -> list[set[str]]:
    """강연결요소 중 모듈이 둘 이상인 것(순환 묶음)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on: set[str] = set()
    comps: list[set[str]] = []

    def visit(v: str) -> None:
        index[v] = low[v] = len(index)
        stack.append(v)
        on.add(v)
        for w in graph[v]:
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = set()
            while True:
                w = stack.pop()
                on.discard(w)
                comp.add(w)
                if w == v:
                    break
            if len(comp) > 1:
                comps.append(comp)

    for v in graph:
        if v not in index:
            visit(v)
    return comps


def test_exports_are_declarations_only():
    """export 는 선언 앞 접두로만 — 목록(``export {…}``)·default 가 없어 가드가 다 센다."""
    for m in module_order():
        src = js_text(m)
        assert not re.search(r"^export (?:\{|default\b)", src, re.MULTILINE), m
        for line in re.findall(r"^export .*$", src, re.MULTILINE):
            assert _EXPORT.match(line), (m, line)


def test_every_export_is_imported_somewhere():
    """(a) 쓰이지 않는 export 금지 — 다른 모듈 또는 node 하니스가 import 한다."""
    used: dict[str, set[str]] = {}
    for m in module_order():
        for src, names in _imports(m).items():
            used.setdefault(src, set()).update(names)
    for src, names in HARNESS_IMPORTS.items():
        used.setdefault(src, set()).update(names)
    for m in module_order():
        unused = _exports(m) - used.get(m, set())
        assert not unused, f"{m}.js exports unused names: {sorted(unused)}"


def test_every_import_is_exported():
    """(b) import 한 이름은 출처 모듈이 export 한다. 하니스가 import 하는 이름도."""
    for m in module_order():
        for src, names in _imports(m).items():
            missing = names - _exports(src)
            assert not missing, (
                f"{m}.js imports {sorted(missing)} not exported by {src}.js"
            )
    for src, names in HARNESS_IMPORTS.items():
        assert set(names) <= _exports(src), (src, set(names) - _exports(src))


def test_modules_before_the_cycle_import_only_earlier_modules():
    """(c) 순환 밖 잎은 app.js 에 적힌 순서가 곧 평가 순서다 — 앞의 모듈만 import."""
    order = module_order()
    graph = _graph()
    cyclic = set().union(*_cyclic(graph))
    for i, m in enumerate(order):
        if m in cyclic:
            break
        for dep in graph[m]:
            assert dep in order[:i], f"{m}.js imports {dep}.js which loads later"


def test_cycle_is_entered_through_channels():
    """(c) 순환 묶음은 하나, 진입점(app.js 가 처음 import 하는 멤버)은 channels.

    top-level 에서 순환 안 함수를 부르는 곳은 channels.js 의 채널 바 IIFE 하나다
    (``ovSyncChannels()``·``frameShowChannel(ovActiveChannel)`` — frames 의 const 와
    session 을 읽는다). 순환의 진입점은 순환의 다른 모든 모듈이 평가된 뒤에 평가되므로
    channels 가 진입점이면 그 호출이 읽는 바인딩이 전부 초기화돼 있다."""
    order = module_order()
    comps = _cyclic(_graph())
    assert len(comps) == 1, comps
    entry = next(m for m in order if m in comps[0])
    assert entry == "channels", entry


def test_imported_bindings_are_never_assigned():
    """(d) import 한 이름에 대입하지 않는다 — 대입은 정의 모듈의 setter 로."""
    for m in module_order():
        code = _code(m)
        for names in _imports(m).values():
            for n in names:
                pat = re.compile(
                    r"(?<![\w$.])" + re.escape(n) + r"\s*(?:=(?!=)|\+\+|--|[-+*/|&]=)"
                    r"|(?:\+\+|--)" + re.escape(n) + r"(?![\w$])"
                )
                hit = pat.search(code)
                assert not hit, f"{m}.js assigns imported {n}: {hit.group(0)!r}"


def test_top_level_let_is_assigned_only_in_its_module():
    """(d) 다른 모듈의 top-level ``let``/``var`` 이름을 import 없이 대입하지 않는다
    (strict 모드에선 ReferenceError). 바깥은 setter(``setOvActiveChannel`` 등)를 부른다."""
    lets = {m: set(_TOP_LET.findall(js_text(m))) for m in module_order()}
    for home, names in lets.items():
        for n in names:
            pat = re.compile(
                r"(?<![\w$.])" + re.escape(n) + r"\s*(?:=(?!=)|\+\+|--|[-+]=)"
            )
            for m in module_order():
                if m == home:
                    continue
                # 같은 이름의 지역 변수 선언이 있는 모듈은 그 지역 변수의 대입이다.
                code = _code(m)
                if re.search(r"(?:let|var|const)\s+" + re.escape(n) + r"\b", code):
                    continue
                hit = pat.search(code)
                assert not hit, f"{m}.js assigns {home}.js's {n}: {hit.group(0)!r}"


def test_session_state_is_one_object():
    """state.js 는 import 없는 잎이고, 여러 모듈이 대입하는 세션 상태는 객체 하나다."""
    src = js_text("state")
    assert not _imports("state")
    assert _exports("state") == {"session"}
    assert "export const session = {" in src
    for field in ("mainAsk", "connId", "workerBusy", "stopRequested"):
        assert f"  {field}: " in src, field
    js = "\n".join(js_text(m) for m in module_order())
    for old in ("ovMainAsk", "myConnId", "workerBusy", "stopRequested"):
        # 옛 이름은 주석에만 남는다 — 코드는 session.<field> 로 쓴다(필드 키 제외).
        assert not re.search(r"(?<![\w$.])" + old + r"\b(?!\s*:)", _strip_all(js)), old


def _strip_all(js: str) -> str:
    return re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", js, flags=re.DOTALL))


def test_state_and_markdown_are_import_free_leaves():
    """state.js·markdown.js 엔 import 줄이 없다. state 는 순수 데이터 잎이고,
    markdown 은 node 가 DOM 대역 없이 import 하는 순수 모듈이다(DESIGN §8.1·§8.6)."""
    for m in ("state", "markdown"):
        assert not re.search(r"^import\b", js_text(m), re.MULTILINE), m
