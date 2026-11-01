"""Python name resolution from the index tables (v10.96.0, DESIGN §21).

A ref is resolved by walking its scope chain the way Python looks a name up:

- a function scope is flat — a name bound anywhere in the function body (a
  parameter, an assignment, a loop/with/except target, an import, a nested
  def or class) is bound in the whole function; a parameter or local makes the
  ref `local` (its value is not known statically), a def/class/import binding
  is followed;
- a class scope is visible only to refs written directly in the class body,
  not to the methods inside it;
- `global` jumps to the module scope, `nonlocal` skips the current scope;
- in a module or class body the binding in effect is the last one on or
  before the ref's line (module code runs top to bottom); a ref from inside a
  function sees the module's final bindings;
- a name bound nowhere is `external` when it is a builtin, `unresolved`
  otherwise (a `from m import *` in the module is tried first).

`from m import x` follows `x` into module `m`'s top level (through further
imports, as deep as the chain goes — a cycle is cut where it closes). Module names map to indexed files through the
source roots: the index root and the parent of every top-level package (a
directory with `__init__.py` whose parent has none), so `src/` layouts and
`tests/` packages resolve. A relative import walks up from the file's own
directory. A module name that two roots map to different files gives
`candidates`; a module outside the index gives `external`.

Attribute calls follow the object chain: `mod.f()`, `pkg.sub.f()`,
`Cls.m()`; `self.m()`/`cls.m()` inside a method look in the method's class
and then its bases, depth first (a base that cannot be resolved makes the
answer `candidates`). Any other object (`obj.m()`, `f().m()`) has an unknown
type: the ref is `candidates`, or `external` when no Python function of that
name is indexed.

While it resolves a file the resolver notes everything it read (`deps`): the
files whose scopes, bindings or class bases it looked at — through imports,
re-exports and base classes, transitively; a module's top-level name looked
up from elsewhere is noted as that one name — the module names and relative
paths it looked up, and the function names whose existence decided a
`candidates`/`external` answer. Resolution reads only what the parser wrote,
never another file's resolution, so an incremental build re-resolves exactly
the files with a noted input that changed (builder `_resolve_targets`).
"""

from __future__ import annotations

import builtins
import json
import os
from collections import defaultdict

#: 이름이 묶이지 않은 참조 중 바깥(내장)인 것.
BUILTINS = frozenset(dir(builtins)) | {
    "__file__",
    "__name__",
    "__doc__",
    "__spec__",
    "__package__",
    "__loader__",
    "__builtins__",
    "__path__",
}
_BASE_DEPTH = 6
_FUNCTION_SCOPED = ("param", "local")


class _File:
    """한 파일의 범위·바인딩 — 표에서 한 번 읽어 둔다."""

    __slots__ = ("bindings", "module", "path", "scopes")

    def __init__(self, path: str):
        self.path = path
        self.module: int | None = None
        # id → (kind, parent_id, symbol_id, start_line)
        self.scopes: dict[int, tuple] = {}
        # scope_id → name → [(kind, line, import_module, import_name, target, id, name)]
        self.bindings: dict[int, dict[str, list]] = defaultdict(
            lambda: defaultdict(list)
        )


