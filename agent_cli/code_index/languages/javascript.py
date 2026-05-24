# Originally ported from minish.ai/Agent-tools tsindex.py (Apache 2.0).
# See NOTICE for the list of modifications.
"""JavaScript language walker for code_index.

Pass-1 (`walk_definitions`) emits Symbol records for `function`/`class`
declarations, `const`/`let`/`var` lexical declarations (with arrow/
function expressions promoted to `kind='function'`), and any of those
forms wrapped in `export`. Class bodies parent their `method_definition`
and `field_definition` children. `const UPPER_SNAKE = …` becomes
`kind='constant'`; other `const` bindings also map to `kind='constant'`
(JS convention); `let`/`var` map to `kind='variable'`.

Pass-2 (`walk_refs`) emits `kind='call'` for call expressions (including
member-expression method calls) and `new` expressions (the constructor
identifier), and `kind='name'` for plain identifiers that resolve to an
indexed defined name and aren't themselves the definition site.

This module also exports the `js_*` helpers reused verbatim by the
TypeScript walker — the two languages share the same tree-sitter node
shape and the only delta is the `language` string in emitted records.
"""

from __future__ import annotations

from typing import Optional

from agent_cli.code_index.languages import LANGUAGES, LangSpec, noop_preprocess
from agent_cli.code_index.languages._shared import text
from agent_cli.code_index.schema import Ref, Symbol


def _lang_javascript():
    import tree_sitter_javascript
    from tree_sitter import Language

    return Language(tree_sitter_javascript.language())


def js_params(plist, src: bytes) -> list[str]:
    names: list[str] = []
    if plist is None:
        return names
    for c in plist.named_children:
        if c.type == "identifier":
            names.append(text(c, src))
        elif c.type in ("required_parameter", "optional_parameter"):  # TS
            n = c.child_by_field_name("pattern")
            if n is None:
                for ch in c.children:
                    if ch.type == "identifier":
                        n = ch
                        break
            if n is not None:
                names.append(text(n, src))
        elif c.type in ("assignment_pattern",):
            lhs = c.child_by_field_name("left")
            if lhs is not None and lhs.type == "identifier":
                names.append(text(lhs, src))
        elif c.type == "rest_pattern":
            for ch in c.children:
                if ch.type == "identifier":
                    names.append(text(ch, src))
    return names


def js_extract_function_decl(
    node,
    src: bytes,
    rel: str,
    parent: Optional[str],
    out: list,
    lang: str,
    extra_mods: list[str] = (),
):
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = text(name_node, src)
    mods = list(extra_mods)
    for ch in node.children:
        if ch.type == "async":
            mods.append("async")
    body = node.child_by_field_name("body")
    if body is not None:
        sig = src[node.start_byte : body.start_byte].decode("utf-8", "replace")
    else:
        sig = text(node, src).split("\n", 1)[0]
    sig = " ".join(sig.split())
    params = node.child_by_field_name("parameters")
    out.append(
        Symbol(
            name=name,
            kind="function",
            file=rel,
            line=node.start_point[0] + 1,
            col=node.start_point[1],
            end_line=node.end_point[0] + 1,
            is_definition=True,
            language=lang,
            kind_raw=node.type,
            modifiers=mods or None,
            parent=parent,
            signature=sig,
            params=js_params(params, src) or None,
        )
    )


def js_extract_method(
    node, src: bytes, rel: str, parent: Optional[str], out: list, lang: str
):
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = text(name_node, src)
    mods: list[str] = []
    # static/async/get/set modifiers come as bare tokens
    for c in node.children:
        if c.type in ("static", "async", "get", "set"):
            mods.append(c.type)
    body = node.child_by_field_name("body")
    if body is not None:
        sig = src[node.start_byte : body.start_byte].decode("utf-8", "replace")
    else:
        sig = text(node, src).split("\n", 1)[0]
    sig = " ".join(sig.split())
    params = node.child_by_field_name("parameters")
    out.append(
        Symbol(
            name=name,
            kind="function",
            file=rel,
            line=node.start_point[0] + 1,
            col=node.start_point[1],
            end_line=node.end_point[0] + 1,
            is_definition=True,
            language=lang,
            kind_raw="method_definition",
            modifiers=mods or None,
            parent=parent,
            signature=sig,
            params=js_params(params, src) or None,
        )
    )


