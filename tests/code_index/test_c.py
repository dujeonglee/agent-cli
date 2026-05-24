"""Tests for the C walker.

Ported from minish.ai/Agent-tools tests/test_c.py (Apache 2.0) and
extended to cover DESIGN.md §12.1 cases:

- Declaration vs definition: prototype ``int compute(struct point *p);``
  in sample.h is recorded with ``is_definition=False``.
- Variadic macro: ``DBG(fmt, ...)``.
- Refs: ``call``, ``name``, ``type`` kinds all appear.
- Error file (``error.c``) — has_error=True recorded; build does not
  crash.
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
    return build_fixture("c")


class TestCWalker:
    # ----- 4-kind vocab on C constructs -----

    def test_function_extracted(self, index):
        store, _ = index
        funcs = names_of_kind(store, "function")
        assert "helper" in funcs
        assert "compute" in funcs

    def test_object_macro_is_constant(self, index):
        store, by_name = index
        assert "MAX_BUF" in names_of_kind(store, "constant")
        macro = by_name["MAX_BUF"][0]
        assert macro["kind_raw"] == "preproc_def"

    def test_function_macro_is_function(self, index):
        store, by_name = index
        assert "DBG" in names_of_kind(store, "function")
        macro = by_name["DBG"][0]
        assert macro["kind_raw"] == "preproc_function_def"

    def test_struct_is_type(self, index):
        store, _ = index
        assert "point" in names_of_kind(store, "type")

    def test_typedef_is_type(self, index):
        _, by_name = index
        u32 = by_name.get("u32")
        assert u32 is not None
        assert u32[0]["kind"] == "type"
        assert u32[0]["kind_raw"] == "typedef"

    def test_enum_is_type(self, index):
        _, by_name = index
        color = by_name.get("color")
        assert color is not None
        assert color[0]["kind"] == "type"
        assert color[0]["kind_raw"] == "enum"
        assert "RED" in (color[0].get("enum_values") or [])

    def test_global_var_is_variable(self, index):
        store, _ = index
        assert "origin" in names_of_kind(store, "variable")

    def test_static_modifier(self, index):
        _, by_name = index
        helper = by_name["helper"][0]
        assert "static" in (helper.get("modifiers") or [])

    # ----- callgraph includes fn-like macros -----

    def test_macro_appears_in_call_graph(self, index):
        store, _ = index
        assert "DBG" in callees_of(store, "compute")

    def test_function_to_function_edge(self, index):
        store, _ = index
        assert "helper" in callees_of(store, "compute")
        assert "compute" in callers_of(store, "helper")


class TestCExtras:
    """DESIGN §12.1 cases beyond the upstream port."""

    # ----- declaration vs definition -----

    def test_prototype_recorded_as_non_definition(self, index):
        _, by_name = index
        proto = [s for s in by_name["compute"] if not s["is_definition"]]
        assert len(proto) == 1
        assert proto[0]["kind_raw"] == "prototype"
        assert proto[0]["file"] == "sample.h"

    def test_definition_and_prototype_coexist(self, index):
        _, by_name = index
        defs = [s for s in by_name["compute"] if s["is_definition"]]
        protos = [s for s in by_name["compute"] if not s["is_definition"]]
        assert len(defs) >= 1
        assert len(protos) >= 1

    # ----- variadic macro -----

    def test_variadic_macro_emitted(self, index):
        _, by_name = index
        dbg = by_name["DBG"][0]
        # We don't assert a specific modifier (walker doesn't normalise
        # variadic-ness into modifiers today); the kind_raw alone proves
        # the variadic preproc_function_def parsed.
        assert dbg["kind_raw"] == "preproc_function_def"

    # ----- refs: all three kinds -----

    def test_refs_emit_call_name_and_type_kinds(self, index):
        store, _ = index
        kinds = {r["kind"] for r in store.all_refs()}
        assert kinds.issuperset({"call", "name", "type"})

    def test_type_ref_to_struct_point(self, index):
        store, _ = index
        type_refs = [
            r for r in store.all_refs() if r["kind"] == "type" and r["name"] == "point"
        ]
        assert type_refs, (
            "expected at least one type-ref to 'point' (e.g. struct point *p)"
        )

    # ----- error file -----

    def test_error_file_does_not_crash_and_is_flagged(self, index):
        store, _ = index
        errs = [f for f in store.files if f.get("has_error")]
        assert any(f["path"] == "error.c" for f in errs)
