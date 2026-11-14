"""세션 대화 요약 — history.jsonl 에서 사용자 요청과 그 답(``complete``) 쌍을 읽는다.

세션 목록·resume 프롬프트가 보여 주는 "마지막 요청 → 결과" 가 여기서 나온다. 시스템 알림
(재시도 힌트·끼어들기 알림)을 사용자 입력에서 거르려고 방언의 알림 접두를 읽으므로, 세션
디렉터리 저장소(``context/session.py``)가 아니라 맥락 쪽에 둔다.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_cli.context import session as _session
from agent_cli.dialects import all_system_user_prefixes


def recent_exchanges(history_path: Path, n: int = 10) -> list[tuple[str, str]]:
    """Return the last `n` (user_query, assistant_final) pairs from
    history.jsonl, in chronological order.

    A "user query" is a role=user message that is neither a tool
    observation (content starting with "Observation:" or carrying a
    `tool` field) nor a loop-emitted system notice (retry hints,
    interrupt notices). Those all share the role=user shape but are
    not real user input.

    The set of "system notice" prefixes comes from
    :func:`agent_cli.dialects.all_system_user_prefixes` so any
    registered dialect plugin's framing strings are picked up
    automatically — no edit here when a new plugin is added.

    The paired final is the next role=assistant `complete` action's
    result. If a new user query arrives before the previous one
    completes, the previous pair is closed with "(no completion)" so
    interrupted runs still surface.
    """
    system_prefixes = all_system_user_prefixes()

    if not history_path.is_file():
        return []

    pairs: list[tuple[str, str]] = []
    pending: str | None = None

    with open(history_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            role = msg.get("role")
            if role == "user":
                content = msg.get("content", "")
                if not isinstance(content, str):
                    content = ""
                if msg.get("tool") or content.startswith("Observation:"):
                    continue
                if any(content.startswith(p) for p in system_prefixes):
                    continue
                if pending is not None:
                    pairs.append((pending, "(no completion)"))
                pending = content
            elif role == "assistant" and msg.get("action") == "complete":
                if pending is None:
                    continue
                action_input = msg.get("action_input", {})
                if isinstance(action_input, dict):
                    result = action_input.get("result", "")
                else:
                    result = str(action_input) if action_input else ""
                pairs.append((pending, result))
                pending = None

    if pending is not None:
        pairs.append((pending, "(no completion)"))

    return pairs[-n:] if n > 0 else pairs


def session_summary(meta: _session.SessionMeta) -> tuple[str, str]:
    """``(last_user_request, last_result)`` for a session, read from its
    history.jsonl — the replacement for the removed ``query`` meta field.

    ``last_result`` is the last ``complete`` action's result, or
    "(no completion)" for a run still open / interrupted. Returns
    ``("", "")`` when the session has no history yet. Reads the file path
    directly (no mkdir side-effect, unlike ``get_session_dir``).
    """
    hp = _session._SESSIONS_DIR / meta.session_id / "history.jsonl"
    pairs = recent_exchanges(hp, n=1)
    return pairs[-1] if pairs else ("", "")
