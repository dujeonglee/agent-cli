"""C name resolution (v10.98.0, docs/code-index/DESIGN.md §22).

Evidence:

- hand-written answers for `fixtures/c_scoping` — one ref per rule (block and
  parameter shadowing, `static` per file, header definitions visible to every
  file, prototypes pairing with the definition, a static prototype without a
  definition, `#undef`/redefinition order, tag vs typedef vs ordinary names,
  member calls, brace-initializer registration through `&`/casts);
- the independent `c_oracle` (clang's AST) on the fixture and on generated
  multi-file programs — skipped when clang is not installed;
- incremental builds compared with a full rebuild, the answers re-resolved in
  different batches, and the tool's output for the fixture.
"""

from __future__ import annotations

import os
import random
import shutil
from pathlib import Path

import pytest

from agent_cli.code_index import build, load_index
from tests.code_index import c_oracle
from tests.code_index.helpers import FIXTURES
from tests.code_index.test_resolution import _refs_after, _snapshot, _tree

FIXTURE = FIXTURES / "c_scoping"


def _answers(db: Path) -> dict:
    """``{(file, line, name): (kind, resolution, "file:line:name" | None, object)}``."""
    store = load_index(db)
    out = {}
    for f, line, name, kind, res, tf, tl, tn, obj in store._conn.execute(
        "SELECT r.file, r.line, r.name, r.kind, r.resolution, s.file, s.line, s.name, "
        "r.object FROM refs r LEFT JOIN symbols s ON s.id = r.target_id"
    ):
        out[(f, line, name)] = (kind, res, f"{tf}:{tl}:{tn}" if tf else None, obj)
    return out


@pytest.fixture(scope="module")
def fixture_db(tmp_path_factory):
    db = tmp_path_factory.mktemp("c") / "i.db"
    build(FIXTURE, db, defs_path=None, verbose=False, force_full=True)
    return db


#: (파일, 줄, 이름) → (종류, 해석, 대상 "파일:줄:이름", object) — 손으로 쓴 정답
HAND = {
    # static 은 파일마다 — a.c 의 helper 는 a.c 의 것, api.c 의 것은 api.c 의 것
    ("a.c", 6, "helper"): ("call", "resolved", "a.c:3:helper", None),
    ("api.c", 5, "helper"): ("call", "resolved", "api.c:4:helper", None),
    # 헤더 프로토타입 ↔ 다른 .c 의 정의
    ("a.c", 7, "api_open"): ("call", "resolved", "api.c:3:api_open", None),
    # 헤더의 static inline·매크로는 포함하는 누구에게나
    ("a.c", 8, "clamp"): ("call", "resolved", "inc/shared.h:8:clamp", None),
    ("a.c", 9, "SQUARE"): ("call", "resolved", "inc/shared.h:4:SQUARE", None),
    ("a.c", 9, "MAX_ITEMS"): ("name", "resolved", "inc/shared.h:3:MAX_ITEMS", None),
    # .c 의 매크로는 그 파일의 것
    ("a.c", 9, "LOCAL_MAX"): ("name", "resolved", "a.c:2:LOCAL_MAX", None),
    # 선언만 있고 정의가 색인에 없다
    ("a.c", 10, "nowhere"): ("call", "external", None, None),
    # 파일 안의 정의가 다른 파일의 같은 이름보다 먼저
    ("a.c", 11, "api_twice"): ("call", "resolved", "a.c:4:api_twice", None),
    # extern 선언 → 다른 파일의 변수 정의
    ("a.c", 12, "global_count"): ("name", "resolved", "api.c:2:global_count", None),
    # 인자·지역·블록 지역이 바깥 함수 이름을 가린다
    ("a.c", 18, "api_open"): ("name", "local", None, None),
    ("a.c", 18, "helper"): ("name", "local", None, None),
    ("a.c", 20, "nowhere"): ("name", "local", None, None),
    # 블록을 나오면 블록 지역은 안 보인다
    ("a.c", 21, "helper"): ("call", "resolved", "a.c:3:helper", None),
    ("a.c", 21, "clamp"): ("name", "local", None, None),
    # 이니셜라이저에 넣은 함수는 registration — `&` 를 거쳐도
    ("b.c", 6, "api_open"): ("registration", "resolved", "api.c:3:api_open", ".open"),
    ("b.c", 6, "api_twice"): ("registration", "resolved", "b.c:3:api_twice", ".twice"),
    # 함수가 아닌 값은 name 으로 남는다
    ("b.c", 6, "MAX_ITEMS"): ("name", "resolved", "inc/shared.h:3:MAX_ITEMS", ".flags"),
    # 비-static 프로토타입 → 다른 파일의 정의 (위치 초기화)
    ("b.c", 7, "api_close"): ("registration", "resolved", "api.c:5:api_close", "[]"),
    # 정의 없는 static 프로토타입은 다른 파일의 static helper 를 가리킬 수 없다
    ("b.c", 7, "helper"): ("name", "external", None, "[]"),
    # 캐스트를 거친 등록
    ("b.c", 12, "api_close"): (
        "registration",
        "resolved",
        "api.c:5:api_close",
        ".open",
    ),
    # 태그와 typedef 는 각자의 이름공간
    ("b.c", 9, "item"): ("type", "resolved", "inc/shared.h:5:item", "struct"),
    ("b.c", 10, "item_t"): ("type", "resolved", "inc/shared.h:6:item_t", None),
    ("b.c", 11, "color"): ("type", "resolved", "inc/shared.h:7:color", "enum"),
    ("c.c", 3, "item"): ("type", "resolved", "inc/shared.h:5:item", "struct"),
    # 같은 줄의 인자 `item` 은 태그와 다른 이름공간
    # (c.c:3 의 name 참조 — 아래 test_tag_and_parameter_share_a_line)
    # 멤버 호출 — 그 이름의 C 함수가 없으면 external
    ("b.c", 13, "open"): ("call", "external", None, "o"),
    ("b.c", 13, "twice"): ("call", "external", None, "local"),
    ("b.c", 13, "api_twice"): ("call", "resolved", "b.c:3:api_twice", None),
    # 두 .c 가 비-static 으로 정의한 이름 — include 를 안 따르니 고를 수 없다
    ("c.c", 2, "api_twice"): ("call", "candidates", None, None),
    # 같은 파일에서 다시 정의한 매크로는 그 줄 이전의 마지막
    ("c.c", 6, "X"): ("name", "resolved", "c.c:5:X", None),
    ("c.c", 9, "X"): ("name", "resolved", "c.c:8:X", None),
    # 다른 .c 의 static 은 밖에서 안 보인다 — d.c 의 static tool 이 있어도 api.c 의 것
    ("c.c", 11, "tool"): ("call", "resolved", "api.c:6:tool", None),
    # 고를 수 없는 이름의 등록도 registration (모두 함수라서)
    ("c.c", 13, "api_twice"): ("registration", "candidates", None, ".cb"),
    # 멤버 호출 — 그 이름은 프로토타입뿐이라 함수 정의가 없다
    ("c.c", 15, "open_dev"): ("call", "external", None, "p"),
    # 다른 파일의 함수를 등록
    ("d.c", 3, "registered_only"): (
        "registration",
        "resolved",
        "c.c:4:registered_only",
        ".cb",
    ),
}


