"""Tests for the read_symbols tree-sitter tool.

Coverage targets — one fixture per language we claim to support, exercising
both ``mode='list'`` and ``mode='fetch'`` plus the cross-cutting behaviors
(definition wins over declaration, name-not-found hint, unsupported
extension fallback, malformed input).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import re

from agent_cli.tools.symbols import (
    _EXT_TO_LANG,
    get_supported_extensions,
    tool_read_symbols,
)


# ── Helpers ───────────────────────────────────────────────────────────
def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content)
    return p


def _names(output: str) -> list[str]:
    """Pull just the symbol names out of mode='list' output."""
    out = []
    for line in output.splitlines():
        if not line.strip():
            continue
        # "<name> (<kind>)[ (decl)] :<lines>"
        out.append(line.split(" (")[0])
    return out


# ── Mode dispatch ─────────────────────────────────────────────────────
class TestModeDispatch:
    def test_default_mode_is_list(self, tmp_path):
        p = _write(tmp_path, "a.py", "def f(): pass\n")
        r = tool_read_symbols({"path": str(p)})
        assert r.success
        assert "f" in r.output

    def test_explicit_list_mode(self, tmp_path):
        p = _write(tmp_path, "a.py", "def f(): pass\n")
        r = tool_read_symbols({"path": str(p), "mode": "list"})
        assert r.success
        assert "f" in r.output

    def test_unknown_mode(self, tmp_path):
        p = _write(tmp_path, "a.py", "def f(): pass\n")
        r = tool_read_symbols({"path": str(p), "mode": "wat"})
        assert not r.success
        assert "unknown mode" in r.error

    def test_fetch_requires_name(self, tmp_path):
        p = _write(tmp_path, "a.py", "def f(): pass\n")
        r = tool_read_symbols({"path": str(p), "mode": "fetch"})
        assert not r.success
        assert "name is required" in r.error

    def test_path_required(self):
        r = tool_read_symbols({})
        assert not r.success
        assert "path is required" in r.error

    def test_action_input_must_be_dict(self):
        r = tool_read_symbols("not a dict")
        assert not r.success
        assert "object" in r.error


# ── Path / language resolution ────────────────────────────────────────
class TestPathResolution:
    def test_missing_file(self, tmp_path):
        r = tool_read_symbols({"path": str(tmp_path / "nope.py")})
        assert not r.success
        assert "file not found" in r.error

    def test_unsupported_extension(self, tmp_path):
        p = _write(tmp_path, "data.bin", "binary\n")
        r = tool_read_symbols({"path": str(p)})
        assert not r.success
        assert "unsupported file extension" in r.error
        # The error message should redirect the model to read_file.
        assert "read_file" in r.error


# ── Python ────────────────────────────────────────────────────────────
PYTHON_SAMPLE = """\
def hello():
    return 1


class Foo:
    def bar(self):
        return 2

    def baz(self, x):
        return x * 2


@some_decorator
def decorated():
    pass


class Outer:
    class Inner:
        def deeply(self):
            pass
"""


class TestPython:
    def test_list_top_level(self, tmp_path):
        p = _write(tmp_path, "a.py", PYTHON_SAMPLE)
        r = tool_read_symbols({"path": str(p), "mode": "list"})
        assert r.success
        names = _names(r.output)
        assert "hello" in names
        assert "Foo" in names
        assert "decorated" in names

    def test_list_methods_use_dot_notation(self, tmp_path):
        p = _write(tmp_path, "a.py", PYTHON_SAMPLE)
        names = _names(tool_read_symbols({"path": str(p)}).output)
        assert "Foo.bar" in names
        assert "Foo.baz" in names

    def test_list_nested_class(self, tmp_path):
        p = _write(tmp_path, "a.py", PYTHON_SAMPLE)
        names = _names(tool_read_symbols({"path": str(p)}).output)
        assert "Outer" in names
        assert "Outer.Inner" in names
        assert "Outer.Inner.deeply" in names

    def test_fetch_method(self, tmp_path):
        p = _write(tmp_path, "a.py", PYTHON_SAMPLE)
        r = tool_read_symbols({"path": str(p), "mode": "fetch", "name": "Foo.bar"})
        assert r.success
        assert "def bar(self):" in r.output
        assert "return 2" in r.output

    def test_fetch_class_returns_full_body(self, tmp_path):
        p = _write(tmp_path, "a.py", PYTHON_SAMPLE)
        r = tool_read_symbols({"path": str(p), "mode": "fetch", "name": "Foo"})
        assert r.success
        assert "def bar(self):" in r.output
        assert "def baz(self, x):" in r.output

    def test_fetch_decorated_function(self, tmp_path):
        p = _write(tmp_path, "a.py", PYTHON_SAMPLE)
        r = tool_read_symbols({"path": str(p), "mode": "fetch", "name": "decorated"})
        assert r.success
        assert "def decorated():" in r.output


# ── JavaScript / TypeScript ───────────────────────────────────────────
TS_SAMPLE = """\
export function hello(): number {
  return 1;
}

