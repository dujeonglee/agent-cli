"""tree-sitter backed symbol tool.

Single public entry point: :func:`tool_read_symbols`. Two modes:
- ``mode='list'`` — outline of a file (functions/classes/headings/...).
- ``mode='fetch'`` — body of one named symbol from that outline.

Languages are auto-detected from the file extension. C and C++ are both
parsed with the C++ grammar (the C grammar's strictness gains us nothing
at the symbol level and would mean maintaining two parsers). Files with
unsupported extensions return a fallback error that points the model
back to ``read_file``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_cli.tools.result import ToolResult


# ── Language map ──────────────────────────────────────────────────
# Keep extensions lower-cased; lookup normalizes input.
_EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    # C and C++ both use the C++ grammar (decision: see module docstring).
    ".c": "cpp",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".h": "cpp",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".md": "markdown",
    ".markdown": "markdown",
}


def _detect_language(path: Path) -> str | None:
    return _EXT_TO_LANG.get(path.suffix.lower())


# ── Symbol dataclass ──────────────────────────────────────────────
@dataclass
class Symbol:
    name: str  # e.g. "Foo.bar" / "ns::Foo::bar" / "## Setup" / "MAX"
    kind: str  # "function" | "class" | "method" | "struct" | "enum" | "typedef" | "macro" | "namespace" | "heading"
    is_definition: (
        bool  # function_definition vs declaration; preproc_def is a definition
    )
    start_line: int  # 1-indexed
    end_line: int  # 1-indexed, inclusive


# ── Helpers ───────────────────────────────────────────────────────
def _ts_lines(node) -> tuple[int, int]:
    """Return 1-indexed (start_line, end_line) inclusive.

    tree-sitter's end_point is exclusive at the byte level — for nodes that
    consume a trailing newline (e.g. ``preproc_def``) it lands at column 0
    of the next line. Trim that case so end_line names the last line that
    actually contains the node's text.
    """
    start = node.start_point[0] + 1
    end = node.end_point[0] + 1
    if node.end_point[1] == 0 and end > start:
        end -= 1
    return (start, end)


def _node_text(node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _get_field(node, name: str):
    """child_by_field_name with None tolerance."""
    return node.child_by_field_name(name)


# ── Python extractor ──────────────────────────────────────────────
def _extract_python(root, source: bytes) -> list[Symbol]:
    symbols: list[Symbol] = []

    def visit(node, class_prefix: str = "") -> None:
        # Decorated definitions wrap the actual function/class — descend into
        # the wrapped node so the symbol is attributed to its definition span.
        if node.type == "decorated_definition":
            inner = _get_field(node, "definition")
            if inner is not None:
                visit(inner, class_prefix)
            return

        if node.type == "function_definition":
            name_node = _get_field(node, "name")
            if name_node is not None:
                base = name_node.text.decode("utf-8", errors="replace")
                full = f"{class_prefix}.{base}" if class_prefix else base
                start, end = _ts_lines(node)
                symbols.append(
                    Symbol(
                        name=full,
                        kind="method" if class_prefix else "function",
                        is_definition=True,
                        start_line=start,
                        end_line=end,
                    )
                )
            return  # Do not descend into nested functions for v1

        if node.type == "class_definition":
            name_node = _get_field(node, "name")
            if name_node is None:
                return
            base = name_node.text.decode("utf-8", errors="replace")
            full = f"{class_prefix}.{base}" if class_prefix else base
            start, end = _ts_lines(node)
            symbols.append(
                Symbol(
                    name=full,
                    kind="class",
                    is_definition=True,
                    start_line=start,
                    end_line=end,
                )
            )
            body = _get_field(node, "body")
            if body is not None:
                for child in body.children:
                    if child.is_named:
                        visit(child, full)
            return

        for child in node.children:
            if child.is_named:
                visit(child, class_prefix)

    visit(root)
    return symbols


# ── JavaScript / TypeScript extractor ─────────────────────────────
def _extract_js(root, source: bytes) -> list[Symbol]:
    symbols: list[Symbol] = []

    def visit(node, class_prefix: str = "") -> None:
        if node.type in ("function_declaration", "function_signature"):
            name_node = _get_field(node, "name")
            if name_node is not None:
                base = name_node.text.decode("utf-8", errors="replace")
                full = f"{class_prefix}.{base}" if class_prefix else base
                start, end = _ts_lines(node)
                symbols.append(
                    Symbol(
                        name=full,
                        kind="method" if class_prefix else "function",
                        is_definition=node.type == "function_declaration",
                        start_line=start,
                        end_line=end,
                    )
                )
            return

        if node.type in ("class_declaration", "abstract_class_declaration"):
            name_node = _get_field(node, "name")
            if name_node is None:
                return
            base = name_node.text.decode("utf-8", errors="replace")
            full = f"{class_prefix}.{base}" if class_prefix else base
            start, end = _ts_lines(node)
            symbols.append(
                Symbol(
                    name=full,
                    kind="class",
                    is_definition=True,
                    start_line=start,
                    end_line=end,
                )
            )
            body = _get_field(node, "body")
            if body is not None:
                for child in body.children:
                    if child.is_named:
                        visit(child, full)
            return

        if node.type in (
            "method_definition",
            "method_signature",
            "abstract_method_signature",
        ):
            name_node = _get_field(node, "name")
            if name_node is not None and class_prefix:
                base = name_node.text.decode("utf-8", errors="replace")
                full = f"{class_prefix}.{base}"
                start, end = _ts_lines(node)
                symbols.append(
                    Symbol(
                        name=full,
                        kind="method",
                        is_definition=node.type == "method_definition",
                        start_line=start,
                        end_line=end,
                    )
                )
            return

        if node.type in ("interface_declaration", "type_alias_declaration"):
            name_node = _get_field(node, "name")
            if name_node is not None:
                base = name_node.text.decode("utf-8", errors="replace")
                full = f"{class_prefix}.{base}" if class_prefix else base
                start, end = _ts_lines(node)
                symbols.append(
                    Symbol(
                        name=full,
                        kind="class"
                        if node.type == "interface_declaration"
                        else "typedef",
                        is_definition=True,
                        start_line=start,
                        end_line=end,
                    )
                )
            return

        for child in node.children:
            if child.is_named:
                visit(child, class_prefix)

    visit(root)
    return symbols


# ── C/C++ extractor ───────────────────────────────────────────────
def _cpp_declarator_name(node) -> str | None:
    """Walk a declarator to extract the human-readable name.

    Handles function/pointer/reference/qualified declarators recursively.
    Returns 'ns::Foo::bar' for qualified names.
    """
    if node is None:
        return None
    t = node.type

    if t == "identifier" or t == "field_identifier" or t == "type_identifier":
        return node.text.decode("utf-8", errors="replace")

    if t == "qualified_identifier":
        # tree-sitter-cpp does not expose stable scope/name fields on
        # qualified_identifier (children are positional). Use the node's
        # raw text — it already reads as ``ns::Foo::bar``.
        return node.text.decode("utf-8", errors="replace")

    if t == "destructor_name":
        # ~Foo
        return node.text.decode("utf-8", errors="replace")

    if t == "operator_name":
        return node.text.decode("utf-8", errors="replace")

    if t == "template_function":
        # template_function has a 'name' field
        name = _get_field(node, "name")
        if name is not None:
            return _cpp_declarator_name(name)

    if t in (
        "function_declarator",
        "pointer_declarator",
        "reference_declarator",
        "parenthesized_declarator",
        "array_declarator",
        "init_declarator",
    ):
        decl = _get_field(node, "declarator")
        if decl is not None:
            return _cpp_declarator_name(decl)

    # Fall through: scan children for the first declarator-like child
    for child in node.children:
        if child.is_named:
            inner = _cpp_declarator_name(child)
            if inner:
                return inner
    return None


def _extract_cpp(root, source: bytes) -> list[Symbol]:
    symbols: list[Symbol] = []

    def visit(node, scope_prefix: str = "") -> None:
        t = node.type

        if t == "namespace_definition":
            name_node = _get_field(node, "name")
            if name_node is None:
                # Anonymous namespace — descend without adding a prefix.
                body = _get_field(node, "body")
                if body is not None:
                    for child in body.children:
                        if child.is_named:
                            visit(child, scope_prefix)
                return
            base = name_node.text.decode("utf-8", errors="replace")
            full = f"{scope_prefix}::{base}" if scope_prefix else base
            start, end = _ts_lines(node)
            symbols.append(
                Symbol(
                    name=full,
                    kind="namespace",
                    is_definition=True,
                    start_line=start,
                    end_line=end,
                )
            )
            body = _get_field(node, "body")
            if body is not None:
                for child in body.children:
                    if child.is_named:
                        visit(child, full)
            return

        if t in ("class_specifier", "struct_specifier", "union_specifier"):
            name_node = _get_field(node, "name")
            if name_node is None:
                return
            base = name_node.text.decode("utf-8", errors="replace")
            full = f"{scope_prefix}::{base}" if scope_prefix else base
            start, end = _ts_lines(node)
            kind = (
                "class"
                if t == "class_specifier"
                else ("struct" if t == "struct_specifier" else "struct")
            )
            symbols.append(
                Symbol(
                    name=full,
                    kind=kind,
                    is_definition=True,
                    start_line=start,
                    end_line=end,
                )
            )
            body = _get_field(node, "body")
            if body is not None:
                for child in body.children:
                    if child.is_named:
                        visit(child, full)
            return

        if t == "enum_specifier":
            name_node = _get_field(node, "name")
            if name_node is not None:
                base = name_node.text.decode("utf-8", errors="replace")
                full = f"{scope_prefix}::{base}" if scope_prefix else base
                start, end = _ts_lines(node)
                symbols.append(
                    Symbol(
                        name=full,
                        kind="enum",
                        is_definition=True,
                        start_line=start,
                        end_line=end,
                    )
                )
            return

        if t == "type_definition":
            # typedef int MyInt; — declarators field carries the names.
            for child in node.children:
                if child.type == "type_identifier":
                    base = child.text.decode("utf-8", errors="replace")
                    full = f"{scope_prefix}::{base}" if scope_prefix else base
                    start, end = _ts_lines(node)
                    symbols.append(
                        Symbol(
                            name=full,
                            kind="typedef",
                            is_definition=True,
                            start_line=start,
                            end_line=end,
                        )
                    )
            return

        if t == "function_definition":
            decl = _get_field(node, "declarator")
            base = _cpp_declarator_name(decl)
            if base is not None:
                # If the declarator already contains :: (qualified), keep it as-is;
                # otherwise prepend the surrounding scope.
                if "::" in base or not scope_prefix:
                    full = base
                else:
                    full = f"{scope_prefix}::{base}"
                start, end = _ts_lines(node)
                symbols.append(
                    Symbol(
                        name=full,
                        kind="method" if "::" in full else "function",
                        is_definition=True,
                        start_line=start,
                        end_line=end,
                    )
                )
            return

        if t in ("declaration", "field_declaration"):
            # A declaration is a function declaration only if the inner
            # declarator chain reaches a function_declarator. Otherwise it's
            # a variable / member field — skip for v1.
            if not _has_function_declarator(node):
                return
            decl = _get_field(node, "declarator")
            base = _cpp_declarator_name(decl)
            if base is not None:
                if "::" in base or not scope_prefix:
                    full = base
                else:
                    full = f"{scope_prefix}::{base}"
                start, end = _ts_lines(node)
                symbols.append(
                    Symbol(
                        name=full,
                        kind="method" if "::" in full or scope_prefix else "function",
                        is_definition=False,  # declaration only
                        start_line=start,
                        end_line=end,
                    )
                )
            return

        if t == "preproc_def":
            name_node = _get_field(node, "name")
            if name_node is not None:
                base = name_node.text.decode("utf-8", errors="replace")
                start, end = _ts_lines(node)
                symbols.append(
                    Symbol(
                        name=base,
                        kind="macro",
                        is_definition=True,
                        start_line=start,
                        end_line=end,
                    )
                )
            return

        if t == "preproc_function_def":
            name_node = _get_field(node, "name")
            if name_node is not None:
                base = name_node.text.decode("utf-8", errors="replace")
                start, end = _ts_lines(node)
                symbols.append(
                    Symbol(
                        name=base,
                        kind="macro",
                        is_definition=True,
                        start_line=start,
                        end_line=end,
                    )
                )
            return

        if t == "template_declaration":
            # Walk the wrapped declaration with the same scope.
            for child in node.children:
                if child.is_named:
                    visit(child, scope_prefix)
            return

        if t == "linkage_specification":
            # extern "C" { ... } — descend.
            body = _get_field(node, "body")
            if body is not None:
                for child in body.children:
                    if child.is_named:
                        visit(child, scope_prefix)
            return

        for child in node.children:
            if child.is_named:
                visit(child, scope_prefix)

    visit(root)
    return symbols


def _has_function_declarator(node) -> bool:
    """True if the declaration's declarator chain ends in function_declarator."""
    if node is None:
        return False
    if node.type == "function_declarator":
        return True
    decl = _get_field(node, "declarator")
    if decl is not None:
        return _has_function_declarator(decl)
    for child in node.children:
        if child.is_named and child.type in (
            "pointer_declarator",
            "reference_declarator",
            "parenthesized_declarator",
            "init_declarator",
            "function_declarator",
        ):
            if _has_function_declarator(child):
                return True
    return False


