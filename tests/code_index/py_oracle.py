"""Independent oracle for the Python resolver (v10.96.0, DESIGN §21).

Built on CPython's own `ast` and `symtable` — a different parser and the
reference implementation of Python's scoping rules — so a tree-sitter node
name the walker misses, or a scoping rule the resolver gets wrong, shows up
as a disagreement instead of being shared by implementation and test.

The oracle answers one question per bare-name ref (`f()` / `f`) that sits
directly in a `def` body: is the name bound to a parameter or a variable
(`local`), or to something the resolver must follow (a def, a class, an
import, a builtin)? Refs inside lambdas and comprehensions, on a `def` line
(defaults, annotations) and names that are both a variable and a def/import
in the same scope are skipped — those are covered by the hand-written
expectations in `test_resolution.py`.
"""

from __future__ import annotations

import ast
import symtable


def _function_spans(tree) -> list[tuple[int, int, int, ast.AST]]:
    """``(body_start, end, def_line, node)`` per def — nested ones included."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append((node.body[0].lineno, node.end_lineno, node.lineno, node))
    return out


def _blocked_lines(tree) -> set[int]:
    """lambda·컴프리헨션이 걸친 줄 — 그 안의 참조는 묻지 않는다."""
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node,
            (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp),
        ):
            lines.update(range(node.lineno, node.end_lineno + 1))
    return lines


def _tables(top) -> dict[tuple[str, int], symtable.SymbolTable]:
    out = {}
    stack = [top]
    while stack:
        t = stack.pop()
        if t.get_type() == "function":
            out[(t.get_name(), t.get_lineno())] = t
        stack.extend(t.get_children())
    return out


def _bound_kind(sym) -> str:
    """``"local"`` (param·variable) | ``"follow"`` (def·class·import). def/class 도
    ``is_assigned`` 라 namespace 를 먼저 본다 — 같은 범위에서 def 이면서 따로 대입도
    된 이름은 symtable 이 가르지 못하니 테스트 자료가 피한다."""
    if sym.is_parameter():
        return "local"
    if sym.is_imported() or sym.is_namespace():
        return "follow"
    return "local" if sym.is_assigned() else "follow"


def _plain_bindings(fn) -> set[str]:
    """def 몸 안(중첩 함수·클래스 제외)에서 대입·루프·with·except 로 묶인 이름."""
    out: set[str] = set()
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        n = stack.pop()
        if isinstance(
            n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
        ):
            continue
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            out.add(n.id)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
        stack.extend(ast.iter_child_nodes(n))
    return out


def classify(source: str, path: str, refs: list[tuple[int, str]]) -> dict:
    """``{(line, name): "local" | "follow"}`` for the refs the oracle can answer.
    A source the parser rejects answers nothing."""
    try:
        tree = ast.parse(source)
        top = symtable.symtable(source, path, "exec")
    except SyntaxError:
        return {}
    tables = _tables(top)
    spans = _function_spans(tree)
    blocked = _blocked_lines(tree)
    parents: dict[ast.AST, ast.AST | None] = {}
    for _s, _e, _d, node in spans:
        parents.setdefault(node, None)
    for s1, e1, _d1, n1 in spans:
        for s2, e2, _d2, n2 in spans:
            if n1 is not n2 and n2.lineno < n1.lineno and e1 <= e2:
                p = parents.get(n1)
                if p is None or p.lineno < n2.lineno:
                    parents[n1] = n2
    module_syms = {s.get_name(): s for s in top.get_symbols()}
    out: dict = {}
    for line, name in refs:
        if line in blocked:
            continue
        inner = [sp for sp in spans if sp[0] <= line <= sp[1] and sp[2] < line]
        if not inner:
            continue
        node = max(inner, key=lambda sp: sp[0])[3]
        verdict = None
        while node is not None:
            t = tables.get((node.name, node.lineno))
            if t is None:
                break
            try:
                sym = t.lookup(name)
            except KeyError:
                sym = None
            if sym is not None and sym.is_local():
                if (
                    sym.is_namespace() or sym.is_imported()
                ) and name in _plain_bindings(node):
                    verdict = "skip"  # def/import 이면서 따로 대입 — 모호
                else:
                    verdict = _bound_kind(sym)
                break
            if sym is not None and (sym.is_global() or sym.is_declared_global()):
                node = None
                break
            node = parents.get(node)
        if verdict is None and node is None:
            msym = module_syms.get(name)
            if msym is None:
                verdict = "follow"  # builtin or unbound — never `local`
            else:
                verdict = _bound_kind(msym)
        if verdict not in (None, "skip"):
            out[(line, name)] = verdict
    return out
