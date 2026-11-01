# Originally ported from minish.ai/Agent-tools tsindex.py (Apache 2.0).
# See NOTICE for the list of modifications.
"""Python language walker for code_index.

Pass-1 (`walk_definitions`) emits Symbol records for functions, classes,
and module-/class-level assignments. UPPER_SNAKE module-level
assignments map to `kind='constant'`; everything else maps to
`kind='variable'`. Decorated definitions keep the decorator names in
`modifiers` (e.g. ``["staticmethod", "property"]``); `async def`
contributes the `"async"` modifier.

Pass-2 (`walk_scopes`, v10.96.0 — docs/code-index/DESIGN.md §21) walks
the tree once with the current lexical scope in hand and emits three
things: the scopes (module, class, function, lambda, comprehension), what
each scope binds (parameters, assignments, loop/with/except targets,
imports, nested defs/classes, `global`/`nonlocal`), and the refs —
`kind='call'` for callable sites (`f(...)`, `obj.method(...)`, with the
object chain text) and `kind='name'` for bare identifier mentions of
names defined elsewhere in the indexed code. Binding targets are not
refs. Default values, annotations, decorators, base classes and the
first iterable of a comprehension are evaluated in the enclosing scope,
as Python does.
"""

from __future__ import annotations

import re

from agent_cli.code_index.languages import LANGUAGES, LangSpec, noop_preprocess
from agent_cli.code_index.languages._shared import qualify, text
from agent_cli.code_index.schema import Binding, Ref, Scope, Symbol


def _lang_python():
    import tree_sitter_python
    from tree_sitter import Language

    return Language(tree_sitter_python.language())


def py_extract_function(
    node,
    src: bytes,
    rel: str,
    parent: str | None,
    out: list,
    extra_modifiers: list[str],
):
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = text(name_node, src)
    params_node = node.child_by_field_name("parameters")
    param_names: list[str] = []
    if params_node is not None:
        for c in params_node.named_children:
            n = None
            if c.type == "identifier":
                n = c
            elif c.type in (
                "typed_parameter",
                "default_parameter",
                "typed_default_parameter",
                "list_splat_pattern",
                "dictionary_splat_pattern",
            ):
                n = c.child_by_field_name("name")
                if n is None:
                    for ch in c.children:
                        if ch.type == "identifier":
                            n = ch
                            break
            if n is not None:
                param_names.append(text(n, src))
    mods = list(extra_modifiers)
    # `async def` adds a leading `async` token child.
    for ch in node.children:
        if ch.type == "async":
            mods.append("async")
            break
    return_type = None
    rt = node.child_by_field_name("return_type")
    if rt is not None:
        return_type = text(rt, src)
    body = node.child_by_field_name("body")
    if body is not None:
        sig = src[node.start_byte : body.start_byte].decode("utf-8", "replace")
        sig = " ".join(sig.split())
    else:
        sig = text(node, src).split("\n", 1)[0]
    out.append(
        Symbol(
            name=name,
            qualified_name=qualify(parent, name),
            kind="function",
            file=rel,
            line=node.start_point[0] + 1,
            col=node.start_point[1],
            end_line=node.end_point[0] + 1,
            is_definition=True,
            language="python",
            kind_raw="function_definition",
            modifiers=mods or None,
            parent=parent,
            signature=sig,
            return_type=return_type,
            params=param_names or None,
        )
    )
    # Descend into the function body so nested function / class definitions
    # (closures, helper inner functions, locally-defined classes) become
    # discoverable. Parent is the dotted chain of enclosing names.
    if body is not None:
        inner_parent = qualify(parent, name)
        for stmt in body.children:
            if stmt.type == "function_definition":
                py_extract_function(stmt, src, rel, inner_parent, out, [])
            elif stmt.type == "class_definition":
                py_extract_class(stmt, src, rel, inner_parent, out, [])
            elif stmt.type == "decorated_definition":
                py_extract_decorated(stmt, src, rel, inner_parent, out)