@pytest.mark.parametrize("site", list(HAND), ids=[f"{f}:{ln}:{n}" for f, ln, n in HAND])
def test_hand_written_answers(fixture_db, site):
    assert _answers(fixture_db)[site] == HAND[site]


def test_tag_and_parameter_share_a_line(fixture_db):
    store = load_index(fixture_db)
    rows = sorted(
        tuple(r)
        for r in store._conn.execute(
            "SELECT kind, resolution, object FROM refs WHERE file = 'c.c' AND line = 3 "
            "AND name = 'item'"
        )
    )
    assert rows == [
        ("name", "local", None),
        ("type", "resolved", "struct"),
    ]


def test_no_duplicate_or_declarator_refs(fixture_db):
    """같은 자리의 call+name 짝도, 정의·프로토타입·인자·지역 선언자의 이름도 참조가
    아니다 (종전 워커는 `is not` 비교로 호출 식별자를 name 으로 한 번 더 냈다)."""
    store = load_index(fixture_db)
    dup = store._conn.execute(
        "SELECT COUNT(*) FROM refs a JOIN refs b ON a.file = b.file AND a.line = b.line "
        "AND a.col = b.col AND a.id < b.id"
    ).fetchone()[0]
    assert dup == 0
    decl_names = store._conn.execute(
        "SELECT r.file, r.line, r.name FROM refs r JOIN symbols s ON s.file = r.file "
        "AND s.line = r.line AND s.name = r.name WHERE r.kind != 'type' "
        "AND s.kind_raw IN ('function', 'prototype', 'var', 'preproc_def', "
        "'preproc_function_def')"
    ).fetchall()
    # 헤더 가드 `#ifndef SHARED_H` 의 자기 줄 정의는 1 줄 위 — 겹치지 않는다
    assert [tuple(r) for r in decl_names] == []


def test_parenthesized_definition_name(tmp_path):
    _tree(
        tmp_path / "t",
        {"p.c": "int (foo)(int x) { return x; }\nint g(void) { return foo(1); }\n"},
    )
    db = tmp_path / "i.db"
    build(tmp_path / "t", db, defs_path=None, verbose=False, force_full=True)
    assert _answers(db)[("p.c", 2, "foo")] == ("call", "resolved", "p.c:1:foo", None)


def test_member_call_with_a_function_of_that_name_is_candidates(tmp_path):
    _tree(
        tmp_path / "t",
        {
            "a.c": "static int open(int x) { return x; }\n"
            "struct o { int (*open)(int); };\n"
            "int f(struct o *p) { return p->open(1); }\n",
        },
    )
    db = tmp_path / "i.db"
    build(tmp_path / "t", db, defs_path=None, verbose=False, force_full=True)
    assert _answers(db)[("a.c", 3, "open")] == ("call", "candidates", None, "p")