def js_extract_class(
    node, src: bytes, rel: str, parent: Optional[str], out: list, lang: str
):
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    cls = text(name_node, src)
    out.append(
        Symbol(
            name=cls,
            kind="type",
            file=rel,
            line=node.start_point[0] + 1,
            col=node.start_point[1],
            end_line=node.end_point[0] + 1,
            is_definition=True,
            language=lang,
            kind_raw=node.type,
            parent=parent,
        )
    )
    body = node.child_by_field_name("body")
    if body is None:
        return
    inner = (parent + "." + cls) if parent else cls
    for stmt in body.children:
        if stmt.type == "method_definition":
            js_extract_method(stmt, src, rel, inner, out, lang)
        elif stmt.type == "field_definition":
            n = stmt.child_by_field_name("property") or stmt.child_by_field_name("name")
            if n is None:
                for c in stmt.children:
                    if c.type == "property_identifier":
                        n = c
                        break
            if n is None:
                continue
            mods: list[str] = []
            for c in stmt.children:
                if c.type == "static":
                    mods.append("static")
            out.append(
                Symbol(
                    name=text(n, src),
                    kind="variable",
                    file=rel,
                    line=stmt.start_point[0] + 1,
                    col=stmt.start_point[1],
                    end_line=stmt.end_point[0] + 1,
                    is_definition=True,
                    language=lang,
                    kind_raw="field_definition",
                    modifiers=mods or None,
                    parent=inner,
                )
            )


def js_extract_lexical(node, src: bytes, rel: str, out: list, lang: str):
    """`const x = ...`, `let x = ...`, `var x = ...` at module scope."""
    # first child indicates the kind: const / let / var
    is_const_kw = False
    for c in node.children:
        if c.type == "const":
            is_const_kw = True
            break
        elif c.type in ("let", "var"):
            break
    for c in node.named_children:
        if c.type != "variable_declarator":
            continue
        n = c.child_by_field_name("name")
        v = c.child_by_field_name("value")
        if n is None or n.type != "identifier":
            continue
        name = text(n, src)
        # Arrow / function expression assigned to const → treat as a function
        if v is not None and v.type in (
            "arrow_function",
            "function_expression",
            "function",
        ):
            js_extract_function_expr_into(c, n, v, src, rel, out, lang, is_const_kw)
            continue
        out.append(
            Symbol(
                name=name,
                # `const` → compile-time constant; `let`/`var` → variable.
                # Uppercase-naming convention is not promoted to `constant`
                # for let/var (matches upstream tsindex behaviour).
                kind="constant" if is_const_kw else "variable",
                file=rel,
                line=c.start_point[0] + 1,
                col=c.start_point[1],
                end_line=c.end_point[0] + 1,
                is_definition=True,
                language=lang,
                kind_raw="lexical_declaration",
                signature=" ".join(text(c, src).split())[:200],
            )
        )


def js_extract_function_expr_into(
    declarator, name_node, fn_node, src, rel, out, lang, is_const
):
    name = text(name_node, src)
    mods: list[str] = []
    for ch in fn_node.children:
        if ch.type == "async":
            mods.append("async")
    body = fn_node.child_by_field_name("body")
    if body is not None:
        sig = src[declarator.start_byte : body.start_byte].decode("utf-8", "replace")
    else:
        sig = text(declarator, src).split("\n", 1)[0]
    sig = " ".join(sig.split())
    params = fn_node.child_by_field_name("parameters")
    out.append(
        Symbol(
            name=name,
            kind="function",
            file=rel,
            line=declarator.start_point[0] + 1,
            col=declarator.start_point[1],
            end_line=declarator.end_point[0] + 1,
            is_definition=True,
            language=lang,
            kind_raw=fn_node.type,
            modifiers=mods or None,
            signature=sig,
            params=js_params(params, src) or None,
        )
    )


