"""read_context tool — query session history with SQL.

The LLM composes a SQL ``SELECT`` against a ``history`` table built on demand
from history.jsonl. One query primitive instead of a pile of filter params: the
record schema is shown to the model (tool description + the no-query help) and
it writes whatever query it needs (search by kind/tool/files/author/turn, fetch
full text at a loc, list sessions, …).

Table ``history`` — one row per history.jsonl record:

    session  TEXT     session id
    loc      TEXT     '<session>/<rel_path>:<line>' (stable reference)
    seq      INTEGER  line number within the file (ordering)
    kind     TEXT     query | action | observation | final | raw | system
    turn     INTEGER  LLM turn index
    ts       TEXT     ISO timestamp
    tools    TEXT     space-joined tool names      (… LIKE '%read_file%')
    files    TEXT     space-joined touched paths   (… LIKE '%auth.py%')
    author   TEXT     nickname (web multi-user)
    text     TEXT     flat searchable + readable surface

Columns are derived on read (shared ``manager._classify_record`` /
``extract_file_paths``), so the query works regardless of whether records carry
the persisted enrich keys. Read-only: a SQLite authorizer denies everything but
SELECT/READ, and the in-memory DB is rebuilt per call and thrown away.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from agent_cli.context.session import get_session_dir, list_sessions
from agent_cli.tools.base import Tool
from agent_cli.tools.result import ToolResult

_SESSIONS_BASE = Path(".agent-cli") / "sessions"
_MAX_ROWS = 50  # result cap
_CELL_CAP = 200  # per-cell preview cap

_COLUMNS = (
    "session",
    "loc",
    "seq",
    "kind",
    "turn",
    "ts",
    "tools",
    "files",
    "author",
    "text",
)


# ── Public entry point ────────────────────────────────────────────


def tool_read_context(args: dict, *, session_dir: Path | None = None) -> ToolResult:
    """Run a SQL query over session history.

    ``args``: ``{query: "SELECT …", sessions?: current|all|<id>|[…]}``.
    With no ``query``, returns the schema + example queries + session list so
    the model can discover what to ask. ``session_dir`` scopes the default
    (current session) when ``sessions`` is omitted.
    """
    query = (args.get("query") or "").strip()
    sessions = args.get("sessions")

    if not query:
        return _help(session_dir)

    target_dirs, error = _resolve_session_dirs(sessions, session_dir)
    if error:
        return ToolResult(False, error=error)
    if target_dirs is None:
        return ToolResult(
            True,
            output=(
                "No current session resolved; pass sessions='all' or specific "
                "session_id(s)."
            ),
        )

    rows = _load_rows(target_dirs)
    conn = _build_db(rows)
    try:
        return _run_sql(conn, query)
    finally:
        conn.close()


# ── Session-dir resolution (which sessions populate the table) ─────


def _resolve_session_dirs(
    sessions: Any, current_session_dir: Path | None
) -> tuple[list[Path] | None, str | None]:
    """Resolve the ``sessions`` selector into directories whose history loads
    into the table. (dirs, error); dirs None = unsatisfiable, [] = none on disk.
    """
    if sessions is None:
        if current_session_dir is None:
            return None, None
        return [Path(current_session_dir)], None

    if isinstance(sessions, str):
        sessions = [sessions]
    if not isinstance(sessions, list):
        return None, (
            f"sessions must be a string or array of strings, "
            f"got {type(sessions).__name__}"
        )

    if "all" in sessions:
        if len(sessions) > 1:
            return None, "sessions='all' cannot be combined with specific session ids"
        if not _SESSIONS_BASE.is_dir():
            return [], None
        return [d for d in sorted(_SESSIONS_BASE.iterdir()) if d.is_dir()], None

    dirs: list[Path] = []
    missing: list[str] = []
    for sid in sessions:
        if not isinstance(sid, str):
            return None, f"sessions must contain strings, got {type(sid).__name__}"
        d = _SESSIONS_BASE / sid
        if d.is_dir():
            dirs.append(d)
        else:
            missing.append(sid)
    if missing:
        return None, f"session(s) not found: {missing}"
    return dirs, None


# ── Load records → table rows ──────────────────────────────────────


def _load_rows(target_dirs: list[Path]) -> list[tuple]:
    """Read every history.jsonl under the target dirs into table rows.

    Each record's ``kind``/``tools``/``text`` come from the shared
    ``_classify_record`` and ``files`` from ``extract_file_paths`` (computed on
    read so any record shape works); ``turn``/``ts``/``author`` from the record.
    """
    from agent_cli.context._file_extract import extract_file_paths
    from agent_cli.context.manager import _classify_record

    rows: list[tuple] = []
    for sdir in target_dirs:
        if not sdir.is_dir():
            continue
        session_id = sdir.name
        for hp in sorted(sdir.rglob("history.jsonl")):
            rel = hp.relative_to(sdir)
            try:
                with open(hp, encoding="utf-8") as f:
                    for lineno, raw in enumerate(f, 1):
                        line = raw.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        kind, tools, text = _classify_record(rec)
                        files = extract_file_paths([rec])
                        turn = rec.get("turn")
                        rows.append(
                            (
                                session_id,
                                f"{session_id}/{rel}:{lineno}",
                                lineno,
                                kind,
                                turn if isinstance(turn, int) else None,
                                rec.get("ts"),
                                " ".join(tools),
                                " ".join(files),
                                rec.get("author"),
                                text,
                            )
                        )
            except OSError:
                continue
    return rows


def _build_db(rows: list[tuple]) -> sqlite3.Connection:
    """Build an in-memory, read-only-enforced ``history`` table."""
    conn = sqlite3.connect(":memory:")
    cols = ", ".join(
        f"{c} {'INTEGER' if c in ('seq', 'turn') else 'TEXT'}" for c in _COLUMNS
    )
    conn.execute(f"CREATE TABLE history ({cols})")
    conn.executemany(
        f"INSERT INTO history ({', '.join(_COLUMNS)}) "
        f"VALUES ({', '.join('?' * len(_COLUMNS))})",
        rows,
    )
    conn.commit()
    conn.set_authorizer(_read_only_authorizer)
    return conn


# Allow only the operations a SELECT needs; deny writes/DDL/pragmas.
_READ_OK = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
}


def _read_only_authorizer(action: int, *_args) -> int:
    return sqlite3.SQLITE_OK if action in _READ_OK else sqlite3.SQLITE_DENY


def _run_sql(conn: sqlite3.Connection, query: str) -> ToolResult:
    head = query.lstrip("( \t\n").lstrip().upper()
    if not (head.startswith("SELECT") or head.startswith("WITH")):
        return ToolResult(
            False,
            error="Only SELECT queries are allowed (start with SELECT or WITH).",
        )
    try:
        cur = conn.execute(query)
    except sqlite3.DatabaseError as e:
        msg = str(e)
        if "not authorized" in msg:
            msg = "query is not read-only (only SELECT/READ allowed)"
        return ToolResult(False, error=f"SQL error: {msg}")
    col_names = [d[0] for d in cur.description] if cur.description else []
    fetched = cur.fetchmany(_MAX_ROWS + 1)
    truncated = len(fetched) > _MAX_ROWS
    return _format_rows(col_names, fetched[:_MAX_ROWS], truncated)


# ── Result rendering ──────────────────────────────────────────────


def _cell(value: Any) -> str:
    s = "" if value is None else str(value)
    s = " ".join(s.split())
    return (s[: _CELL_CAP - 1] + "…") if len(s) > _CELL_CAP else s


def _format_rows(
    col_names: list[str], rows: list[tuple], truncated: bool
) -> ToolResult:
    if not rows:
        return ToolResult(True, output="No rows.")
    header = f"{len(rows)} row(s)"
    if truncated:
        header += f" (capped at {_MAX_ROWS}; add LIMIT/refine the query)"
    header += f"\ncolumns: {', '.join(col_names)}"
    lines = [header, ""]
    for i, row in enumerate(rows, 1):
        cells = " | ".join(f"{c}={_cell(v)}" for c, v in zip(col_names, row))
        lines.append(f"{i}. {cells}")
    return ToolResult(True, output="\n".join(lines))


# ── No-query help (schema discovery) ──────────────────────────────


def _session_title(meta) -> str:
    """First user message of a session — a short title for the help listing."""
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


_HELP_EXAMPLES = (
    "SELECT loc, turn, text FROM history WHERE kind='observation' "
    "AND files LIKE '%auth.py%'",
    "SELECT text FROM history WHERE author='Alice' AND kind='query'",
    "SELECT loc, text FROM history WHERE tools LIKE '%shell%' AND turn>=5 "
    "ORDER BY turn LIMIT 20",
    "SELECT DISTINCT session FROM history",
)


def _help(session_dir: Path | None) -> ToolResult:
    schema = "\n".join(
        f"  {c} {'INTEGER' if c in ('seq', 'turn') else 'TEXT'}" for c in _COLUMNS
    )
    examples = "\n".join(f"  {q}" for q in _HELP_EXAMPLES)
    parts = [
        "read_context: query session history with SQL (read-only SELECT over the",
        "`history` table; pass it as read_context_query).",
        "",
        "Table `history` (one row per turn record):",
        schema,
        "",
        "  kind: query=user ask · action=tool-call turn · observation=tool result"
        " · final=complete answer · raw=unparsed",
        "  Default scope = current session; pass read_context_sessions='all' or"
        " id(s) for others.",
        "",
        "Examples:",
        examples,
    ]
    sessions = list_sessions()
    if sessions:
        parts.append("")
        parts.append("Sessions:")
        for s in sessions[:20]:
            parts.append(f"  {s.session_id} [{s.updated_at}] {_session_title(s)}")
    return ToolResult(True, output="\n".join(parts))


class ReadContextTool(Tool):
    name = "read_context"
    description = (
        "Query past/current session history with SQL. read_context_query='SELECT "
        "… FROM history WHERE …' (read-only). Columns: session, loc, seq, kind"
        "(query/action/observation/final/raw/system), turn, ts, tools, files, "
        "author, text. Search by kind/tools/files/author/turn, read full content "
        "via the text column, list sessions via DISTINCT session. Omit the query "
        "to see the schema + examples + session list. Default scope = current "
        "session; read_context_sessions='all'/id(s) for others."
    )
    parameters = {
        "type": "object",
        "properties": {
            "read_context_query": {
                "type": "string",
                "description": (
                    'SQL SELECT over the `history` table (e.g. "SELECT loc, text '
                    "FROM history WHERE kind='observation' AND files LIKE "
                    "'%auth.py%'\"). Omit to get the schema + examples."
                ),
            },
            "read_context_sessions": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Which sessions populate the table. Default: current session. "
                    "'all' = every session, or specific session_id(s). Single "
                    "string accepted (auto-promoted)."
                ),
            },
        },
        "required": [],
    }

    def _run(self, args: dict, *, session_dir=None) -> ToolResult:
        return tool_read_context(args, session_dir=session_dir)