export class Foo {
  bar(): void {}
  baz(x: number): number { return x * 2; }
}

interface IShape {
  area(): number;
}

type Pair = [number, number];
"""


class TestTypeScript:
    def test_list_includes_function_and_class(self, tmp_path):
        p = _write(tmp_path, "a.ts", TS_SAMPLE)
        names = _names(tool_read_symbols({"path": str(p)}).output)
        assert "hello" in names
        assert "Foo" in names
        assert "Foo.bar" in names
        assert "Foo.baz" in names

    def test_list_includes_interface_and_type_alias(self, tmp_path):
        p = _write(tmp_path, "a.ts", TS_SAMPLE)
        names = _names(tool_read_symbols({"path": str(p)}).output)
        assert "IShape" in names
        assert "Pair" in names

    def test_fetch_method(self, tmp_path):
        p = _write(tmp_path, "a.ts", TS_SAMPLE)
        r = tool_read_symbols({"path": str(p), "mode": "fetch", "name": "Foo.baz"})
        assert r.success
        assert "baz(x: number)" in r.output


JS_SAMPLE = """\
function plain() {
  return 1;
}

class Container {
  constructor(x) { this.x = x; }
  get value() { return this.x; }
}
"""


class TestJavaScript:
    def test_list(self, tmp_path):
        p = _write(tmp_path, "a.js", JS_SAMPLE)
        names = _names(tool_read_symbols({"path": str(p)}).output)
        assert "plain" in names
        assert "Container" in names
        # Constructors and getters surface as methods under the class.
        assert any(n.startswith("Container.") for n in names)


# ── C / C++ ───────────────────────────────────────────────────────────
CPP_SAMPLE = """\
namespace ns {
class Foo {
public:
    void bar();
    int baz() { return 0; }
};
}

void ns::Foo::bar() {
    // out-of-class definition
}

#define MAX 100
#define SQUARE(x) ((x)*(x))

struct Point { int x, y; };
typedef int MyInt;
enum Color { RED, GREEN, BLUE };
"""


class TestCpp:
    def test_namespace_and_class(self, tmp_path):
        p = _write(tmp_path, "a.cpp", CPP_SAMPLE)
        names = _names(tool_read_symbols({"path": str(p)}).output)
        assert "ns" in names
        assert "ns::Foo" in names

    def test_class_methods_use_double_colon(self, tmp_path):
        p = _write(tmp_path, "a.cpp", CPP_SAMPLE)
        names = _names(tool_read_symbols({"path": str(p)}).output)
        assert "ns::Foo::bar" in names
        assert "ns::Foo::baz" in names

    def test_macros_listed(self, tmp_path):
        p = _write(tmp_path, "a.cpp", CPP_SAMPLE)
        names = _names(tool_read_symbols({"path": str(p)}).output)
        assert "MAX" in names
        assert "SQUARE" in names

    def test_struct_typedef_enum(self, tmp_path):
        p = _write(tmp_path, "a.cpp", CPP_SAMPLE)
        names = _names(tool_read_symbols({"path": str(p)}).output)
        assert "Point" in names
        assert "MyInt" in names
        assert "Color" in names

    def test_definition_wins_over_declaration(self, tmp_path):
        """`bar` has both a declaration in the class and a separate
        out-of-class definition. fetch should return the definition."""
        p = _write(tmp_path, "a.cpp", CPP_SAMPLE)
        r = tool_read_symbols({"path": str(p), "mode": "fetch", "name": "ns::Foo::bar"})
        assert r.success
        # Definition body contains the comment; declaration does not.
        assert "out-of-class definition" in r.output
        # Header should NOT mark it as declaration-only.
        assert "[declaration]" not in r.output

    def test_fetch_macro(self, tmp_path):
        p = _write(tmp_path, "a.cpp", CPP_SAMPLE)
        r = tool_read_symbols({"path": str(p), "mode": "fetch", "name": "SQUARE"})
        assert r.success
        assert "#define SQUARE(x) ((x)*(x))" in r.output

    def test_c_extension_uses_cpp_grammar(self, tmp_path):
        """A .c file with C-only code should still parse via the C++
        grammar (per the unified-parser decision)."""
        c_src = """\
