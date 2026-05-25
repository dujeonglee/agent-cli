# Originally ported from minish.ai/Agent-tools tsindex.py (Apache 2.0).
# See NOTICE for the list of modifications.
"""Java language walker for code_index.

Pass-1 (`walk_definitions`) emits Symbol records for class/interface/enum
declarations and their nested members. Methods get `kind='function'`
with `parent` set to the enclosing class chain; fields get
`kind='constant'` when both `static` and `final` are present, otherwise
`kind='variable'`. Enum constants are collected into `enum_values`.

Pass-2 (`walk_refs`) emits `kind='call'` for method invocations and
`new` expressions (the constructor type), `kind='type'` for
`type_identifier` uses outside the definition position, and
`kind='name'` for plain identifiers that resolve to an indexed defined
name.
"""

from __future__ import annotations

from typing import Optional

from agent_cli.code_index.languages import LANGUAGES, LangSpec, noop_preprocess
from agent_cli.code_index.languages._shared import qualify, text
from agent_cli.code_index.schema import Ref, Symbol


def _lang_java():
    import tree_sitter_java
    from tree_sitter import Language

    return Language(tree_sitter_java.language())


def java_modifiers(node, src: bytes) -> list[str]:
    out: list[str] = []
    for c in node.children:
        if c.type == "modifiers":
            for m in c.children:
                if m.is_named or m.type in (
                    "public",
                    "private",
                    "protected",
                    "static",
                    "final",
                    "abstract",
                    "synchronized",
                    "native",
                    "default",
                    "transient",
                    "volatile",
                    "strictfp",
                ):
                    out.append(text(m, src))
            break
    return out


def java_params(plist, src: bytes) -> list[str]:
    names: list[str] = []
    if plist is None:
        return names
    for c in plist.named_children:
        if c.type in ("formal_parameter", "spread_parameter"):
            n = c.child_by_field_name("name")
            if n is not None:
                names.append(text(n, src))
    return names


def java_extract_method(
    node,
    src: bytes,
    rel: str,
    parent: Optional[str],
    out: list,
    is_constructor: bool = False,
):
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = text(name_node, src)
    mods = java_modifiers(node, src)
    body = node.child_by_field_name("body")
    if body is not None:
        sig = src[node.start_byte : body.start_byte].decode("utf-8", "replace")
    else:
        sig = text(node, src).split("\n", 1)[0]
    sig = " ".join(sig.split())
    rt = node.child_by_field_name("type")
    return_type = text(rt, src) if rt is not None else None
    params = node.child_by_field_name("parameters")
    out.append(
        Symbol(
            name=name,
            qualified_name=qualify(parent, name),
            kind="function",
            file=rel,
            line=node.start_point[0] + 1,
            col=node.start_point[1],
            end_line=node.end_point[0] + 1,
            is_definition=(body is not None),
            language="java",
            kind_raw="constructor_declaration"
            if is_constructor
            else "method_declaration",
            modifiers=mods or None,
            parent=parent,
            signature=sig,
            return_type=return_type,
            params=java_params(params, src) or None,
        )
    )


def java_extract_field(node, src: bytes, rel: str, parent: Optional[str], out: list):
    mods = java_modifiers(node, src)
    is_const = "static" in mods and "final" in mods
    type_node = node.child_by_field_name("type")
    type_text = text(type_node, src) if type_node is not None else ""
    for decl in node.children:
        if decl.type != "variable_declarator":
            continue
        n = decl.child_by_field_name("name")
        if n is None:
            continue
        name = text(n, src)
        out.append(
            Symbol(
                name=name,
                qualified_name=qualify(parent, name),
                kind="constant" if is_const else "variable",
                file=rel,
                line=decl.start_point[0] + 1,
                col=decl.start_point[1],
                end_line=decl.end_point[0] + 1,
                is_definition=True,
                language="java",
                kind_raw="field_declaration",
                modifiers=mods or None,
                parent=parent,
                signature=f"{type_text} {text(decl, src)}".strip(),
            )
        )