def test_enum_tag_and_function_with_one_name(tmp_path):
    """`enum pins x = pins(1);` — 호출은 함수, 태그는 enum (리뷰 H2: i40e_ptp.c)."""
    _tree(
        tmp_path / "t",
        {
            "a.c": "enum pins { A, B };\nstatic enum pins pins(int x) { return A; }\n"
            "int f(void) { enum pins v = pins(1); return v; }\n",
        },
    )
    db = tmp_path / "i.db"
    build(tmp_path / "t", db, defs_path=None, verbose=False, force_full=True)
    got = {
        (k, o): f"{tf}:{tl}"
        for k, o, tf, tl in load_index(db)._conn.execute(
            "SELECT r.kind, r.object, s.file, s.line FROM refs r JOIN symbols s "
            "ON s.id = r.target_id WHERE r.line = 3 AND r.name = 'pins'"
        )
    }
    assert got == {("type", "enum"): "a.c:1", ("call", None): "a.c:2"}


def test_a_header_constant_keeps_a_function_macro_elsewhere_from_resolving(tmp_path):
    """헤더 상수와 다른 헤더의 함수형 매크로가 같은 이름이면 `candidates` — 함수만 보고
    고르면 거짓 간선(리뷰 H1: iavf `MIN_MSIX_COUNT` → fm10k 의 매크로)."""
    _tree(
        tmp_path / "t",
        {
            "x/x.h": "#define LIMIT 4\n",
            "y/y.h": "#define LIMIT(hw) ((hw) + 1)\n",
            "x/x.c": '#include "x.h"\nint f(void) { int n = LIMIT; return n; }\n',
        },
    )
    db = tmp_path / "i.db"
    build(tmp_path / "t", db, defs_path=None, verbose=False, force_full=True)
    assert _answers(db)[("x/x.c", 2, "LIMIT")] == ("name", "candidates", None, None)


def test_a_header_enumerator_counts_as_a_visible_definition(tmp_path):
    """헤더 enum 의 열거자도 그 이름의 정의 — 다른 .c 의 같은 이름 함수와 겹치면 고를 수
    없다 (열거자는 기호가 아니라 enum 기호의 `enum_values` 에 있다)."""
    _tree(
        tmp_path / "t",
        {
            "e.h": "enum mode { FAST, SLOW };\n",
            "f.c": "int FAST(void) { return 1; }\n",
            "g.c": '#include "e.h"\nint FAST(void);\nint g(void) { return FAST(); }\n',
        },
    )
    db = tmp_path / "i.db"
    build(tmp_path / "t", db, defs_path=None, verbose=False, force_full=True)
    assert _answers(db)[("g.c", 3, "FAST")] == ("call", "candidates", None, None)


def _build(tmp_path, files: dict) -> Path:
    _tree(tmp_path / "t", files)
    db = tmp_path / "i.db"
    build(tmp_path / "t", db, defs_path=None, verbose=False, force_full=True)
    return db


def test_a_typedef_names_only_its_declarator(tmp_path):
    """`typedef u64 phys_t;` 의 `u64` 는 typedef 가 아니라 타입 참조 (구현 리뷰 H1 — 가짜
    typedef `u64` 가 헤더마다 생겨 drivers/net 의 `u64` 3.4만 자리가 거기로 풀렸다)."""
    db = _build(
        tmp_path,
        {
            "t.h": "typedef unsigned long long u64;\n",
            "x.h": "typedef u64 phys_t;\ntypedef int (*cb_t)(int);\n",
            "a.c": "u64 f(phys_t p) { u64 v = p; return v; }\n",
        },
    )
    store = load_index(db)
    typedefs = sorted(
        tuple(r)
        for r in store._conn.execute(
            "SELECT file, name FROM symbols WHERE kind_raw = 'typedef'"
        )
    )
    assert typedefs == [("t.h", "u64"), ("x.h", "cb_t"), ("x.h", "phys_t")]
    got = _answers(db)
    assert got[("x.h", 1, "u64")] == ("type", "resolved", "t.h:1:u64", None)
    assert got[("a.c", 1, "u64")][:3] == ("type", "resolved", "t.h:1:u64")


