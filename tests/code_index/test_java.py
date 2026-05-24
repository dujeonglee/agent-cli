"""Tests for the Java walker.

Ported from minish.ai/Agent-tools tests/test_java.py (Apache 2.0) and
extended to cover DESIGN.md §12.1 cases:

- Declaration vs definition: ``Greeter.hello`` (interface method, no
  body) and ``AbstractBase.absMethod`` (abstract) both recorded with
  ``is_definition=False``.
- Generics: ``class Extras<T>`` + ``T identity(T x)``.
- Variadic: ``int sumAll(int first, int... rest)``.
- Modifiers: ``abstract`` on class and method.
- Error file (``Broken.java``) — has_error=True.
"""

from __future__ import annotations

import pytest

from tests.code_index.helpers import (
    build_fixture,
    callees_of,
    callers_of,
)


@pytest.fixture(scope="module")
def index():
    return build_fixture("java")


class TestJavaWalker:
    def test_class_is_type(self, index):
        _, by_name = index
        svc = by_name["Service"][0]
        assert svc["kind"] == "type"
        assert svc["kind_raw"] == "class_declaration"

    def test_interface_is_type(self, index):
        _, by_name = index
        sym = by_name["Greeter"][0]
        assert sym["kind"] == "type"
        assert sym["kind_raw"] == "interface_declaration"

    def test_enum_is_type_with_constants(self, index):
        _, by_name = index
        sym = by_name["Color"][0]
        assert sym["kind"] == "type"
        assert sym["kind_raw"] == "enum_declaration"
        assert set(sym["enum_values"]) == {"RED", "GREEN", "BLUE"}

    def test_method_has_parent(self, index):
        _, by_name = index
        for n in ("helper", "process"):
            sym = by_name[n][0]
            assert sym["kind"] == "function"
            assert sym["parent"] == "Service"

    def test_constructor(self, index):
        _, by_name = index
        sym = by_name["Service"]
        kinds = {s["kind"] for s in sym}
        assert "type" in kinds
        ctor = [s for s in sym if s["kind"] == "function"][0]
        assert ctor["kind_raw"] == "constructor_declaration"
        assert ctor["parent"] == "Service"

    def test_static_final_is_constant(self, index):
        _, by_name = index
        c = by_name["MAX_RETRIES"][0]
        assert c["kind"] == "constant"
        mods = c.get("modifiers") or []
        assert "static" in mods
        assert "final" in mods

    def test_instance_field_is_variable(self, index):
        _, by_name = index
        c = by_name["counter"][0]
        assert c["kind"] == "variable"
        assert c["parent"] == "Service"

    def test_call_graph(self, index):
        store, _ = index
        assert "helper" in callees_of(store, "process")
        assert "process" in callers_of(store, "helper")


class TestJavaExtras:
    """DESIGN §12.1 cases beyond the upstream port."""

    # ----- interface method = declaration -----

    def test_interface_method_is_non_definition(self, index):
        _, by_name = index
        hello = by_name["hello"][0]
        assert hello["is_definition"] is False
        assert hello["parent"] == "Greeter"

    # ----- abstract method -----

    def test_abstract_method_is_non_definition(self, index):
        _, by_name = index
        am = by_name["absMethod"][0]
        assert am["is_definition"] is False
        assert "abstract" in (am.get("modifiers") or [])
        assert am["parent"] == "AbstractBase"

    def test_abstract_class_modifier(self, index):
        _, by_name = index
        ab = by_name["AbstractBase"][0]
        assert ab["kind"] == "type"
        assert "abstract" in (ab.get("modifiers") or [])

    # ----- generics -----

    def test_generic_class_extracted(self, index):
        _, by_name = index
        ex = by_name["Extras"][0]
        assert ex["kind"] == "type"
        assert ex["kind_raw"] == "class_declaration"

    def test_generic_method_extracted(self, index):
        _, by_name = index
        ident = by_name["identity"][0]
        assert ident["kind"] == "function"
        assert ident["parent"] == "Extras"

    # ----- variadic -----

    def test_variadic_method_extracted(self, index):
        _, by_name = index
        s = by_name["sumAll"][0]
        assert s["kind"] == "function"
        assert s["parent"] == "Extras"

    # ----- refs: at least call + name + type kinds -----

    def test_refs_emit_call_name_and_type_kinds(self, index):
        store, _ = index
        kinds = {r["kind"] for r in store.all_refs()}
        assert kinds.issuperset({"call", "name", "type"})

    # ----- error file -----

    def test_error_file_does_not_crash_and_is_flagged(self, index):
        store, _ = index
        errs = [f for f in store.files if f.get("has_error")]
        assert any(f["path"] == "Broken.java" for f in errs)