def py_extract_class(
    node,
    src: bytes,
    rel: str,
    parent: str | None,
    out: list,
    extra_modifiers: list[str],
):
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    cls_name = text(name_node, src)
    bases: list[str] = []
    sup = node.child_by_field_name("superclasses")
    if sup is not None:
        for arg in sup.named_children:
            if arg.type in ("identifier", "attribute"):
                bases.append(text(arg, src))
            elif arg.type != "keyword_argument":
                bases.append("<expr>")
    out.append(
        Symbol(
            name=cls_name,
            qualified_name=qualify(parent, cls_name),
            kind="type",
            bases=bases or None,
            file=rel,
            line=node.start_point[0] + 1,
            col=node.start_point[1],
            end_line=node.end_point[0] + 1,
            is_definition=True,
            language="python",
            kind_raw="class_definition",
            modifiers=extra_modifiers or None,
            parent=parent,
        )
    )
    body = node.child_by_field_name("body")
    if body is None:
        return
    inner_parent = qualify(parent, cls_name)
    for stmt in body.children:
        if stmt.type == "function_definition":
            py_extract_function(stmt, src, rel, inner_parent, out, [])
        elif stmt.type == "class_definition":
            py_extract_class(stmt, src, rel, inner_parent, out, [])
        elif stmt.type == "decorated_definition":
            py_extract_decorated(stmt, src, rel, inner_parent, out)
        elif stmt.type == "expression_statement":
            for inner in stmt.children:
                if inner.type == "assignment":
                    py_extract_assignment(
                        inner, src, rel, inner_parent, out, is_class_attr=True
                    )


def py_extract_decorated(node, src: bytes, rel: str, parent: str | None, out: list):
    decorators: list[str] = []
    target = None
    for ch in node.children:
        if ch.type == "decorator":
            dt = text(ch, src).lstrip("@").strip().split("\n", 1)[0]
            m = re.match(r"[A-Za-z_][\w.]*", dt)
            decorators.append(m.group(0) if m else dt)
        elif ch.type == "function_definition":
            target = ("function", ch)
        elif ch.type == "class_definition":
            target = ("class", ch)
    if target is None:
        return
    kind, t = target
    if kind == "function":
        py_extract_function(t, src, rel, parent, out, decorators)
    else:
        py_extract_class(t, src, rel, parent, out, decorators)


def py_extract_assignment(
    node,
    src: bytes,
    rel: str,
    parent: str | None,
    out: list,
    is_class_attr: bool = False,
):
    left = node.child_by_field_name("left")
    if left is None:
        return
    targets: list = []
    if left.type == "identifier":
        targets.append(left)
    elif left.type in ("pattern_list", "tuple_pattern"):
        for c in left.children:
            if c.type == "identifier":
                targets.append(c)
    for t in targets:
        name = text(t, src)
        is_const = name.isupper() and len(name) > 1
        sig = " ".join(text(node, src).split())
        out.append(
            Symbol(
                name=name,
                qualified_name=qualify(parent, name),
                # Class attributes are also variables — `parent` distinguishes
                # them from module-level globals.
                kind="constant" if (is_const and not is_class_attr) else "variable",
                file=rel,
                line=node.start_point[0] + 1,
                col=node.start_point[1],
                end_line=node.end_point[0] + 1,
                is_definition=True,
                language="python",
                kind_raw="assignment",
                parent=parent,
                signature=sig[:200],
            )
        )


def walk_definitions(root, src: bytes, rel: str, syms: list):
    for node in root.children:
        if node.type == "function_definition":
            py_extract_function(node, src, rel, None, syms, [])
        elif node.type == "class_definition":
            py_extract_class(node, src, rel, None, syms, [])
        elif node.type == "decorated_definition":
            py_extract_decorated(node, src, rel, None, syms)
        elif node.type == "expression_statement":
            for inner in node.children:
                if inner.type == "assignment":
                    py_extract_assignment(inner, src, rel, None, syms)