def java_extract_class(
    node,
    src: bytes,
    rel: str,
    parent: Optional[str],
    out: list,
    kind_raw: str = "class_declaration",
):
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    cls_name = text(name_node, src)
    mods = java_modifiers(node, src)
    out.append(
        Symbol(
            name=cls_name,
            qualified_name=qualify(parent, cls_name),
            kind="type",
            file=rel,
            line=node.start_point[0] + 1,
            col=node.start_point[1],
            end_line=node.end_point[0] + 1,
            is_definition=True,
            language="java",
            kind_raw=kind_raw,
            modifiers=mods or None,
            parent=parent,
        )
    )
    body = node.child_by_field_name("body")
    if body is None:
        return
    inner = qualify(parent, cls_name)
    for stmt in body.children:
        if stmt.type == "method_declaration":
            java_extract_method(stmt, src, rel, inner, out)
        elif stmt.type == "constructor_declaration":
            java_extract_method(stmt, src, rel, inner, out, is_constructor=True)
        elif stmt.type == "field_declaration":
            java_extract_field(stmt, src, rel, inner, out)
        elif stmt.type == "class_declaration":
            java_extract_class(stmt, src, rel, inner, out, "class_declaration")
        elif stmt.type == "interface_declaration":
            java_extract_class(stmt, src, rel, inner, out, "interface_declaration")
        elif stmt.type == "enum_declaration":
            java_extract_enum(stmt, src, rel, inner, out)


def java_extract_enum(node, src: bytes, rel: str, parent: Optional[str], out: list):
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = text(name_node, src)
    mods = java_modifiers(node, src)
    body = node.child_by_field_name("body")
    enum_values: list[str] = []
    if body is not None:
        for c in body.children:
            if c.type == "enum_constant":
                n = c.child_by_field_name("name")
                if n is not None:
                    enum_values.append(text(n, src))
    out.append(
        Symbol(
            name=name,
            qualified_name=qualify(parent, name),
            kind="type",
            file=rel,
            line=node.start_point[0] + 1,
            col=node.start_point[1],
            end_line=node.end_point[0] + 1,
            is_definition=True,
            language="java",
            kind_raw="enum_declaration",
            modifiers=mods or None,
            parent=parent,
            enum_values=enum_values or None,
        )
    )


def walk_definitions(root, src: bytes, rel: str, syms: list):
    for node in root.children:
        if node.type == "class_declaration":
            java_extract_class(node, src, rel, None, syms, "class_declaration")
        elif node.type == "interface_declaration":
            java_extract_class(node, src, rel, None, syms, "interface_declaration")
        elif node.type == "enum_declaration":
            java_extract_enum(node, src, rel, None, syms)


def walk_refs(
    root,
    src: bytes,
    rel: str,
    refs: list,
    defined_names: set,
    identifiers_out: set,
    language: str = "java",
):
    stack = [root]
    while stack:
        node = stack.pop()
        nt = node.type
        if nt == "method_invocation":
            n = node.child_by_field_name("name")
            if n is not None:
                name = text(n, src)
                identifiers_out.add(name)
                refs.append(
                    Ref(
                        name=name,
                        kind="call",
                        file=rel,
                        line=node.start_point[0] + 1,
                        col=node.start_point[1],
                        language=language,
                    )
                )
        elif nt == "object_creation_expression":
            # `new Foo(...)` — capture as call to Foo (the type's constructor)
            t = node.child_by_field_name("type")
            if t is not None and t.type == "type_identifier":
                name = text(t, src)
                identifiers_out.add(name)
                refs.append(
                    Ref(
                        name=name,
                        kind="call",
                        file=rel,
                        line=node.start_point[0] + 1,
                        col=node.start_point[1],
                        language=language,
                    )
                )
        elif nt == "type_identifier":
            name = text(node, src)
            identifiers_out.add(name)
            parent = node.parent
            is_def = False
            if parent is not None and parent.type in (
                "class_declaration",
                "interface_declaration",
                "enum_declaration",
            ):
                nf = parent.child_by_field_name("name")
                if nf == node:
                    is_def = True
            if not is_def:
                refs.append(
                    Ref(
                        name=name,
                        kind="type",
                        file=rel,
                        line=node.start_point[0] + 1,
                        col=node.start_point[1],
                        language=language,
                    )
                )
        elif nt == "identifier":
            name = text(node, src)
            identifiers_out.add(name)
            parent = node.parent
            skip = False
            if parent is not None:
                pt = parent.type
                if pt in ("method_declaration", "constructor_declaration"):
                    if parent.child_by_field_name("name") == node:
                        skip = True
                elif pt == "variable_declarator":
                    if parent.child_by_field_name("name") == node:
                        skip = True
                elif pt == "formal_parameter":
                    if parent.child_by_field_name("name") == node:
                        skip = True
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


LANGUAGES["java"] = LangSpec(
    name="java",
    exts=(".java",),
    grammar_factory=_lang_java,
    walk_definitions=walk_definitions,
    walk_refs=walk_refs,
    preprocess=noop_preprocess,
)