# ── Markdown extractor ────────────────────────────────────────────
def _extract_markdown(root, source: bytes) -> list[Symbol]:
    """Return atx_headings as symbols. Heading body span ends right before
    the next heading of the same or higher level (i.e. the section body)."""
    headings: list[tuple[int, int, str]] = []  # (line, level, name)

    def collect(node) -> None:
        if node.type == "atx_heading":
            level = 0
            for child in node.children:
                if child.type.startswith("atx_h") and child.type.endswith("_marker"):
                    # atx_h1_marker .. atx_h6_marker
                    try:
                        level = int(child.type[len("atx_h") : -len("_marker")])
                    except ValueError:
                        level = 0
                    break
            text = _node_text(node, source).strip().splitlines()[0]
            line = node.start_point[0] + 1
            headings.append((line, level, text))
        for child in node.children:
            if child.is_named:
                collect(child)

    collect(root)

    # Compute end_line for each heading: line just before the next heading at
    # the same or higher level (smaller or equal level number). If none, EOF.
    total_lines = len(source.splitlines())
    symbols: list[Symbol] = []
    for i, (line, level, text) in enumerate(headings):
        end_line = total_lines
        for j in range(i + 1, len(headings)):
            next_line, next_level, _ = headings[j]
            if next_level <= level:
                end_line = next_line - 1
                break
        symbols.append(
            Symbol(
                name=text,
                kind="heading",
                is_definition=True,
                start_line=line,
                end_line=end_line,
            )
        )
    return symbols


