# Originally ported from minish.ai/Agent-tools tsindex.py (Apache 2.0).
# See NOTICE for the list of modifications.
"""Markdown context-slice renderer.

`cmd_slice(idx, name, ...)` returns a markdown string with the named
symbol's source, plus optional callee bodies, caller bodies, referenced
types, and called macros — intended for use as LLM context.

Bodies are read from the ORIGINAL files on disk (line numbers were
preserved through the C/C++ preprocessor pipeline), so any kernel-style
`#if/#else` and other macros appear as the author wrote them.

The upstream `cmd_slice` printed to stdout for CLI use; this port
returns the string so the agent-cli tool layer can pass it through the
hashline wrapper.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

from agent_cli.code_index.callgraph import build_callgraph


def cmd_slice(
    idx,
    name: str,
    with_callees: bool,
    with_callers: bool,
    with_types: bool,
    with_macros: bool,
    depth: int,
    max_bytes: int,
):
    """Return a markdown blob with NAME's definition body plus optional context
    (callees, callers, types, macros). Intended as LLM context.

    Source is read from the ORIGINAL files (line numbers preserved by the
    preprocessor), so any kernel-style `#if/#else` and other macros appear
    as the author wrote them."""
    root = Path(idx["root"])

    # Index symbols by name (functions first, then everything else)
    by_name: dict[str, list] = defaultdict(list)
    for s in idx["symbols"]:
        by_name[s["name"]].append(s)
    kind_priority = {"function": 0, "constant": 1, "type": 2, "variable": 3}

    def pick(name: str, prefer_def: bool = True):
        """Pick the best symbol record for `name`. Prefer function definitions,
        then any definition, then any declaration."""
        cands = by_name.get(name) or []
        if not cands:
            return None
        cands = sorted(
            cands,
            key=lambda s: (
                0 if (prefer_def and s.get("is_definition")) else 1,
                kind_priority.get(s["kind"], 99),
                s["file"],
                s["line"],
            ),
        )
        return cands[0]

    target = pick(name)
    if target is None:
        return f"(no symbol {name!r} in index)"

    def read_lines(rel: str, line: int, end_line: int) -> str:
        path = root / rel
        try:
            txt = path.read_text(errors="replace")
        except OSError as e:
            return f"<could not read {rel}: {e}>"
        lines = txt.splitlines()
        return "\n".join(lines[line - 1 : end_line])

    def section(sym, header_prefix: str = "##") -> str:
        body = read_lines(sym["file"], sym["line"], sym["end_line"])
        loc = f"{sym['file']}:{sym['line']}-{sym['end_line']}"
        mods = " ".join(sym.get("modifiers") or [])
        title = f"{sym['name']}  ({sym['kind']}{', ' + mods if mods else ''})"
        lang = sym.get("language", "c")
        return f"{header_prefix} {title}  — {loc}\n\n```{lang}\n{body}\n```"

    out: list[str] = []
    out.append(f"# Slice: {name}\n")
    out.append("## Definition\n\n" + section(target, header_prefix="###"))

    # Callee bodies (transitive up to `depth`)
    if with_callees and target["kind"] == "function":
        calls_of, _, _ = build_callgraph(idx)
        seen: set[str] = {name}
        frontier = list(calls_of.get(name, Counter()).keys())
        for d in range(1, depth + 1):
            next_frontier: list[str] = []
            level_secs: list[str] = []
            for callee in frontier:
                if callee in seen:
                    continue
                seen.add(callee)
                sym = pick(callee)
                if (
                    sym is None
                    or sym["kind"] != "function"
                    or not sym.get("is_definition")
                ):
                    continue
                level_secs.append(section(sym, header_prefix="###"))
                next_frontier.extend(calls_of.get(callee, Counter()).keys())
            if level_secs:
                out.append(f"## Callees (depth {d})\n\n" + "\n\n".join(level_secs))
            frontier = next_frontier
            if not frontier:
                break

    # Caller bodies (transitive up to `depth`)
    if with_callers and target["kind"] == "function":
        _, callers_of, _ = build_callgraph(idx)
        seen: set[str] = {name}
        frontier = list(callers_of.get(name, Counter()).keys())
        for d in range(1, depth + 1):
            next_frontier: list[str] = []
            level_secs: list[str] = []
            for caller in frontier:
                if caller in seen:
                    continue
                seen.add(caller)
                sym = pick(caller)
                if (
                    sym is None
                    or sym["kind"] != "function"
                    or not sym.get("is_definition")
                ):
                    continue
                level_secs.append(section(sym, header_prefix="###"))
                next_frontier.extend(callers_of.get(caller, Counter()).keys())
            if level_secs:
                out.append(f"## Callers (depth {d})\n\n" + "\n\n".join(level_secs))
            frontier = next_frontier
            if not frontier:
                break

    # Types referenced inside the target's body
    if with_types and target.get("is_definition"):
        type_names = set()
        for r in idx["refs"]:
            if r["kind"] != "type":
                continue
            if (
                r["file"] == target["file"]
                and target["line"] <= r["line"] <= target["end_line"]
            ):
                type_names.add(r["name"])
        type_secs = []
        for tn in sorted(type_names):
            sym = pick(tn)
            if sym is None or sym["kind"] != "type":
                continue
            type_secs.append(section(sym, header_prefix="###"))
        if type_secs:
            out.append("## Types referenced\n\n" + "\n\n".join(type_secs))

    # Function-like macros invoked by the target
    if with_macros and target.get("is_definition"):
        macro_names = set()
        for r in idx["refs"]:
            if r["kind"] != "call":
                continue
            if (
                r["file"] == target["file"]
                and target["line"] <= r["line"] <= target["end_line"]
            ):
                macro_names.add(r["name"])
        macro_secs = []
        for mn in sorted(macro_names):
            sym = pick(mn)
            if sym is None:
                continue
            # Show fn-like C macros and object-like constants.
            is_macro = sym.get("kind_raw") in ("preproc_function_def", "preproc_def")
            if not is_macro and sym["kind"] != "constant":
                continue
            macro_secs.append(section(sym, header_prefix="###"))
        if macro_secs:
            out.append("## Macros used\n\n" + "\n\n".join(macro_secs))

    text = "\n\n".join(out)
    if max_bytes and len(text.encode("utf-8")) > max_bytes:
        text = text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
        text += f"\n\n_[truncated to {max_bytes} bytes]_\n"
    return text