#include <stdio.h>
int add(int a, int b) { return a + b; }
struct Vec { float x, y, z; };
"""
        p = _write(tmp_path, "lib.c", c_src)
        names = _names(tool_read_symbols({"path": str(p)}).output)
        assert "add" in names
        assert "Vec" in names

    def test_header_extension_uses_cpp_grammar(self, tmp_path):
        h_src = """\
namespace api {
class Service {
public:
    int handle(int);
};
}
"""
        p = _write(tmp_path, "svc.h", h_src)
        names = _names(tool_read_symbols({"path": str(p)}).output)
        assert "api" in names
        assert "api::Service" in names

    def test_header_only_declarations_marked(self, tmp_path):
        """When a symbol exists only as a declaration, fetch must return
        that declaration and label it [declaration]."""
        h_src = """\
namespace api {
class Service {
public:
    int handle(int);
};
}
"""
        p = _write(tmp_path, "svc.h", h_src)
        r = tool_read_symbols(
            {"path": str(p), "mode": "fetch", "name": "api::Service::handle"}
        )
        assert r.success
        assert "[declaration]" in r.output


# ── Markdown ──────────────────────────────────────────────────────────
MD_SAMPLE = """\
# Title

intro text.

## Setup

Install steps.

### Sub

detail.

## Usage

Run it.
"""


class TestMarkdown:
    def test_list_headings(self, tmp_path):
        p = _write(tmp_path, "doc.md", MD_SAMPLE)
        names = _names(tool_read_symbols({"path": str(p)}).output)
        assert "# Title" in names
        assert "## Setup" in names
        assert "### Sub" in names
        assert "## Usage" in names

    def test_fetch_section_stops_at_next_same_or_higher_heading(self, tmp_path):
        """fetch '## Setup' should include 'Install steps' and the nested
        '### Sub' subsection, but NOT spill into '## Usage'."""
        p = _write(tmp_path, "doc.md", MD_SAMPLE)
        r = tool_read_symbols({"path": str(p), "mode": "fetch", "name": "## Setup"})
        assert r.success
        assert "Install steps." in r.output
        assert "### Sub" in r.output
        assert "detail." in r.output
        assert "Run it." not in r.output

    def test_fetch_subsection(self, tmp_path):
        p = _write(tmp_path, "doc.md", MD_SAMPLE)
        r = tool_read_symbols({"path": str(p), "mode": "fetch", "name": "### Sub"})
        assert r.success
        assert "detail." in r.output
        assert "Run it." not in r.output


# ── Cross-cutting: fetch errors ───────────────────────────────────────
class TestFetchErrors:
    def test_unknown_name_lists_similar(self, tmp_path):
        p = _write(tmp_path, "a.py", "class Foo:\n    def bar(self): pass\n")
        r = tool_read_symbols({"path": str(p), "mode": "fetch", "name": "bar"})
        assert not r.success
        # Bare 'bar' is not a top-level symbol, but the leaf-name suggestion
        # should mention 'Foo.bar'.
        assert "similar names" in r.error
        assert "Foo.bar" in r.error

    def test_unknown_name_no_similar(self, tmp_path):
        p = _write(tmp_path, "a.py", "class Foo:\n    pass\n")
        r = tool_read_symbols(
            {"path": str(p), "mode": "fetch", "name": "totally_absent"}
        )
        assert not r.success
        assert "symbol not found" in r.error


# ── Robustness ────────────────────────────────────────────────────────
class TestRobustness:
    def test_empty_file(self, tmp_path):
        p = _write(tmp_path, "empty.py", "")
        r = tool_read_symbols({"path": str(p)})
        assert r.success
        # Empty file has no symbols; tool returns the explicit marker
        # rather than empty output so the model gets a clear signal.
        assert "no symbols" in r.output

    def test_partial_garbage_still_parses(self, tmp_path):
        """tree-sitter is error-tolerant. A file with a syntax error should
        still surface the symbols around the error."""
        src = """\