# ── Dispatcher ────────────────────────────────────────────────────
_EXTRACTORS = {
    "python": _extract_python,
    "javascript": _extract_js,
    "typescript": _extract_js,
    "tsx": _extract_js,
    "cpp": _extract_cpp,
    "markdown": _extract_markdown,
}


def _parse(path: Path, language: str) -> tuple[object, bytes] | None:
    """Parse the file and return (root_node, source_bytes), or None on error."""
    try:
        source = path.read_bytes()
    except OSError:
        return None
    try:
        from tree_sitter_language_pack import get_parser

        parser = get_parser(language)
        tree = parser.parse(source)
        return tree.root_node, source
    except Exception:
        return None


def _extract(path: Path, language: str) -> list[Symbol] | None:
    parsed = _parse(path, language)
    if parsed is None:
        return None
    root, source = parsed
    extractor = _EXTRACTORS.get(language)
    if extractor is None:
        return None
    return extractor(root, source)


# ── Public tool ───────────────────────────────────────────────────
_UNSUPPORTED_EXT_MSG = (
    "unsupported file extension: {ext}. "
    "Supported: .py, .js/.jsx/.mjs/.cjs, .ts/.tsx, "
    ".c/.cc/.cpp/.cxx/.h/.hh/.hpp/.hxx, .md/.markdown. "
    "Use read_file for other formats."
)


