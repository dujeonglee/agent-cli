"""TurnRecord — per-turn observability data for the robust-harness.

Each record captures the *outcome* of one LLM turn: what stage the parser
landed at, whether a failure was detected, and which recovery primitives
were composed for the retry. The records accumulate across sessions to
enable empirical playbook tuning (Step 4 of the roadmap).

Storage: append-only JSONL at ``{session_dir}/turns.jsonl``. No schema
versioning — any reader should ignore unknown fields. No log rotation —
session-scoped files are bounded by the session length.

Privacy: the schema deliberately excludes any LLM-generated text or user
prompt content. Only structural metadata (parse_stage, failure_signal,
primitive names, timing) is recorded.

See ``docs/robust-harness/DESIGN.md`` §3.3.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# Failure signal labels — kept as bare strings for forward compatibility
# with future signal types (Step 3 adds B1; Step 4 adds A4/A5/etc.).
FAILURE_NO_JSON = "NO_JSON"
FAILURE_NO_ACTION = "NO_ACTION"


@dataclass(frozen=True)
class TurnRecord:
    """One row in the per-turn observability log.

    ``seq`` is monotonic within a session and is the natural ordering for
    walking records. ``failure_signal`` is None on a successful turn;
    ``primitives_applied`` is empty when no recovery happened.

    Recovery rate is *not* stored on the record — it's derived at analysis
    time by walking forward from a failed turn until the next non-failed
    turn (or session end). Keeping that as a query rather than a stored
    field avoids retrospective writes.
    """

    seq: int
    model: str
    timestamp: str  # ISO 8601 UTC
    parse_stage: int  # 0=fail, 1=json.loads, 2=json_repair, 3=regex
    failure_signal: Optional[str] = None
    primitives_applied: list[str] = field(default_factory=list)


class TurnRecorder:
    """Append-only writer for ``turns.jsonl``.

    Disabled (no-op) when:
      - ``session_dir`` is None (headless / subagent / no session)
      - ``enabled`` is False (user passed ``--no-record-turns``)

    The writer is intentionally simple — open/close per record, no buffer.
    Cost is dominated by one ``write()`` per turn (microseconds), not
    file-handle overhead. Crash safety: each line is fully written before
    the call returns; partial lines on crash are easy to tolerate at
    analysis time (drop the last line if it doesn't parse).
    """

    def __init__(self, session_dir: Optional[Path], enabled: bool = True):
        self._path: Optional[Path]
        if session_dir is None or not enabled:
            self._path = None
        else:
            self._path = Path(session_dir) / "turns.jsonl"
        self._seq = 0

    @property
    def enabled(self) -> bool:
        return self._path is not None

    def record(
        self,
        *,
        model: str,
        parse_stage: int,
        failure_signal: Optional[str] = None,
        primitives_applied: Optional[list[str]] = None,
    ) -> None:
        """Append one record to ``turns.jsonl``. No-op when disabled."""
        if self._path is None:
            return

        rec = TurnRecord(
            seq=self._seq,
            model=model,
            timestamp=datetime.now(timezone.utc).isoformat(),
            parse_stage=parse_stage,
            failure_signal=failure_signal,
            primitives_applied=list(primitives_applied) if primitives_applied else [],
        )
        line = json.dumps(asdict(rec), ensure_ascii=False)
        # Parent dir is created by ContextManager; if absent (edge case)
        # we let the OSError propagate so the caller knows recording is
        # broken — better than silently dropping data.
        with self._path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        self._seq += 1
