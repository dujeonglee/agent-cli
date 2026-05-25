"""Tests for the JavaScript and TypeScript walkers.

Ported from minish.ai/Agent-tools tests/test_js_ts.py (Apache 2.0) and
extended to cover DESIGN.md §12.1 cases:

- JS default param (``withDefault(x = 10)``), rest param (``withRest(...rest)``).
- JS generator: ``function* gen()`` emitted with ``modifiers=["generator"]``.
- TS generic function ``identity<T>(x: T): T`` and generic type alias
  ``Maybe<T>`` (already in upstream fixture).
- TS default param ``withDefault(x = 5)``.
- TS type refs (``kind='type'`` Ref records for type_identifier in
  annotations / generic args / extends positions).
- Error files for each language.
"""

from __future__ import annotations

import pytest

from tests.code_index.helpers import (
    build_fixture,
    callees_of,
    names_of_kind,
)


@pytest.fixture(scope="module")
def js_index():
    return build_fixture("javascript")


@pytest.fixture(scope="module")
def ts_index():
    return build_fixture("typescript")


class TestJavaScriptWalker:
    def test_function_declaration(self, js_index):
        store, _ = js_index
        funcs = names_of_kind(store, "function")
        assert "helper" in funcs
        assert "loader" in funcs

    def test_arrow_const_is_function(self, js_index):
        _, by_name = js_index
        sym = by_name["arrowFn"][0]
        assert sym["kind"] == "function"
        assert sym["kind_raw"] == "arrow_function"

    def test_class_is_type(self, js_index):
        _, by_name = js_index
        sym = by_name["Service"][0]
        assert sym["kind"] == "type"

    def test_method_has_parent(self, js_index):
        _, by_name = js_index
        greet = by_name["greet"][0]
        assert greet["parent"] == "Service"

    def test_static_method_modifier(self, js_index):
        _, by_name = js_index
        make = by_name["make"][0]
        assert "static" in (make.get("modifiers") or [])

    def test_field_with_parent(self, js_index):
        _, by_name = js_index
        f = by_name["instances"][0]
        assert f["kind"] == "variable"
        assert f["parent"] == "Service"

    def test_async_modifier(self, js_index):
        _, by_name = js_index
        loader = by_name["loader"][0]
        assert "async" in (loader.get("modifiers") or [])

    def test_call_graph(self, js_index):
        store, _ = js_index
        cs = callees_of(store, "loader")
        assert "helper" in cs
        assert "make" in cs
        assert "greet" in cs


class TestJavaScriptExtras:
    """DESIGN §12.1 cases beyond the upstream port."""

    def test_default_param_function_extracted(self, js_index):
        _, by_name = js_index
        wd = by_name["withDefault"][0]
        assert wd["kind"] == "function"
        assert wd["is_definition"] is True

    def test_rest_param_function_extracted(self, js_index):
        _, by_name = js_index
        wr = by_name["withRest"][0]
        assert wr["kind"] == "function"

    def test_generator_function_extracted(self, js_index):
        # `function* gen()` is a `generator_function_declaration`. The
        # walker treats it like a function declaration but tags it with
        # a `generator` modifier so callers can distinguish.
        _, by_name = js_index
        gen = by_name["gen"][0]
        assert gen["kind"] == "function"
        assert "generator" in (gen.get("modifiers") or [])

    def test_refs_emit_call_and_name_kinds(self, js_index):
        store, _ = js_index
        kinds = {r["kind"] for r in store.all_refs()}
        assert "call" in kinds
        assert "name" in kinds

    def test_error_file_does_not_crash_and_is_flagged(self, js_index):
        store, _ = js_index
        errs = [f for f in store.files if f.get("has_error")]
        assert any(f["path"] == "error.js" for f in errs)


class TestTypeScriptWalker:
    def test_class_extracted(self, ts_index):
        _, by_name = ts_index
        sym = by_name["Service"][0]
        assert sym["kind"] == "type"

    def test_interface_is_type(self, ts_index):
        _, by_name = ts_index
        sym = by_name["Greeter"][0]
        assert sym["kind"] == "type"

    def test_type_alias_is_type(self, ts_index):
        _, by_name = ts_index
        sym = by_name["Maybe"][0]
        assert sym["kind"] == "type"
        assert sym["kind_raw"] == "type_alias_declaration"

    def test_enum_is_type(self, ts_index):
        _, by_name = ts_index
        sym = by_name["Status"][0]
        assert sym["kind"] == "type"
        assert sym["kind_raw"] == "enum_declaration"

    def test_exported_function(self, ts_index):
        _, by_name = ts_index
        helper = by_name["helper"][0]
        assert helper["kind"] == "function"
        assert "exported" in (helper.get("modifiers") or [])


class TestTypeScriptExtras:
    """DESIGN §12.1 cases beyond the upstream port."""

    # ----- generic function -----

    def test_generic_function_extracted(self, ts_index):
        _, by_name = ts_index
        ident = by_name["identity"][0]
        assert ident["kind"] == "function"
        assert "exported" in (ident.get("modifiers") or [])

    # ----- generic type alias (Maybe<T>) — assertion on Maybe already exists; -----
    # check it carries the generic parameter context implicitly via kind_raw.

    def test_generic_type_alias_kind_raw(self, ts_index):
        _, by_name = ts_index
        m = by_name["Maybe"][0]
        assert m["kind_raw"] == "type_alias_declaration"

    # ----- default param -----

    def test_default_param_function_extracted(self, ts_index):
        _, by_name = ts_index
        wd = by_name["withDefault"][0]
        assert wd["kind"] == "function"

    # ----- refs: at minimum call + name (TS walker does not emit type-kind refs) -----

    def test_refs_emit_call_name_and_type_kinds(self, ts_index):
        # The TS walker emits all three ref kinds: call (function/method
        # invocations and `new X()`), name (bare identifier mention of a
        # defined symbol), and type (type_identifier in annotation,
        # generic-arg, or extends position).
        store, _ = ts_index
        kinds = {r["kind"] for r in store.all_refs()}
        assert kinds.issuperset({"call", "name", "type"})

    def test_type_ref_to_defined_type(self, ts_index):
        # A `type_identifier` in type position (e.g. `let foo: Point;`)
        # produces a `kind='type'` ref to the named type. Pick any type
        # that the fixture defines and uses in an annotation.
        store, by_name = ts_index
        type_defs = {
            name
            for name, hits in by_name.items()
            if any(h["kind"] == "type" for h in hits)
        }
        type_refs = {r["name"] for r in store.all_refs() if r["kind"] == "type"}
        # At least one defined type should appear as a type ref.
        assert type_refs & type_defs

    # ----- error file -----

    def test_error_file_does_not_crash_and_is_flagged(self, ts_index):
        store, _ = ts_index
        errs = [f for f in store.files if f.get("has_error")]
        assert any(f["path"] == "error.ts" for f in errs)
