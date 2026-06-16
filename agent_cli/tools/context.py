"""read_context tool — lets the LLM browse + structurally query session history.

Modes:
  - list: show all sessions (session_id, time, first message)
  - search: structured JSON query over history.jsonl records
  - fetch: retrieve full turn(s) at a loc returned by search

Search filters (combine freely; at least one required) match against each
record's retrieval fields (written by ``context.manager`` enrich, and derived
on read via the shared ``_classify_record`` so any record shape works):
  - ``keyword``  : substring over the record's flat ``text`` surface
  - ``kind``     : query | action | observation | final | raw | system
                   (str or list; replaces the old reasoning/tool/... scopes)
  - ``tool``     : tool-name membership (e.g. "read_file"; str or list)
  - ``author``   : nickname (web multi-user attribution)
  - ``turn``     : int, {"from","to"}, or [from,to] — turn range
  - ``sessions`` : (omitted)=current only · "all"=every · "<id>"/["<id>",…]

Returns one block per matching record (loc + kind/turn/tools/author + text
preview). Previews collapse whitespace and cap at 200 chars. The 50-match cap
is honored at append time so a busy session can't starve later ones.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_cli.context.session import get_session_dir, list_sessions
from agent_cli.tools.base import Tool
from agent_cli.tools.result import ToolResult

_SESSIONS_BASE = Path(".agent-cli") / "sessions"

# Record kinds for the mode=search ``kind`` filter (mirrors
# context.manager._classify_record output).
_VALID_KINDS: frozenset[str] = frozenset(
    {"query", "action", "observation", "final", "raw", "system"}
)
_OBSERVATION_PREFIX = "Observation:"  # fetch-mode rendering label
_PREVIEW_CAP = 200
_MAX_MATCHES = 50


# ── Public entry point ────────────────────────────────────────────


def tool_read_context(args: dict, *, session_dir: Path | None = None) -> ToolResult:
    """Dispatch to mode=list, mode=search, or mode=fetch.

    ``session_dir`` is the current session's directory; used to resolve
    the default ``sessions`` filter for search (current-session-only).
    Fetch is loc-driven and does not depend on session_dir.
    """
    mode = args.get("mode", "list")
    if mode == "list":
        return _mode_list()
    if mode == "search":
        return _mode_search(
            keyword=args.get("keyword", ""),
            kind=args.get("kind"),
            tool=args.get("tool"),
            author=args.get("author"),
            turn=args.get("turn"),
            sessions=args.get("sessions"),
            session_dir=session_dir,
        )
    if mode == "fetch":
        return _mode_fetch(
            loc=args.get("loc"),
            range_=args.get("range"),
        )
    return ToolResult(
        False, error=f"unknown mode '{mode}'. Use 'list', 'search', or 'fetch'."
    )


# ── mode=list ─────────────────────────────────────────────────────


def _session_title(meta) -> str:
    """Short title for a session = its first user message (the replacement for
    the removed ``query`` meta field, which now lives in history.jsonl)."""
    hp = get_session_dir(meta) / "history.jsonl"
    if not hp.exists():
        return "(no history)"
    try:
        for line in hp.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("role") == "user":
                txt = (rec.get("content") or "").strip().replace("\n", " ")
                return (txt[:60] + "…") if len(txt) > 60 else (txt or "(empty)")
    except Exception:
        pass
    return "(no query)"


def _mode_list() -> ToolResult:
    sessions = list_sessions()
    if not sessions:
        return ToolResult(True, output="No previous sessions found.")
    lines = [f"- {s.session_id} [{s.updated_at}] {_session_title(s)}" for s in sessions]
    return ToolResult(True, output="\n".join(lines))


# ── mode=search ───────────────────────────────────────────────────


def _mode_search(
    keyword: str,
    kind: Any,
    tool: Any,
    author: Any,
    turn: Any,
    sessions: Any,
    session_dir: Path | None,
) -> ToolResult:
    """Structured JSON query over history.jsonl records.

    Filters (combine freely; at least one required): ``keyword`` substring over
    the record's flat ``text`` surface, ``kind`` (query/action/observation/
    final/raw), ``tool`` (tool-name membership), ``author`` (nickname), ``turn``
    (int or {from,to}). Records are classified on read via the shared
    ``_classify_record`` so the query never re-guesses prefix conventions and
    works regardless of whether the record carries the persisted enrich keys.
    """
    try:
        kinds = _normalize_kinds(kind)
        tools = _normalize_str_set(tool)
        turn_range = _normalize_turn(turn)
    except ValueError as e:
        return ToolResult(False, error=str(e))
    author_f = str(author).strip() if author else ""
    keyword = keyword or ""
    if not (keyword or kinds or tools or author_f or turn_range):
        return ToolResult(
            False,
            error=(
                "mode='search' needs at least one filter: keyword, kind, tool, "
                "author, or turn."
            ),
        )

    target_dirs, error = _resolve_session_dirs(sessions, session_dir)
    if error:
        return ToolResult(False, error=error)
    if target_dirs is None:
        # No session context (headless / mode=list-equivalent fallback)
        return ToolResult(
            True,
            output=(
                "No current session resolved; pass sessions='all' or "
                "specific session_id(s) to search."
            ),
        )
    if not target_dirs:
        return ToolResult(True, output="No sessions found.")

    matches: list[dict] = []
    truncated = False

    for sdir in target_dirs:
        if truncated:
            break
        if not sdir.is_dir():
            continue
        session_id = sdir.name
        for history_path in sorted(sdir.rglob("history.jsonl")):
            if truncated:
                break
            rel_path = history_path.relative_to(sdir)
            try:
                with open(history_path, encoding="utf-8") as f:
                    for line_num, raw in enumerate(f, 1):
                        line = raw.strip()
                        if not line:
                            continue
                        try:
                            msg = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        match = _match_record(
                            msg,
                            keyword=keyword,
                            kinds=kinds,
                            tools=tools,
                            author=author_f,
                            turn_range=turn_range,
                        )
                        if not match:
                            continue
                        match["loc"] = f"{session_id}/{rel_path}:{line_num}"
                        matches.append(match)
                        if len(matches) >= _MAX_MATCHES:
                            truncated = True
                            break
            except OSError:
                continue

    return _format_search_result(
        keyword, kinds, tools, author_f, turn_range, matches, truncated
    )


# ── Argument normalization ────────────────────────────────────────


def _normalize_kinds(kind: Any) -> set[str]:
    """Coerce ``kind`` into a validated set (empty = no kind filter).

    Accepts None (→ {}), str (→ {str}), or list. Unknown values raise
    ``ValueError`` so the model gets explicit feedback.
    """
    if kind is None:
        return set()
    if isinstance(kind, str):
        kind = [kind]
    if not isinstance(kind, list):
        raise ValueError(
            f"kind must be a string or array of strings, got {type(kind).__name__}"
        )
    invalid = [k for k in kind if k not in _VALID_KINDS]
    if invalid:
        raise ValueError(
            f"invalid kind value(s): {invalid}. Valid: {sorted(_VALID_KINDS)}"
        )
    return set(kind)


def _normalize_str_set(value: Any) -> set[str]:
    """Coerce a str / list-of-str into a set (empty = no filter)."""
    if value is None:
        return set()
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {str(v) for v in value}
    raise ValueError(f"expected string or array, got {type(value).__name__}")


def _normalize_turn(turn: Any) -> tuple[int, int] | None:
    """Coerce ``turn`` into an inclusive ``(from, to)`` range, or None.

    Accepts int (→ (n, n)), {"from", "to"} dict, or [from, to] list.
    """
    if turn is None:
        return None
    if isinstance(turn, bool):  # guard: bool is an int subclass
        raise ValueError("turn must be an integer or range")
    if isinstance(turn, int):
        return (turn, turn)
    if isinstance(turn, dict):
        lo = turn.get("from")
        hi = turn.get("to")
        lo = 0 if lo is None else int(lo)
        hi = 10**9 if hi is None else int(hi)
        return (lo, hi)
    if isinstance(turn, list) and len(turn) == 2:
        return (int(turn[0]), int(turn[1]))
    raise ValueError("turn must be an int, {from,to}, or [from,to]")


def _resolve_session_dirs(
    sessions: Any, current_session_dir: Path | None
) -> tuple[list[Path] | None, str | None]:
    """Resolve the ``sessions`` argument into directories to search.

    Returns (dirs, error). ``dirs`` is None when the request cannot be
    satisfied (no current session and no explicit selector); empty list
    is a valid "no sessions exist on disk" signal.
    """
    # Default: current session only
    if sessions is None:
        if current_session_dir is None:
            return None, None
        return [Path(current_session_dir)], None

    # Single string → list
    if isinstance(sessions, str):
        sessions = [sessions]
    if not isinstance(sessions, list):
        return None, (
            f"sessions must be a string or array of strings, "
            f"got {type(sessions).__name__}"
        )

    # 'all' is a special wildcard. It cannot be mixed with specific IDs.
    if "all" in sessions:
        if len(sessions) > 1:
            return None, ("sessions='all' cannot be combined with specific session ids")
        if not _SESSIONS_BASE.is_dir():
            return [], None
        return [d for d in sorted(_SESSIONS_BASE.iterdir()) if d.is_dir()], None

    # Specific IDs
    dirs: list[Path] = []
    missing: list[str] = []
    for sid in sessions:
        if not isinstance(sid, str):
            return None, (f"sessions must contain strings, got {type(sid).__name__}")
        d = _SESSIONS_BASE / sid
        if d.is_dir():
            dirs.append(d)
        else:
            missing.append(sid)
    if missing:
        return None, f"session(s) not found: {missing}"
    return dirs, None


# ── Per-turn matching ─────────────────────────────────────────────


def _match_record(
    msg: dict,
    *,
    keyword: str,
    kinds: set[str],
    tools: set[str],
    author: str,
    turn_range: tuple[int, int] | None,
) -> dict | None:
    """Apply the field filters to one record. Returns a result dict (kind /
    tools / author / turn / preview) or None when it doesn't match.

    Classifies the record on read via the shared ``_classify_record`` — so the
    same logic that writes the enrich keys also reads them, and the query works
    on any record shape (no prefix-convention re-guessing)."""
    from agent_cli.context.manager import _classify_record

    kind, rec_tools, text = _classify_record(msg)
    if kinds and kind not in kinds:
        return None
    if tools and not (set(rec_tools) & tools):
        return None
    if author and (msg.get("author") or "") != author:
        return None
    if turn_range is not None:
        t = msg.get("turn")
        if not isinstance(t, int) or not (turn_range[0] <= t <= turn_range[1]):
            return None
    if keyword and keyword.lower() not in text.lower():
        return None
    return {
        "kind": kind,
        "tools": rec_tools,
        "author": msg.get("author"),
        "turn": msg.get("turn"),
        "preview": _format_text(text),
    }


# ── Preview formatting ────────────────────────────────────────────


def _format_text(text: str, cap: int = _PREVIEW_CAP) -> str:
    """Collapse all whitespace to single spaces and cap at ``cap`` chars."""
    collapsed = " ".join(text.split())
    if len(collapsed) > cap:
        return collapsed[: cap - 3] + "..."
    return collapsed


# ── Result rendering ──────────────────────────────────────────────


def _describe_filters(
    keyword: str,
    kinds: set[str],
    tools: set[str],
    author: str,
    turn_range: tuple[int, int] | None,
) -> str:
    parts = []
    if keyword:
        parts.append(f"keyword='{keyword}'")
    if kinds:
        parts.append(f"kind={sorted(kinds)}")
    if tools:
        parts.append(f"tool={sorted(tools)}")
    if author:
        parts.append(f"author='{author}'")
    if turn_range:
        parts.append(f"turn={turn_range[0]}..{turn_range[1]}")
    return ", ".join(parts)


def _format_search_result(
    keyword: str,
    kinds: set[str],
    tools: set[str],
    author: str,
    turn_range: tuple[int, int] | None,
    matches: list[dict],
    truncated: bool,
) -> ToolResult:
    desc = _describe_filters(keyword, kinds, tools, author, turn_range)
    if not matches:
        return ToolResult(True, output=f"No matches ({desc}).")

    header = f"Search results ({desc}) — {len(matches)} matches"
    if truncated:
        header += f" (capped at {_MAX_MATCHES})"
    header += ":\n"

    blocks = []
    for m in matches:
        meta = [f"kind={m['kind']}"]
        if m.get("turn") is not None:
            meta.append(f"turn={m['turn']}")
        if m.get("tools"):
            meta.append(f"tools={m['tools']}")
        if m.get("author"):
            meta.append(f"author={m['author']}")
        head = f"-- {m['loc']}  ({', '.join(meta)})"
        blocks.append(f"{head}\n   {m['preview']}")

    footer = (
        "\n\nUse mode='fetch' with loc='<above>' to read the full turn "
        "(add range=N to include adjacent turns)."
    )
    return ToolResult(True, output=header + "\n\n".join(blocks) + footer)


# ── mode=fetch ────────────────────────────────────────────────────

# Caps protect against runaway requests; both are enforced at
# normalization time so an error fires before any disk I/O.
_FETCH_MAX_LOCS = 10
_FETCH_MAX_RANGE = 5


def _mode_fetch(loc: Any, range_: Any) -> ToolResult:
    """Retrieve full turn content at one or more locations.

    Semantics: all-or-nothing. Any malformed loc, missing file, or
    out-of-range line aborts the entire request — partial results would
    confuse the caller about what succeeded.
    """
    try:
        locs = _normalize_loc(loc)
    except ValueError as e:
        return ToolResult(False, error=str(e))

    try:
        rng = _normalize_range(range_)
    except ValueError as e:
        return ToolResult(False, error=str(e))

    groups: list[dict] = []
    for loc_str in locs:
        try:
            session_id, rel_path, line_num = _parse_loc(loc_str)
        except ValueError as e:
            return ToolResult(False, error=str(e))

        history_path = _SESSIONS_BASE / session_id / rel_path
        if not history_path.is_file():
            return ToolResult(
                False, error=f"file not found for loc '{loc_str}': {history_path}"
            )

        try:
            with open(history_path, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError as e:
            return ToolResult(False, error=f"failed to read {history_path}: {e}")

        if line_num < 1 or line_num > len(lines):
            return ToolResult(
                False,
                error=(
                    f"line_num {line_num} out of range for '{loc_str}' "
                    f"(file has {len(lines)} lines)"
                ),
            )

        idx = line_num - 1
        start = max(0, idx - rng)
        end = min(len(lines), idx + rng + 1)

        turns = []
        for i in range(start, end):
            raw_line = lines[i].strip()
            if not raw_line:
                continue
            try:
                msg = json.loads(raw_line)
            except json.JSONDecodeError:
                msg = {"_raw": raw_line}
            turns.append(
                {
                    "loc": f"{session_id}/{rel_path}:{i + 1}",
                    "is_target": (i == idx),
                    "msg": msg,
                }
            )

        groups.append({"loc": loc_str, "range": rng, "turns": turns})

    return _format_fetch_result(groups)


def _normalize_loc(loc: Any) -> list[str]:
    """Coerce ``loc`` into a non-empty list of strings, capped at
    ``_FETCH_MAX_LOCS``."""
    if loc is None:
        raise ValueError("loc is required for mode='fetch'")
    if isinstance(loc, str):
        loc = [loc]
    if not isinstance(loc, list):
        raise ValueError(
            f"loc must be a string or array of strings, got {type(loc).__name__}"
        )
    if not loc:
        raise ValueError("loc list must be non-empty")
    if len(loc) > _FETCH_MAX_LOCS:
        raise ValueError(f"max {_FETCH_MAX_LOCS} locations per fetch, got {len(loc)}")
    for x in loc:
        if not isinstance(x, str):
            raise ValueError(f"loc entries must be strings, got {type(x).__name__}")
    return list(loc)


def _normalize_range(range_: Any) -> int:
    """Validate ``range`` arg. None → 0. Outside [0, max] → error."""
    if range_ is None:
        return 0
    if isinstance(range_, bool) or not isinstance(range_, int):
        raise ValueError(
            f"range must be an integer 0-{_FETCH_MAX_RANGE}, "
            f"got {type(range_).__name__}"
        )
    if range_ < 0 or range_ > _FETCH_MAX_RANGE:
        raise ValueError(f"range must be in 0-{_FETCH_MAX_RANGE}, got {range_}")
    return range_


def _parse_loc(loc: str) -> tuple[str, str, int]:
    """Parse '{session_id}/{rel_path}:{line_num}'.

    Uses rpartition on ``:`` so paths containing colons (rare on POSIX
    but possible on Windows-mounted shares) parse correctly when the
    line_num suffix is well-formed.
    """
    if ":" not in loc:
        raise ValueError(f"loc must end with ':<line_num>', got {loc!r}")
    main, _, line_part = loc.rpartition(":")
    try:
        line_num = int(line_part)
    except ValueError as e:
        raise ValueError(
            f"line_num in loc must be an integer, got {line_part!r}"
        ) from e
    if line_num < 1:
        raise ValueError(f"line_num must be >= 1, got {line_num} in {loc!r}")
    if "/" not in main:
        raise ValueError(
            f"loc must be '{{session_id}}/{{path}}:{{line_num}}', got {loc!r}"
        )
    session_id, _, rel_path = main.partition("/")
    if not session_id or not rel_path:
        raise ValueError(f"loc requires non-empty session_id and rel_path, got {loc!r}")
    return session_id, rel_path, line_num


def _format_fetch_result(groups: list[dict]) -> ToolResult:
    """Render fetch groups. Multi-line field values use YAML block style."""
    parts: list[str] = []
    if len(groups) > 1:
        parts.append(f"Fetched {len(groups)} locations:")

    for g in groups:
        head = f"=== {g['loc']}"
        if g["range"] > 0:
            head += f" (range +/-{g['range']})"
        head += " ==="
        block = [head]

        for turn in g["turns"]:
            target = "    <- target" if turn["is_target"] else ""
            msg = turn["msg"]
            role = msg.get("role", "?")
            block.append(f"\n-- {turn['loc']} [{role}]{target}")

            if "_raw" in msg:
                block.append(f"   (corrupt JSON line)\n   raw: {msg['_raw']}")
                continue

            # Order: thought, action, action_input, content, artifact.
            # Matches the natural reading order of a ReAct turn.
            if "thought" in msg and msg["thought"] is not None:
                block.append(_render_field("thought", msg["thought"]))
            if "action" in msg and msg["action"] is not None:
                block.append(_render_field("action", msg["action"]))
            if "action_input" in msg and msg["action_input"] is not None:
                ai = msg["action_input"]
                try:
                    ai_str = json.dumps(ai, ensure_ascii=False)
                except (TypeError, ValueError):
                    ai_str = str(ai)
                block.append(_render_field("action_input", ai_str))
            if "content" in msg and msg["content"] is not None:
                content = msg["content"]
                label = (
                    "observation"
                    if isinstance(content, str)
                    and content.startswith(_OBSERVATION_PREFIX)
                    else "content"
                )
                block.append(_render_field(label, content))
            if "artifact" in msg and msg["artifact"]:
                block.append(f"   [artifact: {msg['artifact']}]")

        parts.append("\n".join(block))

    return ToolResult(True, output="\n\n".join(parts))


def _render_field(label: str, value: Any) -> str:
    """Render a turn field; multi-line values use YAML block style.

    Block style:
        label:
          line one
          line two
    Inline style for single-line values:
        label: value
    """
    s = str(value)
    if "\n" in s:
        out = [f"   {label}:"]
        for ln in s.split("\n"):
            out.append(f"     {ln}")
        return "\n".join(out)
    return f"   {label}: {s}"


class ReadContextTool(Tool):
    name = "read_context"
    description = (
        "Read context from sessions. "
        "read_context_mode='list': session list. read_context_mode='search': structured JSON query "
        "(filters: keyword/kind/tool/author/turn — combine freely, ≥1 required; default current "
        "session, pass read_context_sessions to restrict). read_context_mode='fetch': retrieve full "
        "turn(s) at given read_context_loc (use search results' loc string verbatim; add "
        "'read_context_range' to include adjacent turns)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "read_context_mode": {
                "type": "string",
                "description": "list, search, or fetch",
            },
            "read_context_keyword": {
                "type": "string",
                "description": "Search: substring over each record's text surface.",
            },
            "read_context_kind": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": [
                        "query",
                        "action",
                        "observation",
                        "final",
                        "raw",
                        "system",
                    ],
                },
                "description": (
                    "Search filter by record kind: query=user ask, action=assistant "
                    "tool turn, observation=tool result, final=complete answer, "
                    "raw=unparsed. Single string accepted (auto-promoted)."
                ),
            },
            "read_context_tool": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Search filter by tool name involved (e.g. 'read_file'). "
                    "Single string accepted (auto-promoted)."
                ),
            },
            "read_context_author": {
                "type": "string",
                "description": (
                    "Search filter by author nickname (web multi-user attribution)."
                ),
            },
            "read_context_turn": {
                "type": "integer",
                "description": (
                    "Search filter by turn index. (Range queries: pass "
                    "{from,to} in tooling that supports it.)"
                ),
            },
            "read_context_sessions": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional session selector for read_context_mode=search. "
                    "Default: current session only. "
                    "Pass 'all' (single value) to search every session, "
                    "or specific session_id(s) to scope. "
                    "Single string accepted (auto-promoted)."
                ),
            },
            "read_context_loc": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Required for read_context_mode=fetch. Location(s) returned by "
                    "search: '{session_id}/{rel_path}:{line_num}'. "
                    "Single string accepted (auto-promoted). Max 10 entries."
                ),
            },
            "read_context_range": {
                "type": "integer",
                "description": (
                    "Optional for read_context_mode=fetch. Include +/-N adjacent turns "
                    "around each loc. Default 0 (target only). Max 5."
                ),
            },
        },
        "required": ["read_context_mode"],
    }

    def _run(self, args: dict, *, session_dir=None) -> ToolResult:
        return tool_read_context(args, session_dir=session_dir)
