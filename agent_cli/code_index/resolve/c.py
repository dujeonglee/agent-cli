"""C name resolution from the index tables (v10.98.0, DESIGN §22).

A ref is looked up through its scope chain — block → function → file — the
way C sees names, in its namespace: a `type` ref with a tag keyword in
`object` (`struct foo`) sees struct/union/enum tags, a `type` ref without one
sees typedef names, every other ref sees the ordinary names (parameters,
locals, enumerators, functions, variables, macros). Inside a function or block a
binding is visible in the whole scope; at file scope the binding in effect is
the last one on or before the ref's line (`#ifdef`/`#else` twins, `#undef` and
redefinition), else the first.

- a parameter, local variable or block-level typedef/tag/enumerator → `local`;
- a file-scope definition (function, variable, typedef, `#define`, tag,
  enumerator → its enum) → `resolved` to that symbol, `static` or not;
- a `static` prototype without a definition in the file → `external` (internal
  linkage: no other file's function can be meant);
- a name the file does not define (a non-static prototype, an `extern`
  declaration, or nothing — `#include` is not followed) is looked up among the
  definitions other files make visible (`visible_role`): every definition in a
  header (`.h` — it is visible to whoever includes it, `static inline`
  functions and macros included) and the non-static functions and variables of
  `.c` files. One → `resolved`, several → `candidates`, none → `external`.

A member call `p->f()`/`s.f()` calls through a function pointer: `candidates`
when a C function definition named `f` is indexed (a pointer may hold any of
them, `static` ones included), else `external`.

A name stored by a brace initializer (`.probe = foo_probe`; the walker puts
the designator in `refs.object`) resolves like any name; it becomes a
`registration` ref when it names a function or function-like macro (one
`resolved` function, or `candidates` that are all functions), else a `name`
ref. The stored ref kind is read only to tell `type` refs from the others, so
re-resolving a copied file gives the same answer.

What resolution reads from other files is recorded in `resolve_deps`:
`g:c:<name>` — the visible definitions of that name (only their ids and
roles: kind, kind_raw, visibility and — for an object-like macro — whether its
value is a type pick the role; the builder compares (id, role) pairs, so a
role change under the same id is seen), and `n:c:<name>` — whether a C function
definition of that name exists (member calls). The builder compares both
between the old and the new tables for the names of the files it re-parsed or
removed (`visible_members`, `FUNCTION_DEFINITION`).
"""

from __future__ import annotations

import json
from collections import defaultdict

#: `n:c:` 의 술어 — 해석과 builder 가 같은 것을 쓴다.
FUNCTION_DEFINITION = "+kind = 'function' AND is_definition = 1"

#: 바인딩 종류 → 그것이 묶는 이름의 역할 (``def`` 는 기호의 역할)
_BINDING_ROLE = {
    "param": "value",
    "local": "value",
    "enumerator": "value",
    "decl": "fn",
    "tag": "tag",
    "typedef": "typedef",
}


#: 값이 이 낱말로 시작하는 객체형 매크로는 타입의 별칭(`#define u8 unsigned char`).
_TYPE_WORDS = frozenset(
    [
        "void",
        "char",
        "short",
        "int",
        "long",
        "float",
        "double",
        "signed",
        "unsigned",
        "_Bool",
        "bool",
        "struct",
        "union",
        "enum",
        "const",
        "volatile",
    ]
)


def role_of(kind: str, kind_raw: str | None, signature: str | None = None) -> str:
    """기호의 역할 — ``"fn"``(함수·함수형 매크로) · ``"macro"``(객체형 매크로) ·
    ``"typemacro"``(값이 타입인 객체형 매크로) · ``"value"``(변수·열거자) · ``"tag"``
    (struct/union/enum) · ``"typedef"``. 참조는 자기 이름공간의 역할만 본다."""
    if kind == "function":
        return "fn"
    if kind == "type":
        return "tag" if kind_raw in ("struct", "union", "enum") else "typedef"
    if kind_raw == "preproc_def":
        words = (signature or "").split()
        if len(words) > 2 and words[2] in _TYPE_WORDS:
            return "typemacro"
        return "macro"
    return "value"


def ref_roles(ref_kind: str, obj: str | None) -> tuple[str, ...]:
    """참조가 볼 수 있는 역할 — 객체형 매크로는 이름공간보다 먼저 전개되니 타입 자리
    (`#define NUM double` 를 타입으로)에도 보인다; 다른 파일의 것은 `global_roles` 만."""
    if ref_kind == "type":
        return ("tag",) if obj else ("typedef", "macro", "typemacro")
    return ("fn", "value", "macro", "typemacro")


def global_roles(roles: tuple[str, ...]) -> tuple[str, ...]:
    """다른 파일의 정의에서 볼 역할 — 타입 자리는 매크로를, 어느 자리든 타입 별칭 매크로를
    빼고. 진짜 typedef 가 색인 밖이면(`u32` 는 include/linux/types.h) 드라이버 헤더 하나의
    `#define u32 unsigned int` 로 색인 전체가 풀린다(구현 리뷰 2회차: drivers/net 타입
    참조 19만·`sizeof(u32)` 같은 이름 참조 4,671). 같은 파일의 것은 범위 사슬에서 본다."""
    return tuple(
        r
        for r in roles
        if r != "typemacro" and not (r == "macro" and "typedef" in roles)
    )


