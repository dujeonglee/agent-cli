# Originally ported from minish.ai/Agent-tools tsindex.py (Apache 2.0).
# See NOTICE for the list of modifications.
"""C++ language walker for code_index.

Pass-1 (`walk_definitions`) emits Symbol records for function/method
definitions (with qualified-name `Service::process` resolving the
scope into `parent`), `class_specifier` / `struct_specifier` /
`union_specifier` definitions and their nested members, enums,
typedefs, file-scope declarations, and preprocessor `#define`s.
`namespace_definition` nodes don't emit a symbol themselves but
propagate their name into the `parent` chain of their children;
`template_declaration` nodes unwrap to the inner declaration so the
indexed symbol matches the source name.

Pass-2 (`walk_refs`) extends the C ref vocabulary by also recognising
`obj.method(...)`, `obj->method(...)`, and `Scope::name(...)` as call
sites whose target is the right-most identifier.

Preprocess slot is `noop_preprocess` in PR-1.b; the real C/C++
unifdef + rewriter chain ports separately in PR-1.c.

The internal AST helpers (`find_innermost_function_name`,
`extract_storage_inline`, `c_modifiers`, etc.) are duplicated here so
this module does not import from `c.py`. Each language module stays
self-contained.
"""

from __future__ import annotations

from typing import Optional

from agent_cli.code_index.languages import LANGUAGES, LangSpec, noop_preprocess
from agent_cli.code_index.languages._shared import text
from agent_cli.code_index.schema import Ref, Symbol


def _lang_cpp():
    import tree_sitter_cpp
    from tree_sitter import Language

    return Language(tree_sitter_cpp.language())


# ---------- AST helpers (mirrors c.py — each language module is self-contained) ----------


def find_innermost_function_name(declarator):
    """Walk a declarator chain; return the identifier-like node that names the
    function, or None. Accepts `identifier` (C) or `field_identifier` (C++
    method) as the terminal name."""
    node = declarator
    while node is not None:
        if node.type == "function_declarator":
            inner = node.child_by_field_name("declarator")
            if inner is None:
                return None
            if inner.type in ("identifier", "field_identifier"):
                return inner
            node = inner
            continue
        if node.type in (
            "pointer_declarator",
            "reference_declarator",
            "init_declarator",
            "parenthesized_declarator",
        ):
            node = node.child_by_field_name("declarator")
            continue
        return None
    return None


def find_identifier_in_declarator(declarator):
    """For non-function declarators, find the variable name identifier."""
    node = declarator
    seen = 0
    while node is not None and seen < 16:
        seen += 1
        if node is None:
            return None
        if node.type == "identifier":
            return node
        if node.type in (
            "pointer_declarator",
            "init_declarator",
            "array_declarator",
            "parenthesized_declarator",
        ):
            node = node.child_by_field_name("declarator")
            continue
        return None
    return None


def declarator_is_function(declarator):
    node = declarator
    seen = 0
    while node is not None and seen < 16:
        seen += 1
        if node.type == "function_declarator":
            return True
        if node.type in (
            "pointer_declarator",
            "init_declarator",
            "array_declarator",
            "parenthesized_declarator",
        ):
            node = node.child_by_field_name("declarator")
            continue
        return False
    return False


def extract_storage_inline(node, src):
    storage = None
    is_inline = False
    for c in node.children:
        if c.type == "storage_class_specifier":
            kw = text(c, src)
            if kw in ("static", "extern"):
                storage = kw
        if c.type == "function_specifier" and text(c, src) == "inline":
            is_inline = True
        if c.type == "type_qualifier" and text(c, src) == "inline":
            is_inline = True
        if c.is_named is False and text(c, src) == "inline":
            is_inline = True
    return storage, is_inline


def extract_return_type(fn_node, src):
    decl = fn_node.child_by_field_name("declarator")
    parts = []
    for c in fn_node.children:
        if c == decl:
            break
        if c.type in ("storage_class_specifier", "function_specifier"):
            continue
        if text(c, src) == "inline":
            continue
        parts.append(text(c, src))
    return " ".join(parts).strip() or None


