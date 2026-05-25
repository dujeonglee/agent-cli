"""Tests for the Rust walker.

Ported from minish.ai/Agent-tools tests/test_rust.py (Apache 2.0) and
extended to cover DESIGN.md §12.1 cases:

- Generic function: ``pub fn identity<T>(x: T) -> T``.
- Impl-for-trait methods (``impl Greet for Point`` — ``hello`` recorded
  with ``parent=Point``).
- Trait method signature (``fn hello(&self) -> String;`` inside
  ``pub trait Greet { ... }``) is currently NOT emitted by the walker —
  the rust walker does not descend into trait bodies. Pinned by
  ``test_trait_sig_inside_trait_body_NOT_emitted``.
- Receiver/qualified names: impl-block methods carry ``parent=Point``.
- Refs: ``call``, ``name``, ``type`` kinds.
- Error file (``error.rs``) — has_error=True.
"""

from __future__ import annotations

import pytest

from tests.code_index.helpers import (
    build_fixture,
    callers_of,
    names_of_kind,
)


@pytest.fixture(scope="module")
def index():
    return build_fixture("rust")


class TestRustWalker:
    def test_function_extracted(self, index):
        store, _ = index
        funcs = names_of_kind(store, "function")
        assert "helper" in funcs
        assert "private_caller" in funcs

    def test_struct_is_type(self, index):
        _, by_name = index
        sym = by_name["Point"][0]
        assert sym["kind"] == "type"
        assert sym["kind_raw"] == "struct"

    def test_enum_is_type_with_variants(self, index):
        _, by_name = index
        sym = by_name["Color"][0]
        assert sym["kind"] == "type"
        assert sym["kind_raw"] == "enum"
        assert set(sym["enum_values"]) == {"Red", "Green", "Blue"}

    def test_trait_is_type(self, index):
        _, by_name = index
        sym = by_name["Greet"][0]
        assert sym["kind"] == "type"
        assert sym["kind_raw"] == "trait"

    def test_const_is_constant(self, index):
        store, by_name = index
        assert "MAX_RETRIES" in names_of_kind(store, "constant")
        c = by_name["MAX_RETRIES"][0]
        assert "pub" in (c.get("modifiers") or [])

    def test_static_is_variable(self, index):
        store, _ = index
        assert "COUNTER" in names_of_kind(store, "variable")

    def test_impl_methods_have_parent(self, index):
        _, by_name = index
        new_method = by_name["new"][0]
        assert new_method["parent"] == "Point"
        sum_method = by_name["sum"][0]
        assert sum_method["parent"] == "Point"

    def test_macro_rules_is_function(self, index):
        _, by_name = index
        sym = by_name["shout"][0]
        assert sym["kind"] == "function"
        assert sym["kind_raw"] == "macro_definition"

    def test_call_graph_includes_macro_calls(self, index):
        store, _ = index
        callers = callers_of(store, "helper")
        assert "private_caller" in callers
        assert "sum" in callers


class TestRustExtras:
    """DESIGN §12.1 cases beyond the upstream port."""

    # ----- generic function -----

    def test_generic_function_extracted(self, index):
        _, by_name = index
        ident = by_name["identity"][0]
        assert ident["kind"] == "function"
        assert ident["is_definition"] is True
        assert "pub" in (ident.get("modifiers") or [])

    # ----- impl-for-trait + trait body method sigs -----

    def test_impl_for_trait_method_has_parent(self, index):
        # Two `hello` symbols now: the trait body signature
        # (parent=Greet, is_definition=False) and the impl-for-trait
        # implementation (parent=Point, is_definition=True).
        _, by_name = index
        hellos = by_name["hello"]
        assert len(hellos) == 2
        parents = {h["parent"] for h in hellos}
        assert parents == {"Greet", "Point"}
        impl_hello = next(h for h in hellos if h["parent"] == "Point")
        assert impl_hello["is_definition"] is True

    def test_trait_sig_emitted_with_trait_parent(self, index):
        # The Rust walker descends into trait bodies so the bare method
        # signature ``fn hello(&self) -> String;`` IS emitted with
        # parent=Greet and is_definition=False (no body).
        _, by_name = index
        trait_hello = next(h for h in by_name["hello"] if h["parent"] == "Greet")
        assert trait_hello["is_definition"] is False
        assert trait_hello["kind"] == "function"

    # ----- refs: all three kinds -----

    def test_refs_emit_call_name_and_type_kinds(self, index):
        store, _ = index
        kinds = {r["kind"] for r in store.all_refs()}
        assert kinds.issuperset({"call", "name", "type"})

    def test_type_ref_to_point_or_greet(self, index):
        store, _ = index
        type_refs = {
            r["name"]
            for r in store.all_refs()
            if r["kind"] == "type" and r["name"] in {"Point", "Greet", "Self"}
        }
        assert type_refs, "expected at least one type-ref to Point/Greet/Self"

    # ----- error file -----

    def test_error_file_does_not_crash_and_is_flagged(self, index):
        store, _ = index
        errs = [f for f in store.files if f.get("has_error")]
        assert any(f["path"] == "error.rs" for f in errs)