def _resolve_path_and_language(path_str: str) -> tuple[Path, str] | ToolResult:
    """Validate path + detect language. Returns (path, language) or a
    ToolResult error to propagate."""
    path = Path(path_str).expanduser()
    if not path.is_file():
        return ToolResult(False, error=f"file not found: {path}")
    language = _detect_language(path)
    if language is None:
        return ToolResult(False, error=_UNSUPPORTED_EXT_MSG.format(ext=path.suffix))
    return (path, language)


def _do_list(path: Path, language: str) -> ToolResult:
    symbols = _extract(path, language)
    if symbols is None:
        return ToolResult(False, error=f"could not parse {path}")
    if not symbols:
        return ToolResult(True, output="(no symbols found)")

    lines = []
    for s in symbols:
        if s.start_line == s.end_line:
            range_str = f":{s.start_line}"
        else:
            range_str = f":{s.start_line}-{s.end_line}"
        marker = "" if s.is_definition else " (decl)"
        lines.append(f"{s.name} ({s.kind}){marker} {range_str}")
    return ToolResult(True, output="\n".join(lines))


def _do_fetch(path: Path, language: str, name: str) -> ToolResult:
    symbols = _extract(path, language)
    if symbols is None:
        return ToolResult(False, error=f"could not parse {path}")

    matches = [s for s in symbols if s.name == name]
    if not matches:
        # Friendly hint: list candidates that share the leaf name.
        leaf = name.split(".")[-1].split("::")[-1]
        candidates = [s.name for s in symbols if s.name.endswith(leaf)]
        hint = ""
        if candidates:
            preview = ", ".join(candidates[:5])
            hint = f" (similar names: {preview})"
        return ToolResult(False, error=f"symbol not found: {name}{hint}")

    # Prefer definitions over declarations.
    definitions = [s for s in matches if s.is_definition]
    chosen = definitions[0] if definitions else matches[0]

    try:
        all_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        return ToolResult(False, error=f"read error: {e}")

    body_lines = all_lines[chosen.start_line - 1 : chosen.end_line]
    header = (
        f"# {chosen.name} ({chosen.kind})"
        f" :{chosen.start_line}-{chosen.end_line}"
        + ("" if chosen.is_definition else " [declaration]")
    )
    return ToolResult(True, output=header + "\n" + "\n".join(body_lines))