class Resolver:
    def __init__(self, conn):
        self.conn = conn
        self.files: dict[str, _File] = {}
        self.py_files: set[str] = {
            r[0]
            for r in conn.execute("SELECT path FROM files WHERE language = 'python'")
        }
        self.modmap, self.roots = module_map(self.py_files)
        self.fn_names: set[str] = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT name FROM symbols WHERE +kind = 'function' "
                "AND +language = 'python'"
            )
        }
        self._class_scope: dict[int, tuple[str, int] | None] = {}
        self._bases: dict[int, list[str]] = {}
        # 한 번의 해석 안에서 같은 import·모듈 이름을 다시 따라가지 않는다 — 허브 이름을
        # 다시 묶은 편집(django `TestCase`, 304 파일)에서 해석 시간의 절반이 반복이었다
        # 값은 ``(결과, 그 계산이 읽은 것, 기댄 것)`` — 계산 중이면 읽은 것이 None(순환)
        self._follow: dict[int, tuple] = {}
        self._in_module: dict[tuple[str, str], tuple] = {}
        # 지금 계산이 읽은 것 — ``("f", 파일)``·``("e", 파일, 이름)``·``("m", 모듈 이름)``·
        # ``("p", 상대 import 의 후보 경로)``·``("n", 함수 이름)``
        self._deps: set = set()
        # 지금 계산이 진입점에 기댄 것 — 계산 중이던 다른 메모에 걸렸다(순환). 그런 결과가
        # 해석 묶음을 넘어 남으면 증분(바뀐 파일만)과 전체 재구축의 답이 갈린다. 그렇다고 바로
        # 버리면 `*` 순환 안에서 같은 계산을 가지마다 다시 해 비용이 지수로 는다 — 그래서 기댄
        # 메모가 계산 중인 동안만 남긴다(그 프레임이 끝나면 지운다). import 깊이 상한은 두지
        # 않는다: 얕은 진입에서 깨끗하게 메모된 값을 깊은 진입이 받으면 상한도 묶음에 기댄다.
        # 끝나는 것은 "계산 중" 절단이 보장한다(키는 유한하고 한 키는 스택에 한 번만 오른다)
        self._taint: set = set()
        # 기댄 키 → [(메모, 키, 값)]
        self._leaning: dict[tuple, list] = defaultdict(list)

    def _memo(self, cache: dict, tag: str, key, compute):
        hit = cache.get(key)
        if hit is not None:
            r, deps, taint = hit
            if deps is None:
                self._taint.add((tag, key))
            else:
                self._deps |= deps
                self._taint |= taint
            return r
        cache[key] = (("unresolved",), None, None)  # 순환을 끊는다
        outer_deps, outer_taint = self._deps, self._taint
        self._deps, self._taint = set(), set()
        try:
            r = compute()
        finally:
            deps, taint = self._deps, self._taint
            self._deps, self._taint = outer_deps, outer_taint
        taint.discard((tag, key))
        # 읽은 것은 늘 바깥으로 — 순환으로 걸린 메모는 스택 아래에서 계산 중이니, 그 계산이
        # 끝나며 같은 것을 다 담는다
        outer_deps |= deps
        entry = (r, frozenset(deps), frozenset(taint))
        cache[key] = entry
        if taint:
            outer_taint |= taint
            for t in taint:
                self._leaning[t].append((cache, key, entry))
        # 이 메모가 끝났다 — 계산 중이던 이것에 기댄 메모는 이제 맞지 않는다
        self._drop(self._leaning.pop((tag, key), ()))
        return r

    @staticmethod
    def _drop(entries) -> None:
        for cache, key, entry in entries:
            if cache.get(key) is entry:
                del cache[key]

    # ----- table access -----

    def file(self, path: str, whole: bool = True) -> _File:
        """파일의 범위·바인딩. ``whole=False`` 는 부르는 쪽이 읽는 이름을 따로 적는다
        (`_resolve_in_module` — 모듈 최상위의 한 이름만 본다)."""
        if whole:
            self._deps.add(("f", path))
        f = self.files.get(path)
        if f is not None:
            return f
        f = _File(path)
        for sid, kind, pid, sym, start in self.conn.execute(
            "SELECT id, kind, parent_id, symbol_id, start_line FROM scopes "
            "WHERE file = ?",
            (path,),
        ):
            f.scopes[sid] = (kind, pid, sym, start)
            if kind == "module":
                f.module = sid
        for bid, sid, name, kind, line, imod, iname, tgt in self.conn.execute(
            "SELECT b.id, b.scope_id, b.name, b.kind, b.line, b.import_module, "
            "b.import_name, b.symbol_id FROM bindings b JOIN scopes s "
            "ON s.id = b.scope_id WHERE s.file = ? ORDER BY b.line, b.id",
            (path,),
        ):
            f.bindings[sid][name].append((kind, line, imod, iname, tgt, bid, name))
        self.files[path] = f
        return f

    def class_scope(self, class_id: int) -> tuple[str, int] | None:
        """클래스 기호 → ``(파일, 클래스 범위 id)``."""
        if class_id not in self._class_scope:
            row = self.conn.execute(
                "SELECT file, id FROM scopes WHERE symbol_id = ? AND kind = 'class'",
                (class_id,),
            ).fetchone()
            self._class_scope[class_id] = (row[0], row[1]) if row else None
        return self._class_scope[class_id]

    def bases(self, class_id: int) -> list[str]:
        """부르는 쪽(`method_in_class`)이 그 클래스 파일을 이미 읽었다 — 읽은 것은 거기서."""
        if class_id not in self._bases:
            row = self.conn.execute(
                "SELECT bases FROM symbols WHERE id = ?", (class_id,)
            ).fetchone()
            self._bases[class_id] = json.loads(row[0]) if row and row[0] else []
        return self._bases[class_id]

    # ----- modules -----

    def module_files(self, modtxt: str, from_file: str) -> list[str]:
        if modtxt.startswith("."):
            dots = len(modtxt) - len(modtxt.lstrip("."))
            rest = modtxt[dots:]
            base = os.path.dirname(from_file)
            for _ in range(dots - 1):
                base = os.path.dirname(base)
            path = "/".join(x for x in (base, rest.replace(".", "/")) if x)
            cands = [f"{path}.py", f"{path}/__init__.py"] if path else ["__init__.py"]
            self._deps.update(("p", c) for c in cands)
            return [c for c in cands if c in self.py_files]
        self._deps.add(("m", modtxt))
        return self.modmap.get(modtxt, [])

    def resolve_in_module(self, path: str, name: str):
        """모듈 ``path`` 의 최상위에서 ``name`` 이 뜻하는 것, 없으면 None."""
        key = (path, name)
        return self._memo(
            self._in_module,
            "m",
            key,
            lambda: self._resolve_in_module(path, name),
        )

    def _resolve_in_module(self, path: str, name: str):
        # 그 모듈 최상위의 이 이름과 `*` 만 본다(줄은 안 본다 — 마지막 바인딩)
        f = self.file(path, whole=False)
        self._deps.add(("e", path, name))
        self._deps.add(("e", path, "*"))
        if f.module is None:
            return None
        bs = [
            b
            for b in f.bindings[f.module].get(name, ())
            if b[0] not in ("global", "nonlocal")
        ]
        if bs:
            return self._by_binding(f, bs[-1])
        # `import *` 이 여럿이면 정의를 내놓는 쪽이 이긴다 — 첫 번째가 다시 `*` 를
        # 거쳐 모른다고 해도 다음 것을 본다(django gis `models/__init__` 의 두 `*`)
        stars = f.bindings[f.module].get("*", ())
        fallback = None
        for b in stars:
            for g in self.module_files(b[2] or "", path):
                r = self.resolve_in_module(g, name)
                if r is None or r[0] == "unresolved":
                    continue
                if r[0] in ("def", "class", "module"):
                    return r
                fallback = fallback or r
        if fallback is not None:
            return fallback
        if stars:
            return ("candidates",)
        return None

    def follow_import(self, f: _File, b):
        return self._memo(self._follow, "f", b[5], lambda: self._follow_import(f, b))

    def _follow_import(self, f: _File, b):
        _kind, _line, imod, iname, _tgt, _bid, _name = b
        imod = imod or ""
        if iname is None:
            # `import a.b.c` 는 `a` 를 묶는다(별칭이면 `a.b.c` 자체)
            return self._module(imod, f.path)
        if iname == "*":
            return ("candidates",)
        files = self.module_files(imod, f.path)
        sub = (imod + iname) if imod.endswith(".") else f"{imod}.{iname}"
        if not files:
            m = self._module(sub, f.path)
            return m if m[0] == "module" else ("external",)
        results = []
        for g in files:
            r = self.resolve_in_module(g, iname)
            if r is None or r[0] in ("candidates", "unresolved"):
                # 패키지에 그 이름이 직접 묶여 있지 않으면 서브모듈이 먼저다 —
                # `from pkg import functions` 는 `pkg/functions.py`
                m = self._module(sub, f.path)
                if m[0] == "module":
                    results.append(m)
                    continue
            results.append(r if r is not None else ("unresolved",))
        targets = {r[1] for r in results if r[0] in ("def", "class")}
        if len(targets) == 1:
            return next(r for r in results if r[0] in ("def", "class"))
        if len(targets) > 1:
            return ("candidates",)
        return results[0]

    def _module(self, modtxt: str, from_file: str):
        files = self.module_files(modtxt, from_file)
        if files:
            return ("module", tuple(files), modtxt)
        return ("external",)

    def _import_head(self, f: _File, b):
        """``import a.b.c`` 의 바인딩 ``a`` → 모듈 ``a``; ``import a.b.c as x`` 의
        ``x`` → 모듈 ``a.b.c`` (바인딩 이름이 첫 조각과 다르면 별칭)."""
        _kind, _line, imod, iname, _tgt, _bid, bname = b
        if iname is None and imod and "." in imod:
            first = imod.split(".", 1)[0]
            if bname == first:
                return self._module(first, f.path)
        return self.follow_import(f, b)

    # ----- lookup -----

    def _by_binding(self, f: _File, b):
        kind = b[0]
        if kind in ("def", "class"):
            return (kind, b[4]) if b[4] is not None else ("unresolved",)
        if kind == "import":
            if b[3] is None:
                return self._import_head(f, b)
            return self.follow_import(f, b)
        return ("local", None, kind)

    def lookup(self, f: _File, scope: int, name: str, line: int):
        """어휘 범위 탐색 — ``("def"|"class", id) | ("module", files, name) |
        ("local", scope_id, binding_kind) | ("candidates",) | ("external",) |
        ("unresolved",)``."""
        s: int | None = scope
        first = True
        while s is not None:
            kind, parent, _sym, _start = f.scopes[s]
            if kind == "class" and not first:
                s = parent
                continue
            bs = f.bindings[s].get(name, ())
            if any(b[0] == "nonlocal" for b in bs):
                s, first = parent, False
                continue
            if any(b[0] == "global" for b in bs):
                s, first = f.module, False
                bs = [
                    b
                    for b in f.bindings[s].get(name, ())
                    if b[0] not in ("global", "nonlocal")
                ]
                if bs:
                    return self._pick(f, s, "module", bs, line, False)
                break
            real = [b for b in bs if b[0] not in ("global", "nonlocal")]
            if real:
                return self._pick(f, s, kind, real, line, first)
            first = False
            s = parent
        if f.module is not None and f.bindings[f.module].get("*"):
            r = self.resolve_in_module(f.path, name)
            if r is not None:
                return r
        return ("external",) if name in BUILTINS else ("unresolved",)

    def _pick(self, f: _File, s: int, kind: str, bs: list, line: int, first: bool):
        if kind in ("function", "comprehension"):
            for b in bs:
                if b[0] in _FUNCTION_SCOPED:
                    return ("local", s, b[0])
            return self._by_binding(f, bs[-1])
        # 모듈·클래스 본문은 위에서 아래로 — 그 본문에 직접 쓴 참조는 그 줄 이전의 마지막
        # 바인딩, 함수 안에서 온 참조는 최종 바인딩
        chosen = bs[-1]
        if first:
            before = [b for b in bs if b[1] <= line]
            if before:
                chosen = before[-1]
        if chosen[0] in _FUNCTION_SCOPED:
            return ("local", s, chosen[0])
        return self._by_binding(f, chosen)

    # ----- attributes -----

    def head(self, f: _File, scope: int, name: str, line: int):
        """체인의 첫 조각 — 메서드의 ``self``/``cls`` 는 그 클래스."""
        r = self.lookup(f, scope, name, line)
        if r[0] == "local" and r[2] == "param" and name in ("self", "cls"):
            _kind, parent, _sym, _start = f.scopes[r[1]]
            if parent is not None and f.scopes[parent][0] == "class":
                cls_sym = f.scopes[parent][2]
                if cls_sym is not None:
                    return ("class", cls_sym)
        return r

    def member(self, f: _File, head, seg: str):
        kind = head[0]
        if kind == "module":
            outs = [self.resolve_in_module(g, seg) for g in head[1]]
            for o in outs:
                if o is not None and o[0] in ("def", "class"):
                    return o
            for o in outs:
                if o is not None and o[0] == "module":
                    return o
            sub = self._module(f"{head[2]}.{seg}", f.path)
            if sub[0] == "module":
                return sub
            for o in outs:
                if o is not None:
                    return o
            return ("unresolved",)
        if kind == "class":
            return self.method_in_class(head[1], seg)
        if kind == "external":
            return ("external",)
        return ("candidates",)

    def method_in_class(self, class_id: int, name: str, depth: int = 0, seen=None):
        seen = seen if seen is not None else set()
        if class_id in seen or depth > _BASE_DEPTH:
            return ("candidates",)
        seen.add(class_id)
        loc = self.class_scope(class_id)
        if loc is None:
            return ("candidates",)
        cf = self.file(loc[0])
        bs = cf.bindings[loc[1]].get(name, ())
        if bs:
            b = bs[-1]
            if b[0] in ("def", "class"):
                return (b[0], b[4]) if b[4] is not None else ("unresolved",)
            return ("local", loc[1], b[0])
        parent = cf.scopes[loc[1]][1]
        start = cf.scopes[loc[1]][3]
        for base in self.bases(class_id):
            if base == "<expr>":
                continue
            r = self.chain(cf, parent, base, start)
            if r[0] == "class":
                rr = self.method_in_class(r[1], name, depth + 1, seen)
                if rr[0] in ("def", "class", "local"):
                    return rr
        # 클래스·베이스 어디에도 없다 — 해석 안 되는 베이스, 동적 속성, 믹스인일 수
        # 있으니 이름으로만 맞춘다(그 이름의 함수가 없으면 `external`)
        return ("candidates",)

    def chain(self, f: _File, scope: int, text: str, line: int):
        if text == "<expr>":
            return ("candidates",)
        parts = text.split(".")
        r = self.head(f, scope, parts[0], line)
        for seg in parts[1:]:
            r = self.member(f, r, seg)
        return r

    # ----- refs -----

    def resolve(self, paths: list[str]) -> dict[str, set[str]]:
        """``paths`` 의 참조를 해석해 적고, 파일마다 읽은 것을 돌려준다
        (``"f:파일"``·``"m:모듈"``·``"p:경로"``·``"n:python:이름"``, 자기 파일은 뺀다)."""
        updates: list[tuple] = []
        deps_out: dict[str, set[str]] = {}
        for path in paths:
            self._deps, self._taint = set(), set()
            f = self.file(path)
            for rid, name, scope, obj, line in self.conn.execute(
                "SELECT id, name, scope_id, object, line FROM refs WHERE file = ?",
                (path,),
            ):
                if obj is None:
                    r = self.lookup(f, scope, name, line)
                else:
                    r = self.member(f, self.chain(f, scope, obj, line), name)
                updates.append(self._row(r, name) + (rid,))
            deps_out[path] = {_dep_key(d) for d in self._deps if d[1] != path}
        self._deps, self._taint = set(), set()
        self.conn.executemany(
            "UPDATE refs SET resolution = ?, target_id = ? WHERE id = ?", updates
        )
        return deps_out

    def _row(self, r, name: str) -> tuple:
        kind = r[0]
        if kind in ("def", "class"):
            return ("resolved", r[1])
        if kind == "local":
            return ("local", None)
        if kind == "module":
            return ("external", None)
        if kind == "candidates":
            self._deps.add(("n", name))
            return ("candidates" if name in self.fn_names else "external", None)
        return (kind, None)


