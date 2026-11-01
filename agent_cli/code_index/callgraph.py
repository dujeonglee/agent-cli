# Originally ported from minish.ai/Agent-tools tsindex.py (Apache 2.0).
# See NOTICE for the list of modifications.
"""Function-to-function call graph, read from the stored name resolution.

v10.96.0 (docs/code-index/DESIGN.md §21): every ref was resolved when the
index was built (`agent_cli.code_index.resolve`), so a definition's callers
are the refs whose `target_id` is that definition, and its callees are the
refs inside it (`fn_symbol_id`) that resolved to a function. Nodes are
definitions, not bare names — two functions named `get` are two nodes.
What could not be pinned to one definition stays visible as such: a call on
an object of unknown type is a `candidates` ref, listed apart with the number
of definitions sharing the name.

Languages without scope rules (`schema.SCOPE_RESOLVED_LANGUAGES`) answer by
name as before: every ref of that name that is not an enclosing function's
parameter, for all definitions of the name together (`Edges.by_name_only`).
Self-calls are left out, as they always were.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent_cli.code_index.schema import SCOPE_RESOLVED_LANGUAGES


@dataclass
class Neighbor:
    """한쪽 이웃 하나 — 해석된 정의(``symbol_id``)나, 이름으로만 맞춘 것(``None``)."""

    label: str  # qualified name; 이름으로만 맞춘 이웃은 맨 이름
    name: str
    symbol_id: int | None
    sites: list = field(default_factory=list)  # [(file, line, ref_kind)] 참조 id 순
    n_definitions: int = 0  # 이름으로만 맞춘 이웃: 그 이름의 정의 수

    @property
    def count(self) -> int:
        return len(self.sites)


@dataclass
class Edges:
    """정의 하나(또는 이름으로만 맞춘 정의들)의 한쪽 이웃."""

    definitions: list  # [symbol dict] — 해석되는 언어면 하나
    language: str
    by_name_only: bool
    neighbors: list = field(default_factory=list)  # [Neighbor] 첫 사이트 순
    # callers 만: 그 이름을 타입 모를 객체에서 부른 자리(`?` 줄)
    unknown_sites: list = field(default_factory=list)
    unknown_definitions: int = 0


def definitions_named(store, name: str) -> list[dict]:
    """``name``(한정 이름 먼저, 없으면 맨 이름)인 함수 **정의** — 행 순."""
    return store.function_definitions(name)


def _dedupe(rows):
    """``[(key, file, line, kind)]`` → ``{key: [(file, line, kind)]}`` — 같은 자리의
    call+name 은 하나(call 우선), 첫 등장 순."""
    seen: dict = {}
    for key, file, line, kind in rows:
        k = (key, file, line)
        if k in seen:
            if kind == "call":
                seen[k] = "call"
        else:
            seen[k] = kind
    out: dict = {}
    for (key, file, line), kind in seen.items():
        out.setdefault(key, []).append((file, line, kind))
    return out


def _labels(store, ids) -> dict[int, tuple[str, str, str]]:
    """기호 id → ``(label, name, file)`` — 같은 한정 이름이 둘이면 ``@file`` 을 붙인다."""
    info = store.symbols_by_id(ids)
    by_label: dict[str, int] = {}
    for qn, _name, _file in info.values():
        by_label[qn] = by_label.get(qn, 0) + 1
    return {
        i: ((f"{qn}  @{file}" if by_label[qn] > 1 else qn), name, file)
        for i, (qn, name, file) in info.items()
    }


def callers_of(store, definition: dict) -> Edges:
    """``definition`` 을 부르는(또는 콜백으로 가리키는) 함수들."""
    lang = definition["language"]
    if lang not in SCOPE_RESOLVED_LANGUAGES:
        return _by_name_callers(
            store,
            store.function_definitions_exact(definition["name"], lang) or [definition],
        )
    edges = Edges(definitions=[definition], language=lang, by_name_only=False)
    rows = [
        (fn, file, line, kind)
        for fn, file, line, kind in store.refs_to(definition["id"])
        if fn != definition["id"]
    ]
    grouped = _dedupe(rows)
    labels = _labels(store, grouped.keys())
    for fn, sites in grouped.items():
        label, name, _file = labels[fn]
        edges.neighbors.append(
            Neighbor(label=label, name=name, symbol_id=fn, sites=sites)
        )
    unknown = _dedupe(
        [(0, f, ln, k) for f, ln, k in store.candidate_refs(definition["name"], lang)]
    )
    edges.unknown_sites = unknown.get(0, [])
    if edges.unknown_sites:
        edges.unknown_definitions = store.definition_count(definition["name"], lang)
    return edges


def callees_of(store, definition: dict) -> Edges:
    """``definition`` 이 부르는(또는 가리키는) 함수들 — 해석된 것은 정의로, 타입 모를
    객체에서 부른 것은 이름과 정의 수로."""
    lang = definition["language"]
    if lang not in SCOPE_RESOLVED_LANGUAGES:
        return _by_name_callees(
            store,
            store.function_definitions_exact(definition["name"], lang) or [definition],
        )
    edges = Edges(definitions=[definition], language=lang, by_name_only=False)
    resolved = []
    unknown = []
    for name, file, line, kind, res, target in store.refs_in_functions(
        [definition["id"]]
    ):
        if res == "resolved" and target is not None and target != definition["id"]:
            resolved.append((target, file, line, kind))
        elif res == "candidates":
            unknown.append((name, file, line, kind))
    grouped = _dedupe(resolved)
    functions = store.function_ids(grouped.keys())
    labels = _labels(store, [t for t in grouped if t in functions])
    for target, sites in grouped.items():
        if target not in functions:
            continue  # 클래스·변수 — 오늘처럼 함수만
        label, name, _file = labels[target]
        edges.neighbors.append(
            Neighbor(label=label, name=name, symbol_id=target, sites=sites)
        )
    for name, sites in _dedupe(unknown).items():
        n = store.definition_count(name, lang)
        if n:
            edges.neighbors.append(
                Neighbor(
                    label=name, name=name, symbol_id=None, sites=sites, n_definitions=n
                )
            )
    return edges


def _by_name_callers(store, defs: list[dict]) -> Edges:
    lang = defs[0]["language"]
    name = defs[0]["name"]
    edges = Edges(definitions=defs, language=lang, by_name_only=True)
    rows = list(store.candidate_refs(name, lang, with_fn=True))
    labels = _labels(store, {r[0] for r in rows})
    grouped = _dedupe(
        [(fn, f, ln, k) for fn, f, ln, k in rows if labels[fn][1] != name]
    )
    for fn, sites in grouped.items():
        label, fname, _file = labels[fn]
        edges.neighbors.append(
            Neighbor(label=label, name=fname, symbol_id=fn, sites=sites)
        )
    return edges


def _by_name_callees(store, defs: list[dict]) -> Edges:
    lang = defs[0]["language"]
    name = defs[0]["name"]
    edges = Edges(definitions=defs, language=lang, by_name_only=True)
    rows = [
        (rname, file, line, kind)
        for rname, file, line, kind, res, _t in store.refs_in_functions(
            [d["id"] for d in defs]
        )
        if res == "candidates" and rname != name
    ]
    for rname, sites in _dedupe(rows).items():
        edges.neighbors.append(
            Neighbor(
                label=rname,
                name=rname,
                symbol_id=None,
                sites=sites,
                n_definitions=store.definition_count(rname, lang),
            )
        )
    return edges