def walk_definitions(root, src: bytes, rel: str, syms: list):
    lang = "javascript"
    for node in root.children:
        t = node.type
        if t == "function_declaration":
            js_extract_function_decl(node, src, rel, None, syms, lang)
        elif t == "generator_function_declaration":
            # `function* gen()` — same shape as function_declaration but
            # produces a generator. Modifier marks the generator-ness so
            # downstream tools can distinguish if needed.
            js_extract_function_decl(node, src, rel, None, syms, lang, ["generator"])
        elif t == "class_declaration":
            js_extract_class(node, src, rel, None, syms, lang)
        elif t == "lexical_declaration":
            js_extract_lexical(node, src, rel, syms, lang)
        elif t == "interface_declaration":  # TS
            js_extract_class(node, src, rel, None, syms, lang)
        elif t == "type_alias_declaration":  # TS
            nm = node.child_by_field_name("name")
            if nm is not None:
                syms.append(
                    Symbol(
                        name=text(nm, src),
                        kind="type",
                        file=rel,
                        line=node.start_point[0] + 1,
                        col=node.start_point[1],
                        end_line=node.end_point[0] + 1,
                        is_definition=True,
                        language=lang,
                        kind_raw="type_alias_declaration",
                    )
                )
        elif t == "enum_declaration":  # TS
            nm = node.child_by_field_name("name")
            if nm is not None:
                syms.append(
                    Symbol(
                        name=text(nm, src),
                        kind="type",
                        file=rel,
                        line=node.start_point[0] + 1,
                        col=node.start_point[1],
                        end_line=node.end_point[0] + 1,
                        is_definition=True,
                        language=lang,
                        kind_raw="enum_declaration",
                    )
                )
        elif t == "export_statement":
            # Recurse into exported declarations.
            for c in node.children:
                if c.type == "function_declaration":
                    js_extract_function_decl(
                        c, src, rel, None, syms, lang, ["exported"]
                    )
                elif c.type == "generator_function_declaration":
                    js_extract_function_decl(
                        c, src, rel, None, syms, lang, ["exported", "generator"]
                    )
                elif c.type == "class_declaration":
                    js_extract_class(c, src, rel, None, syms, lang)
                elif c.type == "lexical_declaration":
                    js_extract_lexical(c, src, rel, syms, lang)
                elif c.type == "interface_declaration":
                    js_extract_class(c, src, rel, None, syms, lang)
                elif c.type == "type_alias_declaration":
                    nm = c.child_by_field_name("name")
                    if nm is not None:
                        syms.append(
                            Symbol(
                                name=text(nm, src),
                                kind="type",
                                file=rel,
                                line=c.start_point[0] + 1,
                                col=c.start_point[1],
                                end_line=c.end_point[0] + 1,
                                is_definition=True,
                                language=lang,
                                kind_raw="type_alias_declaration",
                                modifiers=["exported"],
                            )
                        )
                elif c.type == "enum_declaration":
                    nm = c.child_by_field_name("name")
                    if nm is not None:
                        syms.append(
                            Symbol(
                                name=text(nm, src),
                                kind="type",
                                file=rel,
                                line=c.start_point[0] + 1,
                                col=c.start_point[1],
                                end_line=c.end_point[0] + 1,
                                is_definition=True,
                                language=lang,
                                kind_raw="enum_declaration",
                                modifiers=["exported"],
                            )
                        )


def walk_refs(
    root,
    src: bytes,
    rel: str,
    refs: list,
    defined_names: set,
    identifiers_out: set,
    language: str = "javascript",
):
    stack = [root]
    while stack:
        node = stack.pop()
        nt = node.type
        if nt == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is not None:
                target = None
                if fn.type == "identifier":
                    target = text(fn, src)
                elif fn.type == "member_expression":
                    prop = fn.child_by_field_name("property")
                    if prop is not None and prop.type in (
                        "property_identifier",
                        "identifier",
                    ):
                        target = text(prop, src)
                if target is not None:
                    identifiers_out.add(target)
                    refs.append(
                        Ref(
                            name=target,
                            kind="call",
                            file=rel,
                            line=node.start_point[0] + 1,
                            col=node.start_point[1],
                            language=language,
                        )
                    )
        elif nt == "new_expression":
            cn = node.child_by_field_name("constructor")
            if cn is not None and cn.type == "identifier":
                name = text(cn, src)
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
        elif nt == "identifier":
            name = text(node, src)
            identifiers_out.add(name)
            parent = node.parent
            skip = False
            if parent is not None:
                pt = parent.type
                if (
                    pt
                    in (
                        "function_declaration",
                        "class_declaration",
                        "method_definition",
                    )
                    and parent.child_by_field_name("name") == node
                ):
                    skip = True
                elif (
                    pt == "variable_declarator"
                    and parent.child_by_field_name("name") == node
                ):
                    skip = True
                elif pt == "formal_parameters":
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


LANGUAGES["javascript"] = LangSpec(
    name="javascript",
    exts=(".js", ".jsx", ".mjs", ".cjs"),
    grammar_factory=_lang_javascript,
    walk_definitions=walk_definitions,
    walk_refs=walk_refs,
    preprocess=noop_preprocess,
)
