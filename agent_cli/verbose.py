"""``--verbose``: a structured, queryable record of every LLM call (v9.24.3).

Before v9.24.3 verbose PRINTED — the raw response, thinking blocks and a
context dump went to the terminal (CLI) or to a transient SSE event nobody
listened to (web), sub-agents never inherited the flag, and nothing reached
a file. So the one question verbose exists to answer — "what exactly did the
model emit when this turn failed?" — had no answer after the fact (board
session 1zfgc2: nine failed tool calls, all in resident agents, no raw text
anywhere).

Now verbose REDIRECTS to one JSONL file per session,
``<session_dir>/verbose.jsonl``: every loop in the process — the main agent,
every sub-agent, skill and resident agent — appends to it, told apart by
``scope``. Nothing verbose is printed any more.

Designed to be analysed with JSON queries, not text search: the FIRST line
is a ``{"kind": "schema"}`` record carrying the JSON Schema of every record
kind (:data:`SCHEMA`), so a reader — human or LLM — learns the fields from
the file itself. Typical questions::

    # every failed turn, with what the model actually wrote
    jq -c 'select(.kind=="llm_call" and .failure_signal!=null)
           | {scope, turn, failure_signal, text}' verbose.jsonl
    # one agent's calls
    jq -c 'select(.scope=="agents/agt-cf85229e")' verbose.jsonl
    # tool calls whose arguments carried an empty string
    jq -c 'select(.kind=="llm_call") | .ops[]?
           | select(any(.input[]?; . == ""))' verbose.jsonl

``turns.jsonl`` (``--record-turns``) stays the always-on structural log and
deliberately holds no model text; this file is the opt-in one that does.

The recorder is process-global because the scope tree is: sub-agent loops
run in the same process with their own ``ContextManager`` rooted below the
main session directory. The main loop configures it once (:func:`configure`);
sub-loops never switch it off.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_cli.fsio import append_line

#: Record format version — bump when a field changes meaning.
VERSION = 1

FILE_NAME = "verbose.jsonl"

_USAGE = {
    "type": "object",
    "properties": {
        "input_tokens": {"type": "integer"},
        "output_tokens": {"type": "integer"},
        "cache_read_input_tokens": {"type": "integer"},
        "cache_creation_input_tokens": {"type": "integer"},
    },
}

#: JSON Schema (draft 2020-12) of every record, written as the first line.
SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "agent-cli verbose.jsonl record",
    "description": (
        "One JSON object per line. Common fields on every record: v, ts, kind, "
        "scope, turn. `scope` is the loop's session directory relative to the "
        "main session ('main' for the main agent, e.g. 'agents/agt-1a2b' for a "
        "resident agent, 'run_task_…' for a one-shot run). `turn` is that "
        "loop's own turn counter."
    ),
    "type": "object",
    "required": ["v", "ts", "kind", "scope"],
    "properties": {
        "v": {"const": VERSION},
        "ts": {"type": "string", "format": "date-time"},
        "kind": {"enum": ["schema", "llm_call", "llm_error", "context", "debug"]},
        "scope": {"type": "string"},
        "turn": {"type": ["integer", "null"]},
    },
    "oneOf": [
        {
            "description": "First line only: this schema.",
            "properties": {"kind": {"const": "schema"}, "schema": {"type": "object"}},
        },
        {
            "description": (
                "One LLM response and what the harness made of it. `text` is the "
                "raw visible output exactly as received; `thinking` the separate "
                "reasoning channel. `parse_stage`: 0 unparsed, 1 canonical, 2 "
                "repaired. `failure_signal` is null on a clean turn, else e.g. "
                "NO_JSON, NO_ACTION, UNKNOWN_TOOL, SCHEMA_MISMATCH, ACTION_LOOP. "
                "`ops` are the parsed tool calls. On an output-token cut "
                "(stop_reason 'length') the turn is not parsed: parse fields are "
                "null."
            ),
            "properties": {
                "kind": {"const": "llm_call"},
                "model": {"type": "string"},
                "text": {"type": "string"},
                "thinking": {"type": ["string", "null"]},
                "stop_reason": {"type": ["string", "null"]},
                "parse_stage": {"type": ["integer", "null"]},
                "failure_signal": {"type": ["string", "null"]},
                "primitives": {"type": "array", "items": {"type": "string"}},
                "ops": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {"type": ["string", "null"]},
                            "input": {},
                        },
                    },
                },
                "usage": _USAGE,
                "grammar": {
                    "type": "boolean",
                    "description": "A decoding grammar constrained this call.",
                },
            },
        },
        {
            "description": "The LLM call itself failed (transport, server error).",
            "properties": {
                "kind": {"const": "llm_error"},
                "model": {"type": "string"},
                "error": {"type": "string"},
            },
        },
        {
            "description": (
                "What the model was about to be sent: one entry per message, "
                "with its size and first 300 characters (full text would grow "
                "the file quadratically — the conversation itself is in "
                "history.jsonl)."
            ),
            "properties": {
                "kind": {"const": "context"},
                "messages": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {"type": "string"},
                            "chars": {"type": "integer"},
                            "head": {"type": "string"},
                        },
                    },
                },
            },
        },
        {
            "description": "A diagnostic line from the harness.",
            "properties": {"kind": {"const": "debug"}, "message": {"type": "string"}},
        },
    ],
}

_CONTEXT_HEAD_CHARS = 300

_lock = threading.Lock()
_root: Path | None = None
_path: Path | None = None
_schema_written = False


def configure(session_dir: Path | str) -> Path:
    """Start recording into ``<session_dir>/verbose.jsonl`` (idempotent for
    the same directory). Returns the file path. ``session_dir`` is the MAIN
    session directory — scopes of sub-loops are measured from it."""
    global _root, _path, _schema_written
    root = Path(session_dir)
    with _lock:
        if _root != root:
            _root = root
            _path = root / FILE_NAME
            _schema_written = _path.exists() and _path.stat().st_size > 0
    return _path


def reset() -> None:
    """Stop recording (tests, process teardown)."""
    global _root, _path, _schema_written
    with _lock:
        _root = _path = None
        _schema_written = False


def enabled() -> bool:
    return _path is not None


def path() -> Path | None:
    return _path


def scope_of(session_dir: Path | str | None) -> str:
    """``session_dir`` relative to the main session — ``"main"`` for the
    root itself, the relative path for a loop below it, the absolute path
    otherwise, ``"?"`` when unknown."""
    if session_dir is None:
        return "?"
    d = Path(session_dir)
    if _root is None:
        return str(d)
    if d == _root:
        return "main"
    try:
        return d.relative_to(_root).as_posix()
    except ValueError:
        return str(d)


def record(kind: str, *, scope: str = "?", turn: int | None = None, **fields) -> None:
    """Append one record. No-op when verbose is off."""
    global _schema_written
    if _path is None:
        return
    rec = {
        "v": VERSION,
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "scope": scope,
        "turn": turn,
        **fields,
    }
    line = json.dumps(rec, ensure_ascii=False, default=str)
    with _lock:
        if not _schema_written:
            head = {
                "v": VERSION,
                "ts": rec["ts"],
                "kind": "schema",
                "scope": "main",
                "turn": None,
                "schema": SCHEMA,
            }
            append_line(_path, json.dumps(head, ensure_ascii=False))
            _schema_written = True
        append_line(_path, line)


def context_entries(messages: list[dict]) -> list[dict]:
    """Bounded per-message view for a ``context`` record."""
    from agent_cli.context.render import message_display_text

    out = []
    for m in messages:
        # content 밖(native_fc 의 ``tool_calls``)도 센다 — 인스펙터와 같은 표시 함수.
        text = message_display_text(m)
        out.append(
            {
                "role": str(m.get("role", "?")),
                "chars": len(text),
                "head": text[:_CONTEXT_HEAD_CHARS],
            }
        )
    return out


def debug_log(msg: str) -> None:
    """A diagnostic line — recorded as a ``debug`` record when verbose is on
    (it used to go to stderr). Callers in low-level modules (providers)
    have no loop scope, so the scope is ``"?"``."""
    record("debug", message=msg)
