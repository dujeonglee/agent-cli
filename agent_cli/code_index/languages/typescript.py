# Originally ported from minish.ai/Agent-tools tsindex.py (Apache 2.0).
# See NOTICE for the list of modifications.
"""TypeScript language walker for code_index.

TypeScript shares its tree-sitter node shape with JavaScript (the
upstream `js_walk_definitions_for` / `js_walk_refs_for` factories
parametrised the language string), so this module re-uses the `js_*`
extraction helpers from `javascript.py` and provides
`walk_definitions` / `walk_refs` wrappers that emit
`language='typescript'` on every Symbol and Ref record.
"""

from __future__ import annotations

from agent_cli.code_index.languages import LANGUAGES, LangSpec, noop_preprocess
from agent_cli.code_index.languages._shared import text
from agent_cli.code_index.languages.javascript import (
    js_extract_class,
    js_extract_function_decl,
    js_extract_lexical,
)
from agent_cli.code_index.schema import Ref, Symbol


def _lang_typescript():
    import tree_sitter_typescript
    from tree_sitter import Language

    return Language(tree_sitter_typescript.language_typescript())


def walk_definitions(root, src: bytes, rel: str, syms: list):
    lang = "typescript"
    for node in root.children:
        t = node.type
        if t == "function_declaration":
            js_extract_function_decl(node, src, rel, None, syms, lang)
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
    language: str = "typescript",
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


LANGUAGES["typescript"] = LangSpec(
    name="typescript",
    exts=(".ts", ".tsx"),
    grammar_factory=_lang_typescript,
    walk_definitions=walk_definitions,
    walk_refs=walk_refs,
    preprocess=noop_preprocess,
)