def extract_param_names(declarator, src) -> list[str]:
    node = declarator
    seen = 0
    while node is not None and seen < 16:
        seen += 1
        if node.type == "function_declarator":
            pl = node.child_by_field_name("parameters")
            if pl is None:
                return []
            names: list[str] = []
            for c in pl.named_children:
                if c.type == "parameter_declaration":
                    d = c.child_by_field_name("declarator")
                    if d is not None:
                        ident = find_identifier_in_declarator(d)
                        if ident is not None:
                            names.append(text(ident, src))
            return names
        if node.type in (
            "pointer_declarator",
            "init_declarator",
            "parenthesized_declarator",
        ):
            node = node.child_by_field_name("declarator")
            continue
        return []
    return []


def signature_of_function_def(fn_node, src):
    body = fn_node.child_by_field_name("body")
    if body is None:
        return None
    sig = src[fn_node.start_byte : body.start_byte].decode("utf-8", "replace")
    return " ".join(sig.split())


def is_typedef_decl(node, src):
    for c in node.children:
        if c.type == "storage_class_specifier" and text(c, src) == "typedef":
            return True
    return False


def collect_declarators(node):
    for c in node.children:
        if c.type in (
            "init_declarator",
            "identifier",
            "pointer_declarator",
            "array_declarator",
            "function_declarator",
            "parenthesized_declarator",
        ):
            yield c


def c_modifiers(storage: Optional[str], is_inline: bool) -> Optional[list[str]]:
    mods = []
    if storage:
        mods.append(storage)
    if is_inline:
        mods.append("inline")
    return mods or None


def add_declaration(node, src, rel, out):
    # Skip typedefs (handled via type_definition node by tree-sitter-c)
    if is_typedef_decl(node, src):
        return
    # Only emit top-level (file-scope) declarations.
    p = node.parent
    if p is None or p.type != "translation_unit":
        while p is not None and p.type in (
            "preproc_if",
            "preproc_ifdef",
            "preproc_else",
            "preproc_elif",
            "linkage_specification",
        ):
            p = p.parent
        if p is None or p.type != "translation_unit":
            return
    storage, is_inline = extract_storage_inline(node, src)
    for d in collect_declarators(node):
        target = d
        if d.type == "init_declarator":
            inner = d.child_by_field_name("declarator")
            if inner is not None:
                target = inner
        if declarator_is_function(target):
            name_node = find_innermost_function_name(target)
            if name_node is None:
                continue
            out.append(
                Symbol(
                    name=text(name_node, src),
                    kind="function",
                    file=rel,
                    line=node.start_point[0] + 1,
                    col=node.start_point[1],
                    end_line=node.end_point[0] + 1,
                    is_definition=False,
                    language="cpp",
                    kind_raw="prototype",
                    modifiers=c_modifiers(storage, is_inline),
                    signature=" ".join(text(node, src).split()),
                    params=extract_param_names(target, src) or None,
                )
            )
        else:
            name_node = find_identifier_in_declarator(target)
            if name_node is None:
                continue
            out.append(
                Symbol(
                    name=text(name_node, src),
                    kind="variable",
                    file=rel,
                    line=node.start_point[0] + 1,
                    col=node.start_point[1],
                    end_line=node.end_point[0] + 1,
                    is_definition=(storage != "extern"),
                    language="cpp",
                    kind_raw="var",
                    modifiers=c_modifiers(storage, False),
                    signature=" ".join(text(node, src).split()),
                )
            )


def add_record(node, src, rel, out):
    """struct_specifier, union_specifier, enum_specifier — DEFINITIONS only (body present)."""
    name_node = node.child_by_field_name("name")
    body = node.child_by_field_name("body")
    if name_node is None or body is None:
        return
    raw = node.type.replace("_specifier", "")  # struct | union | enum
    enum_values = None
    if raw == "enum":
        enum_values = []
        for c in body.named_children:
            if c.type == "enumerator":
                n = c.child_by_field_name("name")
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
            language="cpp",
            kind_raw=raw,
            enum_values=enum_values,
        )
    )


