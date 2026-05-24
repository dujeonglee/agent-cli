"""Tests for the Python walker.

Ported from minish.ai/Agent-tools tests/test_python.py (Apache 2.0) and
extended to cover DESIGN.md §12.1 cases:

- Default + variadic params (``with_defaults`` in extras.py).
- Nested class (``Outer.Inner`` in extras.py).
- Documents current walker behaviour: nested function definitions inside
  another function are NOT emitted by the Python walker (only the outer
  function is). Test pins this so any future walker change is caught.
- Error file (``error.py``) — tree-sitter ``has_error=True`` recorded
  and build does not crash.
- Same-name function across two files — covered by the ``nested``
  fixture (``sub/mod.py`` and ``other/mod.py`` both define ``dup_helper``);
  see ``test_path_normalize.py`` for the index-level assertions.
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
    return build_fixture("python")


class TestPythonWalker:
    """Symbol-kind, modifier, parent, and callgraph coverage."""

    # ----- symbol kinds -----

    def test_function_extracted(self, index):
        store, _ = index
        assert "helper" in names_of_kind(store, "function")

    def test_class_is_type(self, index):
        store, _ = index
        types = names_of_kind(store, "type")
        assert "Service" in types
        assert "DerivedService" in types

    def test_methods_have_parent(self, index):
        _, by_name = index
        init = by_name["__init__"][0]
        assert init["kind"] == "function"
        assert init["parent"] == "Service"

    def test_upper_snake_is_constant(self, index):
        store, _ = index
        assert "MAX_RETRIES" in names_of_kind(store, "constant")

    def test_lowercase_module_var_is_variable(self, index):
        store, _ = index
        assert "default_timeout" in names_of_kind(store, "variable")

    def test_class_attr_is_variable_with_parent(self, index):
        _, by_name = index
        attr = by_name["instance_count"][0]
        assert attr["kind"] == "variable"
        assert attr["parent"] == "Service"

    # ----- modifiers -----

    def test_async_modifier(self, index):
        _, by_name = index
        async_methods = [
            s for s in by_name["process"] if "async" in (s.get("modifiers") or [])
        ]
        assert len(async_methods) == 1
        assert async_methods[0]["parent"] == "Service"

    def test_decorator_modifiers(self, index):
        _, by_name = index
        label = by_name["label"][0]
        assert "property" in (label["modifiers"] or [])
        make = by_name["make"][0]
        assert "staticmethod" in (make["modifiers"] or [])

    # ----- call graph -----

    def test_helper_called_from_process(self, index):
        store, _ = index
        assert "process" in callers_of(store, "helper")

    def test_async_process_callees_include_helper(self, index):
        store, _ = index
        callees = callees_of(store, "process")
        assert "helper" in callees


class TestPythonExtras:
    """DESIGN §12.1 cases not in the upstream fixture."""

    # ----- default / variadic params -----

    def test_default_param_function_extracted(self, index):
        _, by_name = index
        assert "with_defaults" in by_name
        sym = by_name["with_defaults"][0]
        assert sym["kind"] == "function"
        assert sym["is_definition"] is True

    # ----- nested class -----

    def test_nested_class_parent_uses_dotted_chain(self, index):
        _, by_name = index
        inner = by_name["Inner"][0]
        assert inner["kind"] == "type"
        assert inner["parent"] == "Outer"

    def test_method_on_nested_class_has_dotted_parent(self, index):
        _, by_name = index
        deep = by_name["deep"][0]
        assert deep["kind"] == "function"
        assert deep["parent"] == "Outer.Inner"

    # ----- nested-function support -----

    def test_outer_function_emitted(self, index):
        _, by_name = index
        outer = by_name["outer"][0]
        assert outer["kind"] == "function"
        assert outer["parent"] is None

    def test_inner_function_emitted_with_parent(self, index):
        # The Python walker descends into function bodies so closures and
        # helper inner functions are discoverable. Parent is the dotted
        # chain of enclosing function names.
        _, by_name = index
        inner = by_name["inner"][0]
        assert inner["kind"] == "function"
        assert inner["parent"] == "outer"

    # ----- refs: kind=call and kind=name appear; type-refs aren't a thing in Python -----

    def test_refs_include_call_and_name_kinds(self, index):
        store, _ = index
        kinds = {r["kind"] for r in store.all_refs()}
        assert "call" in kinds
        assert "name" in kinds

    # ----- error file -----

    def test_error_file_does_not_crash_and_is_flagged(self, index):
        store, _ = index
        errs = [f for f in store.files if f.get("has_error")]
        assert any(f["path"] == "error.py" for f in errs)
