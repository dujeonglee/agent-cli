# Originally ported from minish.ai/Agent-tools tsindex.py (Apache 2.0).
# See NOTICE for the list of modifications.
"""Function-to-function call graph derived from the index.

`build_callgraph(idx)` walks every `kind in ('call', 'name')` ref,
resolves the enclosing function via a per-file bisect over function
ranges, and produces three views:

    calls_of[fn]    -> Counter[callee_fn]
    callers_of[fn]  -> Counter[caller_fn]
    sites_of[(caller, callee)] -> list of (file, line, ref_kind)

Edges only exist when both endpoints are functions defined or
declared in the index (so calls to externs, libc, and macros that
aren't indexed are excluded). Param shadowing is filtered to suppress
local-variable accesses that happen to share a function's name.
"""

from __future__ import annotations

import bisect
from collections import Counter, defaultdict

FUNCTION_KINDS = {"function"}


def build_fn_ranges(symbols):
    """file -> (sorted_start_lines, records[(start, end, name, params_set)])."""
    by_file = defaultdict(list)
    for s in symbols:
        if s["kind"] == "function" and s["is_definition"]:
            params = set(s.get("params") or [])
            by_file[s["file"]].append((s["line"], s["end_line"], s["name"], params))
    out = {}
    for f, recs in by_file.items():
        recs.sort()
        out[f] = ([r[0] for r in recs], recs)
    return out


def containing_fn(file, line, fn_ranges):
    """Return (name, params_set) of the function containing file:line, or None."""
    info = fn_ranges.get(file)
    if not info:
        return None
    starts, recs = info
    i = bisect.bisect_right(starts, line) - 1
    if i < 0:
        return None
    s, e, name, params = recs[i]
    return (name, params) if s <= line <= e else None


def build_callgraph(idx):
    """Build a strict function-to-function call graph.

    Edges only when BOTH endpoints are functions we have in the index
    (definitions or prototypes — both stored as kind='function' with
    is_definition true/false). Calls to macros, externs (kfree etc.)
    and reads of variables are excluded — those live in the broader
    `refs` query, not the call graph.

    Param shadowing is filtered (a ref whose name matches the enclosing
    function's parameter is a local var access, not a real call).

    Returns (calls_of, callers_of, sites_of).
      calls_of[fn]   -> Counter[callee_fn]
      callers_of[fn] -> Counter[caller_fn]
      sites_of[(caller, callee)] -> list of (file, line, ref_kind)"""
    fn_ranges = build_fn_ranges(idx["symbols"])
    # Names defined as functions/prototypes in our index
    fn_names: set[str] = {
        s["name"] for s in idx["symbols"] if s["kind"] in FUNCTION_KINDS
    }

    calls_of = defaultdict(Counter)
    callers_of = defaultdict(Counter)
    sites_of = defaultdict(list)
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
        calls_of[cf][r["name"]] += 1
        callers_of[r["name"]][cf] += 1
        sites_of[(cf, r["name"])].append((r["file"], r["line"], r["kind"]))
    return calls_of, callers_of, sites_of
