"""Shared helpers for code_index unit tests.

Each language test follows this pattern:

    from tests.code_index.helpers import build_fixture
    idx, syms_by_name = build_fixture("python")
    assert "MyClass" in syms_by_name

Fixtures live in ``tests/code_index/fixtures/<lang>/`` and get indexed
into a fresh temp DB per call. Pytest fixtures in the per-language test
modules can pin the build to module scope to avoid re-building for every
test method.
"""

from __future__ import annotations

import tempfile
from collections import defaultdict
from pathlib import Path

from agent_cli.code_index import IndexStore, build, build_callgraph, load_index

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def build_fixture(lang: str) -> tuple[IndexStore, dict[str, list]]:
    """Build an index over ``fixtures/<lang>/`` into a fresh temp DB.

    Returns ``(store, symbols_grouped_by_name)``. The DB file is left on
    disk in a temp dir — pytest's temp cleanup is per-session, so this
    is fine; if a test wants explicit cleanup it can stash the path.
    """
    root = FIXTURES / lang
    if not root.is_dir():
        raise FileNotFoundError(f"fixture directory not found: {root}")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        out = Path(tf.name)
    build(
        root,
        out,
        defs_path=None,
        undef_unknown_configs=False,
        force_full=True,
        verbose=False,
    )
    store = load_index(out)
    by_name: dict[str, list] = defaultdict(list)
    for s in store.all_symbols():
        by_name[s["name"]].append(s)
    return store, by_name


def build_tree(root: Path, out: Path | None = None) -> IndexStore:
    """Build an index over an arbitrary directory (useful for builder tests
    that prepare their own fixture trees in tmp_path).

    If ``out`` is None a fresh temp DB path is allocated next to ``root``.
    """
    if out is None:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            out = Path(tf.name)
    build(
        root,
        out,
        defs_path=None,
        undef_unknown_configs=False,
        force_full=True,
        verbose=False,
    )
    return load_index(out)


def syms_of_kind(idx: IndexStore, kind: str) -> list[dict]:
    return idx.find_symbols(kind=kind)


def names_of_kind(idx: IndexStore, kind: str) -> set[str]:
    return {s["name"] for s in syms_of_kind(idx, kind)}


def callers_of(idx, name: str) -> set[str]:
    """Set of caller function names for the given target."""
    _, callers, _ = build_callgraph(idx)
    return set(callers.get(name, {}).keys())


def callees_of(idx, name: str) -> set[str]:
    calls, _, _ = build_callgraph(idx)
    return set(calls.get(name, {}).keys())
