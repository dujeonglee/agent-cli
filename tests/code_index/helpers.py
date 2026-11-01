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

from agent_cli.code_index import IndexStore, build, load_index
from agent_cli.code_index import callees_of as _callees_of
from agent_cli.code_index import callers_of as _callers_of
from agent_cli.code_index.callgraph import (
    FUNCTION_KINDS,
    build_fn_ranges,
    containing_fn,
)

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
    return set(_callers_of(idx, name).counts)


def callees_of(idx, name: str) -> set[str]:
    return set(_callees_of(idx, name).counts)


def reference_callgraph(idx):
    """v10.93.0 까지의 `build_callgraph` — 전체 그래프를 한 번에 만든다. 이름 단위
    `callers_of`/`callees_of` (v10.94.0) 의 동치 오라클로만 남긴다(구현과 함께
    "실수로 같이" 바뀌지 않게 테스트 쪽에 둔다).

    Returns (calls_of, callers_of, sites_of)."""
    from collections import Counter

    fn_ranges = build_fn_ranges(idx["symbols"])
    fn_names = {s["name"] for s in idx["symbols"] if s["kind"] in FUNCTION_KINDS}
    site_kinds: dict = defaultdict(set)
    for r in idx["refs"]:
        if r["kind"] not in ("call", "name"):
            continue
        if r["name"] not in fn_names:
            continue
        ctx = containing_fn(r["file"], r["line"], fn_ranges)
        if ctx is None:
            continue
        cf, params = ctx
        if cf == r["name"] or r["name"] in params:
            continue
        site_kinds[(cf, r["name"], r["file"], r["line"])].add(r["kind"])
    calls_of = defaultdict(Counter)
    callers = defaultdict(Counter)
    sites_of = defaultdict(list)
    for (cf, callee, file, line), kinds in site_kinds.items():
        primary_kind = "call" if "call" in kinds else "name"
        calls_of[cf][callee] += 1
        callers[callee][cf] += 1
        sites_of[(cf, callee)].append((file, line, primary_kind))
    return calls_of, callers, sites_of
