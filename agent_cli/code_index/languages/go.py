# Originally ported from minish.ai/Agent-tools tsindex.py (Apache 2.0).
# See NOTICE for the list of modifications.
"""Go language walker for code_index.

Pass-1 emits Symbol records for `func`/`method`/`type`/`const`/`var`
declarations. Receiver type names propagate into `parent` for methods
so the same method name on different receivers stays distinguishable.
Uppercase-first-letter symbols pick up an `"exported"` modifier (Go's
visibility convention).

Pass-2 emits `kind='call'` for both bare calls and selector-expression
calls (`obj.Method()` → the rightmost field), `kind='type'` for
type_identifier mentions outside the definition position, and
`kind='name'` for plain identifiers that resolve to a defined name.
"""

from __future__ import annotations

from agent_cli.code_index.languages import LANGUAGES, LangSpec, noop_preprocess
from agent_cli.code_index.languages._shared import text
from agent_cli.code_index.schema import Ref, Symbol


def _lang_go():
    import tree_sitter_go
    from tree_sitter import Language

    return Language(tree_sitter_go.language())


def go_extract_param_names(plist, src: bytes) -> list[str]:
    names: list[str] = []
    if plist is None:
        return names
    for c in plist.named_children:
        if c.type != "parameter_declaration":
            continue
        for ch in c.named_children:
            if ch.type == "identifier":
                names.append(text(ch, src))
    return names


def go_extract_function(node, src: bytes, rel: str, out: list, is_method: bool = False):
    if is_method:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            for c in node.children:
                if c.type == "field_identifier":
                    name_node = c
                    break
    else:
        name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = text(name_node, src)
    # Receiver of methods → parent (the receiver type name)
    parent = None
    if is_method:
        recv = node.child_by_field_name("receiver")
        if recv is not None:
            # parameter_list with one parameter_declaration; find its type_identifier
            for c in recv.named_children:
                if c.type == "parameter_declaration":
                    for ch in c.named_children:
                        if ch.type == "type_identifier":
                            parent = text(ch, src)
                            break
                        if ch.type == "pointer_type":
                            for x in ch.named_children:
                                if x.type == "type_identifier":
                                    parent = text(x, src)
                                    break
                            break
    params = node.child_by_field_name("parameters")
    return_type = None
    rt = node.child_by_field_name("result")
    if rt is not None:
        return_type = text(rt, src)
    body = node.child_by_field_name("body")
    if body is not None:
        sig = src[node.start_byte : body.start_byte].decode("utf-8", "replace")
        sig = " ".join(sig.split())
    else:
        sig = text(node, src).split("\n", 1)[0]
    # Go exported (uppercase first letter) → "exported" modifier
    mods = ["exported"] if name and name[0].isupper() else []
    out.append(
        Symbol(
            name=name,
            kind="function",
            file=rel,
            line=node.start_point[0] + 1,
            col=node.start_point[1],
            end_line=node.end_point[0] + 1,
            is_definition=True,
            language="go",
            kind_raw=node.type,
            modifiers=mods or None,
            parent=parent,
            signature=sig,
            return_type=return_type,
            params=go_extract_param_names(params, src) or None,
        )
    )


def go_extract_type(node, src: bytes, rel: str, out: list):
    """type_declaration may contain multiple type_specs."""
    for spec in node.children:
        if spec.type != "type_spec":
            continue
        nm = spec.child_by_field_name("name")
        if nm is None:
            for c in spec.children:
                if c.type == "type_identifier":
                    nm = c
                    break
        if nm is None:
            continue
        name = text(nm, src)
        kind_raw = "type"
        for c in spec.children:
            if c.type in ("struct_type", "interface_type"):
                kind_raw = c.type.replace("_type", "")
        mods = ["exported"] if name and name[0].isupper() else []
        out.append(
            Symbol(
                name=name,
                kind="type",
                file=rel,
                line=spec.start_point[0] + 1,
                col=spec.start_point[1],
                end_line=spec.end_point[0] + 1,
                is_definition=True,
                language="go",
                kind_raw=kind_raw,
                modifiers=mods or None,
            )
        )


def go_extract_const_or_var(node, src: bytes, rel: str, out: list, is_const: bool):
    spec_type = "const_spec" if is_const else "var_spec"
    out_kind = "constant" if is_const else "variable"
    for spec in node.children:
        if spec.type != spec_type:
            continue
        for c in spec.named_children:
            if c.type != "identifier":
                continue
            name = text(c, src)
            mods = ["exported"] if name and name[0].isupper() else []
            out.append(
                Symbol(
                    name=name,
                    kind=out_kind,
                    file=rel,
                    line=spec.start_point[0] + 1,
                    col=spec.start_point[1],
                    end_line=spec.end_point[0] + 1,
                    is_definition=True,
                    language="go",
                    kind_raw=spec_type,
                    modifiers=mods or None,
                    signature=" ".join(text(spec, src).split())[:200],
                )
            )


def walk_definitions(root, src: bytes, rel: str, syms: list):
    for node in root.children:
        if node.type == "function_declaration":
            go_extract_function(node, src, rel, syms, is_method=False)
        elif node.type == "method_declaration":
            go_extract_function(node, src, rel, syms, is_method=True)
        elif node.type == "type_declaration":
            go_extract_type(node, src, rel, syms)
        elif node.type == "const_declaration":
            go_extract_const_or_var(node, src, rel, syms, is_const=True)
        elif node.type == "var_declaration":
            go_extract_const_or_var(node, src, rel, syms, is_const=False)


def walk_refs(
    root,
    src: bytes,
    rel: str,
    refs: list,
    defined_names: set,
    identifiers_out: set,
    language: str = "go",
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
                elif fn.type == "selector_expression":
                    # `pkg.Func()` or `obj.Method()` → take rightmost field
                    field = fn.child_by_field_name("field")
                    if field is not None:
                        target = text(field, src)
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
        elif nt == "type_identifier":
            name = text(node, src)
            identifiers_out.add(name)
            parent = node.parent
            is_def = (
                parent is not None
                and parent.type == "type_spec"
                and parent.child_by_field_name("name") == node
            )
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
            # Skip identifiers that are themselves definition names
            parent = node.parent
            skip = False
            if parent is not None:
                pt = parent.type
                if (
                    pt == "function_declaration"
                    and parent.child_by_field_name("name") == node
                ):
                    skip = True
                elif pt in ("const_spec", "var_spec"):
                    # only skip if it's a name (first identifier child); refs in
                    # initialiser expressions are children further along
                    skip = (
                        node in parent.named_children
                        and parent.named_children[0] == node
                        and not any(
                            c.type == "expression_list" and node in c.named_children
                            for c in parent.children
                        )
                    )
                elif pt == "parameter_declaration":
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


LANGUAGES["go"] = LangSpec(
    name="go",
    exts=(".go",),
    grammar_factory=_lang_go,
    walk_definitions=walk_definitions,
    walk_refs=walk_refs,
    preprocess=noop_preprocess,
)
