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


def _brute_containing(file, line, defs_by_file):
    """담는 함수 정의 중 가장 작은 범위; 동률은 (시작, 끝, 이름) 순 마지막.
    `callgraph.containing_fn` 과 독립인 정의 그대로의 구현 (v10.95.0)."""
    cands = [
        d for d in defs_by_file.get(file, ()) if d["line"] <= line <= d["end_line"]
    ]
    if not cands:
        return None
    size = min(d["end_line"] - d["line"] for d in cands)
    best = max(
        (d for d in cands if d["end_line"] - d["line"] == size),
        key=lambda d: (d["line"], d["end_line"], d["name"]),
    )
    return best["name"], set(best.get("params") or [])


def reference_callgraph(idx):
    """옛 `build_callgraph` 의 모양 — 전체 그래프를 한 번에 만든다. 이름 단위
    `callers_of`/`callees_of` (v10.94.0) 의 동치 오라클. v10.95.0 부터 감싼 함수는
    구현을 import 하지 않는 브루트포스(`_brute_containing`)로 찾는다 — 종전엔 같은
    `containing_fn` 을 써서 결함까지 공유했다.

    Returns (calls_of, callers_of, sites_of)."""
    from collections import Counter

    defs_by_file: dict = defaultdict(list)
    for s in idx["symbols"]:
        if s["kind"] == "function" and s["is_definition"]:
            defs_by_file[s["file"]].append(s)
    fn_names = {s["name"] for s in idx["symbols"] if s["kind"] == "function"}
    site_kinds: dict = defaultdict(set)
    for r in idx["refs"]:
        if r["kind"] not in ("call", "name"):
            continue
        if r["name"] not in fn_names:
            continue
        ctx = _brute_containing(r["file"], r["line"], defs_by_file)
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
