"""Name resolution stored in the index (v10.96.0, docs/code-index/DESIGN.md §21).

The builder writes every file's scopes, bindings and refs to the tables, then
asks `resolve_files(conn, files)` to resolve the refs of the files that need it
and write `refs.resolution`/`refs.target_id`, and records in `resolve_deps`
what each file's resolution read (other files' parse rows, module names,
relative paths, function names). Resolution reads only the parse rows — no
syntax tree, no other file's resolution — so an incremental build re-resolves
exactly the files with a recorded input that changed.

Languages with scope rules (`schema.SCOPE_RESOLVED_LANGUAGES`) have a resolver
module here (`python.py`). The others get one function scope per function
definition with its parameters as bindings (`function_scopes`); their refs are
`local` when a parameter of an enclosing function has the name and otherwise
`candidates` — matched by name only, as before v10.96.0 — or `external` when no
function of that name exists in the language.
"""

from __future__ import annotations

import bisect

from agent_cli.code_index.schema import SCOPE_RESOLVED_LANGUAGES, Binding, Scope


def function_scopes(syms: list[dict], refs: list[dict], end_line: int):
    """언어 규칙이 없는 파일의 ``(scopes, bindings)`` — 모듈 하나 + 함수 정의마다
    함수 범위(가장 안쪽 규칙의 중첩), 인자는 ``param`` 바인딩. ``refs`` 의 각
    참조에 그 줄을 담는 가장 안쪽 함수 범위를 ``scope`` 로 적는다."""
    scopes: list[Scope] = [Scope(kind="module", start_line=1, end_line=end_line)]
    bindings: list[Binding] = []
    defs = sorted(
        (s for s in syms if s["kind"] == "function" and s.get("is_definition")),
        key=lambda s: (s["line"], -s["end_line"], s["name"]),
    )
    stack: list[int] = []  # 열려 있는 범위 (scopes 인덱스)
    starts: list[int] = []
    order: list[int] = []
    for s in defs:
        while stack and scopes[stack[-1]].end_line < s["line"]:
            stack.pop()
        idx = len(scopes)
        scopes.append(
            Scope(
                kind="function",
                start_line=s["line"],
                end_line=s["end_line"],
                parent=stack[-1] if stack else 0,
                def_line=s["line"],
                def_name=s["name"],
            )
        )
        for p in s.get("params") or []:
            bindings.append(Binding(scope=idx, name=p, kind="param", line=s["line"]))
        stack.append(idx)
        starts.append(s["line"])
        order.append(idx)
    for r in refs:
        i = bisect.bisect_right(starts, r["line"]) - 1
        sc = order[i] if i >= 0 else 0
        while sc and scopes[sc].end_line < r["line"]:
            sc = scopes[sc].parent or 0
        r["scope"] = sc
    return scopes, bindings


def resolve_files(conn, files: list[str]) -> None:
    """``files`` 의 참조를 해석해 표에 적고, 읽은 것을 ``resolve_deps`` 에 바꿔 적는다
    (같은 연결의 트랜잭션 안에서)."""
    from agent_cli.code_index.resolve import python as py

    by_lang: dict[str, list[str]] = {}
    for i in range(0, len(files), 500):
        chunk = files[i : i + 500]
        marks = ",".join("?" for _ in chunk)
        for path, lang in conn.execute(
            f"SELECT path, language FROM files WHERE path IN ({marks})", chunk
        ):
            by_lang.setdefault(lang, []).append(path)
    deps: dict[str, set[str]] = {}
    for lang, paths in by_lang.items():
        if lang in SCOPE_RESOLVED_LANGUAGES:
            try:
                deps.update(py.Resolver(conn).resolve(paths))
            except RecursionError as e:
                # 재노출 사슬이 수백 단이면 파이썬 재귀 한도에 닿는다 — 상한으로 자르면 답이
                # 해석 묶음에 기대니(DESIGN §21) 빌드를 분명하게 실패시킨다
                raise RuntimeError(
                    "code_index: an import chain is too deep to resolve "
                    "(Python recursion limit)"
                ) from e
        else:
            deps.update(_resolve_by_name(conn, lang, paths))
    for i in range(0, len(files), 500):
        chunk = files[i : i + 500]
        marks = ",".join("?" for _ in chunk)
        conn.execute(f"DELETE FROM resolve_deps WHERE file IN ({marks})", chunk)
    conn.executemany(
        "INSERT INTO resolve_deps(dep, file) VALUES (?, ?)",
        [(d, f) for f, ds in deps.items() for d in ds],
    )


def _resolve_by_name(conn, lang: str, paths: list[str]) -> dict[str, set[str]]:
    """규칙 없는 언어: 감싼 함수들의 인자면 ``local``, 아니면 그 이름의 함수가 이
    언어에 있으면 ``candidates``, 없으면 ``external``. 읽은 것은 그 함수 이름들."""
    fn_names = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT name FROM symbols WHERE +kind = 'function' "
            "AND +language = ?",
            (lang,),
        )
    }
    updates: list[tuple] = []
    deps: dict[str, set[str]] = {}
    for path in paths:
        names: set[str] = set()
        parent: dict[int, int | None] = {}
        params: dict[int, set[str]] = {}
        for sid, pid in conn.execute(
            "SELECT id, parent_id FROM scopes WHERE file = ?", (path,)
        ):
            parent[sid] = pid
            params[sid] = set()
        for sid, name in conn.execute(
            "SELECT b.scope_id, b.name FROM bindings b JOIN scopes s "
            "ON s.id = b.scope_id WHERE s.file = ? AND b.kind = 'param'",
            (path,),
        ):
            params[sid].add(name)
        for rid, name, sid in conn.execute(
            "SELECT id, name, scope_id FROM refs WHERE file = ?", (path,)
        ):
            s = sid
            res = None
            while s is not None:
                if name in params.get(s, ()):
                    res = "local"
                    break
                s = parent.get(s)
            if res is None:
                names.add(name)
                res = "candidates" if name in fn_names else "external"
            updates.append((res, None, rid))
        deps[path] = {f"n:{lang}:{n}" for n in names}
    conn.executemany(
        "UPDATE refs SET resolution = ?, target_id = ? WHERE id = ?", updates
    )
    return deps
