"""``<session_dir>/failures.jsonl`` — every generation the loop could not use,
with its text (v10.11.0).

``turns.jsonl`` is the structural log (labels and counts, never text — its
privacy contract) and ``verbose.jsonl`` carries text but only when
``--verbose`` is on. When room 67qcmb lost 106 minutes to three runaway
generations, neither could say what the model had actually produced: the
observation keeps a bounded head/tail quote and the middle was gone. This
file is the always-on third log: one JSON object per failed generation —
format drift (NO_JSON, NO_ACTION, …), a cap cut (``length``), a stopped
runaway, a swallowed output — with the raw emission, so the failures can be
read later and the prompts, dialects and detectors improved from evidence.

A record is written only when the loop labelled the generation
(``failure_signal`` from dispatch, or the cut/runaway paths), so a healthy
session has no file at all. Text is capped at ``TEXT_CAP`` characters,
split head/tail, and flagged ``text_truncated``.

``tool_calls`` (v10.38.0, record v2) is the server's structured call list as
delivered — ``[{id, name, input}]``, or ``arguments`` (the raw string) in
place of ``input`` when the arguments were not JSON. Under a server-parsed
dialect (native_fc) the calls never appear in ``text``, so a rejected call's
arguments used to be recorded nowhere. Every string inside is capped like
``text`` and flagged ``tool_calls_truncated``.

    jq -c '{turn, failure_signal, stop_reason, stop_detail, chars: (.text|length)}' failures.jsonl
    jq -c 'select(.tool_calls) | {turn, failure_signal, calls: [.tool_calls[] | {name, input}]}' failures.jsonl
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from agent_cli.fsio import append_line

FILE_NAME = "failures.jsonl"
VERSION = 2
#: raw text kept per record (characters); a 32K-token runaway is ~130K
TEXT_CAP = 1_000_000


def _bounded(text: str | None) -> tuple[str | None, bool]:
    if not text or len(text) <= TEXT_CAP:
        return text or None, False
    half = TEXT_CAP // 2
    return text[:half] + "\n…[truncated]…\n" + text[-half:], True


def _bounded_value(value) -> tuple[object, bool]:
    """``_bounded`` applied to every string inside a JSON-shaped value."""
    if isinstance(value, str):
        if len(value) <= TEXT_CAP:
            return value, False
        return _bounded(value)
    if isinstance(value, dict):
        pairs = {k: _bounded_value(v) for k, v in value.items()}
        return {k: v for k, (v, _) in pairs.items()}, any(t for _, t in pairs.values())
    if isinstance(value, list):
        items = [_bounded_value(v) for v in value]
        return [v for v, _ in items], any(t for _, t in items)
    return value, False


def record_failure(
    session_dir: Path | str | None,
    *,
    turn: int,
    model: str,
    dialect: str,
    failure_signal: str,
    stop_reason: str | None,
    stop_detail: str | None,
    parse_stage: int | None,
    primitives: list[str],
    text: str,
    thinking: str | None,
    usage: dict | None,
    tool_calls: list[dict] | None = None,
) -> Path | None:
    """Append one record; no-op without a session directory. Returns the path."""
    if session_dir is None:
        return None
    body, truncated = _bounded(text)
    think, _ = _bounded(thinking)
    calls, calls_truncated = _bounded_value(tool_calls or None)
    rec = {
        "v": VERSION,
        "ts": datetime.now(timezone.utc).isoformat(),
        "turn": turn,
        "model": model,
        "dialect": dialect,
        "failure_signal": failure_signal,
        "stop_reason": stop_reason,
        "stop_detail": stop_detail or None,
        "parse_stage": parse_stage,
        "primitives": list(primitives),
        "usage": usage,
        "text": body,
        "text_truncated": truncated,
        "thinking": think,
        "tool_calls": calls,
        "tool_calls_truncated": calls_truncated,
    }
    path = Path(session_dir) / FILE_NAME
    append_line(path, json.dumps(rec, ensure_ascii=False, default=str))
    return path
