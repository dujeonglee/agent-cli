# Originally ported from minish.ai/Agent-tools tsindex.py (Apache 2.0).
# See NOTICE for the list of modifications.
"""Function-to-function call graph, asked one name at a time.

`callers_of(store, name)` / `callees_of(store, name)` answer one node's
neighbours with SQL against the index (v10.94.0, docs/code-index/DESIGN.md
§19). Until then `build_callgraph(idx)` loaded every symbol and every ref
(≈650k rows on agent-cli) into Python and built the whole graph for each
query — 1.4–1.6 s and a 556 MB peak to read one entry. The old algorithm
lives on as the test oracle (`tests/code_index/helpers.reference_callgraph`)
and the output is byte-identical.

The enclosing function of a line is the innermost function definition that
contains it (v10.95.0, DESIGN §20). Until then a call that came after a
nested function ended — Python closures — was attributed to nobody.

Edges only exist when both endpoints are functions defined or declared
in the index (so calls to externs, libc, and macros that aren't indexed
are excluded). Param shadowing is filtered to suppress local-variable
accesses that happen to share a function's name. Nodes are bare names —
same-named functions in different files are one node.
"""

from __future__ import annotations

import bisect
from collections import Counter, defaultdict
from dataclasses import dataclass, field

FUNCTION_KINDS = {"function"}


def build_fn_ranges(symbols):
    """file -> (starts, records[(start, end, name, params_set)], parents).

    정렬 키는 (시작, 끝의 역순, 이름) — 같은 시작이면 큰 범위가 먼저라 안쪽이 뒤에
    온다. ``parents[i]`` 는 레코드 i 를 담는 가장 가까운 레코드의 인덱스(없으면 -1),
    스택 한 번으로 만든다 (v10.95.0, DESIGN §20)."""
    by_file = defaultdict(list)
    for s in symbols:
        if s["kind"] == "function" and s["is_definition"]:
            params = set(s.get("params") or [])
            by_file[s["file"]].append((s["line"], s["end_line"], s["name"], params))
    out = {}
    for f, recs in by_file.items():
        recs.sort(key=lambda r: (r[0], -r[1], r[2]))
        parents: list[int] = []
        stack: list[int] = []  # 아직 열려 있는 조상
        for i, (s, _e, _n, _p) in enumerate(recs):
            while stack and recs[stack[-1]][1] < s:
                stack.pop()
            parents.append(stack[-1] if stack else -1)
            stack.append(i)
        out[f] = ([r[0] for r in recs], recs, parents)
    return out


def containing_fn(file, line, fn_ranges):
    """Return (name, params_set) of the innermost function containing file:line,
    or None. 시작 줄이 line 이하인 마지막 레코드에서 출발해, 이미 끝난 함수면
    부모로 올라간다 — 종전엔 그 하나만 보고 끝났으면 None 이었다(안쪽 함수 뒤의
    바깥 본문 호출이 어디에도 붙지 않았다)."""
    info = fn_ranges.get(file)
    if not info:
        return None
    starts, recs, parents = info
    i = bisect.bisect_right(starts, line) - 1
    while i != -1 and recs[i][1] < line:
        i = parents[i]
    if i == -1:
        return None
    _s, _e, name, params = recs[i]
    return (name, params)


@dataclass
class Edges:
    """한 함수의 한쪽 이웃. ``counts`` 의 키 순서 = 그 이웃의 첫 사이트의 참조 id 순
    (slice 섹션 순서), ``sites`` 의 목록도 id 순."""

    counts: Counter = field(default_factory=Counter)
    sites: dict = field(default_factory=dict)  # 이웃 → [(file, line, ref_kind)]


def _collect(edges: Edges, site_kinds: dict) -> Edges:
    """``{(이웃, file, line): {kind…}}`` (첫 등장 순) → Edges. 같은 자리의
    call+name 이중 방출은 사이트 하나, 둘 다면 ``call`` (옛 build_callgraph 와 같다)."""
    for (other, file, line), kinds in site_kinds.items():
        edges.counts[other] += 1
        edges.sites.setdefault(other, []).append(
            (file, line, "call" if "call" in kinds else "name")
        )
    return edges


def callers_of(store, name: str) -> Edges:
    """``name`` 을 부르는(또는 콜백으로 가리키는) 색인된 함수들."""
    edges = Edges()
    if not store.is_function_name(name):
        return edges
    ranges: dict = {}  # 이 호출 안에서만 — 파일마다 함수 범위 한 번
    loaded: set[str] = set()
    site_kinds: dict = {}
    for _id, file, line, kind in store.call_sites_by_name(name):
        if file not in loaded:
            loaded.add(file)
            ranges.update(build_fn_ranges(store.function_ranges_in_file(file)))
        ctx = containing_fn(file, line, ranges)
        if ctx is None:
            continue
        cf, params = ctx
        if cf == name or name in params:
            continue
        site_kinds.setdefault((cf, file, line), set()).add(kind)
    return _collect(edges, site_kinds)


def callees_of(store, name: str) -> Edges:
    """``name`` 이 부르는(또는 가리키는) 색인된 함수들 — 이름이 ``name`` 인 모든
    정의의 합. 중첩 함수 안의 호출은 그 함수 몫이다(`containing_fn`)."""
    edges = Edges()
    defs = store.function_defs(name)
    if not defs:
        return edges
    ranges: dict = {}
    loaded: set[str] = set()
    hits: dict = {}  # ref id → (callee, file, line, kind) — 겹친 정의의 같은 참조는 한 번
    for file, start, end in defs:
        if file not in loaded:
            loaded.add(file)
            ranges.update(build_fn_ranges(store.function_ranges_in_file(file)))
        for rid, rname, line, kind in store.call_sites_in_range(file, start, end):
            if rid in hits:
                continue
            ctx = containing_fn(file, line, ranges)
            if ctx is None or ctx[0] != name:
                continue
            if rname == name or rname in ctx[1]:
                continue
            hits[rid] = (rname, file, line, kind)
    callees = store.function_names_among(h[0] for h in hits.values())
    site_kinds: dict = {}
    for rid in sorted(hits):
        rname, file, line, kind = hits[rid]
        if rname in callees:
            site_kinds.setdefault((rname, file, line), set()).add(kind)
    return _collect(edges, site_kinds)
