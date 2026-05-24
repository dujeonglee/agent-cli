# Originally ported from minish.ai/Agent-tools tsindex.py (Apache 2.0).
# See NOTICE for the list of modifications.
"""Python language walker for code_index.

Pass-1 (`walk_definitions`) emits Symbol records for functions, classes,
and module-/class-level assignments. UPPER_SNAKE module-level
assignments map to `kind='constant'`; everything else maps to
`kind='variable'`. Decorated definitions keep the decorator names in
`modifiers` (e.g. ``["staticmethod", "property"]``); `async def`
contributes the `"async"` modifier.

Pass-2 (`walk_refs`) emits `kind='call'` refs for callable sites
(`f(...)`, `obj.method(...)`) and `kind='name'` refs for bare
identifier mentions of names defined elsewhere in the indexed code
(callback passing, function-pointer-style usage). Skips identifiers
that ARE the definition site (function name, parameter name, LHS of
assignment) to avoid duplicating Pass-1 records.
"""

from __future__ import annotations

import re
from typing import Optional

from agent_cli.code_index.languages import LANGUAGES, LangSpec, noop_preprocess
from agent_cli.code_index.languages._shared import text
from agent_cli.code_index.schema import Ref, Symbol


def _lang_python():
    import tree_sitter_python
    from tree_sitter import Language

    return Language(tree_sitter_python.language())


def py_extract_function(
    node,
    src: bytes,
    rel: str,
    parent: Optional[str],
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


def py_extract_class(
    node,
    src: bytes,
    rel: str,
    parent: Optional[str],
    out: list,
    extra_modifiers: list[str],
):
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    cls_name = text(name_node, src)
    out.append(
        Symbol(
            name=cls_name,
            kind="type",
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
    inner_parent = (parent + "." + cls_name) if parent else cls_name
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


def py_extract_decorated(node, src: bytes, rel: str, parent: Optional[str], out: list):
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
    parent: Optional[str],
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


def walk_refs(
    root,
    src: bytes,
    rel: str,
    refs: list,
    defined_names: set,
    identifiers_out: set,
    language: str = "python",
):
    """Collect call sites + name mentions. Same ref kinds as the C walker."""
    stack = [root]
    while stack:
        node = stack.pop()
        nt = node.type
        if nt == "call":
            fn = node.child_by_field_name("function")
            if fn is not None:
                target_name = None
                if fn.type == "identifier":
                    target_name = text(fn, src)
                elif fn.type == "attribute":
                    attr = fn.child_by_field_name("attribute")
                    if attr is not None and attr.type == "identifier":
                        target_name = text(attr, src)
                if target_name is not None:
                    identifiers_out.add(target_name)
                    refs.append(
                        Ref(
                            name=target_name,
                            kind="call",
                            file=rel,
                            line=node.start_point[0] + 1,
                            col=node.start_point[1],
                            language=language,
                        )
                    )
        elif nt == "identifier":
            parent = node.parent
            skip = False
            if parent is not None:
                pt = parent.type
                if pt in ("function_definition", "class_definition"):
                    if parent.child_by_field_name("name") == node:
                        skip = True
                elif pt == "parameters":
                    skip = True
                elif pt in (
                    "typed_parameter",
                    "default_parameter",
                    "typed_default_parameter",
                ):
                    if (
                        parent.child_by_field_name("name") == node
                        or parent.children[0] == node
                    ):
                        skip = True
                elif pt == "assignment":
                    if parent.child_by_field_name("left") == node:
                        skip = True
            name = text(node, src)
            identifiers_out.add(name)
            if not skip and name in defined_names:
                refs.append(
                    Ref(
                        name=name,
                        kind="name",
                        file=rel,
                        line=node.start_point[0] + 1,
                        col=node.start_point[1],
                        language=language,
                    )
                )
        stack.extend(node.children)


LANGUAGES["python"] = LangSpec(
    name="python",
    exts=(".py", ".pyi"),
    grammar_factory=_lang_python,
    walk_definitions=walk_definitions,
    walk_refs=walk_refs,
    preprocess=noop_preprocess,
)
