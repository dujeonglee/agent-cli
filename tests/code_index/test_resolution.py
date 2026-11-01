"""Stored name resolution (v10.96.0, docs/code-index/DESIGN.md §21).

Three kinds of evidence:

- hand-written answers for the `fixtures/scoping` package — one ref per
  Python rule (module rebinding order, `global`, closures, flat function
  locals, class scope, comprehension and walrus scopes, relative/star/
  re-exported imports, `src/` and `tests/` source roots, `self`/`cls` and
  base classes, unknown-type objects, builtins and outside modules);
- the independent `py_oracle` (CPython `ast` + `symtable`) on the fixtures
  and on generated trees, for the local-vs-follow decision of every bare
  name directly in a `def` body;
- incremental builds compared with a full rebuild table by table, so a
  file that was not re-parsed but depends on what changed is re-resolved.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from agent_cli.code_index import build, load_index
from tests.code_index import py_oracle
from tests.code_index.helpers import FIXTURES


def _index(root: Path, db: Path):
    build(root, db, defs_path=None, verbose=False, force_full=True)
    return load_index(db)


def _resolved(store) -> dict:
    """``{(file, line, name, object): (resolution, "file:qualified" | None)}``."""
    out = {}
    for f, line, name, obj, res, tfile, tqn in store._conn.execute(
        "SELECT r.file, r.line, r.name, r.object, r.resolution, s.file, "
        "s.qualified_name FROM refs r LEFT JOIN symbols s ON s.id = r.target_id"
    ):
        out[(f, line, name, obj)] = (res, f"{tfile}:{tqn}" if tqn else None)
    return out


@pytest.fixture(scope="module")
def scoping(tmp_path_factory):
    return _index(FIXTURES / "scoping", tmp_path_factory.mktemp("sc") / "x.db")


EXPECTED = [
    # 모듈 본문은 위에서 아래로 — def 앞의 호출은 import 한 것, 뒤는 그 def
    (("pkg/rebind.py", 3, "start", None), ("resolved", "pkg/core.py:start")),
    (("pkg/rebind.py", 10, "start", None), ("resolved", "pkg/rebind.py:start")),
    # global 은 모듈의 최종 바인딩
    (("pkg/rebind.py", 15, "start", None), ("resolved", "pkg/rebind.py:start")),
    # `global` 뒤의 대입은 모듈 변수 — 그 이름은 지역이 아니라 모듈에서 찾는다
    (("pkg/rebind.py", 32, "start", None), ("resolved", "pkg/rebind.py:start")),
    # `nonlocal` 은 지금 범위를 건너뛴다 — 감싼 함수의 지역 def
    (("pkg/rebind.py", 49, "stop", None), ("resolved", "pkg/rebind.py:counter.stop")),
    # 클래스 본문의 `outer = None` 은 메서드에서 보이지 않는다 — 모듈 def
    (("pkg/rebind.py", 39, "outer", None), ("resolved", "pkg/rebind.py:outer")),
    # 클로저는 감싼 함수의 지역 def 를 본다
    (("pkg/rebind.py", 23, "start", None), ("resolved", "pkg/rebind.py:outer.start")),
    # 인자는 같은 이름의 import 를 가린다
    (("pkg/sub/util.py", 7, "stop", None), ("local", None)),
    # 지역 별칭은 따라가지 않는다(값 추론 없음) — 별칭에 넣은 이름 자체는 import
    (("pkg/sub/util.py", 11, "stop", None), ("resolved", "pkg/core.py:stop")),
    (("pkg/sub/util.py", 12, "s", None), ("local", None)),
    # 함수 범위는 평평하다 — 대입 앞의 참조도 지역
    (("pkg/sub/util.py", 16, "later", None), ("local", None)),
    # 상대 import 의 모듈 객체·별칭·이름
    (("pkg/sub/util.py", 26, "start", "core"), ("resolved", "pkg/core.py:start")),
    (("pkg/sub/util.py", 27, "E", None), ("resolved", "pkg/core.py:Engine")),
    (("pkg/sub/util.py", 28, "stop", None), ("resolved", "pkg/core.py:stop")),
    # 타입 모를 객체·호출 결과의 메서드
    (("pkg/sub/util.py", 32, "step", "obj"), ("candidates", None)),
    (("pkg/sub/util.py", 27, "run", "<expr>"), ("candidates", None)),
    # 컴프리헨션의 루프 변수는 그 안의 지역, 바깥 이름은 모듈 def
    (("pkg/sub/util.py", 36, "stop", None), ("local", None)),
    (("pkg/sub/util.py", 36, "later", None), ("resolved", "pkg/sub/util.py:later")),
    (("pkg/sub/util.py", 40, "len", None), ("external", None)),
    # star import
    (("pkg/sub/star.py", 5, "start", None), ("resolved", "pkg/core.py:start")),
    (("pkg/sub/star.py", 5, "stop", None), ("resolved", "pkg/core.py:stop")),
    # 클래스 본문의 참조는 모듈 이름, 메서드는 self·cls·베이스로
    (("pkg/core.py", 14, "start", None), ("resolved", "pkg/core.py:start")),
    (("pkg/core.py", 17, "step", "self"), ("resolved", "pkg/core.py:Engine.step")),
    (("pkg/core.py", 25, "build", "cls"), ("resolved", "pkg/core.py:Engine.build")),
    (("pkg/core.py", 37, "run", "self"), ("resolved", "pkg/core.py:Engine.run")),
    (("pkg/core.py", 34, "step", "<expr>"), ("candidates", None)),
    # 바깥 모듈(os)·별칭(osp)
    (("pkg/core.py", 29, "getcwd", "os"), ("external", None)),
    (("pkg/core.py", 29, "join", "osp"), ("external", None)),
    # `import *` 둘 — 두 번째가 정의를 내놓는다; 서브모듈 import 가 star 보다 먼저
    (("pkg/gis_user.py", 5, "Extent", None), ("resolved", "pkg/gis/agg.py:Extent")),
    (
        ("pkg/gis_user.py", 6, "Area", "functions"),
        ("resolved", "pkg/gis/functions.py:Area"),
    ),
    # 소스 루트: src/ 레이아웃, tests/ 패키지, 패키지 __init__ 의 재노출
    (("src/layout/mod.py", 5, "later", None), ("resolved", "pkg/sub/util.py:later")),
    (("tests/app/checks.py", 6, "start", None), ("resolved", "pkg/core.py:start")),
    (("tests/app/checks.py", 7, "Engine", None), ("resolved", "pkg/core.py:Engine")),
    (
        ("tests/app/checks.py", 8, "from_src", None),
        ("resolved", "src/layout/mod.py:from_src"),
    ),
]


@pytest.mark.parametrize(
    ("ref", "answer"), EXPECTED, ids=[f"{r[0]}:{r[1]}:{r[2]}" for r, _ in EXPECTED]
)
def test_hand_written_answers(scoping, ref, answer):
    assert _resolved(scoping)[ref] == answer


def test_every_ref_has_one_known_resolution(scoping):
    rows = list(scoping._conn.execute("SELECT resolution, target_id FROM refs"))
    assert rows
    for res, target in rows:
        assert res in ("resolved", "local", "candidates", "external", "unresolved")
        assert (target is not None) == (res == "resolved")


def _oracle_mismatches(store, root: Path) -> list:
    bad = []
    for (path,) in store._conn.execute(
        "SELECT path FROM files WHERE language = 'python'"
    ):
        rows = list(
            store._conn.execute(
                "SELECT line, name, resolution FROM refs WHERE file = ? "
                "AND object IS NULL AND kind IN ('call', 'name')",
                (path,),
            )
        )
        src = (root / path).read_text()
        verdicts = py_oracle.classify(src, path, [(ln, n) for ln, n, _ in rows])
        for line, name, res in rows:
            want = verdicts.get((line, name))
            if want is None:
                continue
            got = "local" if res == "local" else "follow"
            if got != want:
                bad.append((path, line, name, res, want))
    return bad


@pytest.mark.parametrize("lang", ["python", "scoping"])
def test_fixtures_agree_with_the_symtable_oracle(tmp_path, lang):
    store = _index(FIXTURES / lang, tmp_path / "x.db")
    assert _oracle_mismatches(store, FIXTURES / lang) == []


_NAMES = ["a", "b", "c", "d", "e"]


@st.composite
def _modules(draw):
    """작은 이름 알파벳으로 충돌을 강제한 모듈 2~3개 — 서로 import, 인자·지역·중첩
    def·nonlocal·global 을 섞는다."""
    files = {}
    n = draw(st.integers(2, 3))
    for i in range(n):
        lines = []
        other = f"m{(i + 1) % n}"
        imp = draw(st.sampled_from(_NAMES))
        lines.append(f"from {other} import {imp}")
        for _ in range(draw(st.integers(1, 4))):
            fn = draw(st.sampled_from(_NAMES))
            param = draw(st.sampled_from(["x", *_NAMES]))
            body = []
            for _ in range(draw(st.integers(0, 4))):
                nm = draw(st.sampled_from(_NAMES))
                body.append(
                    draw(
                        st.sampled_from(
                            [
                                f"{nm}()",
                                f"keep({nm})",
                                f"{nm} = 1",
                                f"for {nm} in x: pass",
                            ]
                        )
                    )
                )
            if draw(st.booleans()):
                inner = draw(st.sampled_from(_NAMES))
                body.append(
                    f"def {inner}():\n        return {draw(st.sampled_from(_NAMES))}()"
                )
            if draw(st.booleans()):
                # `global` 은 맨 앞 — 사용 뒤의 `global` 은 파이썬이 거부한다
                g = draw(st.sampled_from([n for n in _NAMES if n != param]))
                body.insert(0, f"global {g}")
            text = "\n    ".join(body or ["pass"])
            lines.append(f"def {fn}({param}):\n    {text}\n")
        lines.append("def keep(v):\n    return v\n")
        files[f"m{i}.py"] = "\n\n".join(lines)
    return files


class TestGeneratedTrees:
    @settings(
        max_examples=30,
        deadline=None,
        suppress_health_check=[
            HealthCheck.function_scoped_fixture,
            HealthCheck.too_slow,
        ],
    )
    @given(files=_modules())
    def test_bare_names_agree_with_the_oracle(self, tmp_path_factory, files):
        root = tmp_path_factory.mktemp("gen")
        for name, text in files.items():
            (root / name).write_text(text)
        try:
            for text in files.values():
                compile(text, "<gen>", "exec")
        except SyntaxError:
            return  # `global x` 뒤의 사용 같은 생성 조합 — 파이썬이 거부하는 것은 건너뛴다
        store = _index(root, root / ".db")
        assert _oracle_mismatches(store, root) == []


def _snapshot(db: Path) -> dict:
    """기호 id 를 ``(file, qualified_name, line)`` 로 바꾼 해석 결과 — 증분 = 전체 비교용.

    맨 식별자 참조(``name``)는 색인 어딘가에 그 이름의 정의가 있을 때만 생긴다. 정의가
    사라진 이름의 ``name`` 참조는 그 파일을 다시 파싱할 때까지 남는다 — 다시 해석돼
    ``unresolved`` 가 되니 간선은 아니다(v10.96.0 이전부터의 동작). 비교에서 뺀다."""
    store = load_index(db)
    defined = {
        r[0]
        for r in store._conn.execute(
            "SELECT DISTINCT name FROM symbols WHERE kind != 'section'"
        )
    }
    key = {
        r[0]: (r[1], r[2], r[3])
        for r in store._conn.execute(
            "SELECT id, file, qualified_name, line FROM symbols"
        )
    }
    return {
        (f, line, col, name): (res, key.get(t))
        for f, line, col, name, kind, res, t in store._conn.execute(
            "SELECT file, line, col, name, kind, resolution, target_id FROM refs"
        )
        if kind != "name" or name in defined
    }


EDITS = {
    "body only": ("pkg/core.py", lambda s: s.replace("return 1", "return 11")),
    "lines shift": ("pkg/core.py", lambda s: "# header\n\n" + s),
    "function removed": (
        "pkg/core.py",
        lambda s: s.replace("def stop():\n    return 0\n", ""),
    ),
    "function added": (
        "pkg/sub/util.py",
        lambda s: s + "\n\ndef start():\n    return 9\n",
    ),
    "re-export dropped": ("pkg/__init__.py", lambda s: s.replace(", start", "")),
    "base changed": (
        "pkg/core.py",
        lambda s: s.replace("class Turbo(Engine)", "class Turbo"),
    ),
    "file deleted": ("src/layout/mod.py", None),
}


@pytest.mark.parametrize("edit", list(EDITS), ids=list(EDITS))
def test_incremental_equals_full_rebuild(tmp_path, edit):
    root = tmp_path / "tree"
    shutil.copytree(FIXTURES / "scoping", root)
    inc = tmp_path / "inc.db"
    build(root, inc, defs_path=None, verbose=False)
    rel, change = EDITS[edit]
    p = root / rel
    if change is None:
        p.unlink()
    else:
        p.write_text(change(p.read_text()))
    stats = build(root, inc, defs_path=None, verbose=False)
    full = tmp_path / "full.db"
    build(root, full, defs_path=None, verbose=False, force_full=True)
    assert _snapshot(inc) == _snapshot(full)
    if edit == "body only":
        assert stats["resolved"] == 1  # 다른 파일은 다시 해석하지 않는다


def _tree(root: Path, files: dict) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


#: 리뷰(2026-11-01)가 찾은 증분 ≠ 전체 사례 — (처음 트리, 편집, 지울 파일)
REVIEW_EDITS = {
    # A. 같은 이름이 다른 모듈에 이미 있을 때 새 def — 식별자 비교는 아무것도 다시 안 걷는다
    "added name that exists elsewhere": (
        {
            "pkg/__init__.py": "",
            "pkg/core.py": "def start():\n    return 1\n",
            "pkg/util.py": "def helper():\n    return 0\n",
            "a.py": "from pkg import core\n\n\ndef a():\n    return core.helper()\n",
            "b.py": "import pkg.core\n\n\ndef b():\n    return pkg.core.helper()\n",
            "c.py": "from pkg.core import helper\n\n\ndef c():\n    return helper()\n",
            "d.py": "from pkg.core import *\n\n\ndef d():\n    return helper()\n",
        },
        {
            "pkg/core.py": "def start():\n    return 1\n\n\ndef helper():\n    return 2\n"
        },
        [],
    ),
    # B. 손자 클래스의 self.m() 이 바뀐 베이스를 거친다
    "method added to a grandparent class": (
        {
            "pkg/__init__.py": "",
            "pkg/core.py": "class Base:\n    pass\n",
            "pkg/util.py": "def helper():\n    return 0\n",
            "pkg/mid.py": "from pkg.core import Base\n\n\nclass Child(Base):\n    pass\n",
            "pkg/leaf.py": (
                "from pkg.mid import Child\n\n\nclass GC(Child):\n"
                "    def go(self):\n        return self.helper()\n"
            ),
        },
        {"pkg/core.py": "class Base:\n    def helper(self):\n        return 1\n"},
        [],
    ),
    # C. import 하던 모듈 파일이 새로 생긴다
    "new module file": (
        {
            "pkg/__init__.py": "",
            "pkg/util.py": "def helper():\n    return 0\n",
            "a.py": "from pkg import newmod\n\n\ndef a():\n    return newmod.helper()\n",
        },
        {"pkg/newmod.py": "def helper():\n    return 1\n"},
        [],
    ),
    # E. 같은 이름의 def 를 하나 더 — 마지막 것이 이긴다
    "second def of the same name": (
        {
            "core.py": "def f():\n    return 1\n",
            "a.py": "from core import f\n\n\ndef a():\n    return f()\n",
        },
        {"core.py": "def f():\n    return 1\n\n\ndef f():\n    return 2\n"},
        [],
    ),
    # H. __init__.py 를 더하면 소스 루트가 바뀐다
    "package made by adding __init__": (
        {
            "lib/tools.py": "def helper():\n    return 1\n",
            "app/main.py": "from lib.tools import helper\n\n\ndef m():\n    return helper()\n",
        },
        {"lib/__init__.py": ""},
        [],
    ),
    # 리뷰 2회차: 재노출·`*` 를 거친 전이 의존
    "re-export retargeted in the source": (
        {
            "pkg/__init__.py": "from .core import start\n",
            "pkg/core.py": "def start():\n    return 1\n",
            "pkg/other.py": "def start():\n    return 2\n",
            "user.py": "from pkg import start\n\n\ndef u():\n    return start()\n",
        },
        {
            "pkg/core.py": "def start():\n    return 1\n\n\nfrom pkg.other import start\n"
        },
        [],
    ),
    "two-level re-export": (
        {
            "pkg/__init__.py": "from .sub import start\n",
            "pkg/sub/__init__.py": "from .core import start\n",
            "pkg/sub/core.py": "def start():\n    return 1\n",
            "pkg/sub/other.py": "def start():\n    return 2\n",
            "user.py": "from pkg import start\n\n\ndef u():\n    return start()\n",
        },
        {"pkg/sub/__init__.py": "from .other import start\n"},
        [],
    ),
    "star chain": (
        {
            "pkg/__init__.py": "",
            "pkg/core.py": "def start():\n    return 1\n",
            "pkg/util.py": "def helper():\n    return 0\n",
            "a.py": "from pkg.core import *\n",
            "b.py": "from a import *\n",
            "c.py": "from b import *\n\n\ndef c():\n    return helper()\n",
        },
        {
            "pkg/core.py": "def start():\n    return 1\n\n\ndef helper():\n    return 2\n"
        },
        [],
    ),
    "class through a re-export": (
        {
            "pkg/__init__.py": "from .core import Base\n",
            "pkg/core.py": "class Base:\n    pass\n",
            "pkg/util.py": "def helper():\n    return 0\n",
            "leaf.py": (
                "from pkg import Base\n\n\nclass GC(Base):\n"
                "    def go(self):\n        return self.helper()\n"
            ),
        },
        {"pkg/core.py": "class Base:\n    def helper(self):\n        return 1\n"},
        [],
    ),
    "four-level base chain": (
        {
            "pkg/__init__.py": "",
            "pkg/util.py": "def helper():\n    return 0\n",
            "pkg/l0.py": "class B0:\n    pass\n",
            "pkg/l1.py": "from pkg.l0 import B0\n\n\nclass B1(B0):\n    pass\n",
            "pkg/l2.py": "from pkg.l1 import B1\n\n\nclass B2(B1):\n    pass\n",
            "pkg/l3.py": "from pkg.l2 import B2\n\n\nclass B3(B2):\n    pass\n",
            "pkg/l4.py": (
                "from pkg.l3 import B3\n\n\nclass B4(B3):\n"
                "    def go(self):\n        return self.helper()\n"
            ),
        },
        {"pkg/l0.py": "class B0:\n    def helper(self):\n        return 1\n"},
        [],
    ),
    # 리뷰 3회차: 옛 기호 id 가 남는 갱신·모듈 객체(대상 기호 없음)를 거친 전파
    "re-export removed two levels up": (
        {
            "pkg/__init__.py": "from .sub import start\n",
            "pkg/sub/__init__.py": "from .core import start\n",
            "pkg/sub/core.py": "def start():\n    return 1\n",
            "u.py": "from pkg import start\n\n\ndef go():\n    return start()\n",
        },
        {"pkg/sub/__init__.py": "\n"},
        [],
    ),
    "base as a module attribute (django models.Model)": (
        {
            "pkg/__init__.py": "",
            "pkg/models/__init__.py": "from pkg.models.base import Model\n",
            "pkg/models/base.py": "class Model:\n    def save(self):\n        return 1\n",
            "pkg/util.py": "def clean():\n    return 0\n",
            "app/__init__.py": "",
            "app/models.py": (
                "from pkg import models\n\n\nclass Foo(models.Model):\n"
                "    def go(self):\n        return self.clean()\n"
            ),
        },
        {
            "pkg/models/base.py": (
                "class Model:\n    def save(self):\n        return 1\n\n"
                "    def clean(self):\n        return 2\n"
            )
        },
        [],
    ),
    "base as an attribute of a package that re-exports it": (
        {
            "pkg/__init__.py": "from .core import Base\n",
            "pkg/core.py": "class Base:\n    pass\n",
            "pkg/other.py": "def helper():\n    return 9\n",
            "leaf.py": (
                "import pkg\n\n\nclass GC(pkg.Base):\n"
                "    def x(self):\n        return self.helper()\n"
            ),
        },
        {"pkg/core.py": "class Base:\n    def helper(self):\n        return 2\n"},
        [],
    ),
    "grandchild through a module import of the middle file": (
        {
            "pkg/__init__.py": "",
            "pkg/other.py": "def helper():\n    return 9\n",
            "pkg/core.py": "class Base:\n    pass\n",
            "pkg/mid.py": "from pkg.core import Base\n\n\nclass Child(Base):\n    pass\n",
            "leaf.py": (
                "from pkg import mid\n\n\nclass GC(mid.Child):\n"
                "    def x(self):\n        return self.helper()\n"
            ),
        },
        {"pkg/core.py": "class Base:\n    def helper(self):\n        return 2\n"},
        [],
    ),
    "module object re-exported": (
        {
            "pkg/__init__.py": "",
            "pkg/other.py": "def helper():\n    return 9\n",
            "pkg/core.py": "def start():\n    return 1\n",
            "pkg/f.py": "from pkg import core\n",
            "g.py": "from pkg import f\n\n\ndef go():\n    return f.core.helper()\n",
        },
        {
            "pkg/core.py": "def start():\n    return 1\n\n\ndef helper():\n    return 2\n"
        },
        [],
    ),
    # 리뷰 4회차: 모듈 객체 재노출의 대상만 바뀜, 클래스 없는 재노출 파일에서 바뀐 베이스
    # — 사례별 전파 규칙 대신 "해석이 읽은 것" 을 적어 두고 그것이 바뀐 파일만 다시
    "package rebinds a submodule name": (
        {
            "pkg/__init__.py": "",
            "other.py": "def helper():\n    return 9\n",
            "pkg/mixins.py": "def helper():\n    return 1\n",
            "pkg/other2.py": "def start():\n    return 1\n",
            "u.py": "from pkg import mixins\n\n\ndef go():\n    return mixins.helper()\n",
        },
        {"pkg/__init__.py": "from . import other2 as mixins\n"},
        [],
    ),
    "package drops a module re-export": (
        {
            "pkg/__init__.py": "from . import other2 as mixins\n",
            "other.py": "def helper():\n    return 9\n",
            "pkg/mixins.py": "def helper():\n    return 1\n",
            "pkg/other2.py": "def start():\n    return 1\n",
            "u.py": "from pkg import mixins\n\n\ndef go():\n    return mixins.helper()\n",
        },
        {"pkg/__init__.py": ""},
        [],
    ),
    "two-level module re-export retargeted": (
        {
            "pkg/__init__.py": "from .db import models\n",
            "pkg/db/__init__.py": "",
            "pkg/db2/__init__.py": "",
            "pkg/db/models.py": "def helper():\n    return 1\n",
            "pkg/db2/models.py": "def start():\n    return 1\n",
            "other.py": "def helper():\n    return 9\n",
            "u.py": "from pkg import models\n\n\ndef go():\n    return models.helper()\n",
        },
        {"pkg/__init__.py": "from .db2 import models\n"},
        [],
    ),
    "mixin re-export retargeted under a grandchild": (
        {
            "pkg/__init__.py": "",
            "pkg/m1.py": "class Mixin:\n    def helper(self):\n        return 1\n",
            "pkg/m2.py": "class Mixin:\n    def helper(self):\n        return 2\n",
            "pkg/a.py": "from pkg.m1 import Mixin\n",
            "pkg/core.py": "from pkg import a as amod\n\n\nclass Base(amod.Mixin):\n    pass\n",
            "pkg/leaf.py": "from pkg.core import Base\n\n\nclass GC(Base):\n    pass\n",
            "pkg/leaf2.py": (
                "from pkg.leaf import GC\n\n\nclass GGC(GC):\n"
                "    def x(self):\n        return self.helper()\n"
            ),
        },
        {"pkg/a.py": "from pkg.m2 import Mixin\n"},
        [],
    ),
    "base module alias retargeted": (
        {
            "pkg/__init__.py": "from . import b1 as base\n",
            "other.py": "def helper():\n    return 9\n",
            "pkg/b1.py": "class Model:\n    def helper(self):\n        return 1\n",
            "pkg/b2.py": "class Model:\n    def helper(self):\n        return 2\n",
            "u.py": (
                "from pkg import base\n\n\nclass X(base.Model):\n"
                "    def go(self):\n        return self.helper()\n"
            ),
        },
        {"pkg/__init__.py": "from . import b2 as base\n"},
        [],
    ),
    # 그 이름의 마지막 파이썬 함수가 사라지면 `obj.m()` 은 `external` — 함수 이름도 읽은 것
    "last function of a name removed": (
        {
            "pkg/__init__.py": "",
            "pkg/core.py": "def step():\n    return 1\n",
            "user.py": "def go(obj):\n    return obj.step()\n",
        },
        {"pkg/core.py": "def other():\n    return 1\n"},
        [],
    ),
    # 읽은 것 기록의 각 갈래 — 순환 중인 메모, 상대 경로, 베이스만, `*` 추가
    "a cycle hides a later definition": (
        {
            "a.py": "from b import *\n",
            "b.py": "from a import *\nfrom c import *\n",
            "c.py": "def f():\n    return 3\n",
            "u1.py": "from a import f\n\n\ndef g():\n    return f()\n",
            "v.py": "from b import f\n\n\ndef h():\n    return f()\n",
        },
        {"a.py": "from b import *\n\n\ndef f():\n    return 1\n"},
        [],
    ),
    "relative import gains its module": (
        {
            "pkg/__init__.py": "",
            "pkg/core.py": "def helper():\n    return 1\n",
            "pkg/a.py": "from .newmod import helper\n\n\ndef a():\n    return helper()\n",
        },
        # 새 모듈은 그 이름을 정의하지 않고 재노출만 — 이름 비교(Pass-2)로는 안 잡힌다
        {"pkg/newmod.py": "from pkg.core import helper\n"},
        [],
    ),
    "only a base expression changes": (
        {
            "pkg/__init__.py": "",
            "pkg/m.py": (
                "class M1:\n    def helper(self):\n        return 1\n\n\n"
                "class M2:\n    def helper(self):\n        return 2\n"
            ),
            "pkg/core.py": "from pkg.m import M1, M2\n\n\nclass Base(M1):\n    pass\n",
            "leaf.py": (
                "from pkg.core import Base\n\n\nclass GC(Base):\n"
                "    def x(self):\n        return self.helper()\n"
            ),
        },
        {"pkg/core.py": "from pkg.m import M1, M2\n\n\nclass Base(M2):\n    pass\n"},
        [],
    ),
    "a star import is added": (
        {
            "pkg/__init__.py": "",
            "pkg/core.py": "def helper():\n    return 1\n",
            "pkg/a.py": "X = 1\n",
            "u.py": "from pkg.a import helper\n\n\ndef go():\n    return helper()\n",
        },
        {"pkg/a.py": "X = 1\nfrom pkg.core import *\n"},
        [],
    ),
    # 리뷰 5회차: 메모가 진입점에 따라 다른 값을 담으면 해석 묶음(증분은 바뀐 파일만)에
    # 따라 답이 갈린다 — 순환에 걸린 결과·깊이 상한에 닿은 결과는 메모하지 않는다
    "star cycle cut depends on the entry": (
        {
            "a.py": "from b import *\nfrom c import *\n",
            "b.py": "from a import *\n",
            "c.py": "def x():\n    return 1\n",
            "aa.py": "from a import x\n\n\ndef g():\n    return x()\n",
            "u.py": "from b import x\n\n\ndef h():\n    return x()\n",
        },
        {"u.py": "from b import x\n\n\ndef h():\n    return x()\n# touched\n"},
        [],
    ),
    "import depth cut depends on the entry": (
        {
            "m0.py": "from m1 import x\n",
            "m1.py": "from m2 import x\n",
            "m2.py": "from m3 import x\n",
            "m3.py": "from m4 import x\n",
            "m4.py": "from m5 import x\n",
            "m5.py": "from m6 import x\n",
            "m6.py": "from m7 import x\n",
            "m7.py": "from m8 import x\n",
            "m8.py": "from m9 import x\n",
            "m9.py": "from m10 import x\n",
            "m10.py": "def x():\n    return 1\n",
            "u.py": "from m0 import x\n\n\ndef g():\n    return x()\n",
            "v.py": "from m4 import x\n\n\ndef h():\n    return x()\n",
        },
        {"v.py": "from m4 import x\n\n\ndef h():\n    return x()\n# touched\n"},
        [],
    ),
    # 리뷰 7회차: 깊이 상한이 있으면 얕은 진입이 메모한 깨끗한 값을 깊은 진입이 받는다
    "two chains of different length into one tail": (
        {
            "c4.py": "from c5 import x\n",
            "c5.py": "from c6 import x\n",
            "c6.py": "from c7 import x\n",
            "c7.py": "from c8 import x\n",
            "c8.py": "from c9 import x\n",
            "c9.py": "from c10 import x\n",
            "c10.py": "from m6 import x\n",
            "m6.py": "from m4 import x\n",
            "m4.py": "from m3 import x\n",
            "m3.py": "from m7 import *\n",
            "m7.py": "def x():\n    return 1\n",
            "u0.py": "from c7 import *\n\n\ndef go():\n    return x()\n",
            "u1.py": "from c5 import *\n\n\ndef go():\n    return x()\n",
        },
        {"u1.py": "from c5 import *\n\n\ndef go():\n    return x()\n# touched\n"},
        [],
    ),
    "module file removed": (
        {
            "pkg/__init__.py": "",
            "pkg/core.py": "def start():\n    return 1\n",
            "a.py": "from pkg.core import start\n\n\ndef a():\n    return start()\n",
        },
        {},
        ["pkg/core.py"],
    ),
}


@pytest.mark.parametrize("case", list(REVIEW_EDITS), ids=list(REVIEW_EDITS))
def test_review_edits_incremental_equals_full(tmp_path, case):
    before, edits, deletes = REVIEW_EDITS[case]
    root = tmp_path / "tree"
    _tree(root, before)
    inc = tmp_path / "inc.db"
    build(root, inc, defs_path=None, verbose=False)
    _tree(root, edits)
    for rel in deletes:
        (root / rel).unlink()
    build(root, inc, defs_path=None, verbose=False)
    full = tmp_path / "full.db"
    build(root, full, defs_path=None, verbose=False, force_full=True)
    assert _snapshot(inc) == _snapshot(full)


def _refs_after(db: Path, batches: list[list[str]]) -> dict:
    """같은 표를 ``batches`` 묶음으로 다시 해석한 뒤의 참조 답."""
    from agent_cli.code_index._sqlite import sqlite3
    from agent_cli.code_index.resolve import resolve_files

    conn = sqlite3.connect(str(db))
    try:
        conn.execute("UPDATE refs SET resolution = 'unresolved', target_id = NULL")
        for batch in batches:
            resolve_files(conn, batch)
        return {
            (f, line, col, name): (res, t)
            for f, line, col, name, res, t in conn.execute(
                "SELECT file, line, col, name, resolution, target_id FROM refs"
            )
        }
    finally:
        conn.close()


@pytest.mark.parametrize(
    "case",
    ["star cycle cut depends on the entry", "import depth cut depends on the entry"],
)
def test_answers_do_not_depend_on_the_batch(tmp_path, case):
    """해석은 표의 함수다 — 파일을 한꺼번에, 거꾸로, 하나씩 해석해도 같은 답 (메모가
    진입점에 기댄 값을 담으면 증분과 전체가 갈린다)."""
    before, _edits, _deletes = REVIEW_EDITS[case]
    root = tmp_path / "tree"
    _tree(root, before)
    db = tmp_path / "x.db"
    build(root, db, defs_path=None, verbose=False, force_full=True)
    files = sorted(before)
    together = _refs_after(db, [files])
    assert _refs_after(db, [files[::-1]]) == together
    assert _refs_after(db, [[f] for f in files]) == together


def test_dense_star_cycle_stays_cheap(tmp_path):
    """`*` 로 서로를 모두 import 하는 모듈 12개에서 없는 이름 찾기 — 순환에 기댄 메모를
    바로 버리면 같은 계산을 가지마다 다시 해 지수(수십 초)."""
    import time

    root = tmp_path / "tree"
    n = 12
    _tree(
        root,
        {
            **{
                f"m{i}.py": "".join(f"from m{j} import *\n" for j in range(n) if j != i)
                for i in range(n)
            },
            "u.py": "from m0 import missing\n\n\ndef go():\n    return missing()\n",
        },
    )
    t = time.perf_counter()
    build(root, tmp_path / "x.db", defs_path=None, verbose=False, force_full=True)
    assert time.perf_counter() - t < 5


def _random_import_graph(rng) -> dict:
    """모듈 6~12개가 서로를 `*`·이름·별칭·모듈째 import 하는 그래프(순환 포함), 절반은 긴
    재노출 사슬(8~12단)도 + 이름을 찾는 사용자 파일들 — 메모 공유가 진입점에 기대는 모양을
    넓게 만든다(리뷰 7회차의 퍼즈를 줄인 것)."""
    names = ["x", "y", "z"]
    n = rng.randint(6, 12)
    files = {}
    for i in range(n):
        lines = []
        for _ in range(rng.randint(0, 4)):
            j, k = rng.randrange(n), rng.random()
            if k < 0.4:
                lines.append(f"from m{j} import *")
            elif k < 0.7:
                lines.append(f"from m{j} import {rng.choice(names)}")
            elif k < 0.8:
                lines.append(
                    f"from m{j} import {rng.choice(names)} as {rng.choice(names)}"
                )
            else:
                lines.append(f"import m{j}")
        if rng.random() < 0.3:
            lines.append(f"def {rng.choice(names)}():\n    return {i}")
        if rng.random() < 0.15:
            lines.append("class K(object):\n    def x(self):\n        return self.y()")
        files[f"m{i}.py"] = "\n".join(lines) + "\n"
    if rng.random() < 0.5:
        length = rng.randint(8, 12)
        for c in range(length):
            files[f"c{c}.py"] = f"from c{c + 1} import x\n"
        files[f"c{length}.py"] = f"from m{rng.randrange(n)} import *\n"
    mods = [m[:-3] for m in files]
    for u in range(rng.randint(3, 5)):
        body = []
        for _ in range(rng.randint(1, 3)):
            mod, k = rng.choice(mods), rng.random()
            if k < 0.4:
                body.append(f"from {mod} import *")
            elif k < 0.8:
                body.append(f"from {mod} import {rng.choice(names)}")
            else:
                body.append(f"import {mod}")
        calls = "".join(f"    {nm}()\n" for nm in names)
        calls += "".join(
            f"    {m}.{nm}()\n" for m in rng.sample(mods, 2) for nm in names[:2]
        )
        files[f"u{u}.py"] = "\n".join(body) + "\n\n\ndef go():\n" + calls
    return files


def test_random_import_graphs_do_not_depend_on_the_batch(tmp_path):
    import random

    for seed in range(150):
        rng = random.Random(seed)
        files = _random_import_graph(rng)
        root = tmp_path / f"g{seed}"
        _tree(root, files)
        db = tmp_path / f"g{seed}.db"
        build(root, db, defs_path=None, verbose=False, force_full=True)
        order = sorted(files)
        together = _refs_after(db, [order])
        shuffled = order[:]
        rng.shuffle(shuffled)
        assert _refs_after(db, [shuffled]) == together, seed
        assert _refs_after(db, [order[::-1]]) == together, seed
        assert _refs_after(db, [[f] for f in order]) == together, seed
        chunks = [shuffled[i : i + 3] for i in range(0, len(shuffled), 3)]
        assert _refs_after(db, chunks) == together, seed


def _chain(root: Path, length: int) -> None:
    files = {f"c{c}.py": f"from c{c + 1} import x\n" for c in range(length)}
    files[f"c{length}.py"] = "def x():\n    return 1\n"
    files["u.py"] = "from c0 import x\n\n\ndef go():\n    return x()\n"
    _tree(root, files)


def test_a_long_reexport_chain_resolves(tmp_path):
    """import 깊이 상한이 없다 — 100단 재노출 사슬도 끝까지 따라간다."""
    _chain(tmp_path / "t", 100)
    store = _index(tmp_path / "t", tmp_path / "x.db")
    assert _resolved(store)[("u.py", 5, "x", None)] == ("resolved", "c100.py:x")


def test_a_chain_past_the_recursion_limit_fails_the_build_plainly(tmp_path):
    """재귀 한도에 닿으면 잘라서 답을 내지 않는다(그 답은 해석 묶음에 기댄다) — 빌드가
    분명한 오류로 실패하고, 있던 색인은 그대로."""
    root = tmp_path / "t"
    _chain(root, 5)
    db = tmp_path / "x.db"
    build(root, db, defs_path=None, verbose=False, force_full=True)
    before = db.read_bytes()
    shutil.rmtree(root)
    _chain(root, 600)
    with pytest.raises(RuntimeError, match="import chain is too deep"):
        build(root, db, defs_path=None, verbose=False, force_full=True)
    assert db.read_bytes() == before


def test_fixture_answers_do_not_depend_on_the_batch(tmp_path):
    root = tmp_path / "scoping"
    shutil.copytree(FIXTURES / "scoping", root)
    db = tmp_path / "x.db"
    build(root, db, defs_path=None, verbose=False, force_full=True)
    files = sorted(str(p.relative_to(root)) for p in root.rglob("*.py") if p.is_file())
    together = _refs_after(db, [files])
    assert _refs_after(db, [files[::-1]]) == together
    assert _refs_after(db, [[f] for f in files]) == together


def test_import_forms_and_aliases(tmp_path):
    """리뷰 2·3: `import a.b.c as x` 는 모듈 a.b.c, `from pkg import core` 의 모듈 이름은
    바인딩이 아니다(앞의 `import pkg` 를 덮지 않는다)."""
    _tree(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/core.py": "def start():\n    return 1\n",
            "pkg/sub/__init__.py": "",
            "pkg/sub/util.py": "def helper():\n    return 2\n",
            "m.py": (
                "import pkg\n"
                "from pkg import core\n"
                "import pkg.sub.util as u\n"
                "import pkg.sub as ps\n"
                "import pkg.sub.util\n\n\n"
                "def go():\n"
                "    pkg.core.start()\n"
                "    u.helper()\n"
                "    ps.util.helper()\n"
                "    pkg.sub.util.helper()\n"
                "    return core.start()\n"
            ),
        },
    )
    got = _resolved(_index(tmp_path, tmp_path / "x.db"))
    want = {
        ("m.py", 9, "start", "pkg.core"): ("resolved", "pkg/core.py:start"),
        ("m.py", 10, "helper", "u"): ("resolved", "pkg/sub/util.py:helper"),
        ("m.py", 11, "helper", "ps.util"): ("resolved", "pkg/sub/util.py:helper"),
        ("m.py", 12, "helper", "pkg.sub.util"): ("resolved", "pkg/sub/util.py:helper"),
        ("m.py", 13, "start", "core"): ("resolved", "pkg/core.py:start"),
    }
    assert {k: got[k] for k in want} == want
    store = load_index(tmp_path / "x.db")
    names = [
        r[0]
        for r in store._conn.execute(
            "SELECT b.name FROM bindings b JOIN scopes s ON s.id = b.scope_id "
            "WHERE s.file = 'm.py' AND s.kind = 'module' ORDER BY b.line"
        )
    ]
    assert names == ["pkg", "core", "u", "ps", "pkg", "go"]


def test_walker_binding_sites(tmp_path):
    """리뷰 4·5: 묶이는 자리는 참조가 아니고, 어노테이션 달린 *args·**kw 와 match 의
    키워드·splat 캡처도 묶인다."""
    _tree(
        tmp_path,
        {
            "lib.py": "def helper():\n    return 1\n\n\nclass Cls:\n    pass\n",
            "m.py": (
                "from lib import helper, Cls\n\n\n"
                "def f(*helper: Cls, **kw: Cls):\n"
                "    return helper()\n\n\n"
                "def g(obj, v):\n"
                "    for obj.x in v:\n"
                "        pass\n"
                "    for helper in v:\n"
                "        pass\n"
                "    match v:\n"
                "        case Cls(y=helper):\n"
                "            return helper()\n"
                "        case [first, *helper]:\n"
                "            return helper()\n"
            ),
        },
    )
    store = _index(tmp_path, tmp_path / "x.db")
    rows = [
        tuple(r)
        for r in store._conn.execute(
            "SELECT line, col, name, kind, resolution FROM refs WHERE file = 'm.py' "
            "ORDER BY line, col"
        )
    ]
    assert rows.count((4, 15, "Cls", "name", "resolved")) == 1  # 어노테이션 한 번
    assert (5, 11, "helper", "call", "local") in rows  # *helper 가 묶였다
    assert not any(
        r[0] == 11 and r[2] == "helper" for r in rows
    )  # for 타깃은 참조 아님
    # `for obj.x in v` 의 obj 는 참조 하나(두 번 넣지 않는다) — obj 는 정의된 이름이
    # 아니라 아예 참조가 아니다
    assert not any(r[0] == 9 and r[2] == "obj" for r in rows)
    assert (15, 19, "helper", "call", "local") in rows  # case Cls(y=helper)
    assert (17, 19, "helper", "call", "local") in rows  # case [first, *helper]


def test_the_oracle_compares_enough(tmp_path):
    """리뷰 6: 오라클이 조용히 아무것도 안 비교하지 않는지 — fixture 에서 판정 수."""
    total = 0
    for lang in ("python", "scoping"):
        store = _index(FIXTURES / lang, tmp_path / f"{lang}.db")
        for (path,) in store._conn.execute(
            "SELECT path FROM files WHERE language = 'python'"
        ):
            rows = list(
                store._conn.execute(
                    "SELECT line, name FROM refs WHERE file = ? AND object IS NULL "
                    "AND kind IN ('call', 'name')",
                    (path,),
                )
            )
            src = (FIXTURES / lang / path).read_text()
            total += len(py_oracle.classify(src, path, rows))
    assert total >= 40, total
