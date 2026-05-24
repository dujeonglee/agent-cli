# Originally ported from minish.ai/Agent-tools tsindex.py (Apache 2.0).
# See NOTICE for the list of modifications.
"""Rust language walker for code_index.

Pass-1 (`walk_definitions`) emits Symbol records for `fn`, `struct`,
`enum`, `trait`, `type`, `union`, `const`, `static`, `impl` method, and
`macro_rules!` definitions. Visibility modifiers (`pub`, `pub(crate)`)
and function modifiers (`async`, `unsafe`, `const`) carry through into
`modifiers`. `impl` blocks parent their methods under the impl-type name.

Pass-2 (`walk_refs`) emits `kind='call'` refs for call expressions
(including selector / scoped function paths) and macro invocations,
`kind='type'` for `type_identifier` uses outside the definition position,
and `kind='name'` for plain identifiers that resolve to an indexed
defined name.
"""

from __future__ import annotations

from typing import Optional

from agent_cli.code_index.languages import LANGUAGES, LangSpec, noop_preprocess
from agent_cli.code_index.languages._shared import text
from agent_cli.code_index.schema import Ref, Symbol


def _lang_rust():
    import tree_sitter_rust
    from tree_sitter import Language

    return Language(tree_sitter_rust.language())


def rs_visibility(node, src: bytes) -> Optional[str]:
    for c in node.children:
        if c.type == "visibility_modifier":
            return text(c, src)
    return None


def rs_params(plist, src: bytes) -> list[str]:
    names: list[str] = []
    if plist is None:
        return names
    for c in plist.named_children:
        if c.type == "self_parameter":
            names.append("self")
        elif c.type == "parameter":
            # `pattern: type` or `mut name: type`
            pat = c.child_by_field_name("pattern")
            if pat is None:
                continue
            if pat.type == "identifier":
                names.append(text(pat, src))
            else:
                # find first identifier
                stack = [pat]
                while stack:
                    n = stack.pop()
                    if n.type == "identifier":
                        names.append(text(n, src))
                        break
                    stack.extend(n.children)
    return names


def rs_extract_function(node, src: bytes, rel: str, parent: Optional[str], out: list):
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = text(name_node, src)
    vis = rs_visibility(node, src)
    mods: list[str] = []
    if vis:
        mods.append(vis)  # "pub", "pub(crate)", etc.
    for c in node.children:
        if c.type == "function_modifiers":
            # async / unsafe / extern etc.
            for m in c.children:
                if m.is_named or m.type in ("async", "unsafe", "const"):
                    mods.append(text(m, src))
    body = node.child_by_field_name("body")
    if body is not None:
        sig = src[node.start_byte : body.start_byte].decode("utf-8", "replace")
    else:
        sig = text(node, src)
    sig = " ".join(sig.split())
    return_type = None
    rt = node.child_by_field_name("return_type")
    if rt is not None:
        return_type = text(rt, src)
    params = node.child_by_field_name("parameters")
    out.append(
        Symbol(
            name=name,
            kind="function",
            file=rel,
            line=node.start_point[0] + 1,
            col=node.start_point[1],
            end_line=node.end_point[0] + 1,
            is_definition=(node.type == "function_item"),
            language="rust",
            kind_raw=node.type,
            modifiers=mods or None,
            parent=parent,
            signature=sig,
            return_type=return_type,
            params=rs_params(params, src) or None,
        )
    )


def rs_extract_type(node, src: bytes, rel: str, out: list):
    name_node = node.child_by_field_name("name")
    if name_node is None:
        for c in node.children:
            if c.type == "type_identifier":
                name_node = c
                break
    if name_node is None:
        return
    vis = rs_visibility(node, src)
    mods = [vis] if vis else None
    raw_map = {
        "struct_item": "struct",
        "enum_item": "enum",
        "trait_item": "trait",
        "type_item": "type_alias",
        "union_item": "union",
    }
    enum_values = None
    if node.type == "enum_item":
        body = node.child_by_field_name("body")
        if body is None:
            for c in node.children:
                if c.type == "enum_variant_list":
                    body = c
                    break
        if body is not None:
            enum_values = []
            for v in body.named_children:
                if v.type == "enum_variant":
                    n = v.child_by_field_name("name")
                    if n is None:
                        for ch in v.children:
                            if ch.type == "identifier":
                                n = ch
                                break
                    if n is not None:
                        enum_values.append(text(n, src))
    out.append(
        Symbol(
            name=text(name_node, src),
            kind="type",
            file=rel,
            line=node.start_point[0] + 1,
            col=node.start_point[1],
            end_line=node.end_point[0] + 1,
            is_definition=True,
            language="rust",
            kind_raw=raw_map.get(node.type, node.type),
            modifiers=mods,
            enum_values=enum_values,
        )
    )
    # For traits, descend into the body and emit each method signature
    # (function_signature_item) or default method (function_item) with
    # `parent` set to the trait name. Without this, a trait's API surface
    # — its primary point of being — is invisible to the index.
    if node.type == "trait_item":
        trait_name = text(name_node, src)
        for c in node.children:
            if c.type == "declaration_list":
                for child in c.children:
                    if child.type in ("function_item", "function_signature_item"):
                        rs_extract_function(child, src, rel, trait_name, out)
                break


