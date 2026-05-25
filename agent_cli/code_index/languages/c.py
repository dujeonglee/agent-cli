# Originally ported from minish.ai/Agent-tools tsindex.py (Apache 2.0).
# See NOTICE for the list of modifications.
"""C language walker for code_index.

Pass-1 (`walk_definitions`) emits Symbol records for function
definitions, file-scope declarations (variables and prototypes),
struct / union / enum definitions (records carry `enum_values` when
applicable), typedefs, and preprocessor `#define`s. Function-like
`#define`s map to `kind='function'`; object-like `#define`s map to
`kind='constant'`. `storage`/`inline` qualifiers carry through into
`modifiers`. Forward declarations (`is_definition=False`) are still
recorded so callgraph/ref resolution can match them.

Pass-2 (`walk_refs`) emits `kind='call'` for `call_expression`
identifier targets, `kind='type'` for `type_identifier` uses outside
the struct/union/enum/typedef definition name position, and
`kind='name'` for plain identifiers whose name is in the defined-name
set.

Preprocess slot is `preprocess_source` from `code_index.preproc`: the
kernel-style regex rewrite chain plus an optional `unifdef -b` pass.
"""

from __future__ import annotations

from typing import Optional

from agent_cli.code_index.languages import LANGUAGES, LangSpec
from agent_cli.code_index.languages._shared import text
from agent_cli.code_index.preproc import preprocess_source
from agent_cli.code_index.schema import Ref, Symbol


def _lang_c():
    import tree_sitter_c
    from tree_sitter import Language

    return Language(tree_sitter_c.language())


# ---------- AST helpers ----------


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
    """Walk to the innermost function_declarator and collect parameter names.
    Used by call-graph filters to suppress refs that are actually local
    parameters shadowing a same-named file-scope symbol."""
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


# ---------- extraction ----------


def is_typedef_decl(node, src):
    for c in node.children:
        if c.type == "storage_class_specifier" and text(c, src) == "typedef":
            return True
    return False


def collect_declarators(node):
    """Yield direct declarator children of a declaration node (excluding type specifiers)."""
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


def add_function_def(node, src, rel, out):
    decl = node.child_by_field_name("declarator")
    if decl is None:
        return
    name_node = find_innermost_function_name(decl)
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
            language="c",
            kind_raw="function",
            modifiers=c_modifiers(storage, is_inline),
            signature=signature_of_function_def(node, src),
            return_type=extract_return_type(node, src),
            params=extract_param_names(decl, src) or None,
        )
    )


def add_declaration(node, src, rel, out):
    # Skip typedefs (handled via type_definition node by tree-sitter-c)
    if is_typedef_decl(node, src):
        return
    # Only emit top-level (file-scope) declarations.
    # Parents inside compound_statement (function bodies) or struct fields are local.
    p = node.parent
    if p is None or p.type != "translation_unit":
        # Allow preproc_if/preproc_ifdef wrappers (#if static int foo; #endif)
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
                    language="c",
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
                    language="c",
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
            language="c",
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
                # Look for type_identifier inside
                cur = d
                while cur is not None:
                    if cur.type == "type_identifier":
                        target = cur
                        break
                    nxt = cur.child_by_field_name("declarator")
                    if nxt is None:
                        # search children for type_identifier
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
                # function-pointer typedef has parenthesized_declarator; skip name extraction
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
                language="c",
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
            # Function-like macros are callable → kind="function".
            # Object-like macros are values → kind="constant".
            # Both keep kind_raw so callers can filter precisely.
            kind="function" if fn_form else "constant",
            file=rel,
            line=node.start_point[0] + 1,
            col=node.start_point[1],
            end_line=node.end_point[0] + 1,
            is_definition=True,
            language="c",
            kind_raw="preproc_function_def" if fn_form else "preproc_def",
            signature=" ".join(sig.split()),
        )
    )


def walk_definitions(root, src, rel, syms):
    """One pass: visit all relevant definition-bearing nodes (including nested)."""
    stack = [root]
    while stack:
        node = stack.pop()
        nt = node.type
        if nt == "function_definition":
            add_function_def(node, src, rel, syms)
            # Don't descend; bodies don't contain top-level defs in C.
            continue
        if nt == "declaration":
            add_declaration(node, src, rel, syms)
            # Declarations may host inline struct/enum defs in the type — descend.
            stack.extend(node.children)
            continue
        if nt == "type_definition":
            add_typedef(node, src, rel, syms)
            stack.extend(node.children)
            continue
        if nt in ("struct_specifier", "union_specifier", "enum_specifier"):
            add_record(node, src, rel, syms)
            stack.extend(node.children)
            continue
        if nt == "preproc_def":
            add_macro(node, src, rel, syms, fn_form=False)
            continue
        if nt == "preproc_function_def":
            add_macro(node, src, rel, syms, fn_form=True)
            continue
        stack.extend(node.children)


def walk_refs(root, src, rel, refs, defined_names, identifiers_out, language="c"):
    """Collect refs (calls, type uses, name mentions) AND the set of all distinct
    identifier names that appear anywhere in the file. The identifier set is used
    by incremental builds to detect which unchanged files need a re-Pass2 when a
    new defined symbol appears in a changed file.

    Kinds:
      - call: identifier in call_expression.function
      - type: type_identifier (except when naming a record/typedef definition)
      - name: bare identifier whose name is a known defined symbol
    """
    stack = [root]
    while stack:
        node = stack.pop()
        nt = node.type
        if nt == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is not None and fn.type == "identifier":
                name = text(fn, src)
                identifiers_out.add(name)
                refs.append(
                    Ref(
                        name=name,
                        kind="call",
                        file=rel,
                        line=fn.start_point[0] + 1,
                        col=fn.start_point[1],
                        language=language,
                    )
                )
                # Don't double-record this identifier under "name" — stop descent into fn child.
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
                ):
                    name_field = parent.child_by_field_name("name")
                    body = parent.child_by_field_name("body")
                    # Only treat as a "definition name" when there's a body — otherwise it's a use.
                    if name_field == node and body is not None:
                        is_def_name = True
                elif parent.type == "type_definition":
                    # The declarator side of typedef is the new name → that IS a def, not a ref.
                    # tree-sitter-c: type_definition has type field (original) + declarator field (new name)
                    declarator_field = parent.child_by_field_name("declarator")
                    if declarator_field == node:
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


LANGUAGES["c"] = LangSpec(
    name="c",
    exts=(".c", ".h"),
    grammar_factory=_lang_c,
    walk_definitions=walk_definitions,
    walk_refs=walk_refs,
    preprocess=preprocess_source,
)