def test_an_object_macro_used_as_a_type(tmp_path):
    """매크로는 이름공간보다 먼저 전개된다 — 같은 파일의 `#define NUM double` 은 타입 자리에서도
    그 매크로. 다른 파일의 것은 보지 않는다: 진짜 typedef 가 색인 밖이면 드라이버 헤더 하나의
    `#define u32 unsigned int` 로 색인 전체의 `u32` 가 풀린다 (구현 리뷰 2회차 — drivers/net
    19만 자리)."""
    db = _build(
        tmp_path,
        {
            "a.c": "#define NUM double\nNUM half(NUM x) { return x; }\n",
            "skfp/targetos.h": "#define u32 unsigned int\n",
            "b.c": "u32 width(u32 x) { return x + sizeof(u32); }\n",
        },
    )
    got = (
        load_index(db)
        ._conn.execute(
            "SELECT file, kind, resolution FROM refs WHERE name IN ('NUM', 'u32') "
            "ORDER BY file, col"
        )
        .fetchall()
    )
    assert [tuple(r) for r in got] == [
        ("a.c", "type", "resolved"),
        ("a.c", "type", "resolved"),
        ("b.c", "type", "external"),
        ("b.c", "type", "external"),
        (
            "b.c",
            "name",
            "external",
        ),  # `sizeof(u32)` — 이름 자리에서도 다른 파일의 타입 별칭은 안 본다
    ]


def test_a_macro_turning_into_a_type_alias_incremental_equals_full(tmp_path):
    """같은 기호 id 의 역할만 바뀌는 편집(`#define W 4` → `#define W unsigned`) — `g:` 는 (id, 역할)
    을 비교한다."""
    root = tmp_path / "t"
    _tree(
        root, {"w.h": "#define W 4\n", "a.c": "int f(void) { return W + sizeof(W); }\n"}
    )
    inc = tmp_path / "inc.db"
    build(root, inc, defs_path=None, verbose=False)
    (root / "w.h").write_text("#define W unsigned\n")
    build(root, inc, defs_path=None, verbose=False)
    full = tmp_path / "full.db"
    build(root, full, defs_path=None, verbose=False, force_full=True)
    assert _snapshot(inc) == _snapshot(full)
    assert {v[0] for v in _snapshot(full).values()} == {"external"}


def test_a_prototype_returning_a_function_pointer_is_a_prototype(tmp_path):
    """`int (*getcb(int x))(int y);` 는 함수 선언 — 변수 정의가 아니다 (리뷰 2회차 L)."""
    db = _build(
        tmp_path,
        {
            "h.h": "int (*getcb(int x))(int y);\n",
            "g.c": "int (*getcb(int x))(int y) { return 0; }\n",
            "u.c": '#include "h.h"\nint u(void) { return getcb(1) != 0; }\n',
        },
    )
    store = load_index(db)
    kinds = sorted(
        tuple(r)
        for r in store._conn.execute(
            "SELECT file, kind_raw, is_definition FROM symbols WHERE name = 'getcb'"
        )
    )
    assert kinds == [("g.c", "function", 1), ("h.h", "prototype", 0)]
    assert _answers(db)[("u.c", 2, "getcb")] == (
        "call",
        "resolved",
        "g.c:1:getcb",
        None,
    )


def test_parameters_of_a_function_returning_a_function_pointer(tmp_path):
    """`int (*getcb(int x))(int y)` 의 인자는 `x` (리뷰 L1)."""
    db = _build(
        tmp_path,
        {
            "g.h": "extern int x;\n",
            "a.c": "int (*getcb(int x))(int y) { return x ? 0 : 0; }\n",
        },
    )
    assert _answers(db)[("a.c", 1, "x")] == ("name", "local", None, None)


def test_a_file_scope_function_pointer_is_a_variable(tmp_path):
    """`static int (*fp)(int);` 는 변수 기호 — 다른 파일의 같은 이름으로 새지 않는다 (리뷰 L2)."""
    db = _build(
        tmp_path,
        {
            "h.h": "int fp;\n",
            "a.c": "static int (*fp)(int);\nint g(void) { return fp(1); }\n",
        },
    )
    assert _answers(db)[("a.c", 2, "fp")] == ("call", "resolved", "a.c:1:fp", None)


def test_gnu_old_style_designator(tmp_path):
    db = _build(
        tmp_path,
        {
            "a.c": "static int f(int x) { return x; }\n"
            "struct o { int (*cb)(int); };\nstatic struct o t = { cb: f };\n"
        },
    )
    assert _answers(db)[("a.c", 3, "f")] == (
        "registration",
        "resolved",
        "a.c:1:f",
        ".cb",
    )


# ---------- clang oracle ----------

needs_clang = pytest.mark.skipif(c_oracle.CLANG is None, reason="clang not installed")


def _oracle_mismatches(db: Path, root: Path) -> tuple[list, int]:
    ours = {}
    store = load_index(db)
    for f, line, col, name, res, tf in store._conn.execute(
        "SELECT r.file, r.line, r.col, r.name, r.resolution, s.file FROM refs r "
        "LEFT JOIN symbols s ON s.id = r.target_id WHERE r.kind != 'type'"
    ):
        ours[(f, line, col, name)] = (res, tf)
    bad, compared = [], 0
    for key, (what, file) in c_oracle.answers(root).items():
        got = ours.get(key)
        if got is None:
            continue  # 색인에 정의가 없는 이름의 식별자는 참조로 안 나온다
        compared += 1
        if what == "local":
            ok = got[0] == "local"
        elif what == "several":
            ok = got[0] == "candidates"
        elif what == "none":
            ok = got[0] == "external"
        else:
            ok = got == ("resolved", file)
        if not ok:
            bad.append((key, what, file, got))
    return bad, compared


