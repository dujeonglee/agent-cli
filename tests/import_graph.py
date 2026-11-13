"""패키지 import 그래프 — AST 로만 읽는다(아무것도 import 하지 않는다).

정의 (docs/ARCHITECTURE.md §3):

- **top** 간선 — 모듈이 import 될 때 실행되는 import. 모듈·클래스 본문, 모듈 수준 ``if``/``try``
  안을 포함하고 ``if TYPE_CHECKING:`` 의 본문은 뺀다.
- **lazy** 간선 — 함수 본문 안의 import. 같은 간선이 top 에도 있으면 lazy 에서 뺀다.
- ``import a.b.c`` 는 ``a.b.c`` 와 그 부모 패키지들(``a.b`` …)로 가는 간선이다(부모의 ``__init__``
  이 먼저 돈다). ``from pkg import name`` 은 ``pkg.name`` 이 모듈이면 그 모듈로, 아니면 ``pkg`` 로.
  자기 자신과 **자기 조상 패키지**로 가는 간선은 뺀다(이미 import 중이다).
- **순환 회피용** lazy 간선 ``u → v`` — ``v`` 가 top 간선만으로 ``u`` 에 닿는다. 이 import 를
  모듈 수준으로 올리면 top 그래프에 순환이 생긴다.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ImportGraph:
    modules: set[str]
    top: dict[str, set[str]] = field(default_factory=dict)
    lazy: dict[str, set[str]] = field(default_factory=dict)

    def lazy_edges(self) -> set[tuple[str, str]]:
        return {(u, v) for u, vs in self.lazy.items() for v in vs}

    def top_reach(self, start: str) -> set[str]:
        """``start`` 에서 top 간선만으로 닿는 모듈(자기 자신 제외 — 순환이면 포함)."""
        seen: set[str] = set()
        stack = list(self.top.get(start, ()))
        while stack:
            m = stack.pop()
            if m in seen:
                continue
            seen.add(m)
            stack.extend(self.top.get(m, ()))
        return seen

    def cycle_avoiding_lazy(self) -> set[tuple[str, str]]:
        return {(u, v) for u, v in self.lazy_edges() if u in self.top_reach(v)}

    def top_cycles(self) -> list[list[str]]:
        """top 그래프의 강결합 성분 중 크기 2 이상(정렬된 이름 목록)."""
        return [c for c in _sccs(self.modules, self.top) if len(c) > 1]


def module_name(root: Path, path: Path) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _ancestors(mod: str) -> list[str]:
    ps = mod.split(".")
    return [".".join(ps[:i]) for i in range(1, len(ps))]


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _targets(
    cur: str, is_pkg: bool, node: ast.stmt, modules: set[str], prefix: str
) -> set[str]:
    named: list[str] = []
    if isinstance(node, ast.Import):
        named = [a.name for a in node.names]
    else:
        assert isinstance(node, ast.ImportFrom)
        if node.level:
            base = cur.split(".")
            if not is_pkg:
                base = base[:-1]
            base = base[: len(base) - (node.level - 1)]
            mod = ".".join(base + ([node.module] if node.module else []))
        else:
            mod = node.module or ""
        for a in node.names:
            sub = f"{mod}.{a.name}"
            named.append(sub if sub in modules else mod)
    out: set[str] = set()
    for n in named:
        if n != prefix and not n.startswith(prefix + "."):
            continue
        if n in modules:
            out.add(n)
        out.update(a for a in _ancestors(n) if a in modules)
    return out - {cur} - set(_ancestors(cur))


def _import_nodes(node: ast.AST, in_func: bool = False):
    """``(import 문, 함수 안인가)`` — ``if TYPE_CHECKING:`` 의 본문은 건너뛴다(``else`` 는 본다)."""
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        yield node, in_func
        return
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        in_func = True
    elif isinstance(node, ast.If) and _is_type_checking(node.test):
        for s in node.orelse:
            yield from _import_nodes(s, in_func)
        return
    for ch in ast.iter_child_nodes(node):
        yield from _import_nodes(ch, in_func)


def build_graph(root: Path, package: str) -> ImportGraph:
    """``root/package`` 아래 모든 ``.py`` 의 그래프(``__pycache__`` 제외)."""
    files = {
        module_name(root, p): p
        for p in sorted((root / package).rglob("*.py"))
        if "__pycache__" not in p.parts
    }
    g = ImportGraph(modules=set(files))
    for name, path in files.items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        is_pkg = path.name == "__init__.py"
        top: set[str] = set()
        lazy: set[str] = set()
        for node, in_func in _import_nodes(tree):
            (lazy if in_func else top).update(
                _targets(name, is_pkg, node, g.modules, package)
            )
        g.top[name] = top
        g.lazy[name] = lazy - top
    return g


def _sccs(nodes: set[str], edges: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan (반복형) — 강결합 성분들."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on: set[str] = set()
    stack: list[str] = []
    out: list[list[str]] = []
    counter = 0
    for start in sorted(nodes):
        if start in index:
            continue
        work = [(start, iter(sorted(edges.get(start, ()))))]
        index[start] = low[start] = counter
        counter += 1
        stack.append(start)
        on.add(start)
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in nodes:
                    continue
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on.add(w)
                    work.append((w, iter(sorted(edges.get(w, ())))))
                    advanced = True
                    break
                if w in on:
                    low[v] = min(low[v], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                out.append(sorted(comp))
    return out