def add_typedef(node, src, rel, out):
    """type_definition: collect every declarator name as a typedef."""
    for d in node.children:
        target = None
        if d.type == "type_identifier":
            target = d
        elif d.type in ("pointer_declarator", "array_declarator"):
            target = find_identifier_in_declarator(d) or None
            if target is None:
                cur = d
                while cur is not None:
                    if cur.type == "type_identifier":
                        target = cur
                        break
                    nxt = cur.child_by_field_name("declarator")
                    if nxt is None:
                        found = None
                        for ch in cur.children:
                            if ch.type == "type_identifier":
                                found = ch
                                break
                        target = found
                        break
                    cur = nxt
        elif d.type == "function_declarator":
            target = find_innermost_function_name(d)
            if target is None:
                pass
        if target is None:
            continue
        out.append(
            Symbol(
                name=text(target, src),
                kind="type",
                file=rel,
                line=node.start_point[0] + 1,
                col=node.start_point[1],
                end_line=node.end_point[0] + 1,
                is_definition=True,
                language="cpp",
                kind_raw="typedef",
                signature=" ".join(text(node, src).split()),
            )
        )


def add_macro(node, src, rel, out, fn_form: bool):
    name = node.child_by_field_name("name")
    if name is None:
        return
    if fn_form:
        params = node.child_by_field_name("parameters")
        sig = f"#define {text(name, src)}" + (text(params, src) if params else "")
    else:
        value = node.child_by_field_name("value")
        sig = f"#define {text(name, src)}" + (f" {text(value, src)}" if value else "")
    out.append(
        Symbol(
            name=text(name, src),
            kind="function" if fn_form else "constant",
            file=rel,
            line=node.start_point[0] + 1,
            col=node.start_point[1],
            end_line=node.end_point[0] + 1,
            is_definition=True,
            language="cpp",
            kind_raw="preproc_function_def" if fn_form else "preproc_def",
            signature=" ".join(sig.split()),
        )
    )


# ---------- C++ specific extraction ----------

# C++ reuses most of the C extraction logic. The extras are: namespaces (no
# symbol emitted, but children are walked with parent scope), classes
# (kind=type with methods/fields parented), and templates (unwrap to inner).


def cpp_extract_function_def(
    node, src: bytes, rel: str, parent: Optional[str], out: list
):
    decl = node.child_by_field_name("declarator")
    if decl is None:
        return
    name_node = find_innermost_function_name(decl)
    eff_parent = parent
    if name_node is None:
        # qualified_identifier case (`Service::process`): descend to find it
        cur = decl
        while cur is not None and cur.type != "function_declarator":
            cur = cur.child_by_field_name("declarator")
        if cur is not None:
            inner = cur.child_by_field_name("declarator")
            if inner is not None and inner.type == "qualified_identifier":
                scope = inner.child_by_field_name("scope")
                name_field = inner.child_by_field_name("name")
                # tree-sitter-cpp may parse the name as identifier OR type_identifier
                if name_field is not None and name_field.type in (
                    "identifier",
                    "type_identifier",
                    "field_identifier",
                ):
                    name_node = name_field
                    if scope is not None:
                        scope_text = text(scope, src)
                        eff_parent = (
                            (eff_parent + "::" + scope_text)
                            if eff_parent
                            else scope_text
                        )
    if name_node is None:
        return
    storage, is_inline = extract_storage_inline(node, src)
    out.append(
        Symbol(
            name=text(name_node, src),
            kind="function",
            file=rel,
            line=node.start_point[0] + 1,
            col=node.start_point[1],
            end_line=node.end_point[0] + 1,
            is_definition=True,
            language="cpp",
            kind_raw="function_definition",
            modifiers=c_modifiers(storage, is_inline),
            parent=eff_parent,
            signature=signature_of_function_def(node, src),
            return_type=extract_return_type(node, src),
            params=extract_param_names(decl, src) or None,
        )
    )


