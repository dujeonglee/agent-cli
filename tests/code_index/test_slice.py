"""Tests for ``cmd_slice`` markdown rendering (DESIGN §12.4).

Covers the LLM-context "slice" output: definition rendering, transitive
callee / caller expansion, type and macro context, ``max_bytes``
truncation, ``depth`` parameter behaviour, the no-symbol fallback
string, and the definition-vs-declaration picker preference.

The Python fixture exercises ``with_callees`` / ``with_callers`` /
``depth``. The C fixture exercises ``with_types`` (struct refs) and
``with_macros`` (``DBG`` function-like macro) — Python's walker does
not emit ``kind='type'`` refs, so those modes are no-ops there.
"""

from __future__ import annotations

import pytest

from agent_cli.code_index import cmd_slice
from tests.code_index.helpers import build_fixture, build_tree


@pytest.fixture(scope="module")
def python_index():
    store, by_name = build_fixture("python")
    return store, by_name


@pytest.fixture(scope="module")
def c_index():
    store, by_name = build_fixture("c")
    return store, by_name


@pytest.fixture(scope="module")
def chain_index(tmp_path_factory):
    """a -> b -> c -> d chain for depth parameter testing."""
    root = tmp_path_factory.mktemp("slice_chain")
    (root / "chain.py").write_text(
        "def d():\n    return 4\n\n\n"
        "def c():\n    return d()\n\n\n"
        "def b():\n    return c()\n\n\n"
        "def a():\n    return b()\n"
    )
    return build_tree(root)


class TestSliceBasic:
    """Definition rendering and top-level header."""

    def test_helper_body_appears(self, python_index):
        store, _ = python_index
        out = cmd_slice(
            store,
            "helper",
            with_callees=False,
            with_callers=False,
            with_types=False,
            with_macros=False,
            depth=1,
            max_bytes=0,
        )
        # The helper body in the fixture is `return x * 2`
        assert "def helper" in out
        assert "return x * 2" in out

    def test_top_level_header_present(self, python_index):
        store, _ = python_index
        out = cmd_slice(
            store,
            "helper",
            with_callees=False,
            with_callers=False,
            with_types=False,
            with_macros=False,
            depth=1,
            max_bytes=0,
        )
        assert out.startswith("# Slice:")
        assert "# Slice: helper" in out

    def test_no_symbol_returns_signal_string_not_raise(self, python_index):
        store, _ = python_index
        out = cmd_slice(
            store,
            "definitely_nonexistent_name_xyz",
            with_callees=False,
            with_callers=False,
            with_types=False,
            with_macros=False,
            depth=1,
            max_bytes=0,
        )
        assert isinstance(out, str)
        assert "no symbol" in out
        assert "definitely_nonexistent_name_xyz" in out


class TestSliceCallees:
    """``with_callees=True`` pulls in callee bodies."""

    def test_callee_body_appears(self, python_index):
        store, _ = python_index
        # process() calls helper() — slicing process with callees should
        # include helper's body.
        out = cmd_slice(
            store,
            "process",
            with_callees=True,
            with_callers=False,
            with_types=False,
            with_macros=False,
            depth=1,
            max_bytes=0,
        )
        assert "## Callees" in out
        assert "def helper" in out
        assert "return x * 2" in out


class TestSliceCallers:
    """``with_callers=True`` pulls in caller bodies."""

    def test_caller_body_appears(self, python_index):
        store, _ = python_index
        # helper() is called by process() — slicing helper with callers
        # should include process's body.
        out = cmd_slice(
            store,
            "helper",
            with_callees=False,
            with_callers=True,
            with_types=False,
            with_macros=False,
            depth=1,
            max_bytes=0,
        )
        assert "## Callers" in out
        assert "process" in out


class TestSliceTypes:
    """``with_types=True`` includes referenced type definitions.

    Uses the C fixture: ``compute(struct point *p)`` references the
    ``point`` struct, which lives in sample.h. Python doesn't emit
    ``kind='type'`` refs, so the option is meaningless there.
    """

    def test_type_definition_appears(self, c_index):
        store, _ = c_index
        out = cmd_slice(
            store,
            "compute",
            with_callees=False,
            with_callers=False,
            with_types=True,
            with_macros=False,
            depth=1,
            max_bytes=0,
        )
        assert "## Types referenced" in out
        # struct point body lives at sample.h:4-7
        assert "struct point" in out
        assert "int x;" in out