@needs_clang
def test_fixture_agrees_with_clang(fixture_db):
    bad, compared = _oracle_mismatches(fixture_db, FIXTURE)
    assert bad == []
    assert compared >= 25


def _random_program(rng: random.Random) -> dict[str, str]:
    """링크되는 C 프로그램 하나 — 헤더 하나, .c 2~4 개. 함수마다 정의 꼴(헤더 프로토타입 +
    한 .c 의 정의 / 한 .c 의 static / 두 .c 가 각자 static / 헤더 static inline)을 고르고,
    본문에서 보이는 함수를 부르고, 함수 이름으로 인자·블록 지역을 만들어 가리고,
    이니셜라이저에 함수를 넣는다(`.cb = f`, `&f`, 캐스트)."""
    nfiles = rng.randint(2, 4)
    names = [f"f{i}" for i in range(rng.randint(4, 8))]
    form = {n: rng.choice(["ext", "ext", "static", "static2", "inline"]) for n in names}
    owner = {n: rng.randrange(nfiles) for n in names}
    other = {n: (owner[n] + 1) % nfiles for n in names}
    header = ["struct ops { int (*cb)(int); };"]
    for n in names:
        if form[n] == "ext":
            header.append(f"int {n}(int a);")
        elif form[n] == "inline":
            header.append(f"static inline int {n}(int a) {{ return a + 1; }}")
    files = {"inc/h.h": "\n".join(header) + "\n"}

    def defined_in(i):
        return [
            n
            for n in names
            if (form[n] in ("ext", "static", "static2") and owner[n] == i)
            or (form[n] == "static2" and other[n] == i)
        ]

    for i in range(nfiles):
        mine = defined_in(i)
        visible = [n for n in names if form[n] in ("ext", "inline")] + [
            n for n in mine if form[n] != "ext"
        ]
        lines = ['#include "inc/h.h"']
        lines += [f"static int {n}(int a);" for n in mine if form[n] != "ext"]
        for n in mine:
            st_ = "static " if form[n] != "ext" else ""
            shadow = rng.choice(names)
            param = shadow if rng.random() < 0.3 else "a"
            body = [f"{st_}int {n}(int {param}) {{", f"    int r = {param};"]
            for _ in range(rng.randint(1, 4)):
                g = rng.choice(visible)
                pick = rng.random()
                if g == param:
                    body.append(f"    r += {g};")
                elif pick < 0.45:
                    body.append(f"    r += {g}(r);")
                elif pick < 0.6:
                    body.append(f"    {{ int {g} = r; r += {g}; }}")
                elif pick < 0.75:
                    body.append(
                        f"    {{ struct ops o = {{ .cb = &{g} }}; r += o.cb(r); }}"
                    )
                elif pick < 0.9:
                    body.append(
                        f"    {{ int (*p)(int) = (int (*)(int)){g}; r += p(r); }}"
                    )
                else:
                    body.append(f"    for (int {g} = 0; {g} < 1; {g}++) r += {g};")
            body += ["    return r;", "}"]
            lines += body
        if visible:
            g = rng.choice(visible)
            lines.append(f"static const struct ops tbl{i} = {{ .cb = {g} }};")
            lines.append(f"int use_tbl{i}(void) {{ return tbl{i}.cb(0); }}")
        files[f"m{i}.c"] = "\n".join(lines) + "\n"
    return files


@needs_clang
@pytest.mark.parametrize("seed", range(12))
def test_generated_programs_agree_with_clang(tmp_path, seed):
    root = tmp_path / "prog"
    _tree(root, _random_program(random.Random(seed)))
    db = tmp_path / "i.db"
    build(root, db, defs_path=None, verbose=False, force_full=True)
    bad, compared = _oracle_mismatches(db, root)
    assert bad == []
    assert compared >= 5


def test_generated_registrations_are_marked(tmp_path):
    """생성 프로그램의 `.cb = f`·`&f`·캐스트는 모두 registration 이고 대상이 함수다."""
    seen = 0
    for seed in range(12):
        root = tmp_path / f"p{seed}"
        _tree(root, _random_program(random.Random(seed)))
        db = tmp_path / f"{seed}.db"
        build(root, db, defs_path=None, verbose=False, force_full=True)
        store = load_index(db)
        for kind, res, tk in store._conn.execute(
            "SELECT r.kind, r.resolution, s.kind FROM refs r LEFT JOIN symbols s "
            "ON s.id = r.target_id WHERE r.object LIKE '.%' OR r.object = '[]'"
        ):
            seen += 1
            assert kind == "registration" and res == "resolved" and tk == "function"
    assert seen >= 10


# ---------- incremental = full, batch independence ----------