def rs_extract_const_or_static(node, src: bytes, rel: str, out: list):
    name_node = None
    for c in node.children:
        if c.type == "identifier":
            name_node = c
            break
    if name_node is None:
        return
    vis = rs_visibility(node, src)
    mods = [vis] if vis else None
    is_const = node.type == "const_item"
    out.append(
        Symbol(
            name=text(name_node, src),
            kind="constant" if is_const else "variable",
            file=rel,
            line=node.start_point[0] + 1,
            col=node.start_point[1],
            end_line=node.end_point[0] + 1,
            is_definition=True,
            language="rust",
            kind_raw=node.type,
            modifiers=mods,
            signature=" ".join(text(node, src).split())[:200],
        )
    )


def rs_extract_impl(node, src: bytes, rel: str, out: list):
    """impl blocks: methods inside get parent = the impl's type name."""
    # impl_item children: optional `impl`, optional trait type_identifier (with `for`),
    # required type_identifier (the type), declaration_list
    type_names = [text(c, src) for c in node.children if c.type == "type_identifier"]
    impl_type = type_names[-1] if type_names else None
    decls = None
    for c in node.children:
        if c.type == "declaration_list":
            decls = c
            break
    if decls is None:
        return
    for child in decls.children:
        if child.type in ("function_item", "function_signature_item"):
            rs_extract_function(child, src, rel, impl_type, out)


def rs_extract_macro(node, src: bytes, rel: str, out: list):
    """macro_rules! foo { ... } — treat as a callable (kind=function) since
    macro invocations look like calls (`foo!(args)`)."""
    name_node = None
    for c in node.children:
        if c.type == "identifier":
            name_node = c
            break
    if name_node is None:
        return
    out.append(
        Symbol(
            name=text(name_node, src),
            kind="function",
            file=rel,
            line=node.start_point[0] + 1,
            col=node.start_point[1],
            end_line=node.end_point[0] + 1,
            is_definition=True,
            language="rust",
            kind_raw="macro_definition",
            modifiers=["macro_rules"],
        )
    )


def walk_definitions(root, src: bytes, rel: str, syms: list):
    stack = list(root.children)
    while stack:
        node = stack.pop()
        nt = node.type
        if nt == "function_item":
            rs_extract_function(node, src, rel, None, syms)
        elif nt == "function_signature_item":
            # bare trait method signature (no body) — top-level form is rare
            rs_extract_function(node, src, rel, None, syms)
        elif nt in (
            "struct_item",
            "enum_item",
            "trait_item",
            "type_item",
            "union_item",
        ):
            rs_extract_type(node, src, rel, syms)
        elif nt in ("const_item", "static_item"):
            rs_extract_const_or_static(node, src, rel, syms)
        elif nt == "impl_item":
            rs_extract_impl(node, src, rel, syms)
        elif nt == "macro_definition":
            rs_extract_macro(node, src, rel, syms)
        elif nt == "mod_item":
            # descend into modules
            body = None
            for c in node.children:
                if c.type == "declaration_list":
                    body = c
                    break
            if body is not None:
                stack.extend(body.children)


def walk_refs(
    root,
    src: bytes,
    rel: str,
    refs: list,
    defined_names: set,
    identifiers_out: set,
    language: str = "rust",
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
                elif fn.type == "field_expression":
                    f = fn.child_by_field_name("field")
                    if f is not None:
                        target = text(f, src)
                elif fn.type == "scoped_identifier":
                    n = fn.child_by_field_name("name")
                    if n is not None:
                        target = text(n, src)
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
        elif nt == "macro_invocation":
            # `foo!(args)` — capture as call to foo
            mn = node.child_by_field_name("macro")
            if mn is not None:
                if mn.type == "identifier":
                    name = text(mn, src)
                elif mn.type == "scoped_identifier":
                    n = mn.child_by_field_name("name")
                    name = text(n, src) if n else None
                else:
                    name = None
                if name:
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
                "struct_item",
                "enum_item",
                "trait_item",
                "type_item",
                "union_item",
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
                if (
                    pt in ("function_item", "function_signature_item")
                    and parent.child_by_field_name("name") == node
                ):
                    skip = True
                elif pt in ("const_item", "static_item", "macro_definition"):
                    if parent.children and node == next(
                        (c for c in parent.children if c.type == "identifier"), None
                    ):
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


LANGUAGES["rust"] = LangSpec(
    name="rust",
    exts=(".rs",),
    grammar_factory=_lang_rust,
    walk_definitions=walk_definitions,
    walk_refs=walk_refs,
    preprocess=noop_preprocess,
)