def visible_role(
    file: str, kind: str, kind_raw: str | None, modifiers, signature: str | None = None
) -> str | None:
    """정의 기호가 자기 파일 밖에서 보이면 그 역할, 아니면 None. 헤더의 정의는 전부,
    ``.c`` 는 비-static 함수·변수만."""
    if not file.endswith(".h"):
        if kind_raw not in ("function", "var"):
            return None
        mods = json.loads(modifiers) if isinstance(modifiers, str) else modifiers
        if "static" in (mods or ()):
            return None
    return role_of(kind, kind_raw, signature)


def visible_members(conn, schema: str, names=None) -> dict[str, frozenset]:
    """이름 → 밖에서 보이는 정의 ``{(기호 id, 역할)}`` — 열거자는 그 enum 의 id 와
    ``"value"``. ``names`` 가 있으면 그 이름만."""
    out: dict[str, set] = defaultdict(set)
    rows = conn.execute(
        f"SELECT id, name, file, kind, kind_raw, modifiers, enum_values, signature "
        f"FROM {schema}.symbols WHERE +language = 'c' AND is_definition = 1"
    )
    for sid, name, file, kind, kraw, mods, evs, sig in rows:
        role = visible_role(file, kind, kraw, mods, sig)
        if role is None:
            continue
        if names is None or name in names:
            out[name].add((sid, role))
        if evs:
            for ev in json.loads(evs):
                if names is None or ev in names:
                    out[ev].add((sid, "value"))
    return {k: frozenset(v) for k, v in out.items()}


class Resolver:
    """C 파일들의 참조를 표에서 풀어 적는다 — ``resolve(paths)`` 가 읽은 것을 돌려준다."""

    def __init__(self, conn):
        self.conn = conn
        self.visible = visible_members(conn, "main")
        self.fn_names = {
            r[0]
            for r in conn.execute(
                f"SELECT DISTINCT name FROM symbols WHERE {FUNCTION_DEFINITION} "
                "AND +language = 'c'"
            )
        }

    def resolve(self, paths: list[str]) -> dict[str, set[str]]:
        updates: list[tuple] = []
        deps_out: dict[str, set[str]] = {}
        for path in paths:
            deps: set[str] = set()
            parent: dict[int, int | None] = {}
            kind_of: dict[int, str] = {}
            for sid, pid, skind in self.conn.execute(
                "SELECT id, parent_id, kind FROM scopes WHERE file = ?", (path,)
            ):
                parent[sid] = pid
                kind_of[sid] = skind
            role = {
                sid: role_of(k, kr, sig)
                for sid, k, kr, sig in self.conn.execute(
                    "SELECT id, kind, kind_raw, signature FROM symbols WHERE file = ?",
                    (path,),
                )
            }
            binds: dict[int, dict[str, list]] = defaultdict(lambda: defaultdict(list))
            for sid, name, bkind, line, sym in self.conn.execute(
                "SELECT b.scope_id, b.name, b.kind, b.line, b.symbol_id FROM bindings b "
                "JOIN scopes s ON s.id = b.scope_id WHERE s.file = ? ORDER BY b.line, b.id",
                (path,),
            ):
                binds[sid][name].append((bkind, line, sym))
            for rid, name, rkind, scope, obj, line in self.conn.execute(
                "SELECT id, name, kind, scope_id, object, line FROM refs WHERE file = ?",
                (path,),
            ):
                stored = obj is not None and obj[:1] in (".", "[")
                if rkind == "call" and obj is not None and not stored:
                    deps.add(f"n:c:{name}")
                    res = "candidates" if name in self.fn_names else "external"
                    updates.append((res, None, rkind, rid))
                    continue
                roles = ref_roles(rkind, None if stored else obj)
                res, target, fn_like = self._lookup(
                    name, roles, scope, line, parent, kind_of, binds, role, deps
                )
                kind = rkind
                if stored:
                    kind = "registration" if fn_like else "name"
                updates.append((res, target, kind, rid))
            deps_out[path] = deps
        self.conn.executemany(
            "UPDATE refs SET resolution = ?, target_id = ?, kind = ? WHERE id = ?",
            updates,
        )
        return deps_out

    def _lookup(self, name, roles, scope, line, parent, kind_of, binds, role, deps):
        """``(resolution, target_id, 함수인가)``."""
        s = scope
        while s is not None:
            found = None
            cands = [
                b
                for b in binds.get(s, {}).get(name, ())
                if (role.get(b[2]) if b[0] == "def" else _BINDING_ROLE.get(b[0]))
                in roles
            ]
            if cands:
                if kind_of.get(s) == "module":
                    before = [b for b in cands if b[1] <= line]
                    found = before[-1] if before else cands[0]
                else:
                    found = cands[0]
            if found is not None:
                bkind, _line, sym = found
                if bkind == "decl":
                    return ("external", None, False)
                if bkind in ("param", "local", "typedef") or sym is None:
                    return ("local", None, False)
                return ("resolved", sym, role.get(sym) == "fn")
            s = parent.get(s)
        deps.add(f"g:c:{name}")
        g_roles = global_roles(roles)
        members = [m for m in self.visible.get(name, ()) if m[1] in g_roles]
        if len(members) == 1:
            sid, r = members[0]
            return ("resolved", sid, r == "fn")
        if members:
            return ("candidates", None, all(r == "fn" for _s, r in members))
        return ("external", None, False)