C_EDITS = {
    "body only": ("a.c", lambda s: s.replace("x * 2; }", "x * 3; }", 1)),
    "static removed from a helper": (
        "api.c",
        lambda s: s.replace("static int helper", "int helper"),
    ),
    "second definition of a shared name gone": (
        "b.c",
        lambda s: s.replace("int api_twice(int x) { return x + x; }\n", ""),
    ),
    "third definition of a shared name": (
        "c.c",
        lambda s: s + "int api_twice(int x) { return x; }\n",
    ),
    "header gains a definition": (
        "inc/shared.h",
        lambda s: s.replace(
            "int nowhere(void);", "static inline int nowhere(void) { return 0; }"
        ),
    ),
    "definition made static": (
        "api.c",
        lambda s: s.replace("int api_open(int fd)", "static int api_open(int fd)"),
    ),
    "object macro becomes a function macro": (
        "inc/shared.h",
        lambda s: s.replace("#define MAX_ITEMS 8", "#define MAX_ITEMS(x) 8"),
    ),
    "registered function becomes a variable": (
        "c.c",
        lambda s: s.replace(
            "int registered_only(int x) { return x; }", "int registered_only = 0;"
        ),
    ),
    "enumerator collides with a function": (
        "inc/shared.h",
        lambda s: s.replace(
            "enum color { RED, GREEN };", "enum color { RED, GREEN, api_close };"
        ),
    ),
    "type moved to a .c": (
        "inc/shared.h",
        lambda s: s.replace("struct item { int v; };", "struct item;"),
    ),
    "prototype becomes a definition": (
        "d.c",
        lambda s: s.replace("int open_dev(int);", "int open_dev(int x) { return x; }"),
    ),
    "file deleted": ("api.c", None),
}


@pytest.mark.parametrize("edit", list(C_EDITS), ids=list(C_EDITS))
def test_incremental_equals_full_rebuild(tmp_path, edit):
    root = tmp_path / "tree"
    shutil.copytree(FIXTURE, root)
    inc = tmp_path / "inc.db"
    build(root, inc, defs_path=None, verbose=False)
    rel, change = C_EDITS[edit]
    p = root / rel
    if change is None:
        p.unlink()
    else:
        new = change(p.read_text())
        assert new != p.read_text(), edit
        p.write_text(new)
    stats = build(root, inc, defs_path=None, verbose=False)
    full = tmp_path / "full.db"
    build(root, full, defs_path=None, verbose=False, force_full=True)
    assert _snapshot(inc) == _snapshot(full)
    if edit == "body only":
        assert stats["resolved"] == 1


def test_registration_flips_back_and_forth(tmp_path):
    """복사된 파일의 kind 가 이미 `registration` 이어도 다시 해석하면 전체와 같다 (리뷰 I2)."""
    root = tmp_path / "tree"
    shutil.copytree(FIXTURE, root)
    inc = tmp_path / "inc.db"
    build(root, inc, defs_path=None, verbose=False)
    p = root / "c.c"
    fn = "int registered_only(int x) { return x; }"
    for text in (fn, "int registered_only = 0;", fn):
        p.write_text(
            p.read_text().replace("int registered_only = 0;", text).replace(fn, text)
        )
        build(root, inc, defs_path=None, verbose=False)
        full = tmp_path / "full.db"
        build(root, full, defs_path=None, verbose=False, force_full=True)
        assert _snapshot(inc) == _snapshot(full)


def test_fixture_answers_do_not_depend_on_the_batch(tmp_path):
    db = tmp_path / "x.db"
    build(FIXTURE, db, defs_path=None, verbose=False, force_full=True)
    files = sorted(
        str(p.relative_to(FIXTURE))
        for p in FIXTURE.rglob("*")
        if p.suffix in (".c", ".h")
    )
    together = _refs_after(db, [files])
    assert _refs_after(db, [files[::-1]]) == together
    assert _refs_after(db, [[f] for f in files]) == together


def _random_edit(rng: random.Random, files: dict) -> dict:
    """생성 프로그램 하나에 편집 하나 — static 붙이기·떼기, 정의 지우기, 헤더에 정의 더하기."""
    out = dict(files)
    rel = rng.choice(sorted(out))
    text = out[rel]
    choice = rng.randrange(4)
    if choice == 0 and "static int" in text:
        out[rel] = text.replace("static int", "int", 1)
    elif choice == 1 and "\nint f" in text:
        out[rel] = text.replace("\nint f", "\nstatic int f", 1)
    elif choice == 2:
        out["inc/h.h"] = out["inc/h.h"] + f"#define f{rng.randrange(8)}(x) (x)\n"
    else:
        out[rel] = text + "int f0(int a) { return a; }\n"
    return out