def cpp_extract_class(node, src: bytes, rel: str, parent: Optional[str], out: list):
    """class_specifier / struct_specifier / union_specifier with a body."""
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    # tree-sitter-cpp puts the body in field_declaration_list (not a "body" field).
    body = None
    for c in node.children:
        if c.type == "field_declaration_list":
            body = c
            break
    cls = text(name_node, src)
    kind_raw = (
        "class"
        if node.type == "class_specifier"
        else node.type.replace("_specifier", "")
    )
    out.append(
        Symbol(
            name=cls,
            kind="type",
            file=rel,
            line=node.start_point[0] + 1,
            col=node.start_point[1],
            end_line=node.end_point[0] + 1,
            is_definition=True,
            language="cpp",
            kind_raw=kind_raw,
            parent=parent,
        )
    )
    if body is None:
        return
    inner_parent = (parent + "::" + cls) if parent else cls
    for stmt in body.children:
        st = stmt.type
        if st == "function_definition":
            cpp_extract_function_def(stmt, src, rel, inner_parent, out)
        elif st == "field_declaration":
            decl = stmt.child_by_field_name("declarator")
            if decl is not None and declarator_is_function(decl):
                # method prototype (no body in field_declaration form)
                nm = find_innermost_function_name(decl)
                if nm is not None:
                    storage, is_inline = extract_storage_inline(stmt, src)
                    out.append(
                        Symbol(
                            name=text(nm, src),
                            kind="function",
                            file=rel,
                            line=stmt.start_point[0] + 1,
                            col=stmt.start_point[1],
                            end_line=stmt.end_point[0] + 1,
                            is_definition=False,
                            language="cpp",
                            kind_raw="method_declaration",
                            modifiers=c_modifiers(storage, is_inline),
                            parent=inner_parent,
                            signature=" ".join(text(stmt, src).split()),
                            params=extract_param_names(decl, src) or None,
                        )
                    )
            else:
                # data field. tree-sitter-cpp uses `field_identifier` for the name
                # (inside declarator). Find it.
                nm = None
                if decl is not None:
                    if decl.type == "field_identifier":
                        nm = decl
                    else:
                        cur = decl
                        seen = 0
                        while cur is not None and seen < 8:
                            seen += 1
                            if cur.type in ("field_identifier", "identifier"):
                                nm = cur
                                break
                            inner = cur.child_by_field_name("declarator")
                            if inner is None:
                                # search children for field_identifier
                                for ch in cur.children:
                                    if ch.type in ("field_identifier", "identifier"):
                                        nm = ch
                                        break
                                break
                            cur = inner
                if nm is not None:
                    storage, _ = extract_storage_inline(stmt, src)
                    has_const = "const" in text(stmt, src).split("=", 1)[0]
                    is_const = storage == "static" and has_const
                    out.append(
                        Symbol(
                            name=text(nm, src),
                            kind="constant" if is_const else "variable",
                            file=rel,
                            line=stmt.start_point[0] + 1,
                            col=stmt.start_point[1],
                            end_line=stmt.end_point[0] + 1,
                            is_definition=True,
                            language="cpp",
                            kind_raw="field_declaration",
                            modifiers=c_modifiers(storage, False),
                            parent=inner_parent,
                            signature=" ".join(text(stmt, src).split()),
                        )
                    )
        elif st in ("class_specifier", "struct_specifier", "union_specifier"):
            cpp_extract_class(stmt, src, rel, inner_parent, out)
        # Skip access_specifier (public:/private:) etc.


