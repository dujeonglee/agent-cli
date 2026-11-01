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

from agent_cli.code_index.languages import LANGUAGES, LangSpec
from agent_cli.code_index.languages._shared import qualify, text
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
        if node.type == "parenthesized_declarator":
            # `size_t (foo)(int)` — 괄호 안의 이름 그대로가 함수 이름 (`(*fp)(int)` 는
            # 포인터를 거쳐 아래에서 None)
            inner = _inner_declarator(node)
            if inner is not None and inner.type in ("identifier", "field_identifier"):
                return inner
            node = inner
            continue
        if node.type in (
            "pointer_declarator",
            "reference_declarator",
            "init_declarator",
        ):
            node = _inner_declarator(node)
            continue
        return None
    return None


def _inner_declarator(node):
    """선언자 한 겹 안쪽 — ``parenthesized_declarator`` 는 ``declarator`` 필드가 없어
    첫 이름 있는 자식(`size_t (foo)(int)` 의 `foo`)."""
    if node.type == "parenthesized_declarator":
        return node.named_children[0] if node.named_children else None
    return node.child_by_field_name("declarator")


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
            node = _inner_declarator(node)
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
            node = _inner_declarator(node)
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
            node = _inner_declarator(node)
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


def c_modifiers(storage: str | None, is_inline: bool) -> list[str] | None:
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
            qualified_name=qualify(None, text(name_node, src)),
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
        if _has_function_declarator(target):
            name_node = find_innermost_function_name(target)
            if name_node is None:
                continue
            out.append(
                Symbol(
                    name=text(name_node, src),
                    qualified_name=qualify(None, text(name_node, src)),
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
            # 함수 포인터 변수(`static int (*fp)(int);`)도 변수 (v10.98.0 — 종전엔 기호 없음)
            name_node = _declarator_name(target)
            if name_node is None:
                continue
            out.append(
                Symbol(
                    name=text(name_node, src),
                    qualified_name=qualify(None, text(name_node, src)),
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
            qualified_name=qualify(None, text(name_node, src)),
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
    """type_definition: every declarator name is a typedef — not the ``type``
    field (`typedef u64 phys_t;` names ``phys_t`` only; until v10.98.0 the
    ``u64`` was emitted as a typedef too). Function-pointer typedefs
    (`typedef int (*cb_t)(int);`) name ``cb_t``."""
    base = node.child_by_field_name("type")
    for d in node.children:
        if d == base or d.type not in _TYPEDEF_DECLARATORS:
            continue
        target = _declarator_name(d)
        if target is None:
            continue
        out.append(
            Symbol(
                name=text(target, src),
                qualified_name=qualify(None, text(target, src)),
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


#: ``type_definition`` 의 선언자 자식이 될 수 있는 노드.
_TYPEDEF_DECLARATORS = (
    "type_identifier",
    "pointer_declarator",
    "array_declarator",
    "function_declarator",
    "parenthesized_declarator",
    "attributed_declarator",
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
            qualified_name=qualify(None, text(name, src)),
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


#: 이니셜라이저 값에서 식별자까지 벗겨 내려가는 식 (`&f`, `(void *)f`, `(f)`, `c ? f : g`).
_INIT_WRAPPERS = (
    "pointer_expression",
    "cast_expression",
    "parenthesized_expression",
    "conditional_expression",
)


def _declarator_name(node):
    """선언자 사슬 끝의 이름 노드 — 변수·함수·함수 포인터(`int (*cb)(int)` 의 `cb`)."""
    seen = 0
    while node is not None and seen < 32:
        seen += 1
        if node.type in ("identifier", "type_identifier", "field_identifier"):
            return node
        if node.type in (
            "pointer_declarator",
            "init_declarator",
            "array_declarator",
            "parenthesized_declarator",
            "function_declarator",
            "attributed_declarator",
        ):
            node = _inner_declarator(node)
            continue
        return None
    return None


def _has_function_declarator(node) -> bool:
    """선언자가 함수를 선언하나 — 이름을 바로 감싼 선언자(괄호는 건너뛰고)가
    `function_declarator` 면 함수. `(*cb)(int)`·`(*tbl[2])(int)` 는 이름을 포인터·배열이
    감싸 변수, `(*getcb(int x))(int y)` 는 함수 포인터를 돌려주는 함수."""
    wrapper = None
    seen = 0
    while node is not None and seen < 32:
        seen += 1
        if node.type in ("identifier", "field_identifier"):
            return wrapper == "function_declarator"
        if node.type in (
            "function_declarator",
            "pointer_declarator",
            "init_declarator",
            "array_declarator",
            "parenthesized_declarator",
            "attributed_declarator",
        ):
            if node.type not in (
                "parenthesized_declarator",
                "init_declarator",
                "attributed_declarator",
            ):
                wrapper = node.type
            node = _inner_declarator(node)
            continue
        return False
    return False


def _param_declarators(fn_declarator):
    """함수 정의 선언자의 인자 선언자들 — 이름에 가장 가까운 `function_declarator` 의
    것(`int (*getcb(int x))(int y)` 는 `x` — 바깥 것은 돌려주는 함수 포인터의 인자)."""
    node = fn_declarator
    fn = None
    seen = 0
    while node is not None and seen < 32:
        seen += 1
        if node.type == "function_declarator":
            fn = node
        if node.type in ("identifier", "field_identifier"):
            break
        node = _inner_declarator(node)
    if fn is None:
        return []
    pl = fn.child_by_field_name("parameters")
    if pl is None:
        return []
    out = []
    for c in pl.named_children:
        if c.type == "parameter_declaration":
            d = c.child_by_field_name("declarator")
            if d is not None:
                out.append(d)
    return out


def _designator(pair, src) -> str:
    """``initializer_pair`` 의 지정자 텍스트(`.ndo_open`, `[3]`, `.a.b`)."""
    parts = []
    for c in pair.children:
        if c.type == "field_identifier":  # GNU 옛 꼴 `{ name: foo }`
            parts.append("." + text(c, src))
        elif c.type in (
            "field_designator",
            "subscript_designator",
            "subscript_range_designator",
        ):
            parts.append("".join(text(c, src).split()))
    return "".join(parts) or "[]"


def _object_text(node, src) -> str:
    """멤버 호출의 앞부분 — 이름·필드 사슬이면 공백을 뺀 그대로, 아니면 ``"<expr>"``."""
    cur = node
    while cur is not None and cur.type == "field_expression":
        cur = cur.child_by_field_name("argument")
    if cur is not None and cur.type == "identifier":
        return "".join(text(node, src).split())
    return "<expr>"


def walk_scopes(root, src, rel, defined_names, identifiers_out, language="c"):
    """``(scopes, bindings, refs)`` for one C file (DESIGN §22).

    Scopes: the file (``module``), each function definition (``function`` —
    parameters and the outermost block are one scope), each nested
    ``compound_statement`` and each ``for`` that declares (``block``). Within a
    scope a binding is visible in the whole scope (flat, as in Python).

    Bindings (kind):
      - ``param`` — a function definition's parameters;
      - ``local`` — a variable declared in a function or block (incl. ``static``
        locals); ``enumerator`` — an enum constant (file scope: linked to the enum
        symbol); ``tag`` — a struct/union/enum name (file scope: its symbol);
        ``typedef`` — a typedef declared in a function or block;
      - file scope: ``def`` — every definition Pass-1 emits (function, variable,
        typedef, ``#define``), linked to its symbol; ``decl`` — a ``static``
        prototype with no definition in the file (internal linkage — it cannot
        name another file's function). A non-static prototype and an ``extern``
        variable declaration bind nothing: the name is looked up in the other
        files (``resolve/c.py``).

    Refs: ``call`` for ``f(...)``; ``call`` with ``object`` for ``p->f(...)``/
    ``s.f(...)`` (the object text); ``name`` for an identifier whose name is
    defined somewhere in the index; ``type`` for a type name outside its own
    definition — a struct/union/enum tag carries its keyword in ``object``
    (``"struct"``), a typedef name none. A name used as the value of a brace initializer (``.probe =
    foo_probe``, ``{ f, g }``, through ``&``/casts/parentheses/``?:``) is a
    ``name`` ref whose ``object`` is the designator (``".probe"``, ``"[]"`` for a
    positional value); the resolver turns it into ``registration`` when it names
    a function. Declarator names (a definition's or prototype's name, its
    parameters, declared variables, macro names, enumerators) are not refs.
    """
    from agent_cli.code_index.schema import Binding, Scope

    scopes: list[Scope] = [
        Scope(kind="module", start_line=1, end_line=root.end_point[0] + 1)
    ]
    bindings: list[Binding] = []
    refs: list[Ref] = []
    skip: set[tuple[int, int]] = set()  # 선언자 이름 노드의 바이트 범위

    def line(node) -> int:
        return node.start_point[0] + 1

    def bind(scope, name, kind, ln, key=None):
        bindings.append(
            Binding(scope=scope, name=name, kind=kind, line=ln, symbol_key=key)
        )

    # ---- file scope: Pass-1 의 기호를 그대로 바인딩으로 (같은 줄·이름·kind_raw 로 잇는다)
    syms: list[Symbol] = []
    walk_definitions(root, src, rel, syms)
    defined_here = {s.name for s in syms if s.kind_raw == "function"}
    for s in syms:
        key = (s.line, s.name, s.kind_raw)
        mods = s.modifiers or []
        if s.kind_raw == "prototype":
            if "static" in mods and s.name not in defined_here:
                bind(0, s.name, "decl", s.line)
            continue
        if s.kind_raw == "var" and not s.is_definition:
            continue  # extern 선언 — 정의는 다른 파일
        if s.kind_raw in ("struct", "union", "enum"):
            bind(0, s.name, "tag", s.line, key)
            for ev in s.enum_values or []:
                bind(0, ev, "enumerator", s.line, key)
            continue
        bind(0, s.name, "def", s.line, key)

    def declare(node, scope, file_scope: bool):
        """``declaration``/``type_definition`` 의 선언자 — 이름은 참조에서 빼고, 함수·
        블록 안이면 바인딩한다(파일 범위는 위에서 Pass-1 기호로)."""
        is_typedef = node.type == "type_definition" or is_typedef_decl(node, src)
        storage, _inline = extract_storage_inline(node, src)
        if node.type == "type_definition":
            # 선언자만 — ``type`` 필드(`typedef u64 x;` 의 `u64`)는 타입 참조로 남는다
            base = node.child_by_field_name("type")
            decls = [
                c for c in node.children if c != base and c.type in _TYPEDEF_DECLARATORS
            ]
        else:
            decls = list(collect_declarators(node))
        seen_nodes = set()
        for d in decls:
            if d is None or (d.start_byte, d.end_byte) in seen_nodes:
                continue
            seen_nodes.add((d.start_byte, d.end_byte))
            nm = _declarator_name(d)
            if nm is None:
                continue
            skip.add((nm.start_byte, nm.end_byte))
            if file_scope:
                continue
            name = text(nm, src)
            if is_typedef:
                bind(scope, name, "typedef", line(nm))
            elif storage == "extern" or _has_function_declarator(d):
                continue  # 블록 안의 extern·함수 선언 — 바깥을 가리킨다
            else:
                bind(scope, name, "local", line(nm))

    def record(node, scope, file_scope: bool):
        """함수·블록 안의 struct/union/enum 정의 — 태그와 열거자."""
        name_node = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if body is None:
            return
        if name_node is not None and not file_scope:
            bind(scope, text(name_node, src), "tag", line(name_node))
        if node.type == "enum_specifier":
            for c in body.named_children:
                if c.type == "enumerator":
                    n = c.child_by_field_name("name")
                    if n is not None:
                        skip.add((n.start_byte, n.end_byte))
                        if not file_scope:
                            bind(scope, text(n, src), "local", line(n))

    def add_ref(node, kind, scope, obj=None, name=None):
        nm = name if name is not None else text(node, src)
        refs.append(
            Ref(
                name=nm,
                kind=kind,
                file=rel,
                line=line(node),
                col=node.start_point[1],
                language=language,
                scope=scope,
                object=obj,
            )
        )

    def init_value(node):
        """이니셜라이저 값 자리면 그 지정자, 아니면 None."""
        child = node
        par = node.parent
        while par is not None and par.type in _INIT_WRAPPERS:
            if (
                par.type == "conditional_expression"
                and par.child_by_field_name("condition") == child
            ):
                return None
            if (
                par.type == "cast_expression"
                and par.child_by_field_name("value") != child
            ):
                return None
            child, par = par, par.parent
        if par is None:
            return None
        if par.type == "initializer_pair" and par.child_by_field_name("value") == child:
            return _designator(par, src)
        if par.type == "initializer_list":
            return "[]"
        return None

    # ---- 걷기: (노드, 범위, 파일 범위인가)
    stack = [(root, 0, True)]
    while stack:
        node, scope, fs = stack.pop()
        nt = node.type
        if nt == "function_definition":
            decl = node.child_by_field_name("declarator")
            nm = find_innermost_function_name(decl) if decl is not None else None
            if nm is not None:
                skip.add((nm.start_byte, nm.end_byte))
            idx = len(scopes)
            scopes.append(
                Scope(
                    kind="function",
                    start_line=line(node),
                    end_line=node.end_point[0] + 1,
                    parent=scope,
                    def_line=line(node),
                    def_name=text(nm, src) if nm is not None else None,
                )
            )
            for pd in _param_declarators(decl) if decl is not None else []:
                pn = _declarator_name(pd)
                if pn is not None:
                    skip.add((pn.start_byte, pn.end_byte))
                    bind(idx, text(pn, src), "param", line(pn))
            body = node.child_by_field_name("body")
            for c in node.children:
                if c == body:
                    # 맨 바깥 블록은 함수 범위 그 자체
                    stack.extend((cc, idx, False) for cc in c.children)
                else:
                    stack.append((c, idx, False))
            continue
        if nt == "compound_statement" or (
            nt == "for_statement"
            and node.child_by_field_name("initializer") is not None
            and node.child_by_field_name("initializer").type == "declaration"
        ):
            idx = len(scopes)
            scopes.append(
                Scope(
                    kind="block",
                    start_line=line(node),
                    end_line=node.end_point[0] + 1,
                    parent=scope,
                )
            )
            stack.extend((c, idx, False) for c in node.children)
            continue
        if nt in ("declaration", "type_definition"):
            declare(node, scope, fs)
        elif nt == "parameter_declaration":
            # 프로토타입·함수 포인터 타입의 인자 이름 — 참조 아님
            d = node.child_by_field_name("declarator")
            pn = _declarator_name(d) if d is not None else None
            if pn is not None:
                skip.add((pn.start_byte, pn.end_byte))
        elif nt in ("struct_specifier", "union_specifier", "enum_specifier"):
            record(node, scope, fs)
        elif nt in ("preproc_def", "preproc_function_def"):
            n = node.child_by_field_name("name")
            if n is not None:
                skip.add((n.start_byte, n.end_byte))
            params = node.child_by_field_name("parameters")
            if params is not None:
                for c in params.children:
                    skip.add((c.start_byte, c.end_byte))
        elif nt == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is not None and fn.type == "identifier":
                identifiers_out.add(text(fn, src))
                add_ref(fn, "call", scope)
                skip.add((fn.start_byte, fn.end_byte))
            elif fn is not None and fn.type == "field_expression":
                field = fn.child_by_field_name("field")
                if field is not None:
                    identifiers_out.add(text(field, src))
                    add_ref(
                        field,
                        "call",
                        scope,
                        obj=_object_text(fn.child_by_field_name("argument"), src),
                    )
        elif nt == "identifier":
            k = (node.start_byte, node.end_byte)
            name = text(node, src)
            identifiers_out.add(name)
            if k not in skip and name in defined_names:
                add_ref(node, "name", scope, obj=init_value(node))
        elif nt == "type_identifier":
            parent = node.parent
            is_def_name = False
            if parent is not None:
                if parent.type in (
                    "struct_specifier",
                    "union_specifier",
                    "enum_specifier",
                ):
                    if (
                        parent.child_by_field_name("name") == node
                        and parent.child_by_field_name("body") is not None
                    ):
                        is_def_name = True
                elif (
                    parent.type == "type_definition"
                    and parent.child_by_field_name("declarator") == node
                ):
                    is_def_name = True
            name = text(node, src)
            identifiers_out.add(name)
            if not is_def_name and (node.start_byte, node.end_byte) not in skip:
                # 태그(`struct foo`)와 typedef 이름은 다른 이름공간 — 태그는 그 키워드를 적는다
                tag = None
                if parent is not None and parent.type in (
                    "struct_specifier",
                    "union_specifier",
                    "enum_specifier",
                ):
                    tag = parent.type.split("_", 1)[0]
                add_ref(node, "type", scope, obj=tag)
        # 자식은 원래 순서대로 처리되도록 거꾸로 쌓는다 (skip 이 부모에서 먼저 정해진다)
        stack.extend((c, scope, fs) for c in reversed(node.children))
    return scopes, bindings, refs


LANGUAGES["c"] = LangSpec(
    name="c",
    exts=(".c", ".h"),
    grammar_factory=_lang_c,
    walk_definitions=walk_definitions,
    walk_refs=None,
    preprocess=preprocess_source,
    walk_scopes=walk_scopes,
)
