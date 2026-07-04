"""Session memory — an LLM-curated, compaction-immune store of salient notes.

The agent records critical failures, important discoveries, decisions, and
plain notes via the ``memory`` tool. Entries persist in
``<session_dir>/memory.jsonl`` (surviving compaction AND ``--resume``) and
surface as a compact **always-on** ``## Session Memory`` index in the system
prompt, with full ``detail`` pulled on demand (``memory(mode=get, id=N)``).

Why a separate store (not the rolling context): memory's whole job is to
survive compaction — the system prompt is outside ``ctx.get_messages()`` so it
is never compacted, whereas a context message would be summarized/dropped
exactly when the note matters most. Re-feeding LLM-authored text as context
messages also risks mimicry (see docs/session-memory/DESIGN.md §5, and the
``feedback_refeed_own_output_mimicry`` lesson).

The store is stateless — every op reads the JSONL fresh — so it is correct
across ``--resume`` (the file is the single source of truth) with no in-memory
cache to keep in sync. Mutating ops flip a module-level dirty flag the loop
consumes to rebuild the system-prompt index (mirrors DIRECTIVE.md reload).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

_MEMORY_FILE = "memory.jsonl"

# type → icon for the always-on index. The enum is the source of truth for
# validation; unknown types are rejected so the index stays scannable and the
# model treats each kind distinctly (failure=avoid-repeat, discovery=reuse, …).
_TYPE_ICONS = {
    "failure": "⚠",
    "discovery": "💡",
    "decision": "🔀",
    "note": "📝",
}
VALID_TYPES = tuple(_TYPE_ICONS)

# How many summaries the always-on index shows before capping (rest via
# ``memory(mode=list)``). Summaries are one line each; this bounds the section
# so a long-running session's memory can't bloat every system prompt.
_INDEX_CAP = 30

# Read-modify-write on the shared JSONL is serialized: main-loop tool dispatch
# is already sequential and delegates use separate session dirs, but a global
# lock is free insurance against any concurrent writer corrupting the file.
_io_lock = threading.Lock()


class MemoryError(ValueError):
    """A recoverable memory-op error (bad type / missing id) — the tool turns
    it into a clean failure Observation the model can act on, not a crash."""


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _path(session_dir) -> Path:
    return Path(session_dir) / _MEMORY_FILE


def load(session_dir) -> list[dict]:
    """All entries for the session in id order, ``[]`` if none / no session.
    A corrupt line is skipped rather than failing the whole load (a partial
    write from a crash must not brick the store)."""
    if session_dir is None:
        return []
    p = _path(session_dir)
    if not p.is_file():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _write_all(session_dir, entries: list[dict]) -> None:
    p = _path(session_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries),
        encoding="utf-8",
    )


def _next_id(entries: list[dict]) -> int:
    """max id + 1 — monotonic, never reused even after delete (stable refs)."""
    return max((e.get("id", 0) for e in entries), default=0) + 1


def _clean_tags(tags) -> list[str]:
    if not isinstance(tags, list):
        return []
    return [t.strip() for t in tags if isinstance(t, str) and t.strip()]


def add(session_dir, *, type: str, summary: str, detail=None, tags=None) -> dict:
    """Append a new memory entry; returns it (with the assigned id)."""
    if session_dir is None:
        raise MemoryError("no active session — memory is session-scoped.")
    if type not in VALID_TYPES:
        raise MemoryError(
            f"unknown type '{type}'. Use one of: {', '.join(VALID_TYPES)}."
        )
    if not summary or not summary.strip():
        raise MemoryError("summary is required (a one-line index label).")
    with _io_lock:
        entries = load(session_dir)
        entry = {
            "id": _next_id(entries),
            "type": type,
            "summary": summary.strip(),
            "detail": (detail or "").strip(),
            "tags": _clean_tags(tags),
            "ts": _now_iso(),
        }
        entries.append(entry)
        _write_all(session_dir, entries)
    mark_memory_dirty()
    return entry


def get(session_dir, mem_id: int) -> dict | None:
    for e in load(session_dir):
        if e.get("id") == mem_id:
            return e
    return None


def update(session_dir, mem_id: int, **fields) -> dict:
    """Replace only the provided fields of entry ``mem_id`` (None = keep)."""
    if fields.get("type") is not None and fields["type"] not in VALID_TYPES:
        raise MemoryError(
            f"unknown type '{fields['type']}'. Use one of: {', '.join(VALID_TYPES)}."
        )
    with _io_lock:
        entries = load(session_dir)
        for e in entries:
            if e.get("id") != mem_id:
                continue
            if fields.get("type") is not None:
                e["type"] = fields["type"]
            for k in ("summary", "detail"):
                if fields.get(k) is not None:
                    e[k] = str(fields[k]).strip()
            if fields.get("tags") is not None:
                e["tags"] = _clean_tags(fields["tags"])
            _write_all(session_dir, entries)
            mark_memory_dirty()
            return e
    raise MemoryError(f"no memory with id {mem_id}.")


def delete(session_dir, mem_id: int) -> bool:
    with _io_lock:
        entries = load(session_dir)
        kept = [e for e in entries if e.get("id") != mem_id]
        if len(kept) == len(entries):
            raise MemoryError(f"no memory with id {mem_id}.")
        _write_all(session_dir, kept)
    mark_memory_dirty()
    return True


def list_entries(session_dir, *, type=None, tag=None) -> list[dict]:
    out = load(session_dir)
    if type is not None:
        out = [e for e in out if e.get("type") == type]
    if tag is not None:
        out = [e for e in out if tag in (e.get("tags") or [])]
    return out


def render_index(session_dir) -> str:
    """The always-on ``## Session Memory`` section (summaries only — detail is
    pulled on demand). ``""`` when empty (the section is then omitted, like an
    absent DIRECTIVE)."""
    entries = load(session_dir)
    if not entries:
        return ""
    shown = entries[-_INDEX_CAP:]
    lines = [
        f"## Session Memory ({len(entries)}) — pull full detail with "
        f"memory(mode=get, id=N)"
    ]
    hidden = len(entries) - len(shown)
    if hidden > 0:
        lines.append(f"({hidden} older entries hidden — see memory(mode=list))")
    for e in shown:
        icon = _TYPE_ICONS.get(e.get("type"), "•")
        lines.append(f"{icon} #{e.get('id')} [{e.get('type')}] {e.get('summary', '')}")
    return "\n".join(lines)


def format_entry(entry: dict) -> str:
    """Full-detail rendering for ``memory(mode=get)``."""
    icon = _TYPE_ICONS.get(entry.get("type"), "•")
    parts = [
        f"{icon} #{entry.get('id')} [{entry.get('type')}] {entry.get('summary', '')}"
    ]
    if entry.get("tags"):
        parts.append("tags: " + ", ".join(entry["tags"]))
    if entry.get("ts"):
        parts.append(f"recorded: {entry['ts']}")
    if entry.get("detail"):
        parts.append("")
        parts.append(entry["detail"])
    return "\n".join(parts)


# ── dirty flag: mutating ops signal the loop to rebuild the prompt index ──
_dirty_lock = threading.Lock()
_dirty = False


def mark_memory_dirty() -> None:
    global _dirty
    with _dirty_lock:
        _dirty = True


def consume_memory_reload() -> bool:
    """True (once) after a memory mutation — the loop rebuilds the system
    prompt so the ``## Session Memory`` index reflects the change next turn."""
    global _dirty
    with _dirty_lock:
        was = _dirty
        _dirty = False
    return was
