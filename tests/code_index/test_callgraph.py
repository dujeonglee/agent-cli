"""Call graph on the stored name resolution (DESIGN §12.4, §21).

v10.96.0: callers/callees read `refs.target_id`/`fn_symbol_id` — nodes are
definitions, so two functions named `process` are two nodes; a call on an
object of unknown type is listed apart (`unknown_sites`, `?` neighbors);
languages without scope rules answer by name (`by_name_only`).
"""

from __future__ import annotations

from collections import Counter

import pytest

from agent_cli.code_index import callees_of, callers_of, definitions_named
from tests.code_index.helpers import build_fixture, build_tree
from tests.code_index.helpers import callees_of as callee_names
from tests.code_index.helpers import callers_of as caller_names


def _one(store, name):
    (d,) = definitions_named(store, name)
    return d


def _counts(edges) -> Counter:
    return Counter({n.label: n.count for n in edges.neighbors})


@pytest.fixture(scope="module")
def python_index():
    return build_fixture("python")[0]


class TestDefinitionsAreNodes:
    def test_two_methods_of_one_name_are_two_callers(self, python_index):
        edges = callers_of(python_index, _one(python_index, "helper"))
        assert _counts(edges) == Counter(
            {"Service.process": 1, "DerivedService.process": 1}
        )
        sites = {n.label: n.sites for n in edges.neighbors}
        assert sites["Service.process"] == [("app.py", 27, "call")]
        assert sites["DerivedService.process"] == [("app.py", 32, "call")]

    def test_callees_name_the_definition(self, python_index):
        edges = callees_of(python_index, _one(python_index, "DerivedService.process"))
        assert [(n.label, n.symbol_id is not None) for n in edges.neighbors] == [
            ("helper", True)
        ]

    def test_orphans_have_no_edges(self, python_index):
        d = _one(python_index, "with_defaults")
        assert callers_of(python_index, d).neighbors == []
        assert callees_of(python_index, _one(python_index, "helper")).neighbors == []

    def test_same_name_in_two_files_stays_two_nodes(self):
        store, _ = build_fixture("nested")
        defs = definitions_named(store, "dup_helper")
        assert sorted(d["file"] for d in defs) == ["other/mod.py", "sub/mod.py"]
        for d in defs:
            assert callers_of(store, d).definitions == [d]


class TestSemantics:
    @pytest.fixture(scope="class")
    def idx(self, tmp_path_factory):
        root = tmp_path_factory.mktemp("sem")
        (root / "m.py").write_text(
            "def helper():\n    return 1\n\n\n"
            "def self_call():\n    return self_call()\n\n\n"
            "def shadow(helper):\n    return helper()\n\n\n"
            "def register(fn):\n    return fn\n\n\n"
            "def uses_callback():\n    return register(helper)\n\n\n"
            "def twice():\n    helper(); helper()\n\n\n"
            "def outer():\n"
            "    def inner():\n        return helper()\n"
            "    return helper()\n\n\n"
            "def on_unknown(obj):\n    return obj.helper()\n\n\n"
            "class Service:\n    pass\n"
        )
        return build_tree(root)

    def test_self_call_is_not_an_edge(self, idx):
        d = _one(idx, "self_call")
        assert callers_of(idx, d).neighbors == []
        assert callees_of(idx, d).neighbors == []

    def test_a_parameter_with_the_same_name_is_local(self, idx):
        assert "shadow" not in _counts(callers_of(idx, _one(idx, "helper")))

    def test_a_callback_reference_is_a_name_site(self, idx):
        edges = callers_of(idx, _one(idx, "helper"))
        (n,) = [n for n in edges.neighbors if n.label == "uses_callback"]
        assert [k for _f, _l, k in n.sites] == ["name"]

    def test_two_calls_on_one_line_are_one_site(self, idx):
        edges = callers_of(idx, _one(idx, "helper"))
        (n,) = [n for n in edges.neighbors if n.label == "twice"]
        assert n.count == 1 and n.sites[0][2] == "call"

    def test_nested_function_and_the_body_after_it(self, idx):
        counts = _counts(callers_of(idx, _one(idx, "helper")))
        assert counts["outer.inner"] == 1 and counts["outer"] == 1

    def test_unknown_type_object_is_listed_apart(self, idx):
        edges = callers_of(idx, _one(idx, "helper"))
        assert "on_unknown" not in _counts(edges)
        assert edges.unknown_sites == [("m.py", 32, "call")]
        assert edges.unknown_definitions == 1

    def test_a_class_is_not_a_function_node(self, idx):
        assert definitions_named(idx, "Service") == []


def test_cycle_records_both_directions(tmp_path):
    (tmp_path / "cycle.py").write_text(
        "def a():\n    return b()\n\n\ndef b():\n    return a()\n"
    )
    store = build_tree(tmp_path)
    assert _counts(callees_of(store, _one(store, "a"))) == Counter({"b": 1})
    assert _counts(callers_of(store, _one(store, "a"))) == Counter({"b": 1})


class TestClosureFixture:
    """v10.95.0 정답(fixtures/python/closures.py) — 이름 해석 위에서도 그대로."""

    def test_callers(self, python_index):
        assert caller_names(python_index, "twig") == {
            "make_adder",
            "mid",
            "decorated_outer",
            "local",
        }
        assert caller_names(python_index, "leaf") == {"add", "core", "nest3", "method"}

    def test_callees(self, python_index):
        assert callee_names(python_index, "make_adder") == {"twig", "add"}
        assert callee_names(python_index, "nest3") == {"leaf", "mid"}
        assert callee_names(python_index, "mid") == {"twig", "core"}
        assert callee_names(python_index, "decorated_outer") == {"twig", "wrapped"}

    def test_outer_returns_its_inner(self, python_index):
        assert "inner" in callee_names(python_index, "outer")


class TestByNameLanguages:
    """범위 규칙이 없는 언어 — 이름으로만 맞추고 그렇다고 말한다."""

    def test_javascript(self):
        store, by_name = build_fixture("javascript")
        assert "innerJs" not in by_name
        edges = callees_of(store, _one(store, "outerJs"))
        assert edges.by_name_only
        assert {n.name for n in edges.neighbors} == {"leafJs", "twigJs"}

    def test_parameters_still_shadow(self, tmp_path):
        (tmp_path / "a.go").write_text(
            "package a\n\nfunc helper() int { return 1 }\n\n"
            "func uses(helper func() int) int { return helper() }\n\n"
            "func calls() int { return helper() }\n"
        )
        store = build_tree(tmp_path)
        edges = callers_of(store, _one(store, "helper"))
        assert edges.by_name_only
        assert {n.name for n in edges.neighbors} == {"calls"}