@pytest.mark.parametrize("seed", range(30))
def test_generated_edits_incremental_equals_full(tmp_path, seed):
    rng = random.Random(1000 + seed)
    files = _random_program(rng)
    root = tmp_path / "tree"
    _tree(root, files)
    inc = tmp_path / "inc.db"
    build(root, inc, defs_path=None, verbose=False)
    edited = _random_edit(rng, files)
    for rel, text in edited.items():
        if files.get(rel) != text:
            (root / rel).write_text(text)
    build(root, inc, defs_path=None, verbose=False)
    full = tmp_path / "full.db"
    build(root, full, defs_path=None, verbose=False, force_full=True)
    assert _snapshot(inc) == _snapshot(full)
    together = _refs_after(full, [sorted(edited)])
    assert _refs_after(full, [[f] for f in sorted(edited)[::-1]]) == together


# ---------- tool output ----------


@pytest.fixture
def in_fixture(tmp_path, monkeypatch):
    root = tmp_path / "tree"
    shutil.copytree(FIXTURE, root)
    monkeypatch.chdir(root)
    return root


def _out(q: dict) -> str:
    from agent_cli.tools.code_index import _dispatch_one

    r = _dispatch_one(q)
    assert r.success, r.error
    return r.output


def test_callers_keep_static_functions_apart(in_fixture):
    assert _out({"mode": "callers", "name": "helper"}) == (
        "helper — a.c:3-3\nshadow  (1×)  a.c:21\nuses  (1×)  a.c:6\n\n"
        "helper — api.c:4-4\napi_close  (1×)  api.c:5"
    )


def test_callers_show_registrations_and_shared_names(in_fixture):
    assert _out({"mode": "callers", "name": "api_twice"}) == (
        "api_twice — a.c:4-4\nuses  (1×)  a.c:11\n\n"
        "api_twice — b.c:3-3\nrun  (1×)  b.c:13\nregistered in a table: b.c:6 (.twice)"
        '\n\n? "api_twice" is defined in several places (includes are not followed) — '
        "any of 2 definitions:  c.c:2, c.c:13"
    )
    assert _out({"mode": "callers", "name": "registered_only"}) == (
        "registered_only — c.c:4-4\nregistered in a table: d.c:3 (.cb)"
    )


def test_callees_say_why_a_neighbour_is_a_guess(in_fixture):
    assert _out({"mode": "callees", "name": "other"}) == (
        "other — c.c:2-2\n"
        "? api_twice  (1×, any of 2 definitions, defined in several places)  c.c:2"
    )


def test_refs_tags_and_note(in_fixture):
    assert _out({"mode": "refs", "name": "api_twice"}).splitlines() == [
        "a.c:11:9 call api_twice → api_twice  @a.c",
        "b.c:6:62 registration api_twice → api_twice  @b.c",
        "b.c:13:48 call api_twice → api_twice  @b.c",
        "c.c:2:25 call api_twice → ?",
        "c.c:13:46 registration api_twice → ?",
        (
            '? = "api_twice" is defined in several places (includes are not followed) '
            "— any of 2 definitions"
        ),
    ]
    assert _out({"mode": "refs", "name": "api_twice", "path": "a.c"}).splitlines() == [
        "a.c:11:9 call api_twice → api_twice  @a.c",
        (
            '? 2 more uses of "api_twice" that could not be pinned to one definition '
            'may reach it — "refs" with the bare name lists them.'
        ),
    ]


def test_a_static_definition_counts_no_shared_uses(in_fixture):
    """`.c` 의 static 정의에는 다른 파일의 맨 이름 `?` 가 닿지 않는다 — note 도 callers 도."""
    _tree(in_fixture, {"e.c": "static int api_twice(int x) { return x; }\n"})
    out = _out({"mode": "refs", "name": "api_twice", "path": "e.c"})
    assert "could not be pinned" not in out
    assert "several places" not in _out(
        {"mode": "callers", "name": "api_twice", "path": "e.c"}
    )


def test_callees_skip_shared_names_that_are_not_functions(in_fixture):
    """여러 헤더가 정의한 상수는 callees 의 `?` 이웃이 아니다 (리뷰 M1 — `SPEED_10`)."""
    _tree(
        in_fixture,
        {
            "s1.h": "#define SPEED 10\n",
            "s2.h": "#define SPEED 100\n",
            "s.c": "int speed(void) { return SPEED + api_twice(1); }\n",
        },
    )
    assert _out({"mode": "callees", "name": "speed"}) == (
        "speed — s.c:1-1\n"
        "? api_twice  (1×, any of 2 definitions, defined in several places)  s.c:1"
    )


def test_slice_counts_shared_caller_sites(in_fixture):
    """slice 의 따라가지 않은 수에 여러 파일 이름의 자리도 (리뷰 M2)."""
    out = _out(
        {"mode": "slice", "name": "api_twice", "path": "a.c", "with_callers": True}
    )
    assert (
        "_? 2 caller sites that could not be pinned to one definition not followed._"
        in out
    )