class TestSliceMacros:
    """``with_macros=True`` includes function-like macros used by the target.

    C fixture: ``compute`` invokes the ``DBG(fmt, ...)`` macro.
    """

    def test_macro_definition_appears(self, c_index):
        store, _ = c_index
        out = cmd_slice(
            store,
            "compute",
            with_callees=False,
            with_callers=False,
            with_types=False,
            with_macros=True,
            depth=1,
            max_bytes=0,
        )
        assert "## Macros used" in out
        assert "#define DBG" in out


class TestSliceMaxBytes:
    """``max_bytes`` truncates the body portion and appends a marker."""

    def test_truncation_marker_appears(self, python_index):
        store, _ = python_index
        # Render the same slice without max_bytes to confirm the untruncated
        # output is larger than the cap, so the marker test is meaningful.
        full = cmd_slice(
            store,
            "helper",
            with_callees=True,
            with_callers=True,
            with_types=False,
            with_macros=False,
            depth=2,
            max_bytes=0,
        )
        assert len(full.encode("utf-8")) > 200
        out = cmd_slice(
            store,
            "helper",
            with_callees=True,
            with_callers=True,
            with_types=False,
            with_macros=False,
            depth=2,
            max_bytes=200,
        )
        # The implementation slices `text.encode("utf-8")[:max_bytes]` then
        # decodes (errors='ignore') then appends "\n\n_[truncated to N bytes]_\n".
        # So the truncated body is at most max_bytes bytes; the appended marker
        # and "\n\n" separator push the total slightly past max_bytes.
        marker = "_[truncated to 200 bytes]_"
        assert marker in out
        marker_idx = out.index("\n\n" + marker)
        body_bytes = out[:marker_idx].encode("utf-8")
        assert len(body_bytes) <= 200

    def test_max_bytes_zero_does_not_truncate(self, python_index):
        store, _ = python_index
        out = cmd_slice(
            store,
            "helper",
            with_callees=False,
            with_callers=False,
            with_types=False,
            with_macros=False,
            depth=1,
            max_bytes=0,
        )
        assert "_[truncated" not in out


class TestSliceDepth:
    """``depth`` controls how far the callee/caller BFS walks."""

    def test_depth_two_strictly_more_than_depth_one(self, chain_index):
        # a -> b -> c -> d in the chain fixture.
        out_d1 = cmd_slice(
            chain_index,
            "a",
            with_callees=True,
            with_callers=False,
            with_types=False,
            with_macros=False,
            depth=1,
            max_bytes=0,
        )
        out_d2 = cmd_slice(
            chain_index,
            "a",
            with_callees=True,
            with_callers=False,
            with_types=False,
            with_macros=False,
            depth=2,
            max_bytes=0,
        )
        # depth=1 reaches b; depth=2 also reaches c
        assert "def b" in out_d1
        assert "def c" not in out_d1
        assert "def b" in out_d2
        assert "def c" in out_d2


class TestSlicePicksDefinition:
    """When a name has both a declaration and a definition the picker
    prefers the definition body. C fixture: ``compute`` has a prototype
    in sample.h (``is_definition=False``) and a definition in sample.c
    (``is_definition=True``) — the slice should render the .c body.
    """

    def test_definition_wins_over_prototype(self, c_index):
        store, _ = c_index
        out = cmd_slice(
            store,
            "compute",
            with_callees=False,
            with_callers=False,
            with_types=False,
            with_macros=False,
            depth=1,
            max_bytes=0,
        )
        # The definition header reads "compute  (function)  — sample.c:12-15".
        # The prototype header would read "— sample.h:13-13".
        # Look at the Definition block specifically.
        def_block = out.split("## Definition", 1)[1].split("```", 1)[0]
        assert "sample.c:" in def_block
        assert "sample.h:" not in def_block
        # Body should contain the function definition, not just the prototype.
        assert "int compute(struct point *p) {" in out
