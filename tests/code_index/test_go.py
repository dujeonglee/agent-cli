"""Tests for the Go walker.

Ported from minish.ai/Agent-tools tests/test_go.py (Apache 2.0) and
extended to cover DESIGN.md §12.1 cases:

- Generic function: ``Identity[T any](x T) T`` (Go 1.18+).
- Variadic: ``Variadic(prefix string, vals ...int)``.
- Refs: ``type`` ref kind verified.
- Error file (``error.go``) — has_error=True.
"""

from __future__ import annotations

import pytest

from tests.code_index.helpers import (
    build_fixture,
    callees_of,
    callers_of,
    names_of_kind,
)


@pytest.fixture(scope="module")
def index():
    return build_fixture("go")


class TestGoWalker:
    def test_function_extracted(self, index):
        store, _ = index
        funcs = names_of_kind(store, "function")
        assert "Helper" in funcs
        assert "unexported" in funcs

    def test_method_has_receiver_as_parent(self, index):
        _, by_name = index
        for name in ("String", "Sum"):
            sym = by_name[name][0]
            assert sym["kind"] == "function"
            assert sym["parent"] == "Point"

    def test_struct_is_type(self, index):
        _, by_name = index
        point = by_name["Point"][0]
        assert point["kind"] == "type"
        assert point["kind_raw"] == "struct"

    def test_interface_is_type(self, index):
        _, by_name = index
        stringer = by_name["Stringer"][0]
        assert stringer["kind"] == "type"
        assert stringer["kind_raw"] == "interface"

    def test_const_is_constant(self, index):
        store, _ = index
        assert "MaxRetries" in names_of_kind(store, "constant")

    def test_var_is_variable(self, index):
        store, _ = index
        assert "counter" in names_of_kind(store, "variable")

    def test_exported_modifier(self, index):
        _, by_name = index
        helper = by_name["Helper"][0]
        assert "exported" in (helper.get("modifiers") or [])
        un = by_name["unexported"][0]
        assert "exported" not in (un.get("modifiers") or [])

    def test_call_graph(self, index):
        store, _ = index
        assert "Helper" in callees_of(store, "unexported")
        assert "unexported" in callers_of(store, "Helper")
        assert "Sum" in callers_of(store, "Helper")


class TestGoExtras:
    """DESIGN §12.1 cases beyond the upstream port."""

    # ----- generic function -----

    def test_generic_function_extracted(self, index):
        _, by_name = index
        ident = by_name["Identity"][0]
        assert ident["kind"] == "function"
        assert ident["is_definition"] is True
        assert "exported" in (ident.get("modifiers") or [])

    # ----- variadic -----

    def test_variadic_function_extracted(self, index):
        _, by_name = index
        v = by_name["Variadic"][0]
        assert v["kind"] == "function"
        assert v["is_definition"] is True

    # ----- refs: all three kinds -----

    def test_refs_emit_call_name_and_type_kinds(self, index):
        store, _ = index
        kinds = {r["kind"] for r in store.all_refs()}
        assert kinds.issuperset({"call", "name", "type"})

    def test_type_ref_to_point(self, index):
        store, _ = index
        type_refs = [
            r for r in store.all_refs() if r["kind"] == "type" and r["name"] == "Point"
        ]
        assert type_refs

    # ----- error file -----

    def test_error_file_does_not_crash_and_is_flagged(self, index):
        store, _ = index
        errs = [f for f in store.files if f.get("has_error")]
        assert any(f["path"] == "error.go" for f in errs)