def cpp_walk_one(node, src: bytes, rel: str, parent: Optional[str], out: list):
    """Recursive dispatcher for a single top-level / namespace-scope node."""
    nt = node.type
    if nt == "namespace_definition":
        # No symbol for the namespace itself; children inherit it as parent.
        name_node = node.child_by_field_name("name")
        ns = text(name_node, src) if name_node is not None else None
        inner = (parent + "::" + ns) if (parent and ns) else (ns or parent)
        body = node.child_by_field_name("body")
        if body is not None:
            for c in body.children:
                cpp_walk_one(c, src, rel, inner, out)
    elif nt == "template_declaration":
        # Unwrap: the inner declaration is what gets indexed.
        for c in node.children:
            if c.type in (
                "function_definition",
                "class_specifier",
                "struct_specifier",
                "declaration",
            ):
                cpp_walk_one(c, src, rel, parent, out)
    elif nt == "function_definition":
        cpp_extract_function_def(node, src, rel, parent, out)
    elif nt in ("class_specifier", "struct_specifier", "union_specifier"):
        # Only emit if it has a body (definition, not just a type reference).
        if node.child_by_field_name("body") is not None:
            cpp_extract_class(node, src, rel, parent, out)
    elif nt == "enum_specifier":
        # Reuse C-style handler (covers C++ enums fine).
        add_record(node, src, rel, out)
    elif nt == "type_definition":
        add_typedef(node, src, rel, out)
    elif nt == "declaration":
        # File-scope variable/prototype — reuse the C-style handler.
        add_declaration(node, src, rel, out)
    elif nt == "preproc_def":
        add_macro(node, src, rel, out, fn_form=False)
    elif nt == "preproc_function_def":
        add_macro(node, src, rel, out, fn_form=True)
    elif nt in ("linkage_specification",):  # `extern "C" { ... }`
        for c in node.children:
            cpp_walk_one(c, src, rel, parent, out)


def walk_definitions(root, src: bytes, rel: str, syms: list):
    for node in root.children:
        cpp_walk_one(node, src, rel, None, syms)


def walk_refs(
    root,
    src: bytes,
    rel: str,
    refs: list,
    defined_names: set,
    identifiers_out: set,
    language: str = "cpp",
):
    """Like c_walk_refs, but also recognises `obj.method(...)` and `obj->method(...)`
    as calls to `method` (via field_expression)."""
    stack = [root]
    while stack:
        node = stack.pop()
        nt = node.type
        if nt == "call_expression":
            fn = node.child_by_field_name("function")
            target_name = None
            if fn is not None:
                if fn.type == "identifier":
                    target_name = text(fn, src)
                elif fn.type == "field_expression":
                    # `s.foo` or `s->foo` — take the rightmost member
                    fld = fn.child_by_field_name("field")
                    if fld is not None and fld.type in (
                        "field_identifier",
                        "identifier",
                    ):
                        target_name = text(fld, src)
                elif fn.type == "qualified_identifier":
                    n = fn.child_by_field_name("name")
                    if n is not None and n.type in (
                        "identifier",
                        "type_identifier",
                        "field_identifier",
                    ):
                        target_name = text(n, src)
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
                # Don't double-record the function-position identifier as a name ref.
                for c in node.children:
                    if c is not fn:
                        stack.append(c)
                continue
        elif nt == "identifier":
            name = text(node, src)
            identifiers_out.add(name)
            if name in defined_names:
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
        elif nt == "type_identifier":
            parent = node.parent
            is_def_name = False
            if parent is not None:
                if parent.type in (
                    "struct_specifier",
                    "union_specifier",
                    "enum_specifier",
                    "class_specifier",
                ):
                    name_field = parent.child_by_field_name("name")
                    body = parent.child_by_field_name("body")
                    if body is None:
                        for ch in parent.children:
                            if ch.type == "field_declaration_list":
                                body = ch
                                break
                    if name_field == node and body is not None:
                        is_def_name = True
                elif parent.type == "type_definition":
                    if parent.child_by_field_name("declarator") == node:
                        is_def_name = True
            name = text(node, src)
            identifiers_out.add(name)
            if not is_def_name:
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
        stack.extend(node.children)


LANGUAGES["cpp"] = LangSpec(
    name="cpp",
    exts=(".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".h++"),
    grammar_factory=_lang_cpp,
    walk_definitions=walk_definitions,
    walk_refs=walk_refs,
    preprocess=noop_preprocess,
)