def test_a_table_registration_of_a_shared_name_reaches_callers(in_fixture):
    """함수 밖 표에 고를 수 없는 이름으로 등록 — 그 함수가 죽은 코드처럼 보이지 않게 (리뷰 L5)."""
    _tree(
        in_fixture,
        {
            "t.c": "struct o4 { int (*cb)(int); };\nstatic struct o4 t4 = { .cb = api_twice };\n"
        },
    )
    out = _out({"mode": "callers", "name": "api_twice", "path": "a.c"})
    assert out.endswith("any of 2 definitions:  c.c:2, c.c:13, t.c:2")


def test_registration_is_a_ref_kind(in_fixture):
    assert _out(
        {"mode": "refs", "name": "api_close", "ref_kind": "registration"}
    ).splitlines() == [
        "b.c:7:31 registration api_close → api_close",
        "b.c:12:47 registration api_close → api_close",
    ]


# ---------- 무작위 C 트리 퍼즈 (구현 리뷰의 퍼저: 이름 7 개로 충돌을 일부러 많이) ----------

_FUZZ_NAMES = ["foo", "bar", "baz", "qux", "RED", "ops_t", "node"]


def _snippet(r: random.Random) -> str:
    n, m = r.choice(_FUZZ_NAMES), r.choice(_FUZZ_NAMES)
    return r.choice(
        [
            f"int {n}(int a) {{ return {m}(a); }}",
            f"static int {n}(int a) {{ return a + {m}; }}",
            f"int {n}(int);",
            f"static int {n}(int);",
            f"extern int {n};",
            f"int {n} = 1;",
            f"static int {n} = 2;",
            f"#define {n} 3",
            f"#define {n}(x) ((x) + 1)",
            f"#undef {n}",
            f"enum e_{n} {{ {n}, {m}_V }};",
            f"enum {{ {m} }};",
            f"struct {n} {{ int v; }};",
            f"typedef struct {n} {{ int v; }} {n};",
            f"typedef int {n};",
            f"typedef {m} t_{n};",
            f"static inline int {n}(int a) {{ return a; }}",
            f"struct ops {{ int (*cb)(int); }}; static struct ops o_{n} = {{ .cb = {m} }};",
            f"int use_{n}(struct {m} *p) {{ {m} x; return p->{n}(1) + {n}; }}",
            f"int blk_{n}(int a) {{ {{ int {m} = a; a += {m}; }} return {m}(a) + {n}; }}",
            f"static int (*tab_{n}[])(int) = {{ {n}, &{m} }};",
            (
                f"#ifdef X\nint {n}(int a) {{ return 1; }}\n#else\n"
                f"static int {n}(int a) {{ return 2; }}\n#endif"
            ),
            f"void reg_{n}(void) {{ struct ops q = {{ .cb = (void *){m} }}; (void)q; }}",
        ]
    )


def _fuzz_edit(r: random.Random, files: dict) -> dict:
    out = dict(files)
    rel = r.choice(sorted(out))
    k = r.randrange(10)
    if k == 0:  # .c <-> .h
        new = rel[:-2] + (".h" if rel.endswith(".c") else ".c")
        if new not in out:
            out[new] = out.pop(rel)
    elif k == 1 and len(out) > 1:
        out.pop(rel)
    elif k == 2:
        out[rel] = out[rel].replace("static ", "", 1)
    elif k == 3:
        out[rel] = out[rel].replace("int ", "static int ", 1)
    elif k == 4:
        lines = out[rel].split("\n")
        lines.pop(r.randrange(len(lines)))
        out[rel] = "\n".join(lines)
    elif k == 5:
        out[rel] = _snippet(r) + "\n" + out[rel]
    elif k == 7:
        out[rel] = out[rel].replace("_V", "", 1).replace("{ int v; }", "", 1)
    elif k == 8:
        out[rel] = (
            out[rel].replace("#ifdef X", "#ifndef X", 1).replace("typedef ", "", 1)
        )
    else:
        out[f"n{r.randrange(9)}" + r.choice([".c", ".h"])] = _snippet(r) + "\n"
    return out


@pytest.mark.parametrize("seed", range(40))
def test_fuzzed_trees_incremental_equals_full(tmp_path, seed):
    r = random.Random(seed)
    files = {
        f"f{i}{r.choice(['.c', '.c', '.h'])}": "\n".join(
            _snippet(r) for _ in range(r.randint(2, 6))
        )
        + "\n"
        for i in range(r.randint(2, 4))
    }
    root = tmp_path / "t"
    _tree(root, files)
    inc = tmp_path / "inc.db"
    build(root, inc, defs_path=None, verbose=False)
    cur = files
    for step in range(3):
        new = _fuzz_edit(r, cur)
        for rel in set(cur) - set(new):
            (root / rel).unlink()
        for rel, text in new.items():
            if cur.get(rel) != text:
                p = root / rel
                p.write_text(text)
                st = p.stat()
                os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9 * (step + 1)))
        cur = new
        build(root, inc, defs_path=None, verbose=False)
        full = tmp_path / "full.db"
        build(root, full, defs_path=None, verbose=False, force_full=True)
        assert _snapshot(inc) == _snapshot(full), (seed, step)