def _dep_key(d: tuple) -> str:
    """``resolve_deps.dep`` 문자열 — 이름에는 ``:`` 가 없으니 경로를 끝에 둔다."""
    if d[0] == "n":
        return f"n:python:{d[1]}"
    if d[0] == "e":
        return f"e:{d[2]}:{d[1]}"
    return f"{d[0]}:{d[1]}"


def module_map(py_files: set[str]):
    """모듈 이름 → 파일들, 그리고 소스 루트 집합 (모듈 docstring 의 규칙)."""
    pkg_dirs = {
        os.path.dirname(p) for p in py_files if os.path.basename(p) == "__init__.py"
    }
    roots = {""}
    for d in pkg_dirs:
        parent = os.path.dirname(d)
        if parent not in pkg_dirs:
            roots.add(parent)
    modmap: dict[str, list[str]] = defaultdict(list)
    for p in sorted(py_files):
        if not p.endswith(".py"):
            continue
        for root in roots:
            if root and not p.startswith(root + "/"):
                continue
            rel = p[len(root) + 1 :] if root else p
            parts = rel[:-3].split("/")
            if parts[-1] == "__init__":
                parts = parts[:-1]
            if not parts:
                continue
            # 모듈을 담는 디렉토리는 전부 패키지여야 한다
            dirs = parts if p.endswith("__init__.py") else parts[:-1]
            cur = root
            ok = True
            for seg in dirs:
                cur = f"{cur}/{seg}" if cur else seg
                if cur not in pkg_dirs:
                    ok = False
                    break
            if ok:
                name = ".".join(parts)
                if p not in modmap[name]:
                    modmap[name].append(p)
    return dict(modmap), roots