def good():
    return 1

def broken(:
    pass

def also_good():
    return 2
"""
        p = _write(tmp_path, "weird.py", src)
        r = tool_read_symbols({"path": str(p)})
        assert r.success
        names = _names(r.output)
        # At least one of the well-formed functions should still appear.
        assert "good" in names or "also_good" in names


_HASHLINE_RE = re.compile(r"^\d+#[A-Z]{2}:")


# ── Hashline output (fetch) ───────────────────────────────────────────
class TestFetchHashlineFormat:
    """fetch returns hashline-formatted bodies (LINE#HASH:content) so the
    model can pipe results straight into edit_file without a separate
    read_file call. The header line ('# Foo.bar (method) :start-end')
    is plain text — only the body lines carry hashlines."""

    def _body_lines(self, output: str) -> list[str]:
        # Drop the header (first line starts with '# ').
        lines = output.splitlines()
        assert lines, "fetch output should not be empty"
        assert lines[0].startswith("# "), f"first line is the header: {lines[0]!r}"
        return lines[1:]

    def test_python_fetch_method_is_hashlined(self, tmp_path):
        p = _write(tmp_path, "a.py", PYTHON_SAMPLE)
        out = tool_read_symbols(
            {"path": str(p), "mode": "fetch", "name": "Foo.bar"}
        ).output
        body = self._body_lines(out)
        assert body, "method body should not be empty"
        for line in body:
            assert _HASHLINE_RE.match(line), f"not hashlined: {line!r}"

    def test_cpp_fetch_macro_is_hashlined(self, tmp_path):
        p = _write(tmp_path, "a.cpp", CPP_SAMPLE)
        out = tool_read_symbols(
            {"path": str(p), "mode": "fetch", "name": "SQUARE"}
        ).output
        body = self._body_lines(out)
        assert body
        for line in body:
            assert _HASHLINE_RE.match(line), f"not hashlined: {line!r}"

    def test_markdown_fetch_section_is_hashlined(self, tmp_path):
        p = _write(tmp_path, "doc.md", MD_SAMPLE)
        out = tool_read_symbols(
            {"path": str(p), "mode": "fetch", "name": "## Setup"}
        ).output
        body = self._body_lines(out)
        assert body
        for line in body:
            assert _HASHLINE_RE.match(line), f"not hashlined: {line!r}"

    def test_hashline_numbers_match_source_line_numbers(self, tmp_path):
        """Hashline prefix line numbers must match the symbol's actual
        line range — otherwise edit_file would target the wrong region."""
        src = "\n".join(
            ["# line 1", "# line 2", "def hello():", "    return 1", "# line 5"]
        )
        p = _write(tmp_path, "a.py", src + "\n")
        out = tool_read_symbols(
            {"path": str(p), "mode": "fetch", "name": "hello"}
        ).output
        # Header tells us the range; body line numbers must match.
        header = out.splitlines()[0]
        # e.g. "# hello (function) :3-4"
        m = re.search(r":(\d+)-(\d+)", header)
        assert m, f"no range in header: {header!r}"
        start = int(m.group(1))
        body = self._body_lines(out)
        for offset, line in enumerate(body):
            expected = start + offset
            assert line.startswith(f"{expected}#"), (
                f"line {offset} should start with {expected}#, got {line!r}"
            )


# ── Single source of truth: extension list ───────────────────────────
class TestSupportedExtensions:
    """The error message and the system prompt's inline guide both
    derive their extension list from get_supported_extensions(). Adding
    a grammar to _EXT_TO_LANG should automatically propagate to both
    surfaces — these tests pin that contract."""

    def test_returns_sorted_unique_list(self):
        exts = get_supported_extensions()
        assert exts == sorted(exts)
        assert len(exts) == len(set(exts))

    def test_covers_every_ext_in_table(self):
        exts = set(get_supported_extensions())
        assert exts == set(_EXT_TO_LANG.keys())

    def test_unsupported_error_lists_supported_exts(self, tmp_path):
        """The error message shown when the model passes an unknown
        extension should enumerate the supported extensions verbatim
        from get_supported_extensions()."""
        p = _write(tmp_path, "data.bin", "x")
        err = tool_read_symbols({"path": str(p)}).error
        for ext in get_supported_extensions():
            assert ext in err, f"{ext} missing from error: {err!r}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
