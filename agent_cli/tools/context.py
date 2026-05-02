"""read_context tool — lets LLM browse and search session history.

Modes:
  - list: show all sessions (session_id, time, query)
  - search: structured keyword search across history.jsonl files

Search supports two orthogonal filters:
  1. ``scope`` — restrict matches by record field
       reasoning   : assistant.thought
       tool        : assistant.action + action_input
       observation : user.content starting with "Observation:"
       query       : user.content NOT starting with "Observation:"
     Default = all four. Single string ("reasoning") auto-promoted to list.
  2. ``sessions`` — restrict matches by session
       (omitted)   : current session only
       "all"       : every session
       "<id>"      : specific session
       ["<id>", …] : multiple specific sessions
     Single string auto-promoted to list.

Search returns one block per matching turn (records with multiple matched
scopes are aggregated). Previews collapse whitespace and cap at 200 chars.
The 50-match cap is honored at append time (early break) so a single
session with many matches cannot starve later sessions of slots.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_cli.context.session import list_sessions
from agent_cli.tools.result import ToolResult

_SESSIONS_BASE = Path(".agent-cli") / "sessions"

# Field-filter scope names for mode=search.
_VALID_SCOPES: tuple[str, ...] = ("reasoning", "tool", "observation", "query")
_OBSERVATION_PREFIX = "Observation:"
_PREVIEW_CAP = 200
_MAX_MATCHES = 50


# ── Public entry point ────────────────────────────────────────────


def tool_read_context(args: dict, *, session_dir: Path | None = None) -> ToolResult:
    """Dispatch to mode=list or mode=search.

    ``session_dir`` is the current session's directory; used to resolve
    the default ``sessions`` filter for search (current-session-only).
    """
    mode = args.get("mode", "list")
    if mode == "list":
        return _mode_list()
    if mode == "search":
        return _mode_search(
            keyword=args.get("keyword", ""),
            scope=args.get("scope"),
            sessions=args.get("sessions"),
            session_dir=session_dir,
        )
    return ToolResult(False, error=f"unknown mode '{mode}'. Use 'list' or 'search'.")


# ── mode=list ─────────────────────────────────────────────────────


def _mode_list() -> ToolResult:
    sessions = list_sessions()
    if not sessions:
        return ToolResult(True, output="No previous sessions found.")
    lines = [
        f"- {s.session_id} [{s.updated_at}] {s.query or '(no query)'}" for s in sessions
    ]
    return ToolResult(True, output="\n".join(lines))


# ── mode=search ───────────────────────────────────────────────────


def _mode_search(
    keyword: str,
    scope: Any,
    sessions: Any,
    session_dir: Path | None,
) -> ToolResult:
    if not keyword:
        return ToolResult(False, error="keyword is required for mode='search'.")

    try:
        scopes = _normalize_scope(scope)
    except ValueError as e:
        return ToolResult(False, error=str(e))

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
                        match = _match_turn(msg, keyword, scopes)
                        if not match:
                            continue
                        matches.append(
                            {
                                "loc": f"{session_id}/{rel_path}:{line_num}",
                                "role": msg.get("role", "?"),
                                "matched": match["matched"],
                                "previews": match["previews"],
                            }
                        )
                        if len(matches) >= _MAX_MATCHES:
                            truncated = True
                            break
            except OSError:
                continue

    return _format_search_result(keyword, scopes, matches, truncated)


# ── Argument normalization ────────────────────────────────────────


def _normalize_scope(scope: Any) -> list[str]:
    """Coerce ``scope`` into a list of valid scope names.

    Accepts None (→ all), str (→ [str]), or list. Unknown names raise
    ``ValueError`` so the model gets explicit feedback rather than
    silently broadened search.
    """
    if scope is None:
        return list(_VALID_SCOPES)
    if isinstance(scope, str):
        scope = [scope]
    if not isinstance(scope, list):
        raise ValueError(
            f"scope must be a string or array of strings, got {type(scope).__name__}"
        )
    invalid = [s for s in scope if s not in _VALID_SCOPES]
    if invalid:
        raise ValueError(
            f"invalid scope value(s): {invalid}. Valid: {list(_VALID_SCOPES)}"
        )
    return list(scope) if scope else list(_VALID_SCOPES)


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


def _match_turn(msg: dict, keyword: str, scopes: list[str]) -> dict | None:
    """Check whether a turn matches in any requested scope.

    Returns ``{"matched": [scope, …], "previews": {scope: str, …}}`` or
    None if no scope matched. A single turn matched in multiple scopes
    yields one result with both previews.
    """
    kw = keyword.lower()
    role = msg.get("role")
    matched: list[str] = []
    previews: dict[str, str] = {}

    if role == "assistant":
        if "reasoning" in scopes:
            thought = msg.get("thought") or ""
            if isinstance(thought, str) and kw in thought.lower():
                matched.append("reasoning")
                previews["reasoning"] = _format_text(thought)

        if "tool" in scopes:
            action = msg.get("action") or ""
            if not isinstance(action, str):
                action = str(action)
            ai = msg.get("action_input")
            ai_str = ""
            if ai is not None:
                try:
                    ai_str = json.dumps(ai, ensure_ascii=False)
                except (TypeError, ValueError):
                    ai_str = str(ai)
            if (action and kw in action.lower()) or (ai_str and kw in ai_str.lower()):
                matched.append("tool")
                previews["tool"] = _format_tool(action, ai_str)

    elif role == "user":
        content = msg.get("content") or ""
        if not isinstance(content, str):
            content = str(content)
        is_obs = content.startswith(_OBSERVATION_PREFIX)

        if "observation" in scopes and is_obs and kw in content.lower():
            matched.append("observation")
            previews["observation"] = _format_obs_match(content, kw)

        if "query" in scopes and not is_obs and kw in content.lower():
            matched.append("query")
            previews["query"] = _format_text(content)

    if not matched:
        return None
    return {"matched": matched, "previews": previews}


# ── Preview formatting ────────────────────────────────────────────


def _format_text(text: str, cap: int = _PREVIEW_CAP) -> str:
    """Collapse all whitespace to single spaces and cap at ``cap`` chars."""
    collapsed = " ".join(text.split())
    if len(collapsed) > cap:
        return collapsed[: cap - 3] + "..."
    return collapsed


def _format_tool(action: str, ai_str: str) -> str:
    """Render ``action(action_input_json)`` with cap."""
    if not action and not ai_str:
        return ""
    if not ai_str:
        return _format_text(f"{action}()")
    return _format_text(f"{action}({ai_str})")


def _format_obs_match(content: str, keyword_lower: str) -> str:
    """Pick the matching line from a (potentially huge) observation.

    Falls back to whole-content preview when no single line contains the
    keyword (rare; happens when keyword spans newlines).
    """
    for line in content.split("\n"):
        if keyword_lower in line.lower():
            return _format_text(line)
    return _format_text(content)


# ── Result rendering ──────────────────────────────────────────────


def _format_search_result(
    keyword: str,
    scopes: list[str],
    matches: list[dict],
    truncated: bool,
) -> ToolResult:
    scope_str = ", ".join(scopes)
    if not matches:
        return ToolResult(
            True, output=f"No matches for '{keyword}' (scope: {scope_str})."
        )

    header = (
        f"Search results for '{keyword}' (scope: {scope_str}) — {len(matches)} matches"
    )
    if truncated:
        header += f" (capped at {_MAX_MATCHES})"
    header += ":\n"

    blocks = []
    for m in matches:
        head = f"-- {m['loc']} [{m['role']}]  matched: {', '.join(m['matched'])}"
        lines = [head]
        for s in m["matched"]:
            preview = m["previews"].get(s, "")
            lines.append(f"   {s}: {preview}")
        blocks.append("\n".join(lines))

    return ToolResult(True, output=header + "\n\n".join(blocks))