#: 객체 체인 텍스트의 상한 — 넘거나 이름·속성 체인이 아니면 ``"<expr>"``.
_CHAIN_MAX = 64
_COMPREHENSIONS = frozenset(
    {
        "list_comprehension",
        "set_comprehension",
        "dictionary_comprehension",
        "generator_expression",
    }
)


def _chain(node, src: bytes) -> str:
    """``a.b.c`` 같은 이름·속성 체인의 텍스트, 아니면 ``"<expr>"``."""
    parts: list[str] = []
    n = node
    while n is not None and n.type == "attribute":
        attr = n.child_by_field_name("attribute")
        if attr is None:
            return "<expr>"
        parts.append(text(attr, src))
        n = n.child_by_field_name("object")
    if n is None or n.type != "identifier":
        return "<expr>"
    parts.append(text(n, src))
    out = ".".join(reversed(parts))
    return out if len(out) <= _CHAIN_MAX else "<expr>"


def _target_names(node, src: bytes, out: list) -> list:
    """대입·for·with-as·컴프리헨션 좌변에서 **묶이는** 식별자들. 속성·첨자 좌변의
    부분식(``a.b = …`` 의 ``a``)은 참조라 ``exprs`` 로 따로 돌려준다."""
    exprs: list = []
    stack = [node]
    while stack:
        n = stack.pop()
        t = n.type
        if t == "identifier":
            out.append(n)
        elif t in (
            "pattern_list",
            "tuple_pattern",
            "list_pattern",
            "list_splat_pattern",
            "parenthesized_expression",
            "as_pattern_target",
            "tuple",
            "list",
            "expression_list",
        ):
            stack.extend(n.named_children)
        elif t in ("attribute", "subscript"):
            exprs.append(n)
    return exprs


def _param_names(params_node, src: bytes):
    """``(이름 노드들, 바깥 범위에서 평가할 노드들)`` — 기본값·어노테이션."""
    names: list = []
    outer: list = []
    for c in params_node.named_children:
        t = c.type
        if t == "identifier":
            names.append(c)
        elif t in (
            "typed_parameter",
            "default_parameter",
            "typed_default_parameter",
            "list_splat_pattern",
            "dictionary_splat_pattern",
        ):
            n = c.child_by_field_name("name")
            if n is None:
                for ch in c.named_children:
                    if ch.type == "identifier":
                        n = ch
                        break
                    if ch.type in ("list_splat_pattern", "dictionary_splat_pattern"):
                        # `*args: int` — 이름은 splat 패턴 안에
                        n = next(
                            (g for g in ch.named_children if g.type == "identifier"),
                            None,
                        )
                        break
            if n is not None:
                names.append(n)
            for field_name in ("value", "type"):
                v = c.child_by_field_name(field_name)
                if v is not None:
                    outer.append(v)
            if t == "typed_parameter" and c.child_by_field_name("type") is None:
                for ch in c.named_children:
                    if ch.type == "type":
                        outer.append(ch)
        elif t == "tuple_pattern":
            _target_names(c, src, names)
    return names, outer


