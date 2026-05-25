"""Tests for the C++ walker.

Ported from minish.ai/Agent-tools tests/test_cpp.py (Apache 2.0) and
extended to cover DESIGN.md §12.1 cases:

- Declaration vs definition: in-class ``process`` (method_declaration,
  is_definition=False) coexists with out-of-line ``Service::process``
  (function_definition, is_definition=True).
- Templates: ``template <typename T> T identity(T x)`` — only the inner
  function is indexed (template wrapper unwrapped).
- Qualified names: ``demo::Service`` as parent.
- Refs: ``call``, ``name``, ``type`` kinds appear.
- Error file (``error.cpp``) — has_error=True.
"""

from __future__ import annotations

import pytest

from tests.code_index.helpers import (
    build_fixture,
    callees_of,
    names_of_kind,
)


@pytest.fixture(scope="module")
def index():
    return build_fixture("cpp")


class TestCppWalker:
    def test_class_in_namespace(self, index):
        _, by_name = index
        svc = by_name["Service"][0]
        assert svc["kind"] == "type"
        assert svc["kind_raw"] == "class"
        assert svc["parent"] == "demo"

    def test_inline_methods_have_parent(self, index):
        _, by_name = index
        h = by_name["helper"][0]
        assert h["kind"] == "function"
        assert h["parent"] == "demo::Service"

    def test_out_of_line_method(self, index):
        _, by_name = index
        procs = by_name["process"]
        assert len(procs) >= 1
        defs = [s for s in procs if s["is_definition"]]
        assert defs
        assert defs[0]["parent"] in ("demo::Service", "Service")

    def test_field(self, index):
        _, by_name = index
        c = by_name["count"][0]
        assert c["kind"] == "variable"
        assert c["parent"] == "demo::Service"

    def test_free_function(self, index):
        _, by_name = index
        f = by_name["free_function"][0]
        assert f["kind"] == "function"
        assert f.get("parent") is None

    def test_template_unwrapped(self, index):
        store, _ = index
        # template <typename T> T identity(T x) — inner function indexed.
        assert "identity" in names_of_kind(store, "function")

    def test_call_graph(self, index):
        store, _ = index
        assert "helper" in callees_of(store, "process")
        assert "process" in callees_of(store, "free_function")


class TestCppExtras:
    """DESIGN §12.1 cases beyond the upstream port."""

    # ----- declaration vs definition -----

    def test_in_class_method_declaration_is_non_definition(self, index):
        _, by_name = index
        procs = by_name["process"]
        decls = [s for s in procs if not s["is_definition"]]
        assert decls, "expected an is_definition=False entry for in-class 'process'"
        assert decls[0]["kind_raw"] == "method_declaration"
        assert decls[0]["parent"] in ("demo::Service", "Service")

    # ----- qualified parent -----

    def test_qualified_namespace_parent(self, index):
        _, by_name = index
        identity = by_name["identity"][0]
        assert identity["parent"] == "demo"

    # ----- refs: all three kinds -----

    def test_refs_emit_call_name_and_type_kinds(self, index):
        store, _ = index
        kinds = {r["kind"] for r in store.all_refs()}
        assert kinds.issuperset({"call", "name", "type"})

    def test_type_ref_to_service(self, index):
        store, _ = index
        type_refs = [
            r
            for r in store.all_refs()
            if r["kind"] == "type" and r["name"] == "Service"
        ]
        assert type_refs

    # ----- error file -----

    def test_error_file_does_not_crash_and_is_flagged(self, index):
        store, _ = index
        errs = [f for f in store.files if f.get("has_error")]
        assert any(f["path"] == "error.cpp" for f in errs)