def tool_read_symbols(action_input: dict) -> ToolResult:
    """Read structural symbols from a source/markdown file using tree-sitter.

    Modes:
        - ``mode='list'`` (default) — outline of the file: every function,
          class, method, struct/enum/typedef, ``#define``, or markdown
          heading, with its line range. Use as a structure-aware
          alternative to ``read_file`` with ``stat=true``.
        - ``mode='fetch'`` — body of a single named symbol. Pair the
          ``name`` argument with a name shown by ``mode='list'``. When a
          name matches both a declaration (e.g. a ``.h`` prototype) and a
          definition, the definition is returned.

    Languages auto-detected from extension (see ``_EXT_TO_LANG``). C and
    C++ both parsed with the C++ grammar.
    """
    if not isinstance(action_input, dict):
        return ToolResult(False, error="action_input must be an object")

    path_str = action_input.get("path")
    if not path_str:
        return ToolResult(False, error="path is required")

    resolved = _resolve_path_and_language(path_str)
    if isinstance(resolved, ToolResult):
        return resolved
    path, language = resolved

    mode = action_input.get("mode", "list")
    if mode == "list":
        return _do_list(path, language)
    if mode == "fetch":
        name = action_input.get("name")
        if not name:
            return ToolResult(False, error="name is required for mode='fetch'")
        return _do_fetch(path, language, name)
    return ToolResult(False, error=f"unknown mode '{mode}'. Use 'list' or 'fetch'.")