def walk_scopes(
    root,
    src: bytes,
    rel: str,
    defined_names: set,
    identifiers_out: set,
    language: str = "python",
):
    """``(scopes, bindings, refs)`` for one file — see the module docstring."""
    scopes: list[Scope] = [
        Scope(kind="module", start_line=1, end_line=root.end_point[0] + 1)
    ]
    bindings: list[Binding] = []
    refs: list[Ref] = []

    def bind(scope: int, node, kind: str, imod=None, iname=None, name=None):
        bindings.append(
            Binding(
                scope=scope,
                name=name if name is not None else text(node, src),
                kind=kind,
                line=node.start_point[0] + 1,
                import_module=imod,
                import_name=iname,
            )
        )

    def new_scope(kind: str, node, parent: int, def_node=None) -> int:
        scopes.append(
            Scope(
                kind=kind,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                parent=parent,
                def_line=def_node.start_point[0] + 1 if def_node is not None else None,
                def_name=(
                    text(def_node.child_by_field_name("name"), src)
                    if def_node is not None
                    else None
                ),
            )
        )
        return len(scopes) - 1

    def ref(node, name: str, kind: str, scope: int, obj: str | None = None):
        refs.append(
            Ref(
                name=name,
                kind=kind,
                file=rel,
                line=node.start_point[0] + 1,
                col=node.start_point[1],
                language=language,
                scope=scope,
                object=obj,
            )
        )

    def nearest_non_comprehension(scope: int) -> int:
        while (
            scopes[scope].kind == "comprehension" and scopes[scope].parent is not None
        ):
            scope = scopes[scope].parent
        return scope

    stack: list = [(c, 0) for c in reversed(root.children)]
    while stack:
        node, scope = stack.pop()
        nt = node.type
        push: list = []
        if nt in ("function_definition", "lambda"):
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                bind(scope, name_node, "def")
            fs = new_scope(
                "function", node, scope, node if name_node is not None else None
            )
            params = node.child_by_field_name("parameters")
            if params is not None:
                names, outer = _param_names(params, src)
                for n in names:
                    bind(fs, n, "param")
                push += [(o, scope) for o in outer]
            rt = node.child_by_field_name("return_type")
            if rt is not None:
                push.append((rt, scope))
            body = node.child_by_field_name("body")
            if body is not None:
                push.append((body, fs))
        elif nt == "class_definition":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                bind(scope, name_node, "class")
            cs = new_scope(
                "class", node, scope, node if name_node is not None else None
            )
            sup = node.child_by_field_name("superclasses")
            if sup is not None:
                push.append((sup, scope))
            body = node.child_by_field_name("body")
            if body is not None:
                push.append((body, cs))
        elif nt in _COMPREHENSIONS:
            cs = new_scope("comprehension", node, scope)
            first = True
            for c in node.named_children:
                if c.type == "for_in_clause":
                    left = c.child_by_field_name("left")
                    right = c.child_by_field_name("right")
                    if left is not None:
                        names: list = []
                        for e in _target_names(left, src, names):
                            push.append((e, cs))
                        for n in names:
                            bind(cs, n, "local")
                    if right is not None:
                        # 첫 iterable 은 바깥 범위에서 평가된다
                        push.append((right, scope if first else cs))
                    first = False
                else:
                    push.append((c, cs))
        elif nt in ("assignment", "augmented_assignment"):
            left = node.child_by_field_name("left")
            if left is not None:
                names = []
                for e in _target_names(left, src, names):
                    push.append((e, scope))
                for n in names:
                    bind(scope, n, "local")
            for field_name in ("right", "type"):
                v = node.child_by_field_name(field_name)
                if v is not None:
                    push.append((v, scope))
        elif nt == "named_expression":
            n = node.child_by_field_name("name")
            if n is not None:
                bind(nearest_non_comprehension(scope), n, "local")
            v = node.child_by_field_name("value")
            if v is not None:
                push.append((v, scope))
        elif nt == "for_statement":
            left = node.child_by_field_name("left")
            if left is not None:
                names = []
                for e in _target_names(left, src, names):
                    push.append((e, scope))
                for n in names:
                    bind(scope, n, "local")
            for c in node.children:
                # py-tree-sitter 는 접근마다 새 Node 를 만든다 — `is` 가 아니라 `==`
                if c != left and c.is_named:
                    push.append((c, scope))
        elif nt == "as_pattern":
            alias = node.child_by_field_name("alias")
            if alias is not None:
                names = []
                for e in _target_names(alias, src, names):
                    push.append((e, scope))
                for n in names:
                    bind(scope, n, "local")
            for c in node.named_children:
                if c != alias:
                    push.append((c, scope))
        elif nt == "keyword_pattern":
            # `case P(y=h)` — `y` 는 속성 이름, `h` 가 묶인다
            kids = node.named_children
            v = kids[-1] if kids else None
            if (
                v is not None
                and v.type == "dotted_name"
                and len(v.named_children) == 1
                and text(v, src) != "_"
            ):
                bind(scope, v, "local")
            elif v is not None and v.type != "identifier":
                push.append((v, scope))
        elif nt == "splat_pattern":
            for c in node.named_children:
                if c.type == "identifier" and text(c, src) != "_":
                    bind(scope, c, "local")
        elif nt == "case_pattern":
            kids = node.named_children
            if (
                len(kids) == 1
                and kids[0].type == "dotted_name"
                and len(kids[0].named_children) == 1
                and text(kids[0], src) != "_"
            ):
                bind(scope, kids[0], "local")
            else:
                push += [(c, scope) for c in kids]
        elif nt in ("global_statement", "nonlocal_statement"):
            kind = "global" if nt == "global_statement" else "nonlocal"
            for c in node.named_children:
                if c.type == "identifier":
                    bind(scope, c, kind)
        elif nt == "import_statement":
            for c in node.named_children:
                if c.type == "dotted_name":
                    first_part = c.named_children[0] if c.named_children else c
                    bind(scope, first_part, "import", imod=text(c, src))
                elif c.type == "aliased_import":
                    dn = c.child_by_field_name("name")
                    al = c.child_by_field_name("alias")
                    if dn is not None and al is not None:
                        bind(scope, al, "import", imod=text(dn, src))
        elif nt == "import_from_statement":
            mn = node.child_by_field_name("module_name")
            modtxt = text(mn, src) if mn is not None else ""
            for c in node.named_children:
                if c == mn:
                    continue
                if c.type == "dotted_name":
                    bind(scope, c, "import", imod=modtxt, iname=text(c, src))
                elif c.type == "aliased_import":
                    dn = c.child_by_field_name("name")
                    al = c.child_by_field_name("alias")
                    if dn is not None and al is not None:
                        bind(scope, al, "import", imod=modtxt, iname=text(dn, src))
                elif c.type == "wildcard_import":
                    bind(scope, c, "import", imod=modtxt, iname="*", name="*")
        elif nt == "call":
            fn = node.child_by_field_name("function")
            if fn is not None and fn.type == "identifier":
                ref(fn, text(fn, src), "call", scope)
            elif fn is not None and fn.type == "attribute":
                attr = fn.child_by_field_name("attribute")
                obj = fn.child_by_field_name("object")
                if attr is not None and attr.type == "identifier":
                    ref(attr, text(attr, src), "call", scope, _chain(obj, src))
                if obj is not None:
                    push.append((obj, scope))
            elif fn is not None:
                push.append((fn, scope))
            args = node.child_by_field_name("arguments")
            if args is not None:
                push.append((args, scope))
        elif nt == "attribute":
            obj = node.child_by_field_name("object")
            if obj is not None:
                push.append((obj, scope))
        elif nt == "keyword_argument":
            v = node.child_by_field_name("value")
            if v is not None:
                push.append((v, scope))
        elif nt == "identifier":
            name = text(node, src)
            if name in defined_names:
                ref(node, name, "name", scope)
        else:
            push = [(c, scope) for c in node.named_children]
        stack.extend(reversed(push))
    _note_identifiers(root, src, identifiers_out)
    return scopes, bindings, refs


_IDENT_QUERY = None


def _note_identifiers(root, src: bytes, out: set) -> None:
    """파일의 모든 식별자 — 증분 빌드의 "새 이름에 영향받는 파일" 판정 근거.
    구조 걷기는 묶이는 자리를 건너뛰므로 Query 로 따로 모은다(C 쪽에서 돈다)."""
    global _IDENT_QUERY
    from tree_sitter import Query, QueryCursor

    if _IDENT_QUERY is None:
        _IDENT_QUERY = Query(_lang_python(), "(identifier) @i")
    for nodes in QueryCursor(_IDENT_QUERY).captures(root).values():
        for n in nodes:
            out.add(text(n, src))


LANGUAGES["python"] = LangSpec(
    name="python",
    exts=(".py", ".pyi"),
    grammar_factory=_lang_python,
    walk_definitions=walk_definitions,
    walk_refs=None,
    preprocess=noop_preprocess,
    walk_scopes=walk_scopes,
)
